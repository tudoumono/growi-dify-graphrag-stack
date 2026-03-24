# Phase 5 スペック: 管理UI・ファイル操作エンドポイント・ingest.py 廃止

作成日: 2026-03-24
対象ファイル: graphrag/main.py, graphrag/ingest.py（廃止）, graphrag/docker-compose.yml

---

## 目的

- ブラウザからファイルをアップロード・削除できる管理 UI を提供する
- ファイル操作と同時に ES・Neo4j を更新し、`input/` を Git で自動管理する
- `ingest.py` に残っているクライアント機能を全てサーバー側へ移植し廃止する

---

## 1. 事前準備: input/ を Git リポジトリ化

Phase 5 実装開始前にホスト側で一度だけ実施する。

```bash
cd /root/mywork/DIfy_Growi_Langfuse/input
git init
git add .
git commit -m "初期ファイル登録"
```

以降、ファイルの追加・削除のたびにコンテナ側が自動でコミットする。

---

## 2. docker-compose.yml の変更

`graphrag-api` の `input/` マウントを読み取り専用（`:ro`）から読み書き可能（`:rw`）に変更する。

```yaml
# 変更前
- ${INGEST_INPUT_ROOT:-../input}:/input:ro

# 変更後
- ${INGEST_INPUT_ROOT:-../input}:/input:rw
```

Git の `.git/` ディレクトリもコンテナからアクセスできるよう、同じマウントに含まれる。

また Dockerfile に `git` をインストールする：

```dockerfile
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
```

---

## 3. 追加するエンドポイント

### 3-1. POST /ingest-file

単一ファイルをアップロードして `input/` 配下の指定フォルダに保存し、GraphRAG に取り込む。

**リクエスト（multipart/form-data）:**

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `file` | ファイル | 必須 | アップロードするファイル（.pdf / .md / .txt） |
| `folder` | 文字列 | 任意 | 保存先サブフォルダ（例: `contracts/nda`）。省略時はルート |

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "pdf-contracts_nda_sample",
  "source_ref": "contracts/nda/sample.pdf",
  "skipped": false
}
```

**処理フロー:**
1. `INGEST_INPUT_ROOT/<folder>/` にファイルを保存
2. 拡張子を判定して `build_*_payload()` を呼び出す（カテゴリは `folder` から自動付与）
3. ES・Neo4j に書き込む
4. `git add <file> && git commit -m "add: contracts/nda/sample.pdf"` を実行

**対応拡張子:** `.pdf`, `.md`, `.txt`
**非対応の場合:** 400 Bad Request

---

### 3-2. DELETE /documents/{document_id}

ドキュメントを ES・Neo4j から削除し、`input/` のファイルも削除する。

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "pdf-contracts_nda_sample",
  "file_deleted": true
}
```

**処理フロー:**
1. ES から `document_id` に紐づくチャンクを全削除
2. Neo4j から `document_id` に紐づく Document ノードとエッジを全削除
3. `source_ref` からファイルパスを特定し `INGEST_INPUT_ROOT/<source_ref>` を削除
4. `git rm <file> && git commit -m "delete: contracts/nda/sample.pdf"` を実行

**レスポンス（404 Not Found）:** document_id が存在しない場合

---

### 3-3. POST /ingest-growi

Growi のページパスを指定してサーバー側から Growi API を呼び出して取り込む。

**リクエスト（JSON）:**
```json
{
  "page_path": "/docs/spec/system-design"
}
```

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "growi-docs_spec_system-design",
  "skipped": false
}
```

**処理フロー:**
1. `GROWI_URL` と `GROWI_API_KEY` 環境変数でクレデンシャルを取得
2. Growi API でページ内容を取得
3. `build_growi_payload()` を呼び出す
4. ES・Neo4j に書き込む（Growi はファイルではないので Git コミットなし）

**必要な環境変数（docker-compose.yml に追加）:**

| 変数名 | 説明 |
|--------|------|
| `GROWI_URL` | Growi のベース URL |
| `GROWI_API_KEY` | Growi の API キー |

---

### 3-4. GET /ui（管理画面）

ブラウザで操作できるシンプルなファイル管理画面。FastAPI の `StaticFiles` または HTML レスポンスで提供。

**画面構成:**

```
[GraphRAG ファイル管理]

[取り込み済みドキュメント一覧]  ← GET /documents の結果を表示
  document_id          source_ref              status
  pdf-contracts_nda_sample  contracts/nda/sample.pdf   ✅ ok
  pdf-hr_handbook           hr/handbook.pdf            ⚠ mismatch  [再取り込み] [削除]

[ファイルアップロード]
  フォルダ: [contracts/nda     ▼]
  [ここにファイルをドロップ または クリックして選択]
  [アップロード & 取り込み]

[一括取り込み]
  [/ingest-dir を実行]  ← POST /ingest-dir を呼び出す
  ジョブ状態: done (processed: 12, skipped: 3)
```

---

## 4. ingest.py の廃止

Phase 5 完了時に `graphrag/ingest.py` を削除する。

| 旧コマンド | 移行先 |
|-----------|--------|
| `ingest.py pdf <file>` | `POST /ingest-file` または 管理UI |
| `ingest.py md <file>` | `POST /ingest-file` または 管理UI |
| `ingest.py txt <file>` | `POST /ingest-file` または 管理UI |
| `ingest.py growi <page_path>` | `POST /ingest-growi` |
| `ingest.py input-dir` | `POST /ingest-dir`（Phase 4 で移行済み） |

---

## 5. 動作確認手順

```bash
# コンテナ再ビルド
docker compose build graphrag-api && docker compose up -d graphrag-api

# 1. ファイルアップロード
curl -s -X POST http://localhost:8080/ingest-file \
  -F "file=@/path/to/sample.pdf" \
  -F "folder=contracts/nda" | python3.12 -m json.tool

# 2. ドキュメント一覧と整合性確認
curl -s http://localhost:8080/documents | python3.12 -m json.tool

# 3. ドキュメント削除
curl -s -X DELETE http://localhost:8080/documents/pdf-contracts_nda_sample \
  | python3.12 -m json.tool

# 4. Git ログで操作履歴を確認（ホスト側）
cd /root/mywork/DIfy_Growi_Langfuse/input && git log --oneline

# 5. 管理UI をブラウザで確認
# http://localhost:8080/ui
```

---

## 6. 実装しないこと（Phase 5 のスコープ外）

- watchdog によるファイル監視（自動取り込み）
- cron による `/ingest-dir` の定期実行
- Growi の全ページ一括取り込み（`/ingest-growi-all` 等）
- Git の push / リモートリポジトリ連携
- ユーザー認証（管理 UI はローカル運用前提）

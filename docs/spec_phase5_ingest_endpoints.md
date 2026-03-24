# Phase 5 スペック: 管理UI・ファイル操作エンドポイント・ingest.py 廃止

作成日: 2026-03-24
更新日: 2026-03-24（ChatGPT レビュー反映）
対象ファイル: graphrag/main.py, graphrag/ingest.py（廃止）, graphrag/docker-compose.yml, graphrag/requirements.txt

---

## 目的

- ブラウザからファイルをアップロード・削除できる管理 UI を提供する
- ファイル操作と同時に ES・Neo4j を更新し、`input/` を Git で自動記録する
- `ingest.py` に残っているクライアント機能を全てサーバー側へ移植し廃止する

---

## 1. 事前準備

### 1-1. python-multipart の追加

FastAPI でファイルアップロードを受け取るには `python-multipart` が必要。
`graphrag/requirements.txt` に追加する：

```
python-multipart>=0.0.9
```

### 1-2. docker-compose.yml の変更

`graphrag-api` の `input/` マウントを読み取り専用（`:ro`）から読み書き可能（`:rw`）に変更する。

```yaml
# 変更前
- ${INGEST_INPUT_ROOT:-../input}:/input:ro

# 変更後
- ${INGEST_INPUT_ROOT:-../input}:/input:rw
```

### 1-3. Dockerfile への git インストールと commit identity 設定

コンテナ内から git コマンドを実行するため、Dockerfile に追加する：

```dockerfile
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
ENV GIT_AUTHOR_NAME="GraphRAG Bot"
ENV GIT_AUTHOR_EMAIL="graphrag@localhost"
ENV GIT_COMMITTER_NAME="GraphRAG Bot"
ENV GIT_COMMITTER_EMAIL="graphrag@localhost"
```

`user.name` / `user.email` が未設定だと `git commit` がエラーになるため、
Dockerfile の ENV で固定する。

### 1-4. input/ の Git 管理方針

`input/` は親リポジトリ（`/root/mywork/DIfy_Growi_Langfuse/`）配下にある。
**新たに `git init` はしない**（nested git repo になり親リポジトリと衝突するため）。

コンテナ内でのコミットは親リポジトリの `.git` に対して行う：

```
マウント構成:
  ホスト: /root/mywork/DIfy_Growi_Langfuse/input/  → コンテナ: /input/
  ホスト: /root/mywork/DIfy_Growi_Langfuse/.git/   → コンテナ: /repo/.git/  （追加マウント）
```

`docker-compose.yml` に `.git` ディレクトリのマウントを追加する：

```yaml
volumes:
  - ${INGEST_INPUT_ROOT:-../input}:/input:rw
  - ${GIT_REPO_ROOT:-.}/.git:/repo/.git:rw   # 親リポジトリの .git をマウント
```

コンテナ内で git コマンドを実行する際は `GIT_DIR=/repo/.git GIT_WORK_TREE=/input` を指定する：

```bash
GIT_DIR=/repo/.git GIT_WORK_TREE=/input git add contracts/nda/sample.pdf
GIT_DIR=/repo/.git GIT_WORK_TREE=/input git commit -m "add: contracts/nda/sample.pdf"
```

**「nothing to commit」の扱い:** git commit が変更なしで終了する場合（exit code 1）はエラーとせずスキップする。

---

## 2. build_*_payload() のリファクタリング前提

**現状の問題:** `ingest.py` の `build_*_payload()` 系関数は異常時に `sys.exit(1)` を呼ぶ（CLI 前提の実装）。API の BackgroundTasks やエンドポイントから呼ぶと、プロセスごと落ちる危険がある。

**Phase 5 実装前に必須のリファクタリング:**

`sys.exit(1)` を呼んでいる箇所を `ValueError` または `RuntimeError` に置き換える。

```python
# 変更前（CLI 向け）
sys.exit(1)

# 変更後（API 向け）
raise ValueError("ファイルが INGEST_INPUT_ROOT の外にあります: ...")
raise RuntimeError("PDF からテキストを抽出できませんでした: ...")
```

対象箇所:
- `get_input_root()`: 未設定時の `sys.exit(1)` → `RuntimeError`
- `build_pdf_payload()`: パス外・空テキスト時の `sys.exit(1)` → `ValueError`
- `build_markdown_payload()`: パス外・空テキスト時の `sys.exit(1)` → `ValueError`
- `build_txt_payload()`: パス外・空テキスト時の `sys.exit(1)` → `ValueError`
- `extract_pdf_text()`: `pdfplumber` 未インストール時の `sys.exit(1)` → `ImportError` そのまま伝播

API 側では `ValueError` / `RuntimeError` を catch して 400 / 500 を返す。
`ingest.py` の CLI 側（`cmd_*` 関数）では catch して `sys.exit(1)` に変換する（既存動作を維持）。

---

## 3. 追加するエンドポイント

### 3-1. POST /ingest-file

単一ファイルをアップロードして `input/` 配下の指定フォルダに保存し、GraphRAG に取り込む。

**リクエスト（multipart/form-data）:**

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `file` | ファイル | 必須 | アップロードするファイル（.pdf / .md / .txt） |
| `folder` | 文字列 | 任意 | 保存先サブフォルダ（例: `contracts/nda`）。省略時はルート |

**パストラバーサル対策（folder パラメータ）:**

```python
input_root = Path(INGEST_INPUT_ROOT).resolve()
dest_dir = (input_root / folder).resolve()
# INGEST_INPUT_ROOT の外に出る指定（../ や絶対パス）を拒否
dest_dir.relative_to(input_root)  # ValueError が出たら 400 を返す
```

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
1. `folder` パラメータをパストラバーサルチェック（`relative_to()` で `INGEST_INPUT_ROOT` 内に閉じ込める）
2. `INGEST_INPUT_ROOT/<folder>/` にファイルを保存
3. 拡張子を判定して `build_*_payload()` を呼び出す（カテゴリは `folder` から自動付与）
4. ES・Neo4j に書き込む
5. Git コミット: `GIT_DIR=/repo/.git GIT_WORK_TREE=/input git add ... && git commit -m "add: <source_ref>"`（nothing to commit はスキップ）

**対応拡張子:** `.pdf`, `.md`, `.txt`
**非対応の場合:** 400 Bad Request

---

### 3-2. DELETE /documents/{document_id}

ドキュメントを ES・Neo4j から削除し、ファイルが `input/` 配下にある場合はファイルも削除する。

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "pdf-contracts_nda_sample",
  "file_deleted": true,
  "git_committed": true
}
```

**処理フロー:**
1. ES から `document_id` に紐づくチャンクを全削除（`delete_by_query`）
2. Neo4j から以下を全削除（Chunk を先に消してから Document を消す）:
   ```cypher
   -- Chunk の MENTIONS エッジを削除
   MATCH (c:Chunk {document_id: $document_id})-[r:MENTIONS]->() DELETE r
   -- HAS_CHUNK エッジと Chunk ノードを削除
   MATCH (d:Document {id: $document_id})-[r:HAS_CHUNK]->(c:Chunk) DELETE r, c
   -- Document ノードを削除
   MATCH (d:Document {id: $document_id}) DELETE d
   ```
   **Entity ノードは削除しない**（他のドキュメントから参照されている可能性があるため）

3. ソース種別による分岐:
   - `source = "pdf" / "markdown" / "txt"`: `source_ref` からファイルパスを特定し削除 → Git コミット
   - `source = "growi"`: ファイルは存在しないためファイル削除・Git コミットはスキップ

**レスポンス（404 Not Found）:** document_id が存在しない場合

---

### 3-3. POST /ingest-growi

Growi のページパスを UI 入力として受け取り、内部で page_id に解決してから取り込む。

**背景:** Growi v5 以降は同一ページパスが重複しうる（ページ移動・再作成）。
`document_id` は page_id ベースで生成し一意性を保証する。現行の `build_growi_payload()` も page_id ベース（`growi-{page_id}`）。

**リクエスト（JSON）:**
```json
{
  "page_path": "/docs/spec/system-design"
}
```

**処理フロー:**
1. `GROWI_URL` と `GROWI_API_KEY` 環境変数でクレデンシャルを取得
2. Growi API `GET /_api/v3/pages?path=<page_path>` でページを検索し `page_id` を取得
3. `page_id` を使って既存の `build_growi_payload(growi_url, page_id, api_key)` を呼び出す
4. ES・Neo4j に書き込む（ファイル操作・Git コミットなし）

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "growi-12345",
  "page_id": "12345",
  "skipped": false
}
```

**必要な環境変数（docker-compose.yml に追加）:**

| 変数名 | 説明 |
|--------|------|
| `GROWI_URL` | Growi のベース URL（例: `http://host.docker.internal:3300`） |
| `GROWI_API_KEY` | Growi の API キー |

---

### 3-4. GET /ui（管理画面）

ブラウザで操作できるシンプルなファイル管理画面。FastAPI の HTML レスポンスで提供。

**画面構成:**

```
[GraphRAG ファイル管理]

[取り込み済みドキュメント一覧]  ← GET /documents の結果
  document_id                source_ref                status
  pdf-contracts_nda_sample   contracts/nda/sample.pdf  ✅ ok    [削除]
  pdf-hr_handbook            hr/handbook.pdf           ⚠ mismatch  [再取り込み] [削除]

[ファイルアップロード]
  フォルダ: [contracts/nda     ]
  [ここにファイルをドロップ または クリックして選択]
  [アップロード & 取り込み]

[一括取り込み]
  [/ingest-dir を実行]
  ジョブ状態: done (processed: 12, skipped: 3)
```

---

## 4. ingest.py の廃止

Phase 5 完了時に `graphrag/ingest.py` を削除する。

| 旧コマンド | 移行先 |
|-----------|--------|
| `ingest.py pdf --file <path>` | `POST /ingest-file` または 管理UI |
| `ingest.py md --file <path>` | `POST /ingest-file` または 管理UI |
| `ingest.py txt --file <path>` | `POST /ingest-file` または 管理UI |
| `ingest.py growi --page-id <id>` | `POST /ingest-growi`（page_path 入力に変更） |
| `ingest.py input-dir` | `POST /ingest-dir`（Phase 4 で移行済み） |

---

## 5. 動作確認手順

```bash
# graphrag/ ディレクトリに移動してから実行すること
cd /root/mywork/DIfy_Growi_Langfuse/graphrag

# コンテナ再ビルド
docker compose build graphrag-api && docker compose up -d graphrag-api

# 1. 単一PDFアップロード
curl -s -X POST http://localhost:8080/ingest-file \
  -F "file=@/path/to/sample.pdf" \
  -F "folder=contracts/nda" | python3.12 -m json.tool

# 2. ドキュメント一覧と整合性確認
curl -s http://localhost:8080/documents | python3.12 -m json.tool

# 3. ドキュメント削除
curl -s -X DELETE http://localhost:8080/documents/pdf-contracts_nda_sample \
  | python3.12 -m json.tool

# 4. Growi ページ取り込み（page_path 指定）
curl -s -X POST http://localhost:8080/ingest-growi \
  -H "Content-Type: application/json" \
  -d '{"page_path": "/docs/spec"}' | python3.12 -m json.tool

# 5. Git ログで操作履歴を確認（ホスト側）
cd /root/mywork/DIfy_Growi_Langfuse && git log --oneline -- input/

# 6. 管理UI をブラウザで確認
# http://localhost:8080/ui
```

---

## 6. 実装しないこと（Phase 5 のスコープ外）

- watchdog によるファイル監視（自動取り込み）
- cron による `/ingest-dir` の定期実行
- Growi の全ページ一括取り込み（`/ingest-growi-all` 等）
- Git の push / リモートリポジトリ連携
- ユーザー認証（管理 UI はローカル運用前提）
- Entity ノードの削除（他文書との共有を考慮して対象外）

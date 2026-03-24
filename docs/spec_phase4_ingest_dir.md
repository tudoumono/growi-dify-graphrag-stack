# Phase 4 スペック: /ingest-dir エンドポイント

作成日: 2026-03-24
対象ファイル: graphrag/main.py, graphrag/ingest.py

---

## 目的

`ingest.py input-dir` で行っていた「input/ ディレクトリ一括取り込み」を
サーバー側エンドポイントへ移植する。

- 呼び出し側は HTTP POST 一発でよくなる
- 処理は非同期（バックグラウンド）で行い、ジョブIDで状態を確認できる
- `ingest.py` の `input-dir` サブコマンドは廃止

---

## 1. 追加するエンドポイント

### 1-1. POST /ingest-dir

input/ ディレクトリ以下を再帰スキャンして取り込みを開始する。

**リクエスト:** ボディなし

**レスポンス（202 Accepted）:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716",
  "status": "running",
  "message": "ingest-dir started"
}
```

**処理フロー（サーバー内部）:**
1. UUID でジョブIDを生成
2. ジョブ管理テーブルに `{status: "running", started_at, processed: 0, skipped: 0, failed: 0}` を登録
3. FastAPI の `BackgroundTasks` でスキャン処理を非同期起動
4. 即座に 202 を返す
5. バックグラウンドで `INGEST_INPUT_ROOT` 以下を再帰スキャン
   - 対象拡張子: `.pdf`, `.md`, `.txt`
   - 各ファイルに既存の `build_*_payload()` を呼び出す
   - `POST /ingest` の内部ロジックを直接呼び出す（HTTP は介さない）
   - processed / skipped / failed をカウント
6. 完了後にジョブ状態を `done` または `failed` に更新

### 1-2. GET /ingest-job/{job_id}

ジョブの状態を確認する。

**レスポンス（200 OK）:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716",
  "status": "done",
  "started_at": "2026-03-24T10:00:00Z",
  "finished_at": "2026-03-24T10:00:45Z",
  "processed": 12,
  "skipped": 3,
  "failed": 0,
  "errors": []
}
```

status の値: `running` / `done` / `failed`

**レスポンス（404 Not Found）:** ジョブIDが存在しない場合

### 1-3. GET /documents

ES と Neo4j の両方に取り込まれているドキュメント一覧を返す。
整合性チェックにより「どちらか片方にしか存在しない」ドキュメントを検出できる。

**リクエスト:** ボディなし

**レスポンス（200 OK）:**
```json
[
  {
    "document_id": "pdf-contracts_nda_sample",
    "source_ref": "contracts/nda/sample.pdf",
    "category": "contracts/nda",
    "in_es": true,
    "in_neo4j": true,
    "status": "ok"
  },
  {
    "document_id": "pdf-hr_handbook",
    "source_ref": "hr/handbook.pdf",
    "category": "hr",
    "in_es": true,
    "in_neo4j": false,
    "status": "mismatch"
  }
]
```

**status の値:**

| status | 意味 |
|--------|------|
| `ok` | ES・Neo4j 両方に存在 |
| `mismatch` | どちらか片方にしか存在しない（再取り込み推奨） |

**処理フロー:**
1. ES から全 document_id を取得（`graphrag_chunks` インデックスを集約）
2. Neo4j から全 Document ノードの document_id を取得
3. 両者を突合して status を付与して返却

**不整合時のメタデータの扱い:**
- `status: mismatch` のドキュメントのメタデータ（source_ref / category）は **ES を正**として返す
- Neo4j にしか存在しない場合は Neo4j の document_id のみ返し、他フィールドは null とする

---

## 2. ジョブ管理の実装方針

### データ構造（インメモリ dict）

```python
# main.py グローバル変数
jobs: dict[str, dict] = {}
```

### TTL とクリーンアップ

- 各ジョブに `created_at` を保持
- `/ingest-dir` が呼ばれるたびに、`created_at` が 1時間以上前のエントリを削除してからジョブ登録
- 再起動するとジョブ情報はリセットされる（許容する）

### 制約事項（単一プロセス前提）

- `jobs` dict はプロセス内メモリであるため、uvicorn を単一ワーカーで起動する前提
- 現在の Dockerfile は `uvicorn main:app --host 0.0.0.0 --port 8080` で単一プロセス起動のため問題なし
- 将来 `--workers N` や複数コンテナ構成にする場合は Redis 等の外部ストアへの移行が必要

---

## 3. ingest.py の変更

`input-dir` サブコマンド（`cmd_input_dir` 関数）を削除する。

残すサブコマンド: `pdf`, `md`, `txt`, `growi`（Phase 5 の `/ingest-file` / `/ingest-growi` が完成するまでの過渡期）

---

## 4. 動作確認手順

```bash
# graphrag/ ディレクトリに移動してから実行すること
cd /root/mywork/DIfy_Growi_Langfuse/graphrag

# コンテナ再ビルド
docker compose build graphrag-api && docker compose up -d graphrag-api

# 1. バックグラウンド取り込み開始
curl -s -X POST http://localhost:8080/ingest-dir | python3.12 -m json.tool

# 2. ジョブIDを確認して状態をポーリング
curl -s http://localhost:8080/ingest-job/<job_id> | python3.12 -m json.tool

# 3. done になったらドキュメント一覧と整合性を確認
curl -s http://localhost:8080/documents | python3.12 -m json.tool

# 4. mismatch があれば再取り込み
curl -s -X POST http://localhost:8080/ingest-dir | python3.12 -m json.tool
```

---

## 5. 実装しないこと（Phase 5 以降）

- `/ingest-file`（単一ファイルアップロード + Git コミット）
- `/ingest-growi`（Growi ページ取り込み）
- `DELETE /documents/{document_id}`（削除 + Git コミット）
- 管理 UI（ブラウザからのファイル操作画面）
- `input/` の Git リポジトリ化
- docker-compose.yml の `:ro` → `:rw` 変更
- watchdog によるファイル監視
- cron による定期実行
- `ingest.py` の廃止

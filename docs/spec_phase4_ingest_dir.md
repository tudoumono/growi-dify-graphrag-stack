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
   - payload に **`scope="official"`, `expires_at=null`** を必ず付与する
   - `POST /ingest` の内部ロジックを直接呼び出す（HTTP は介さない）
   - processed / skipped / failed をカウント
6. 完了後にジョブ状態を `done` または `failed` に更新

**`/ingest-dir` のスコープ:**
- `official` ドキュメントのみを対象とする
- temporary ドキュメントのクリーンアップは行わない（クリーンアップは `/ingest-temp` の責務）

**同時実行制御（409）:**
- ジョブ実行中（`status: "running"`）に再度 POST された場合は **409 Conflict** を返す
- レスポンス例: `{"error": "job_already_running", "running_job_id": "550e8400-..."}`
- 完了済み（`done` / `failed`）であれば新規ジョブを受け付ける
- `jobs` dict に `status: "running"` のエントリが存在するかで判定する

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
`scope` パラメータで正式/一時を切り替えられる。
`status` フィールドは「ES/Neo4j の整合性」のみを表す（scope とは独立）。

**クエリパラメータ:**

| パラメータ | デフォルト | 説明 |
|-----------|-----------|------|
| `scope` | `official` | `official` / `temporary` / `all` |

**レスポンス（200 OK）:**
```json
[
  {
    "document_id": "pdf-contracts_nda_sample",
    "source_ref": "contracts/nda/sample.pdf",
    "category": "contracts/nda",
    "scope": "official",
    "expires_at": null,
    "in_es": true,
    "in_neo4j": true,
    "status": "ok"
  },
  {
    "document_id": "tmp-abc123-proposal",
    "source_ref": "proposal.pdf",
    "category": null,
    "scope": "temporary",
    "expires_at": "2026-03-25T10:00:00Z",
    "in_es": true,
    "in_neo4j": true,
    "status": "ok"
  }
]
```

**status の値（ES/Neo4j 整合性のみ。scope とは独立）:**

| status | 意味 |
|--------|------|
| `ok` | ES・Neo4j 両方に存在 |
| `mismatch` | どちらか片方にしか存在しない（再取り込み推奨） |

`expired` は定義しない。期限切れドキュメントはクエリ時フィルタで除外するため、
API レスポンスに出現しない。クリーンアップ未実行でも同様。

**期限切れ temporary の除外ルール（クエリ時フィルタ）:**
- `GET /documents` は `expires_at IS NULL OR expires_at > now` をクエリ条件に必ず付与する
- クリーンアップ（物理削除）が実行されていなくても、期限切れ temporary はレスポンスに含めない
- `?scope=all` を指定した場合も同様（期限切れは常に除外）

**`expires_at` の型と格納形式:**
- ES: `date` 型（`format: strict_date_optional_time||epoch_millis`）。値は UTC ISO8601 文字列（例: `"2026-03-25T10:00:00Z"`）で保存する
- Neo4j: 文字列プロパティとして UTC ISO8601 形式（`"YYYY-MM-DDTHH:MM:SSZ"`）で保存する
- Cypher 比較: `$now` パラメータも同じ UTC ISO8601 文字列で渡す（例: `datetime().epochMillis` は使わず文字列比較で統一）
- Python 側での生成: `datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")`

**scope の格納場所:**
`scope` と `expires_at` は **ES と Neo4j の両方に保持する**。
Neo4j 側の graph hit も scope でフィルタリングするため、ES だけでは不十分。

**処理フロー:**
1. ES から全 document_id を `scope` フィルタ + `expires_at IS NULL OR expires_at > now` フィルタ付きで集約
2. Neo4j から Document ノードを取得（**ES と同じ scope + expires_at フィルタを付与**）
   ```cypher
   MATCH (d:Document)
   WHERE ($scope = 'all' OR d.scope = $scope)
     AND (d.expires_at IS NULL OR d.expires_at > $now)
   RETURN d.id AS document_id
   ```
3. 両者を突合して status を付与して返却

**GET /documents のエラー:**
- `scope` 値が `official` / `temporary` / `all` 以外の場合は 400 Bad Request
  - `{"error": "invalid_scope", "allowed": ["official", "temporary", "all"]}`

**不整合時のメタデータの扱い:**
- `status: mismatch` のメタデータは **ES を正**として返す
- Neo4j にしか存在しない場合は document_id のみ返し、他フィールドは null とする

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

残すサブコマンド: `pdf`, `md`, `txt`, `growi`（Phase 5a の `/ingest-temp` / `/ingest-growi` が完成するまでの過渡期）

---

## 4. 動作確認手順

### 4-0. ES インデックス再構築（Phase 4 初回のみ必須）

`scope` と `expires_at` を ES マッピングに追加するにはインデックスの再作成が必要。
既存インデックスへのフィールド追加は ES では型変更を伴うためできない。

```bash
cd /root/mywork/DIfy_Growi_Langfuse/graphrag

# 既存インデックスを削除（データも消える）
curl -s -X DELETE -u elastic:graphrag http://localhost:9201/graphrag_chunks | python3.12 -m json.tool

# コンテナ再ビルド（新マッピングを含む main.py を反映）
docker compose build graphrag-api && docker compose up -d graphrag-api

# 起動後、/ingest-dir を実行してフル再取り込み（インデックスは自動再作成される）
curl -s -X POST http://localhost:8080/ingest-dir | python3.12 -m json.tool
```

### 4-1. 通常の動作確認

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

- `/ingest-temp`（一時取り込み・TTL・クリーンアップ）
- `/ingest-growi`（Growi ページ取り込み）
- `DELETE /documents/{document_id}`
- `/documents/{id}/reingest`（整合性修復）
- 管理 UI（ブラウザからのファイル操作画面）
- `input/` の Git 自動コミット
- docker-compose.yml の `:ro` → `:rw` 変更
- watchdog によるファイル監視
- cron による定期実行
- `ingest.py` の廃止

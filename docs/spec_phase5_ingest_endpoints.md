# Phase 5 スペック: 正式・一時 2系統ドキュメント管理

作成日: 2026-03-24
更新日: 2026-03-24（2系統設計・scope 導入・フェーズ分割反映）
対象ファイル: graphrag/main.py, graphrag/ingest.py（廃止）, graphrag/docker-compose.yml, graphrag/requirements.txt

---

## 設計方針

### 2系統の分離

| 系統 | 正本 | 取り込み方法 | Git 管理 | 寿命 |
|------|------|------------|---------|------|
| **正式** | `input/` ディレクトリ | `/ingest-dir` | あり | 永続 |
| **一時** | なし（ファイルは一時領域のみ） | `/ingest-temp` | なし | TTL（デフォルト24h） |

### scope フィールド

ES と Neo4j の **両方** に `scope` と `expires_at` を持たせる。
検索は ES のベクトル検索だけでなく Neo4j の関連チャンク探索も経由するため、
どちらか片方だけに持たせると graph hit 側で一時データが混入する。

```
scope: "official" | "temporary"
expires_at: ISO8601 or null
```

### 検索と一覧のデフォルト

- `/search` デフォルト: `official` のみ（一時データは混入しない）
- `GET /documents` デフォルト: `official` のみ
- 一時データを見るには明示的に `?scope=temporary` または `?scope=all` を指定

---

## Phase 5 の構成

Phase 5 は 3 サブフェーズに分割する：

| フェーズ | 内容 |
|---------|------|
| **Phase 5a** | `/ingest-temp`、`DELETE /documents/{id}`、`/documents/{id}/reingest`、scope 対応 |
| **Phase 5b** | `GET /ui`（管理 UI） |
| **Phase 5c** | Git 自動コミット、`ingest.py` 完全廃止 |

---

## Phase 5a: /ingest-temp・削除・再取り込み・scope 対応

### 事前準備: python-multipart の追加

```
# graphrag/requirements.txt に追加
python-multipart>=0.0.9
```

### 環境変数の追加

```yaml
# docker-compose.yml の graphrag-api environment に追加
TEMP_DOC_TTL_HOURS: ${TEMP_DOC_TTL_HOURS:-24}
```

---

### エンドポイント: POST /ingest-temp

一時的な検索・検証用途でファイルを取り込む。`input/` には保存しない。

**リクエスト（multipart/form-data）:**

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `file` | ファイル | 必須 | .pdf / .md / .txt |

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "tmp-abc123-proposal",
  "scope": "temporary",
  "expires_at": "2026-03-25T10:00:00Z",
  "skipped": false
}
```

**処理フロー:**
1. **期限切れ一時データのクリーンアップ**（毎回実行）
   - ES: `expires_at < now` かつ `scope=temporary` のドキュメントを `delete_by_query`
   - Neo4j: 同条件の Chunk・Document を削除
   - コンテナ内の一時領域（`/tmp/graphrag_temp/`）のファイルも削除
2. ファイルを `/tmp/graphrag_temp/` に一時保存
3. `document_id = "tmp-{uuid8}-{stem}"` を生成（`uuid8` は UUID の先頭8文字）
4. 拡張子を判定して `build_*_payload()` を呼び出す
5. payload に `scope="temporary"`, `expires_at=now+TTL` を追加
6. ES・Neo4j に書き込む（`scope` と `expires_at` は両方に保存）

**対応拡張子:** `.pdf`, `.md`, `.txt` / 非対応: 400 Bad Request

**パストラバーサル:** folder 指定なし（一時領域に固定）のため不要

---

### エンドポイント: DELETE /documents/{document_id}

`scope` によって削除対象と手順が異なる。

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "pdf-contracts_nda_sample",
  "scope": "official",
  "file_deleted": true,
  "git_committed": false
}
```

**scope による分岐:**

| scope | ES/Neo4j | ファイル削除 | Git |
|-------|----------|------------|-----|
| `official` | 削除 | `input/<source_ref>` を削除 | Phase 5c で対応 |
| `temporary` | 削除 | `/tmp/graphrag_temp/` のファイルを削除 | なし |

**Neo4j 削除手順（scope 共通）:**
```cypher
-- 1. Chunk の MENTIONS エッジを削除
MATCH (c:Chunk {document_id: $document_id})-[r:MENTIONS]->() DELETE r
-- 2. HAS_CHUNK エッジと Chunk ノードを削除
MATCH (d:Document {id: $document_id})-[r:HAS_CHUNK]->(c:Chunk) DELETE r, c
-- 3. Document ノードを削除
MATCH (d:Document {id: $document_id}) DELETE d
```

**Entity ノードは削除しない**（他ドキュメントから参照されている可能性があるため）

**レスポンス（404）:** document_id が存在しない場合

---

### エンドポイント: POST /documents/{document_id}/reingest

`mismatch` 状態のドキュメントを再取り込みして整合性を修復する。

**レスポンス（200 OK）:**
```json
{
  "status": "ok",
  "document_id": "pdf-contracts_nda_sample",
  "scope": "official"
}
```

**処理フロー（official の場合）:**
1. ES から `source_ref` を取得
2. `INGEST_INPUT_ROOT/<source_ref>` のファイルを読んで `build_*_payload()` を呼び出す
3. ES・Neo4j に書き込む（`scope="official"` で上書き）

**処理フロー（temporary の場合）:**
1. `/tmp/graphrag_temp/` にファイルが残っている場合のみ再取り込み
2. ファイルがない場合は 409 を返す（「一時ファイルは既に削除されています。再アップロードしてください」）

---

### scope 対応: /search

`SearchRequest` に `scope` フィールドを追加。

```python
class SearchRequest(BaseModel):
    query: str
    scope: str = "official"   # デフォルト: 正式のみ
    # ... 既存フィールド
```

**scope フィルタを通す箇所（3か所）:**

1. **ES フィルタ**: `term: {scope: req.scope}` を追加（`scope=all` の場合はフィルタなし）
2. **Neo4j seed Cypher**: `WHERE c.scope = $scope` を追加
3. **Neo4j graph hit Cypher**: `WHERE related.scope = $scope` を追加

---

### 昇格（promotion）の設計方針

**専用エンドポイントは作らない。**

昇格フロー:
1. ユーザーが `input/` の適切なフォルダにファイルをコピー
2. `POST /ingest-dir` を実行
3. official として取り込まれる
4. 古い temporary エントリは TTL で自動削除

**一時的な二重存在について:**
昇格直後、同じ内容が `official` と `temporary` で同時に存在する場合がある。
`scope=all` や `GET /documents?scope=all` では二重に表示されるが、これは**仕様として許容する**。
既定の検索は `official` のみなので実害はない。

---

## Phase 5b: GET /ui（管理 UI）

FastAPI の HTML レスポンスで提供するシンプルな管理画面。

**画面構成:**

```
[GraphRAG ファイル管理]

[表示: ● 正式のみ  ○ 一時のみ  ○ すべて]

[ドキュメント一覧]  ← GET /documents の結果
  document_id                  source_ref                scope      status
  pdf-contracts_nda_sample     contracts/nda/sample.pdf  正式       ✅ ok      [削除] [再取込]
  pdf-hr_handbook              hr/handbook.pdf           正式       ⚠ mismatch [削除] [再取込]
  tmp-abc123-proposal          proposal.pdf              一時 23h   ✅ ok      [削除]

[一時ファイルアップロード]
  [ここにファイルをドロップ または クリックして選択]
  → POST /ingest-temp を呼び出す

[正式一括再同期]
  [/ingest-dir を実行]  → POST /ingest-dir を呼び出す
  ジョブ状態: done (processed: 12, skipped: 3)
```

---

## Phase 5c: Git 自動コミット・ingest.py 廃止

### docker-compose.yml の変更

```yaml
# 変更前
- ${INGEST_INPUT_ROOT:-../input}:/input:ro

# 変更後
- ${INGEST_INPUT_ROOT:-../input}:/input:rw
- ${GIT_REPO_ROOT:-.}/.git:/repo/.git:rw
```

### Dockerfile への追加

```dockerfile
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
ENV GIT_AUTHOR_NAME="GraphRAG Bot"
ENV GIT_AUTHOR_EMAIL="graphrag@localhost"
ENV GIT_COMMITTER_NAME="GraphRAG Bot"
ENV GIT_COMMITTER_EMAIL="graphrag@localhost"
```

### build_*_payload() のリファクタリング前提

`sys.exit(1)` を `ValueError` / `RuntimeError` に置き換える（CLI 側は catch して sys.exit に変換）。

### DELETE /documents の Git 対応（official のみ）

```bash
GIT_DIR=/repo/.git GIT_WORK_TREE=/input git rm <source_ref>
GIT_DIR=/repo/.git GIT_WORK_TREE=/input git commit -m "delete: <source_ref>"
```

"nothing to commit" は exit code で判定してスキップする。

### ingest.py の廃止

| 旧コマンド | 移行先 |
|-----------|--------|
| `ingest.py pdf --file <path>` | `POST /ingest-temp`（一時）または `input/` に置いて `/ingest-dir` |
| `ingest.py md --file <path>` | 同上 |
| `ingest.py txt --file <path>` | 同上 |
| `ingest.py growi --page-id <id>` | `POST /ingest-growi` |
| `ingest.py input-dir` | `POST /ingest-dir`（Phase 4 で移行済み） |

---

## 動作確認手順（Phase 5a）

```bash
# graphrag/ ディレクトリに移動してから実行すること
cd /root/mywork/DIfy_Growi_Langfuse/graphrag

# コンテナ再ビルド
docker compose build graphrag-api && docker compose up -d graphrag-api

# 1. 一時ファイルアップロード
curl -s -X POST http://localhost:8080/ingest-temp \
  -F "file=@/path/to/proposal.pdf" | python3.12 -m json.tool

# 2. 一覧確認（デフォルト: official のみ）
curl -s http://localhost:8080/documents | python3.12 -m json.tool

# 3. 一覧確認（一時を含む）
curl -s "http://localhost:8080/documents?scope=all" | python3.12 -m json.tool

# 4. 一時ドキュメントを scope 指定で検索
curl -s -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "提案内容", "scope": "temporary"}' | python3.12 -m json.tool

# 5. ドキュメント削除
curl -s -X DELETE http://localhost:8080/documents/tmp-abc123-proposal \
  | python3.12 -m json.tool

# 6. mismatch 修復
curl -s -X POST http://localhost:8080/documents/pdf-hr_handbook/reingest \
  | python3.12 -m json.tool
```

---

## 実装しないこと（Phase 5 のスコープ外）

- watchdog によるファイル監視
- cron による `/ingest-dir` の定期実行
- Growi の全ページ一括取り込み
- Git の push / リモートリポジトリ連携
- 専用の昇格エンドポイント（`/documents/{id}/promote`）
- Entity ノードの削除
- ユーザー認証（管理 UI はローカル運用前提）

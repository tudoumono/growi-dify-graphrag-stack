# Phase 3 スペック: 一括取り込み・未変更スキップ・カテゴリ自動推定

SDD Phase 3 / ステアリング参照: docs/steering_ingest_redesign.md
作成日: 2026-03-24
対象ファイル: graphrag/ingest.py, graphrag/main.py

---

## このスペックが解決する問題

| 問題 | 対応 Goal |
|------|-----------|
| ファイルごとに手動でコマンドを実行しなければならない | G3 拡張性 |
| 内容が変わっていないファイルも毎回再処理される | G4 効率性 |
| カテゴリを手動指定しなければならない | G4 効率性 |
| `category` の prefix 検索が大規模データで遅い | G4 効率性 |

---

## 変更 1: カテゴリ自動推定（ingest.py 全サブコマンドに適用）

### ルール

`INGEST_INPUT_ROOT` からの相対パスの **親ディレクトリ全体** をカテゴリとする。

```
input/contracts/nda/sample.pdf      → category: "contracts/nda"
input/contracts/service/agree.pdf   → category: "contracts/service"
input/hr/policies/policy.md         → category: "hr/policies"
input/readme.txt                    → category: "" （直置きは空）
```

### 優先順位

`--category` で明示指定した場合はそちらを優先する。省略時は自動推定を使う。

```python
# build_*_payload() 内の共通ロジック
auto_category = str(relative.parent).replace("\\", "/")
if auto_category == ".":
    auto_category = ""
category = category_arg or auto_category  # 引数優先、なければ自動
```

### 適用範囲

Phase 1,2 で実装済みの `build_pdf_payload()` / `build_markdown_payload()` / `build_txt_payload()` にも追加する。

---

## 変更 2: ES マッピングに path_hierarchy tokenizer を追加（main.py）

### 背景

`category` フィールドを `term` クエリで検索しつつ、階層の上位でもヒットさせたい。
`prefix` クエリは大規模データで遅くなるため、インデックス時に階層分解する方式を採用する。

### 仕組み

```
"contracts/nda" を登録すると ES 内部で自動分解される：
  → "contracts"
  → "contracts/nda"

検索: {"term": {"category": "contracts"}}
  → "contracts/nda" も "contracts/service" もヒット ✓
```

### マッピング変更内容

`ensure_es_index()` に `settings` を追加し、`category` フィールドの型を変更する。

```python
mapping = {
    "settings": {
        "analysis": {
            "analyzer": {
                "path_analyzer": {
                    "tokenizer": "path_tokenizer"
                }
            },
            "tokenizer": {
                "path_tokenizer": {
                    "type": "path_hierarchy",
                    "delimiter": "/"
                }
            }
        }
    },
    "mappings": {
        "properties": {
            # ... 既存フィールド ...
            "category": {
                "type": "text",
                "analyzer": "path_analyzer",
                "fields": {
                    "keyword": {"type": "keyword"}  # 完全一致が必要な場合は category.keyword を使う
                }
            },
            # ... 既存フィールド ...
        }
    }
}
```

### 検索側の変更

`build_es_filters()` の category フィルタは **変更不要**。
`{"term": {"category": req.category}}` のままで階層検索が機能する。

---

## 変更 3: content_hash による未変更スキップ（main.py）

### 期待する振る舞い

同じ `document_id` で同じ内容のファイルを再送信した場合、処理をスキップして即座に返す。

```
1回目の取り込み: hash 計算 → 新規なので保存する
2回目（内容同じ）: hash 計算 → 一致したのでスキップ
3回目（内容変更）: hash 計算 → 不一致なので再処理する
```

### hash の計算方法

`text` フィールド全体の SHA-256 ハッシュを使う。

```python
import hashlib
content_hash = hashlib.sha256(req.text.encode("utf-8")).hexdigest()
```

### スキップ判定の場所

`/ingest` エンドポイントの先頭で、既存の Document ノードの `content_hash` と比較する。

```python
@app.post("/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    content_hash = hashlib.sha256(req.text.encode("utf-8")).hexdigest()

    # Neo4j の Document ノードに保存済みの hash と比較
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (d:Document {id: $id}) RETURN d.content_hash AS hash",
                id=req.document_id,
            )
            record = result.single()
            if record and record["hash"] == content_hash:
                # 未変更: スキップして既存チャンク情報を返す
                existing = session.run(
                    "MATCH (d:Document {id: $id})-[:HAS_CHUNK]->(c:Chunk) "
                    "RETURN c.id AS chunk_id ORDER BY c.chunk_index",
                    id=req.document_id,
                )
                chunk_ids = [r["chunk_id"] for r in existing]
                return {
                    "document_id": req.document_id,
                    "chunks_stored": 0,
                    "chunk_ids": chunk_ids,
                    "skipped": True,
                    "reason": "content unchanged",
                }
    finally:
        driver.close()

    # 変更あり: 通常の取り込み処理へ
    ...
```

### Document ノードへの content_hash 保存

`document_properties()` に `content_hash` を追加する。

```python
def document_properties(req: IngestRequest, content_hash: str) -> dict[str, Any]:
    return {
        ...
        "content_hash": content_hash,
    }
```

### IngestRequest の変更

なし。`content_hash` は `main.py` 側で `text` から自動計算するため、
`ingest.py` 側で意識する必要はない。

---

## 変更 4: input-dir サブコマンドの追加（ingest.py）

### 期待する振る舞い

`INGEST_INPUT_ROOT` 配下を再帰的にスキャンして、対応形式のファイルを全て取り込む。

```bash
python ingest.py input-dir
```

### 対応拡張子

| 拡張子 | 処理関数 |
|--------|---------|
| `.pdf` | `build_pdf_payload()` |
| `.md` | `build_markdown_payload()` |
| `.txt` | `build_txt_payload()` |
| それ以外 | スキップ＋警告を表示 |

### 処理の流れ

```python
def cmd_input_dir(args: argparse.Namespace) -> None:
    input_root = get_input_root()
    all_files = sorted(input_root.rglob("*"))  # 再帰的に全ファイル取得

    supported = {".pdf", ".md", ".txt"}
    targets = []
    for f in all_files:
        if not f.is_file():
            continue
        if f.suffix.lower() not in supported:
            print(f"[スキップ] 未対応形式: {f.relative_to(input_root)}", file=sys.stderr)
            continue
        targets.append(f)

    print(f"{len(targets)} ファイルを取り込みます...")

    for f in targets:
        ext = f.suffix.lower()
        if ext == ".pdf":
            payload = build_pdf_payload(f, input_root)
        elif ext == ".md":
            payload = build_markdown_payload(f, input_root)
        elif ext == ".txt":
            payload = build_txt_payload(f, input_root)
        send_and_print(args.graphrag_url, payload)
```

### カテゴリ

`build_*_payload()` 内の自動推定（変更 1）がそのまま適用される。
`input-dir` サブコマンドに `--category` オプションは持たない（常に自動推定）。

---

## 変更対象ファイルと変更箇所のまとめ

### graphrag/ingest.py

| 変更内容 | 対象 |
|---------|------|
| カテゴリ自動推定ロジックを追加 | `build_pdf_payload()` / `build_markdown_payload()` / `build_txt_payload()` |
| `cmd_input_dir()` 関数を追加 | `cmd_txt()` の直後 |
| `input-dir` サブコマンドの引数定義を追加 | `main()` 内 |
| `main()` の分岐に `input-dir` を追加 | `main()` 末尾 |

### graphrag/main.py

| 変更内容 | 対象 |
|---------|------|
| `ensure_es_index()` に path_hierarchy tokenizer の settings を追加 | `ensure_es_index()` |
| `category` フィールドのマッピングを text + path_analyzer に変更 | `ensure_es_index()` 内のマッピング定義 |
| `content_hash` の計算とスキップ判定を追加 | `/ingest` エンドポイント先頭 |
| `document_properties()` に `content_hash` を追加 | `document_properties()` |

---

## 実装手順

1. `main.py`: `ensure_es_index()` の path_hierarchy tokenizer 追加・category マッピング変更
2. `main.py`: `document_properties()` に `content_hash` 追加
3. `main.py`: `/ingest` にスキップ判定を追加
4. `ingest.py`: `build_pdf_payload()` / `build_markdown_payload()` / `build_txt_payload()` にカテゴリ自動推定を追加
5. `ingest.py`: `cmd_input_dir()` と `input-dir` サブコマンドを追加

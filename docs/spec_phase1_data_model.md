# Phase 1 スペック: データモデルの基盤

SDD Phase 1 / ステアリング参照: docs/steering_ingest_redesign.md
作成日: 2026-03-24
対象ファイル: graphrag/ingest.py, graphrag/main.py

---

## このスペックが解決する問題

| 問題 | 対応 Goal |
|------|-----------|
| 同名 PDF で document_id が衝突する | G1 一意性 |
| url フィールドに Web URL とファイル名が混在している | G5 意味の一貫性 |
| フォルダ情報が取り込み後に消える | G2 追跡可能性 |

---

## 変更 1: document_id の再設計

### 現在の振る舞い

```python
document_id = f"pdf-{pdf_path.stem}"
# 例: input/contracts/nda/sample.pdf → "pdf-sample"
# 例: input/hr/sample.pdf           → "pdf-sample"  ← 衝突する
```

### 期待する振る舞い

`INGEST_INPUT_ROOT` 環境変数で指定されたディレクトリからの相対パスを使い、
パス区切り `/` をアンダースコア `_` に置換して document_id を生成する。

```python
# INGEST_INPUT_ROOT=/workspace/input の場合
# input/contracts/nda/sample.pdf → "pdf-contracts_nda_sample"
# input/hr/sample.pdf            → "pdf-hr_sample"  ← 衝突しない
```

### ルール

- プレフィックスは入力形式に合わせる: `pdf-`, `growi-`, `md-`
- 拡張子は含めない（`sample.pdf` → `sample` として扱う）
- `INGEST_INPUT_ROOT` が設定されていない場合はエラーを出して終了する
- `INGEST_INPUT_ROOT` の外のファイルを指定した場合もエラーを出して終了する

### 入出力例

| INGEST_INPUT_ROOT | --file に渡すパス | 生成される document_id |
|-------------------|-----------------|----------------------|
| `/workspace/input` | `/workspace/input/sample.pdf` | `pdf-sample` |
| `/workspace/input` | `/workspace/input/contracts/nda/sample.pdf` | `pdf-contracts_nda_sample` |
| `/workspace/input` | `/workspace/input/hr/sample.pdf` | `pdf-hr_sample` |

---

## 変更 2: url / source_ref フィールドの意味整理

### 現在の振る舞い

`url` フィールドに「Web URL（Growi）」と「ファイル名（PDF）」が混在している。

```python
# Growi の場合（ingest.py:57）
"url": "http://localhost:3300/path/to/page"   # Web URL

# PDF の場合（ingest.py:181）
"url": "sample.pdf"                            # ファイル名だけ
```

### 期待する振る舞い

フィールドの役割を2つに分離する。

| フィールド | 役割 | PDF の場合 | Growi の場合 |
|-----------|------|------------|--------------|
| `url` | ブラウザで開ける Web URL。なければ空文字 | `""` | `http://growi/path` |
| `source_ref` | 元文書の参照先。ファイルパスや内部 ID | `input/contracts/nda/sample.pdf` | `growi-12345` |

`source_ref` の値:
- PDF: `INGEST_INPUT_ROOT` からの相対パス（例: `contracts/nda/sample.pdf`）
- Growi: `growi-{page_id}`（例: `growi-12345`）

### main.py 側の変更

`IngestRequest` モデルに `source_ref` フィールドを追加する。

```python
class IngestRequest(BaseModel):
    document_id: str
    title: str
    url: str           # 既存: Web URL（なければ空文字）
    source_ref: str    # 追加: 元文書の参照先
    text: str
    ...
```

ES マッピングにも `source_ref` フィールドを追加する。

```python
"source_ref": {"type": "keyword"},
```

---

## 変更 3: metadata にフォルダ構造を自動付与

### 現在の振る舞い

metadata は呼び出し元から明示的に渡さない限り空 `{}` になる。
フォルダの位置情報はどこにも保存されない。

### 期待する振る舞い

PDF の取り込み時に、`build_pdf_payload()` がフォルダ情報を metadata に自動的に追加する。

```python
metadata = {
    "source_type": "pdf",
    "filename": "sample.pdf",
    "dir": "contracts/nda",          # INGEST_INPUT_ROOT からの親ディレクトリ
    "path": "contracts/nda/sample.pdf",  # INGEST_INPUT_ROOT からの相対パス
}
```

### ルール

- metadata への自動付与は `build_pdf_payload()` 内で行う
- 呼び出し元（`cmd_pdf()`）から追加で metadata を渡すことも引き続き可能
- 自動付与した値は呼び出し元からの値で上書きできない（衝突した場合は自動付与を優先）

---

## 変更 4: build_pdf_payload() への分割（拡張への準備）

### 現在の振る舞い

`cmd_pdf()` の中にファイル読み込み・payload 組み立て・送信が混在している。

### 期待する振る舞い

責務を分離する。

```python
def build_pdf_payload(pdf_path: Path, input_root: Path, ...) -> dict:
    """PDF ファイルを読んで payload dict を返す。送信はしない。"""
    ...

def cmd_pdf(args: argparse.Namespace) -> None:
    """CLI からの引数を受け取り、build_pdf_payload() を呼んで送信する。"""
    payload = build_pdf_payload(...)
    send_and_print(args.graphrag_url, payload)
```

`build_growi_payload()` も同様に分離する。

---

## 環境変数

| 変数名 | 必須 | 説明 | 例 |
|--------|------|------|----|
| `INGEST_INPUT_ROOT` | 必須 | 取り込みファイルのルートディレクトリ | `/workspace/input` |

---

## 変更対象ファイルと変更箇所のまとめ

### graphrag/ingest.py

| 変更内容 | 現在の場所 |
|---------|-----------|
| `INGEST_INPUT_ROOT` を読み込む処理を追加 | モジュールトップレベル |
| `cmd_pdf()` を `build_pdf_payload()` + `cmd_pdf()` に分割 | L155〜190 |
| `build_pdf_payload()` 内で document_id をパスベースで生成 | L174 |
| `build_pdf_payload()` 内で `url=""`, `source_ref=相対パス` を設定 | L178〜184 |
| `build_pdf_payload()` 内で metadata にフォルダ情報を自動付与 | L178〜184 |
| `fetch_growi_page()` を `build_growi_payload()` にリネーム | L39〜60 |
| `build_growi_payload()` 内で `source_ref="growi-{page_id}"` を設定 | L54〜60 |

### graphrag/main.py

| 変更内容 | 現在の場所 |
|---------|-----------|
| `IngestRequest` に `source_ref: str` フィールドを追加 | L263〜274 |
| `document_properties()` に `source_ref` を追加 | L307〜322 |
| ES マッピングに `source_ref` フィールドを追加 | L174〜205 |
| Neo4j の Document ノードプロパティに `source_ref` を追加 | L311 |

---

## スペックレビュー後の実装手順

1. `ingest.py` の変更（`build_pdf_payload()` 分割、document_id 再設計、source_ref 追加）
2. `main.py` の変更（`IngestRequest` に `source_ref` 追加、ES マッピング更新）
3. ES / Neo4j を全削除して再取り込みし、動作確認する

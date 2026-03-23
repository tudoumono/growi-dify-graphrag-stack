# Phase 2 スペック: テキスト系入力形式の追加

SDD Phase 2 / ステアリング参照: docs/steering_ingest_redesign.md
作成日: 2026-03-24
対象ファイル: graphrag/ingest.py

---

## このスペックが解決する問題

| 問題 | 対応 Goal |
|------|-----------|
| PDF と Growi しか取り込めない | G3 拡張性 |
| Markdown 特有の処理（将来のフロントマター解析等）の追加起点がない | G3 拡張性 |

---

## 設計方針

- `build_markdown_payload()` と `build_txt_payload()` を独立した関数として追加する
- 今の段階では両者の処理はほぼ同じだが、Markdown 特有の処理は将来 `build_markdown_payload()` に追加していく
- CLI には `md` サブコマンドと `txt` サブコマンドをそれぞれ追加する
- `build_pdf_payload()` との一貫性を保つため、引数と戻り値の構造を揃える

---

## 変更 1: build_markdown_payload() の追加

### 期待する振る舞い

`.md` ファイルを読んで payload dict を返す。

```python
def build_markdown_payload(
    md_path: Path,
    input_root: Path,
    title: str | None = None,
    category: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
```

- `document_id`: `md-{相対パスのステム}` 形式
  - 例: `input/notes/design.md` → `md-notes_design`
- `url`: `""` （Web URL がないため空文字）
- `source_ref`: `INGEST_INPUT_ROOT` からの相対パス
  - 例: `notes/design.md`
- `source`: `"markdown"`
- `metadata`: フォルダ情報を自動付与

```python
metadata = {
    "source_type": "markdown",
    "filename": "design.md",
    "dir": "notes",
    "path": "notes/design.md",
}
```

### 将来の拡張ポイント（今回は実装しない）

この関数の中に以下を追加していく予定：
- フロントマター（`---` で囲まれた YAML）の解析 → `title` / `category` / `tags` の自動抽出
- `#` ヘッダー構造を使ったチャンク境界のヒント

---

## 変更 2: build_txt_payload() の追加

### 期待する振る舞い

`.txt` ファイルを読んで payload dict を返す。

```python
def build_txt_payload(
    txt_path: Path,
    input_root: Path,
    title: str | None = None,
    category: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
```

- `document_id`: `txt-{相対パスのステム}` 形式
  - 例: `input/docs/readme.txt` → `txt-docs_readme`
- `url`: `""`
- `source_ref`: `INGEST_INPUT_ROOT` からの相対パス
- `source`: `"txt"`
- `metadata`: フォルダ情報を自動付与

```python
metadata = {
    "source_type": "txt",
    "filename": "readme.txt",
    "dir": "docs",
    "path": "docs/readme.txt",
}
```

---

## 変更 3: CLI サブコマンドの追加

### md サブコマンド

```bash
python ingest.py md --file input/notes/design.md
python ingest.py md --file input/notes/design.md --category 設計書 --title "設計メモ"
```

### txt サブコマンド

```bash
python ingest.py txt --file input/docs/readme.txt
python ingest.py txt --file input/docs/readme.txt --category ドキュメント
```

### 共通オプション（pdf / md / txt 全サブコマンド）

| オプション | 説明 |
|-----------|------|
| `--file` | ファイルパス（必須） |
| `--title` | タイトル（省略時はファイル名のステム） |
| `--category` | GraphRAG 側のカテゴリ |
| `--language` | ドキュメント言語（例: ja） |

---

## 変更対象ファイルと変更箇所

### graphrag/ingest.py

| 変更内容 | 追加場所 |
|---------|---------|
| `build_markdown_payload()` 関数を追加 | `build_pdf_payload()` の直後 |
| `build_txt_payload()` 関数を追加 | `build_markdown_payload()` の直後 |
| `cmd_md()` 関数を追加 | `cmd_pdf()` の直後 |
| `cmd_txt()` 関数を追加 | `cmd_md()` の直後 |
| `md` サブコマンドの引数定義を追加 | `main()` 内の pdf サブコマンドの直後 |
| `txt` サブコマンドの引数定義を追加 | `main()` 内の md サブコマンドの直後 |
| `main()` の分岐に `md` / `txt` を追加 | `main()` 末尾の if-elif ブロック |

---

## main.py への変更

なし。Phase 1 で追加した `source_ref` フィールドと `source` フィールドの組み合わせで
`"markdown"` / `"txt"` をそのまま受け取れる。

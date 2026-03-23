# 前処理パイプライン（tmp/files）と GraphRAG の連携メモ

作成日: 2026-03-21

---

## 前処理パイプラインとは

`tmp/files` 配下にある既存ツール。
Word / Excel / PDF / RTF / BAGLES 等の各種ドキュメントを Markdown に変換して Dify UI から手動登録するために使っていたもの。

```
各種ドキュメント（.doc / .docx / .xls / .xlsx / .pdf / .rtf 等）
  ↓ Phase1: コピー & ファイル分類
  ↓ Phase2: フォーマット正規化（.doc→.docx, .xls→.xlsx 等）
  ↓ Phase3: トークン推定 & 物理分割
  ↓ Phase4: 構造抽出 / Markdown化
  ↓ Phase5: 品質判定 & 構造化
  ↓ Phase6: Dify 投入向け整形 & 出力（.md）
```

---

## GraphRAG への再利用可否

**Phase6 出力（Markdown）はそのまま GraphRAG に流せる。**

GraphRAG API の `/ingest` は `text` フィールドにテキストを渡すだけの汎用 API のため、
前処理パイプラインが出力した Markdown をそのまま `text` に渡せばよい。
前処理パイプライン側は一切変更不要。

```
【現状】
前処理パイプライン → Markdown → Dify UI（Weaviate）

【GraphRAG 追加後】
前処理パイプライン → Markdown → GraphRAG API /ingest（ES + Neo4j）
                             ↑ ここを繋ぐスクリプトが未実装
```

---

## 中間ファイル（02_extracted JSON）の活用検討

前処理パイプラインは `intermediate/` 配下に段階的な中間ファイルを生成している。

| フォルダ | 内容 |
|--|--|
| `01_normalized/` | フォーマット正規化済みファイル（.docx / .xlsx 等） |
| `02_extracted/` | 構造化 JSON（要素タイプ・見出しレベル・表の行列情報を保持） |
| `03_transformed/` | Markdown 変換済みテキスト |
| `04_review/` | LLM 解釈結果レビュー用成果物 |

### 02_extracted JSON の構造（例）

```json
{
  "metadata": {
    "source_path": "word/large_document.docx",
    "doc_role_guess": "spec_body"
  },
  "document": {
    "elements": [
      { "type": "heading",   "content": { "level": 1, "text": "仕様書タイトル" } },
      { "type": "heading",   "content": { "level": 2, "text": "第1章 機能1" } },
      { "type": "paragraph", "content": { "text": "..." } },
      { "type": "table",     "content": { "rows": [ [{"text":"No","is_header":true}, ...] ] } }
    ]
  }
}
```

### 02_extracted を使うメリット

- 見出しレベルが明示的 → **見出し単位でセマンティックチャンキングできる**（GraphRAG の品質向上に有効）
- 表の行列構造が保持されている → より正確なテキスト変換が可能
- `doc_role_guess`（`spec_body` / `data_sheet` 等）→ GraphRAG の `category` フィールドに流用できる

### 02_extracted をそのまま渡せない理由

GraphRAG API の `/ingest` は `text`（文字列）しか受け取れない。
JSON 構造のまま渡すことはできず、渡す前にテキスト化が必要。

---

## 推奨アプローチ

| フェーズ | 使うファイル | 理由 |
|--|--|--|
| PoC 段階 | `03_transformed` の Markdown | すぐ使える・変換不要 |
| 品質改善段階 | `02_extracted` JSON を読んでセマンティックチャンキング | 見出し単位で切ることで GraphRAG のエンティティ抽出精度が上がる |

### PoC での実装イメージ

```
03_transformed/*.md を読む
  ↓ ファイル名から document_id を生成
  ↓ metadata.doc_role_guess を category に使いたい場合は
    02_extracted/*.json から metadata だけ読む
  ↓ GraphRAG API /ingest を呼ぶ
```

### 将来的な改善イメージ

```
02_extracted/*.json を読む
  ↓ elements を走査して heading レベルで区切る（セマンティックチャンキング）
  ↓ 各チャンクを /ingest に投入
  ↓ doc_role_guess を category にマッピング
```

---

## 未実装タスク

- [ ] `03_transformed` の Markdown を読んで GraphRAG `/ingest` に送るブリッジスクリプトの作成
- [ ] `doc_role_guess` → `category` のマッピング定義
- [ ] `02_extracted` JSON を使ったセマンティックチャンキングの検討（品質改善フェーズ）

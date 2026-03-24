# ingest 改修ステアリング（SDD Phase 0）

作成日: 2026-03-23
対象ファイル: graphrag/ingest.py, graphrag/main.py

---

## 1. 設計目標（Design Goals）

改修全体を通して、以下の5つを判断軸にする。
個別の改修候補を評価するときは「どのGoalを満たすか」で優先度を決める。

| # | 目標 | 意味 |
|---|------|------|
| G1 | **一意性（Uniqueness）** | document_id が同名ファイル・異なるパスで衝突しない |
| G2 | **追跡可能性（Traceability）** | どのファイル・フォルダから来たデータか、後から特定できる |
| G3 | **拡張性（Extensibility）** | 新しい入力形式（MD, docx, Notion 等）を最小の変更で追加できる |
| G4 | **効率性（Efficiency）** | 内容が変わっていない文書を再処理しない仕組みを持てる |
| G5 | **意味の一貫性（Semantic Clarity）** | `url`/`document_id`/`metadata` の役割が設計として統一されている |

---

## 2. 現状の問題マップ

コードを読んで確認した「今の問題」と、それがどのGoal違反かを整理する。

### 問題A: document_id の衝突 → G1 違反

```python
# ingest.py:174 現在のコード
document_id = f"pdf-{pdf_path.stem}"
```

`input/contracts/nda/sample.pdf` と `input/hr/sample.pdf` は両方とも
`document_id = "pdf-sample"` になる。後から取り込んだ方が前のデータを丸ごと上書きする。

### 問題B: url フィールドの意味不統一 → G5 違反

```python
# ingest.py:181 現在のコード（PDF の場合）
"url": pdf_path.name,   # "sample.pdf" というファイル名だけ

# ingest.py:57 現在のコード（Growi の場合）
"url": f"{growi_url}/{page_path}",   # "http://..." という Web URL
```

同じ `url` フィールドに「Web URL」と「ファイル名」が混在している。
意味が統一されていないと、検索結果の引用リンクが正しく生成できない。

### 問題C: フォルダ情報の消失 → G2 違反

取り込み後、「このデータが `input/contracts/nda/` 由来」という情報がどこにも残らない。
後から「契約書フォルダ以下だけを検索」することができない。

### 問題D: 拡張しにくい構造 → G3 違反

現在は `cmd_pdf()` の中に「ファイル読み込み → payload 組み立て → 送信」が混在している。
Markdown や HTML を追加するには、似たコードをほぼコピーするしかない。

### 問題E: 毎回フルで再処理される → G4 違反

内容が変わっていない PDF でも、実行するたびに LLM でエンティティ抽出が走る。
API コストと処理時間の無駄が発生する。

---

## 3. 改修候補の整理と Goal 対応表

提示された改修候補一覧を Goal に紐づけてまとめる。

| 改修候補 | 対応 Goal | 優先度 |
|---------|-----------|--------|
| document_id の再設計 | G1 | 高（他の改修の前提） |
| url を input/ 以降の相対パスで保持 | G5, G2 | 高（document_id と連動） |
| url の意味を整理（Web URL と参照先を分ける） | G5 | 高（document_id と連動） |
| metadata にフォルダ構造を保持 | G2 | 高（パス設計が決まれば自然に実装できる） |
| ingest.py を build_*_payload() に分割 | G3 | 中（Phase 1 後に着手） |
| PDF 以外の入力形式対応（MD, txt） | G3 | 中（分割設計が終わってから） |
| directory 一括取り込み | G3, G4 | 中（分割設計が終わってから） |
| content_hash で未変更スキップ | G4 | 中（document_id 設計が確定してから） |
| 既存文書の重複判定改善 | G4 | 中（hash 設計と連動） |
| 前処理パイプラインとの接続設計 | G3 | 低（全体が整ってから検討） |

---

## 4. 改修の依存関係

改修には「先にやらないと後が決まらない」順序がある。

```
[Phase 1 済] データモデルの基盤を固める
  A: document_id の再設計（パスベースの一意ID）
  B: url / source_ref フィールドの意味整理
  C: metadata にフォルダ構造（path, dir, filename）を自動付与

[Phase 2 済] 拡張できる構造にする
  D: ingest.py を build_*_payload() に分割
  E: Markdown / txt 対応を追加

[Phase 3 済] 運用品質を上げる
  F: content_hash による未変更スキップ
  G: directory 一括取り込みサブコマンド
  H: カテゴリ自動推定（親ディレクトリパス全体）
  I: ES の path_hierarchy tokenizer で階層検索を高速化

[Phase 4] ingest.py 廃止・サーバー側取り込みへ移行
  J: main.py に /ingest-dir エンドポイントを追加
  K: ファイル監視（watchdog）や定期実行（cron）への対応
```

---

## 5. フェーズ別の改修スコープ

### Phase 1 済: データモデルの基盤を固める

| 対象 | 変更ファイル | ゴール |
|------|------------|--------|
| document_id をパスベースで再設計 | `ingest.py` | 同名ファイルの衝突をなくす |
| url と source_ref を分離 | `ingest.py`, `main.py` | フィールドの意味を統一する |
| metadata にパス情報を自動付与 | `ingest.py` | フォルダ単位の検索を将来可能にする |

Phase 1 が完了すると、「何が、どこから来たか」がデータから追跡できる状態になる。

### Phase 2 済: 拡張できる構造にする

| 対象 | 変更ファイル | ゴール |
|------|------------|--------|
| build_*_payload() への分割 | `ingest.py` | 入力形式を追加するコストを下げる |
| Markdown / txt 対応を追加 | `ingest.py` | 実際に使える入力形式を増やす |

### Phase 3 済: 運用品質を上げる

| 対象 | 変更ファイル | ゴール |
|------|------------|--------|
| content_hash スキップ | `ingest.py`, `main.py` | LLM 再処理コストを削減する |
| directory 一括取り込み | `ingest.py` | 手動実行の手間をなくす |
| カテゴリ自動推定 | `ingest.py` | 親ディレクトリパスをカテゴリに自動設定する |
| path_hierarchy tokenizer | `main.py` | 上位階層カテゴリでの高速な階層検索を実現する |

### Phase 4: ingest.py 廃止・サーバー側取り込みへ移行

**背景と判断:**
`ingest.py` は「ファイルを読んで POST /ingest を叩くクライアント」であり、
`main.py` の `/ingest` 実装そのものではない。
将来の定期実行・ファイル監視への自動化を考えると、コンテナ内で直接処理する方が自然。
そのため `ingest.py` は一時的なツールと位置づけ、Phase 4 で廃止する。

**やること:**

| 対象 | 変更内容 |
|------|---------|
| `main.py` | `/ingest-dir` エンドポイントを追加。コンテナ内の `input/` を直接スキャン・取り込む |
| `main.py` | ファイル監視（watchdog 等）や定期実行（cron）への対応 |
| `ingest.py` | 廃止 |
| `docker-compose.yml` | `input/` マウントは Phase 3 で追加済み。定期実行設定をここに追加 |

**現状（Phase 3 完了時点）の運用:**
```bash
docker exec graphrag-api python ingest.py input-dir
```

**Phase 4 完了後の運用（予定）:**
```bash
# 手動トリガー
curl -X POST http://localhost:8080/ingest-dir

# 将来: cron やファイル監視で自動実行
```

---

## 6. Phase 1 着手前の問題と決定事項

### Q1: input_root の基準はどこか → 決定済み

**決定**: 環境変数 `INGEST_INPUT_ROOT` で固定する。

`ingest.py` は起動時に `INGEST_INPUT_ROOT` を読み、
document_id はその配下からの相対パスで生成する。

### Q2: 既存データの後方互換 → 決定済み

**決定**: 考慮しない。改修完了後に ES / Neo4j のデータを全削除して再取り込みする。

### Q3: ES マッピング変更のタイミング → 決定済み

**決定**: 考慮しない。全削除・再取り込みのタイミングで新マッピングが自動作成される。

---

## 7. 次のステップ

- Phase 1〜3 実装・動作確認 完了（2026-03-24）
- 現在の運用: `docker exec graphrag-api python ingest.py input-dir`
- Phase 4 は実際のドキュメントを運用しながら安定を確認してから着手する

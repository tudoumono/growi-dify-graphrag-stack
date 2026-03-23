# アーキテクチャ設計ドキュメント

## 背景・目的

Dify の標準ナレッジ機能（チャンキング + Weaviate）だけでは検索精度に限界があるため、
GraphRAG（Elasticsearch によるベクトル検索 + Neo4j によるグラフ探索）を導入して精度を向上させる。

また、社内ナレッジの蓄積基盤として Growi を活用し、
Growi・Dify・Langfuse の3サービスが連携する継続改善サイクルを実現する。

---

## サービスの立ち位置

### Growi - ナレッジ蓄積基盤
- メンバーが社内ドキュメント・ナレッジを書き溜める場所
- Growi API 経由で記事を取得し、GraphRAG に取り込む
- Growi 自身も内部に Elasticsearch を持つが、これは Growi の全文検索専用であり GraphRAG とは完全に無関係

### Dify - LLMアプリ基盤・ユーザー接点
- チャットワークフローを構築・公開するプラットフォーム
- **標準ナレッジ機能（Weaviate）はそのまま変更しない**
- Dify 自体の設定・ファイルは一切変更しない

#### Dify 標準ナレッジ（Weaviate）と GraphRAG の使い分け

> **重要**: この2つは「どちらを使うか」を **ワークフローの組み方で選択** する。

| 方式 | 使い方 | 特徴 |
|--|--|--|
| **Dify 標準ナレッジ（Weaviate）** | ワークフローに「Knowledge Retrieval ノード」を追加 | Dify が自動で検索してくれる。設定が簡単 |
| **GraphRAG（ES + Neo4j）** | ワークフローに「HTTP Request ノード」を追加し、GraphRAG API `/search` を呼ぶ | 手動設定が必要。ハイブリッド検索で精度が高い |

- Dify の設定ファイルは一切変更していないため、**Dify 標準ナレッジはいつでも使える状態のまま**
- GraphRAG を使いたい場合は、Dify が自動で見にいくことはない。ワークフロー内に **HTTP Request ノードを手動で設定し、GraphRAG API `/search` を明示的に呼ぶ必要がある**
- 両方を並列で呼ぶワークフローも構築可能

### GraphRAG - ハイブリッド検索基盤（独立したプロジェクト）
- **Dify・Growi・Langfuse とは完全に独立した** Docker Compose プロジェクト
- Elasticsearch（ベクトル検索）+ Neo4j（グラフ探索）を組み合わせたハイブリッド検索
- 独自の Elasticsearch・Kibana・Neo4j・GraphRAG API コンテナを持つ
- ingest.py が ES と Neo4j を統一管理することで chunk_id の一致を保証する
- FastAPI で `/ingest`・`/search` エンドポイントを提供し、Dify ワークフローから呼び出す

### Elasticsearch - ベクトル検索基盤（GraphRAGプロジェクト内）
- GraphRAG プロジェクトの docker-compose.yml で独自に定義したコンテナ
- Dify の Elasticsearch（Weaviate に付随するもの）とは**完全に別物・別コンテナ**
- Growi の Elasticsearch（全文検索用）とも**完全に別物・別コンテナ**
- インデックス名: `graphrag_chunks`
- **Kibana**（同じくGraphRAGプロジェクト内）で中身を確認する

### Neo4j - グラフ検索基盤（GraphRAGプロジェクト内）
- GraphRAG プロジェクトの docker-compose.yml で独自に定義したコンテナ
- Elasticsearch に格納したドキュメントと同じ chunk_id でエンティティ・関係性を格納
- ベクトル検索では拾えない「概念同士のつながり」を補完する
- **Neo4j Browser** で中身・グラフ構造を確認する

### Langfuse - トレース・評価基盤
- Dify の全ワークフロー実行ログを記録・可視化する
- 回答品質の評価・モニタリングに使用する
- チャットログから暗黙知を抽出する起点となる

---

## 動線（ユーザー・管理者フロー）

### 動線①: ユーザーが Growi に記事を書く

```
ユーザー
  └─→ Growi UI にアクセス
        └─→ 記事を書いて保存
```

- ユーザー側に特別な操作は不要
- Growi が全て処理するため追加実装なし

---

### 動線②: ユーザーが Dify でチャット質問する

```
ユーザーが質問を入力
  ↓
質問テキストの受け取り
  ↓（並列実行）
  ├─→ 【GraphRAG 検索】
  │     クエリをベクトル化
  │     Elasticsearch でベクトル検索
  │     Neo4j でエンティティ・グラフ探索
  │     結果をマージ（merged_context + citations）
  │
  └─→ 【Growi 検索】
        Growi API にキーワードを渡して記事検索
        関連記事の本文を取得
  ↓
両方の結果を LLM へのプロンプトに組み込む
  ↓
LLM が回答生成
  ↓
回答 + 引用元（citations）をユーザーに返す
```

---

### 動線③: 管理者がナレッジ（ドキュメント）を追加する

```
管理者がファイルを用意（PDF / Markdown / txt / その他）
  ↓ ファイル種別の判定
  ↓ テキスト抽出
  │   PDF       → ライブラリ（pdfminer 等）でテキスト抽出
  │   Markdown  → そのままテキストとして利用
  │   txt       → そのままテキストとして利用
  ↓ document_id 生成（ファイル名やパスから一意IDを作成）
  ↓ GraphRAG API /ingest 呼び出し
      ↓ チャンキング（テキストを一定サイズの断片に分割）
      ↓ ベクトル化（Bedrock Titan Embed / Ollama でベクトル生成）
      ↓ Elasticsearch に格納（chunk_id + ベクトル + テキスト）
      ↓ エンティティ抽出（LLM が名前・組織・概念等を抽出）
      ↓ リレーション抽出（LLM がエンティティ間の関係を抽出）
      ↓ Neo4j に格納（Document → Chunk → Entity グラフ構造）
  ↓ 格納完了・結果出力（chunk数・chunk_idリスト）
```

- Dify のナレッジ UI は使わない（chunk_id の一貫性を保つため）
- 管理者が `ingest.py` CLI を直接操作する
- 将来的には管理画面 or Dify ワークフローから操作できるようにする（動線④）

---

### 動線④: 管理者が Dify ワークフロー画面からナレッジ登録をトリガーする（将来実装）

```
管理者が Dify のワークフロー画面を開く
  ↓ ナレッジ登録ワークフローを実行
  ↓ ファイルアップロードノード（Dify UI 上でファイルを選択・送信）
  ↓ Python コードノード
  │   ファイルのバイト列を受け取る
  │   種別判定・テキスト抽出（PDF / Markdown / txt）
  │   document_id 生成
  │   /ingest 用 JSON ペイロードを組み立て
  ↓ HTTP Request ノード → GraphRAG API /ingest 呼び出し
  ↓ （以降は動線③と同じ: チャンキング→ベクトル化→ES格納→エンティティ抽出→Neo4j格納）
  ↓ 完了ステータスを Dify UI に返す
```

- 動線③（CLI操作）を Dify の UI 上から実行できるようにしたもの
- 管理者が CLI を使わずに Dify 画面だけで完結できる
- 現時点では未実装・将来の拡張として検討

---

### 動線⑥: Growi 記事を GraphRAG に同期する（将来実装）

```
バッチ処理（定期実行 / cron 等）
  ↓ sync_state.json を読み込む（前回同期時のタイムスタンプ・page_id 一覧）
  ↓ Growi API で全記事の更新日時一覧を取得
  ↓ 差分を検出（前回同期以降に追加・更新された記事を特定）
  │   新規記事 → 取り込み対象
  │   更新記事 → 既存 chunk を削除してから再取り込み対象
  │   削除記事 → ES・Neo4j から該当 document_id を削除
  ↓ 対象記事ごとに Growi API /page?pageId=XXX で本文を取得
  ↓ GraphRAG API /ingest 呼び出し
  ↓ （動線③と同じ: チャンキング→ベクトル化→ES格納→エンティティ抽出→Neo4j格納）
  ↓ sync_state.json を更新（最終同期日時・処理済み page_id）
```

- 現時点では未実装・将来の拡張として検討
- `sync_state.json` で差分管理し、変更のない記事は再取り込みしない
- 削除記事の検出・クリーンアップも含めて整合性を保つ

---

### 動線⑦: Langfuse でチャットを分析してナレッジを更新する（将来実装）

```
Langfuse にチャットログが蓄積される
  ↓ 管理者が Langfuse のトレース一覧を確認（または定期バッチが起動）
  ↓ Langfuse API でログデータを取得
  │   （質問テキスト・回答テキスト・スコア・引用元 citations）
  ↓ LLM でログを分析
  │   「よくある質問で回答精度が低いパターン」を検出
  │   「引用元が空欄になっている質問」= ナレッジ不足の候補を抽出
  │   暗黙知・未文書化ナレッジの候補テキストを生成
  ↓ ナレッジの追加先を選択
  ├─→ 【即時追加】GraphRAG API /ingest を呼び出す
  │         → ES + Neo4j に不足ナレッジを直接追加
  └─→ 【正式文書化】Growi に新規記事を作成（手動 or Growi API）
              ↓
         動線③〜⑥ へ戻る（継続的改善サイクル）
```

---

## データフロー

### ① ナレッジ蓄積フロー（Ingest）

```
ドキュメントソース（Growi記事 / PDFファイル / Markdownなど）
  ↓ ingest.py または GraphRAG API /ingest
  ├──→ Elasticsearch: graphrag_chunks（ベクトル格納）
  └──→ Neo4j（エンティティ・関係性格納）
         ※ chunk_id が両方で完全一致
```

**ドキュメントソースはGrowiに限らない。**
`/ingest` は `document_id / title / url / text` を受け取る汎用APIのため、
どんなソースからでも取り込み可能。

**Growi の継続的同期（検討中）:**
- 方法案①: Growi の Webhook を受け取る同期サービスを別途用意
- 方法案②: 定期バッチ（cron）で Growi API をポーリングして差分を取り込む
- 方法案③: Dify ワークフローの HTTP Request ノードから `/ingest` を呼ぶ

### ② 回答生成フロー（Query）

```
ユーザー質問（Dify チャット）
  ↓
┌─── Dify チャットワークフロー ────────────────────────────────┐
│                                                              │
│  [A] Growi API 検索                                          │
│      → Growi の最新記事をリアルタイム取得                     │
│                                                              │
│  [B] HTTP Request → GraphRAG API /search                     │
│      → Elasticsearch（ベクトル検索）で意味的に近いチャンク取得 │
│      → Neo4j（グラフ探索）でエンティティ経由の関連チャンク補完 │
│      → merged_context + citations を返す                     │
│                                                              │
│  [LLM] A + B の結果をコンテキストとして回答生成               │
│      → 回答テキスト + citations（document_id / chunk_id / url）│
└──────────────────────────────────────────────────────────────┘
  ↓ 実行ログをトレース
Langfuse
```

### ③ 継続改善ループ（DevOps）

```
Langfuse（チャットログ蓄積）
  ↓ 暗黙知の抽出（LLM で分析）
  ├──→ Growi に新規記事として追加
  └──→ GraphRAG /ingest → ES + Neo4j に追加インデックス
            ↓
       ① ナレッジ蓄積フローへ戻る（継続的改善）
```

---

## GraphRAG API の役割

Neo4j・Elasticsearch はそれぞれ独自の API を持つが、
以下の処理がどちらにも属さないため、仲介役として GraphRAG API（FastAPI）を置く。

| 処理 | ES単体 | Neo4j単体 | GraphRAG API |
|--|--|--|--|
| ベクトル検索 | ✅ | ❌ | ✅（ESを呼ぶ） |
| グラフ探索 | ❌ | ✅ | ✅（Neo4jを呼ぶ） |
| 埋め込み生成（Bedrock / Ollama） | ❌ | ❌ | ✅ |
| エンティティ・関係抽出（LLM） | ❌ | ❌ | ✅ |
| ES + Neo4j の結果マージ | ❌ | ❌ | ✅ |
| chunk_id の一貫管理 | ❌ | ❌ | ✅ |

GraphRAG API により、Dify ワークフローは `/search` を1回呼ぶだけで
ハイブリッド検索結果（merged_context + citations）を受け取れる。

---

## システム構成（Docker）

**方針: 各サービスは完全に独立した Docker Compose プロジェクトとして管理する。
既存サービス（Growi・Dify・Langfuse）のファイルは一切変更しない。**

### コンテナ構成図

```
【growi プロジェクト】       【dify プロジェクト】        【graphrag プロジェクト】
docker-compose.yml           docker-compose.yaml           docker-compose.yml
（変更なし）                  （変更なし）                   （新規作成）

┌──────────────────┐        ┌───────────────────────┐    ┌─────────────────────────┐
│ growi-app  :3300 │        │ nginx    :80 / :443   │    │ graphrag-api   :8080    │
│ growi-mongo      │        │ plugin_daemon  :5003  │    │ (FastAPI)               │
│ (非公開)         │        │ dify-api (内部のみ)   │    │                         │
│ growi-es         │        │ worker   (内部のみ)   │    │ graphrag-es    :9201    │
│ (非公開)         │        │ weaviate (内部のみ)   │    │ (Elasticsearch)         │
│ (全文検索専用)   │        └───────────────────────┘    │                         │
└──────────────────┘                                     │ graphrag-kibana :5602   │
                             【langfuse プロジェクト】    │                         │
                             docker-compose.yml           │ neo4j          :7474    │
                             （変更なし）                  │                :7687    │
                            ┌───────────────────────┐    └─────────────────────────┘
                            │ langfuse-web    :3100  │
                            │ langfuse-worker :3030  │
                            │ minio           :3190  │
                            │ clickhouse (非公開)    │
                            │ redis      (非公開)    │
                            │ postgres   (非公開)    │
                            └───────────────────────┘
```

### フォルダ構成

```
mywork/DIfy_Growi_Langfuse/
  ├── ARCHITECTURE.md
  │
  ├── growi/
  │   └── docker-compose.yml        # 変更なし
  │
  ├── dify/
  │   └── docker-compose.yaml       # 変更なし（.env も含め一切変更しない）
  │
  ├── langfuse/
  │   └── docker-compose.yml        # 変更なし
  │
  └── graphrag/                     # 新規・完全独立プロジェクト
      ├── docker-compose.yml        # ES + Kibana + Neo4j + GraphRAG API を定義
      ├── Dockerfile                # GraphRAG API コンテナのビルド定義
      ├── requirements.txt          # Python 依存ライブラリ
      ├── providers.py              # Bedrock / Ollama 切り替え抽象層
      ├── main.py                   # FastAPI（/ingest・/search・/providers）
      └── ingest.py                 # ドキュメント取り込み CLI
```

### サービス一覧とポート

| サービス | URL | 用途 |
|--|--|--|
| Growi | `http://localhost:3300` | ナレッジ記事の閲覧・編集 |
| Dify | `http://localhost:80` | チャットワークフロー操作（変更なし） |
| Dify plugin_daemon | `http://localhost:5003` | Difyプラグイン管理 |
| Langfuse | `http://localhost:3100` | トレース・評価の確認 |
| Langfuse Worker | `http://localhost:3030` | Langfuseワーカー |
| Langfuse MinIO | `http://localhost:3190` | S3互換ストレージ |
| GraphRAG API | `http://localhost:8080/docs` | API 動作確認・テスト |
| GraphRAG ES | `http://localhost:9201` | GraphRAG専用Elasticsearch |
| GraphRAG Kibana | `http://localhost:5602` | GraphRAG ESの中身確認（elastic / graphrag） |
| Neo4j Browser | `http://localhost:7474` | グラフ構造の確認（neo4j / dify-graphrag） |

---

## GraphRAG の設計詳細

### chunk_id の統一管理

ES と Neo4j の両方で同じ chunk_id を使うことで、回答の出典追跡を正確にする。

```
document_id : growi-{page_id}              例) growi-12345
chunk_id    : growi-{page_id}-chunk-{n}    例) growi-12345-chunk-0
```

### Neo4j グラフスキーマ

**ノード（3種類）:**

| ノード | プロパティ | 説明 |
|--|--|--|
| `Document` | `id, title, url` | ドキュメント単位 |
| `Chunk` | `id, document_id, text, chunk_index` | チャンク分割した断片 |
| `Entity` | `canonical_name, name, type` | 抽出したエンティティ |

- `Entity.canonical_name` を MERGE キーとし、表記ゆれを同一ノードに寄せる
  （例: 「AWS」と「Amazon Web Services」を同じノードにまとめる）

**リレーション（3種類）:**

| リレーション | プロパティ | 説明 |
|--|--|--|
| `Document -[:HAS_CHUNK]-> Chunk` | なし | ページとチャンクの親子関係 |
| `Chunk -[:MENTIONS]-> Entity` | なし | チャンクが言及するエンティティ |
| `Entity -[:RELATED_TO]-> Entity` | `relation_type`（動詞句） | エンティティ間の関係 |

### ナレッジの分け方（Dify標準 vs GraphRAG）

Dify 標準ナレッジでは「ナレッジベース」単位でDBを物理的に分けて管理できる。
GraphRAG では同じ設計をすると**グラフの最大の強みが失われる**ため、設計が異なる。

**GraphRAGでDBを分けてはいけない理由:**

```
【NG: DBを物理分割した場合】
ES index-A（人事規程）+ Neo4j graph-A
ES index-B（技術文書）+ Neo4j graph-B

→「就業規則」（人事）が「セキュリティポリシー」（技術）を参照していても
  グラフが分断されているためエンティティのつながりを辿れない
```

**GraphRAGの正しい設計: 1つのDBにカテゴリ属性を持たせる**

```
ES index: graphrag_chunks（1つ）
  └── { text, vector, category: "人事規程", document_id, ... }
  └── { text, vector, category: "技術文書", document_id, ... }

Neo4j graph（1つ）
  └── Document { id, category: "人事規程" }
  └── Document { id, category: "技術文書" }
  └── Entity { name: "セキュリティポリシー" }
        ↑ 両カテゴリの Chunk から MENTIONS で繋がれる（関係性が保たれる）
```

検索時に `category` フィルターを渡すことでスコープを絞れる：

```
/search?query="セキュリティ"&category="技術文書"  → 技術文書だけ検索
/search?query="セキュリティ"                      → 全体検索（関係性を跨いで辿れる）
```

| | Dify標準（Weaviate） | GraphRAG（ES + Neo4j） |
|--|--|--|
| 分け方 | ナレッジベース単位でDBを物理分割 | 1つのDBにカテゴリ属性を持たせる |
| 理由 | 独立しているので分割が自然 | グラフの関係性を跨いで辿るのが強みのため分断しない |
| スコープ絞り | ワークフローでナレッジを選択 | 検索時に `category` フィルターを指定 |

---

### /search レスポンス形式

```json
{
  "es_hits":        [...],
  "graph_hits":     [...],
  "merged_context": "...",
  "citations": [
    { "document_id": "...", "chunk_id": "...", "url": "...", "score": 0.92 }
  ]
}
```

---

## LLMプロバイダー

埋め込み（Embed）とエンティティ抽出（LLM）は環境変数で切り替え可能。

| フェーズ | Embed | LLM | 次元数 |
|--|--|--|--|
| PoC（現在） | Amazon Bedrock Titan Embed v2 | Amazon Bedrock Claude Haiku | 1024 |
| 本番移行後 | Ollama nomic-embed-text | Ollama llama3 | 768 |

**切り替え方法**: `graphrag/docker-compose.yml` の環境変数を変更するだけ。

> **注意**: Embed プロバイダーを切り替えると次元数が変わるため、
> Elasticsearch インデックスの再作成とドキュメントの再取り込みが必要。

---

## VectorDB から GraphRAG に移行する際に考え直すこと

Dify 標準ナレッジ（VectorDB）の感覚のまま GraphRAG を設計すると誤った判断をしやすい。
以下に主要な考え方の違いを整理する。

### ① 検索の仕組みが「類似」から「類似＋繋がり」に変わる

| | VectorDB（Weaviate） | GraphRAG（ES + Neo4j） |
|--|--|--|
| 検索の基本 | 意味的に似ているチャンクを返す | 似ているチャンク＋エンティティで繋がるチャンクを返す |
| 得意な質問 | 「〇〇とは何か」 | 「〇〇と△△の関係は？」「〇〇に関連するすべての情報」 |
| 苦手な質問 | 関係性を問うもの（直接ヒットしないと取れない） | 最新情報（Growi直接検索が向いている） |

### ① ドキュメントは1ファイルずつ投入すればよい

1ファイルずつ `/ingest` を呼ぶだけでよい。バッチ処理や複数ファイル同時投入は不要。
グラフの関係性は「複数ファイルをまとめて渡す」ことで生まれるのではなく、
**ファイルを1つずつ追加するたびに自動的に育つ**。

```
1回目: 人事規程.md を ingest → エンティティ「セキュリティポリシー」が生まれる
2回目: 技術標準.md を ingest → 同じエンティティに自動的に繋がる
  ↑ バッチ処理は不要。投入順序も関係ない
```

### ② ナレッジの品質がLLMの抽出精度に依存する・チャンキング戦略

VectorDB はテキストを入れればベクトル化できる。精度はEmbedモデルだけに依存する。
GraphRAG は **LLMがエンティティ・関係性をどれだけ正確に抽出できるか** が品質に直結する。
そのため **チャンキング戦略はVectorDBより重要度が高く、失敗するとグラフ構造自体が壊れる**。

**チャンキングの概念はVectorDBと同じ**（区切り位置・サイズ・オーバーラップすべて必要）だが、パラメータの目安が変わる：

| 項目 | VectorDB | GraphRAG |
|--|--|--|
| チャンクサイズの目安 | 256〜512トークン | 512〜1024トークン（やや大きめ） |
| 理由 | ユーザーの質問と一致しやすいサイズ | LLMがエンティティ・関係性を抽出するのに十分な文脈が必要 |
| オーバーラップ | 文脈の欠落防止 | 同上＋エンティティ間の関係が文をまたぐ場合に対応 |
| 失敗時の影響 | 検索精度が下がる | 検索精度＋**グラフ構造そのものが壊れる** |

チャンキングが悪いと何が起きるか：

```
【チャンクが小さすぎる場合】
「情報システム部が         」← chunk1
「セキュリティポリシーを管理する」← chunk2
→ LLMがchunk1だけ見ても関係性を抽出できない
→ グラフに「情報システム部→セキュリティポリシー」の繋がりが生まれない

【チャンクが大きすぎる場合】
→ 1チャンクから大量のエンティティが抽出される
→ 関係性が薄くノイズが増える
→ LLMのトークン上限を超えるリスクもある
```

### ③ エンティティの表記ゆれを意識する必要がある

VectorDB はベクトルが近ければ類似とみなすため表記ゆれを自然に吸収する。
GraphRAG は **エンティティをノードとして MERGE するため、同一概念を同一ノードにまとめる設計が必要**。

- 例: 「AWS」と「Amazon Web Services」を別ノードにしてしまうと関係性が分断される
- `canonical_name`（正規名）を MERGE キーとして使うことで表記ゆれを同一ノードに寄せる設計にしている

### ④ 削除・更新の処理がVectorDBより複雑になる

| 操作 | VectorDB | GraphRAG |
|--|--|--|
| ドキュメント削除 | 該当ベクトルを削除するだけ | ES＋Neo4jの両方から削除。エンティティが他ドキュメントからも参照されている場合は削除しない |
| ドキュメント更新 | 古いベクトルを差し替え | 古いチャンクを削除して再ingest。エンティティの参照関係を再構築 |

**同じドキュメントを2回 ingest すると重複が生まれる（現状は未対処）:**

- ES: chunk_idが同じなら上書きされるが、チャンク数が減った場合に古いチャンクが残る
- Neo4j: Chunk・MENTIONSが重複して生成される

**正しい更新手順:**

```
ドキュメント更新時:
  ↓ 1. ES から該当 document_id の全チャンクを削除
  ↓ 2. Neo4j から該当 document_id の Chunk ノード・MENTIONS リレーションを削除
  │      ※ Entity ノード自体は削除しない（他ドキュメントが参照している可能性）
  ↓ 3. どこからも MENTIONS されなくなった孤立 Entity ノードのみ削除
  ↓ 4. 新しい内容で /ingest を呼ぶ（再取り込み）
```

現在の `/ingest` はこの削除処理を自動で行わない。将来的には `/ingest` が `document_id` の既存チャンクを自動削除してから再登録する upsert 動作に対応することが望ましい。

### ⑤ グラフは「育てる」もの。最初は効果が薄い

VectorDB はドキュメントを1件入れた時点で検索に使える。
GraphRAG は **ドキュメントが増えるほどエンティティ間の繋がりが増えて検索精度が上がる**。

- 数件しかない状態ではグラフ探索の恩恵が少ない
- ドキュメントが蓄積されるほど「VectorDBでは取れなかった関連情報」が取れるようになる
- PoCでは効果が見えにくく、本番データが増えると真価を発揮する

---

## GraphRAG 設計の肝：「何をどう定義するか」

VectorDB は「テキストを入れればある程度動く」が、GraphRAG は **「何をどう定義するか」がナレッジ品質に直結する**。
設計は以下の2つのレイヤーで行われる。

### レイヤー1: /ingest に送るJSON（人間が設計する）

どんなメタデータを持たせるかの設計。ここの設計次第で「後から絞り込める情報」が決まる。

```json
{
  "document_id": "...",
  "category":    "技術文書",   ← 検索スコープを絞れるか
  "source":      "growi",      ← どこから来たかを追跡できるか
  "text":        "..."         ← 何をLLMに渡すか
}
```

### レイヤー2: LLMがtextから抽出するエンティティ・関係性（プロンプトが設計する）

`text` の中から何を抽出するかは **LLMへのプロンプト設計** で決まる。
同じドキュメントでも、プロンプト次第でグラフの形が変わる。

```
プロンプトA: 「固有名詞と組織名を抽出して」
  → 「AWS」「情報システム部」などが抽出される

プロンプトB: 「手順・条件・制約も関係性として抽出して」
  → 「承認が必要」「例外あり」のような業務ルールも抽出される
```

### 設計の責任分担

```
【人間が設計する部分】
  JSON のフィールド設計             → 検索・フィルタリングの粒度を決める
  Neo4j のノード・リレーション設計   → グラフ構造の器を決める
  チャンキング戦略                   → LLMが見る文脈の範囲を決める

【プロンプトが決める部分】
  エンティティ抽出の粒度             → どんな概念をノードにするか
  リレーションの粒度                 → どんな関係性をエッジにするか
```

VectorDB との最大の違いはここにある。VectorDB はテキストを入れれば自動的にベクトル化されるが、
GraphRAG は設計者が「何を構造化するか」を意識的に決めることで初めて真価を発揮する。

---

## PoC 成功条件

| # | 条件 |
|--|--|
| 1 | Growi の1ページを ES + Neo4j に取り込める |
| 2 | Dify ワークフローから Growi API・GraphRAG API を呼び出せる |
| 3 | LLM が結果をまとめて回答を生成できる |
| 4 | 回答根拠として `document_id` / `chunk_id` / `url` を返せる |
| 5 | ベクトル検索単体では出てこない関連情報がグラフ経由で取得できる（定性確認） |

---

## 段階的な発展計画

### PoC（現在）
- Growi → GraphRAG（ES + Neo4j）の取り込み動作確認
- Dify ワークフローから GraphRAG を呼んで回答生成
- Langfuse でトレース確認

### PoC 後の改善
- チャンキング戦略の最適化（LlamaIndex のセマンティックチャンキング等）
- Growi 更新の自動同期パイプライン構築
- 一般ユーザー向けのチャンキング設定UI（自前実装 or 既存ライブラリ活用）

### 将来
- Langfuse のチャットログから暗黙知を抽出して Growi・ES・Neo4j に自動フィードバック
- Neo4j 公式 GraphRAG ライブラリ（`neo4j-graphrag-python`）への移行検討

---

## 今後の検討事項

- [ ] `ingest.py` の汎用化（Growi 以外のドキュメントソースにも対応）
- [ ] Growi 記事の継続的同期パイプライン設計・実装
- [ ] チャンキング設定UI の設計（自前 or LlamaIndex / Haystack 活用）
- [ ] Langfuse の暗黙知抽出パイプライン設計
- [ ] Ollama 移行時のインデックス再作成手順の整備

---

## よくある疑問（Q&A）

**Q. GraphRAG の Elasticsearch は Dify の Elasticsearch と同じものですか？**
A. 別物です。Dify は Weaviate を Vector DB として使っており変更していません。
GraphRAG は `graphrag/docker-compose.yml` で独自に立てた Elasticsearch コンテナを使います。
Growi が内部に持つ Elasticsearch（全文検索用）とも完全に別コンテナです。

---

**Q. Kibana はどの Elasticsearch を見ていますか？**
A. GraphRAG プロジェクト内の Kibana（Port: 5602）は GraphRAG 専用の Elasticsearch（Port: 9201）だけを見ています。Dify や Growi の Elasticsearch は見ていません。

---

**Q. Dify のナレッジ UI からドキュメントを GraphRAG に取り込めますか？**
A. できません。Dify のナレッジ UI は Dify 内部の Weaviate にデータを格納します。
GraphRAG への取り込みは `ingest.py` CLI または GraphRAG API `/ingest` エンドポイントを使います。

---

**Q. Dify のナレッジ UI でチャンキングを設定できますか？**
A. Dify のナレッジ UI のチャンキング設定は Weaviate 向けのものであり、GraphRAG には適用されません。
GraphRAG のチャンキング設定は `graphrag/docker-compose.yml` の環境変数で管理します。
将来的にはチャンキング設定 UI の追加を検討しています。

---

**Q. GraphRAG API はなぜ必要ですか？ES や Neo4j に直接アクセスすればよいのでは？**
A. ES・Neo4j はそれぞれ独自 API を持ちますが、以下の処理はどちらにもできません。
- 埋め込みベクトルの生成（Bedrock / Ollama 呼び出し）
- エンティティ・関係性の抽出（LLM 呼び出し）
- ES と Neo4j の結果マージ・citations 生成
GraphRAG API はこれらを担当し、Dify が1回の HTTP Request で結果を受け取れるようにします。

---

**Q. Dify のファイルは変更していますか？**
A. 変更していません。Growi・Dify・Langfuse の docker-compose ファイル・設定ファイルは一切変更しません。
GraphRAG は `graphrag/` フォルダ内に完全独立した Docker Compose プロジェクトとして存在します。

---

**Q. Dify 標準ナレッジ（Weaviate）と GraphRAG は共存できますか？どう使い分けますか？**
A. 共存できます。Dify の設定ファイルは一切変更していないため、Dify 標準ナレッジはいつでも使える状態のままです。
使い分けはワークフローの組み方で決まります。

- **Dify 標準ナレッジを使う場合**: ワークフローに「Knowledge Retrieval ノード」を追加するだけ。Dify が自動的に Weaviate を検索する。
- **GraphRAG を使う場合**: ワークフローに「HTTP Request ノード」を手動で追加し、GraphRAG API `/search` を明示的に呼ぶ必要がある。Dify が自動で GraphRAG を見にいくことはない。

両方を並列で呼ぶワークフローも構築可能です。

---

**Q. Dify のようにナレッジを複数に分けて管理することはできますか？**
A. GraphRAG では「DBを物理的に分ける」設計は推奨しません。
グラフの最大の強みは「エンティティのつながりを跨いで辿れること」であり、DBを分断するとその関係性が失われます。

正しい設計は「1つのDBにカテゴリ属性を持たせ、検索時にフィルタリングする」です。
- ES の各 chunk に `category` フィールドを持たせる
- Neo4j の各ノードにも `category` を持たせる
- `/search` 呼び出し時に `category` フィルターを指定すれば特定カテゴリだけ検索できる
- フィルターなしで呼べば全体を横断してグラフ関係性も辿れる

---

**Q. ドキュメント間の関係性はどうやって判断・格納しているのですか？**
A. 関係性は「人間が定義する」のではなく、**LLMがテキストを読んで自動抽出**します。

`/ingest` を呼ぶと内部でこういう処理が走ります：

```
Markdownテキスト（1チャンク分）
  ↓ LLMへのプロンプト:
    「このテキストから登場する固有名詞・概念・組織名（エンティティ）と
     それらの間の関係性を抽出してください」
  ↓ LLMの出力例:
    エンティティ: 「セキュリティポリシー」「AWS」「情報システム部」
    関係性: 「情報システム部 が セキュリティポリシー を 管理する」
            「セキュリティポリシー は AWS の 利用規定 を 含む」
  ↓ Neo4j に格納
```

ドキュメント間の繋がりは **共通エンティティを経由して自動的に発生**します：

```
【人事規程.md を ingest】→ エンティティ「セキュリティポリシー」を Neo4j に格納
【技術標準.md を ingest】→ 同じ「セキュリティポリシー」を抽出
                         → Neo4j に同一ノードが存在するので MERGE（同じノードに紐付く）

結果:
人事規程チャンク ─MENTIONS→ セキュリティポリシー ←MENTIONS─ 技術標準チャンク
                                    ↑
                       2つのドキュメントがこのエンティティを経由して繋がった
```

検索時はこの繋がりを辿ることで、ベクトル検索では直接ヒットしなかったドキュメントも関連情報として取得できます。

| 疑問 | 答え |
|--|--|
| 関係性は誰が定義する？ | LLMがテキストを読んで自動抽出 |
| ドキュメント間の繋がりは？ | 共通のエンティティノードを経由して自動的に繋がる |
| 明示的なリンク設定は必要？ | 不要。取り込むだけでグラフが自動的に育つ |

---

**Q. GraphRAG API に送る /ingest の JSON 形式は固定ですか？**
A. 固定ではありません。私たちが自分で設計した API なので自由に拡張できます。

現在の4フィールドは「最低限これだけあれば動く」というミニマム設計です：
```json
{
  "document_id": "...",
  "title":       "...",
  "url":         "...",
  "text":        "..."
}
```

必要に応じて以下のようなフィールドを追加できます：

| フィールド | 用途 |
|--|--|
| `category` | 検索スコープの絞り込み（カテゴリフィルター用） |
| `tags` | 複数タグで柔軟に分類 |
| `source` | どこから来たか（`growi` / `file` / `dify` 等） |
| `language` | 言語（日本語/英語 等） |
| `created_at` / `updated_at` | 作成日・更新日（鮮度フィルターに使える） |
| `metadata` | 任意のキーバリュー（汎用的な拡張用） |

追加したフィールドは ES のメタデータとして格納され、検索時のフィルター条件として使えます。

---

**Q. ES や Neo4j に格納するデータ構造はユーザーが自由に決められますか？**
A. はい、完全に自由に決められます。

Dify 標準ナレッジ（Weaviate）はDifyが内部でスキーマを管理しているためユーザーは変更できません。
GraphRAG は自前で API を立てているため、データ構造をすべて自分で設計できます。

```
【Dify標準ナレッジ】
  スキーマはDifyが管理 → ユーザーは変更できない

【GraphRAG】
  ES のインデックス設計          → 自分で決める
  Neo4j のノード・リレーション設計 → 自分で決める
  /ingest の JSON 形式           → 自分で決める
  /search のレスポンス形式        → 自分で決める
```

将来「部署」や「プロジェクト」といったノードを Neo4j に追加したくなれば `main.py` を修正するだけで対応できます。
GraphRAG API を自前で立てている理由の一つは、**ESとNeo4jへの直接アクセスではなくAPIを一枚かませることで、データ構造の変更を1箇所に集約できる**点にあります。

---

**Q. Dify 標準ナレッジ（Weaviate）と GraphRAG を両方同時に使う意味はありますか？**
A. ほぼありません。GraphRAG の Elasticsearch はDify標準（Weaviate）と同じベクトル検索を行った上で、さらにグラフ探索を加えた上位互換です。

両方使うと以下の問題が生じます：
- 同じドキュメントを Weaviate と ES の2箇所に格納する二重管理
- 埋め込み生成が2回走る無駄なコスト
- ワークフローで両方の結果をマージする必要が出て複雑化

| | Dify標準（Weaviate） | GraphRAG（ES + Neo4j） |
|--|--|--|
| ベクトル検索 | ✅ | ✅（同等以上） |
| グラフ探索 | ❌ | ✅ |
| 両方使う意味 | GraphRAGがあれば不要 | こちらだけ使えばよい |

唯一 Dify 標準を残す意味があるのは、GraphRAG の実装・検証が完了するまでの**繋ぎ期間**だけです。
本番運用では GraphRAG だけで十分です。

---

**Q. ES と Neo4j の chunk_id はなぜ揃える必要がありますか？**
A. 回答の出典（citations）として `document_id / chunk_id / url` を返す際、
ES で見つかったチャンクと Neo4j で見つかったチャンクが同じ chunk_id を持っていないと、
「どのドキュメントのどの箇所から引用したか」を正確に追跡できなくなるためです。

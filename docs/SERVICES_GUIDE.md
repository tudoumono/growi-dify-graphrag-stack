# 未経験サービス 使い方ガイド

## サービス連携のイメージ

```
Growi（ドキュメント作成）
    ↓ データ投入
GraphRAG API（検索インデックス化）
    ↓ 検索
Dify（ワークフロー実行）← Langfuse（トレース記録）
```

---

## 1. Langfuse（http://localhost:3100）

**LLMアプリの「監視カメラ」的な存在**

Dify で LLM を使うたびに、その通信内容・コスト・応答時間などを自動記録します。

### 使い方の流れ

1. Langfuse にログイン → **プロジェクト作成**
2. `Settings > API Keys` で API キーを発行
3. その API キーを Dify の環境変数に設定（Dify との連携）
4. Dify でチャットすると自動的にトレースが記録される

### 見られる情報

| 項目 | 内容 |
|------|------|
| トレース | 各会話のプロンプト・レスポンスの全文 |
| コスト | トークン数・推定費用 |
| パフォーマンス | 応答時間・エラー率・成功率のグラフ |
| プロンプト管理 | プロンプトのバージョン管理 |

---

## 2. GraphRAG API（http://localhost:8080）

**Elasticsearch + Neo4j を使ったハイブリッド検索エンジン**

Dify のワークフローから HTTP リクエストで呼び出して使います。直接ブラウザで使うものではなく、**Dify のバックエンドとして機能**します。

### 使い方の流れ

1. Growi に書いたドキュメントを GraphRAG API に登録（インデックス化）
2. Dify のワークフローに `HTTP Request` ノードを追加
3. `POST http://host.docker.internal:8080/search` を叩く
4. 返ってきた検索結果を LLM に渡す

### API 仕様の確認

```bash
# Swagger UI で API 仕様を確認
curl http://localhost:8080/docs
```

---

## 3. MinIO Console（http://localhost:3191）

**Langfuse が使う S3 互換ストレージの管理画面**

基本的に触る必要はありません。Langfuse がメディアファイル（画像など）をここに保存しています。
デバッグ時に「ファイルが正しく保存されているか」確認する用途です。

### ログイン情報

URL.md を参照。

---

## 4. Kibana（http://localhost:5602）

**Elasticsearch の中身を GUI で見るツール**

GraphRAG がインデックス化したドキュメントの確認・検索テストに使います。

### 主な使い方

| 機能 | 場所 | 用途 |
|------|------|------|
| データ閲覧 | `Discover` タブ | インデックスのデータを検索・閲覧 |
| クエリ実行 | `Dev Tools` タブ | Elasticsearch に直接クエリを叩いてデバッグ |

### ログイン情報

URL.md を参照。

---

## 5. Neo4j Browser（http://localhost:7474）

**グラフDBの中身を GUI で見るツール**

GraphRAG がドキュメント間の関係（エンティティ・リレーション）をグラフとして保存しており、それを視覚的に確認できます。

### 接続情報

Neo4j Browser を開くと接続先 URL の入力欄があります。そこには **Bolt プロトコル** の URL を入力します。

| 項目 | 値 |
|------|-----|
| 接続 URL | `bolt://localhost:7687` |
| ユーザー | `neo4j` |
| パスワード | URL.md を参照 |

> Neo4j Browser（http://localhost:7474）はUIの入口で、**DBへの実際の接続**には `bolt://localhost:7687` を使います。

### よく使うクエリ

```cypher
-- 全ノードを表示（最大25件）
MATCH (n) RETURN n LIMIT 25

-- エンティティと関係を表示
MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 50
```

### ログイン情報

URL.md を参照。

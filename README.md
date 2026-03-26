# Langfuse / Dify / GROWI / GraphRAG 独立コンテナ構成

LLM アプリ開発で使う 4 つの主要コンポーネントを、ローカルで独立稼働させるための構成です。
サービス間は Docker network を共有せず、必要な連携だけを API でつなぎます。

詳細な設計方針は [ARCHITECTURE.md](ARCHITECTURE.md) を参照してください。

## 役割分担

| システム | 役割 | URL |
|---|---|---|
| **GROWI** | ナレッジの正本（Wiki・社内ドキュメント蓄積） | `http://localhost:3300` |
| **Dify** | チャット UI / ワークフロー実行基盤 | `http://localhost:80` |
| **Langfuse** | トレース・評価・改善の観測基盤 | `http://localhost:3100` |
| **GraphRAG** | ハイブリッド検索基盤（FastAPI + Elasticsearch + Neo4j） | `http://localhost:8080` |

## 設計方針

- Growi・Dify・Langfuse は既存 compose をそのまま使う
- GraphRAG は `graphrag/` に分離した独立プロジェクトとして管理する
- Dify 標準ナレッジ（Weaviate）は変更しない
- GraphRAG を使うときだけ、Dify ワークフローの HTTP Request ノードから `/search` を呼ぶ

## ディレクトリ構成

```text
DIfy_Growi_Langfuse/
├── ARCHITECTURE.md
├── README.md
├── Makefile
├── .gitignore
├── growi/
│   └── docker-compose.yml
├── dify/
│   └── docker-compose.yaml
├── langfuse/
│   └── docker-compose.yml
├── graphrag/
│   ├── .env.example
│   ├── docker-compose.yml
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── providers.py
│   ├── main.py         # FastAPI アプリ本体（全エンドポイント）
│   └── ingest.py       # ドキュメント変換ユーティリティ（ライブラリ）
└── docs/
    ├── SETUP_GUIDE.md
    ├── SERVICES_GUIDE.md
    ├── spec_phase*.md
    └── graphrag-learning.md
```

## 初回セットアップ

### 1. GROWI の `.env` を作成

```bash
cd growi
cp .env.example .env
sed -i "s|PASSWORD_SEED=changeme|PASSWORD_SEED=$(openssl rand -base64 24 | tr -d '/')|" .env
sed -i "s|SECRET_TOKEN=changeme|SECRET_TOKEN=$(openssl rand -hex 32)|" .env
cd ..
```

### 2. Dify / GraphRAG の `.env` を作成

```bash
cp dify/.env.example dify/.env
cp graphrag/.env.example graphrag/.env
```

`langfuse/.env` はこのワークスペースでは既存ファイルを使う想定です。

### 3. GraphRAG のローカル Python 環境を作成

GraphRAG の CLI (`ingest.py`) やローカル動作確認には `venv` を使います。  
Python は `3.11` を採用しています。安定していて、Dockerfile でも同じ系統を使っています。

この環境では Python 3.11 が `uv` 管理なので、`venv` 作成も `uv venv` を使うのが安全です。

```bash
cd graphrag
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install --python .venv/bin/python -r requirements.txt
cd ..
```

`~/.cache/uv` へ書き込めない環境では、先頭に `UV_CACHE_DIR=/tmp/uv-cache` を付けて実行してください。

## 起動方法

```bash
# 個別起動
make up-growi
make up-langfuse
make up-dify
make up-graphrag

# 一括起動
make up-all

# ログ確認
make logs-growi
make logs-langfuse
make logs-dify
make logs-graphrag

# 個別停止
make down-growi
make down-langfuse
make down-dify
make down-graphrag

# 一括停止
make down-all

# 状態確認
make status
```

## ポート一覧

| サービス | ホストポート | 用途 |
|---|---:|---|
| GROWI Web UI | `3300` | ナレッジ Wiki |
| Dify (nginx) | `80` | チャット UI / ワークフロー |
| Dify plugin_daemon | `5003` | Dify プラグイン管理 |
| Langfuse Web UI | `3100` | トレース・評価 |
| Langfuse Worker | `3030` | Langfuse ワーカー |
| Langfuse MinIO | `3190` | S3 互換ストレージ |
| GraphRAG API | `8080` | `/ingest`・`/search` |
| GraphRAG Elasticsearch | `9201` | ベクトル検索インデックス |
| GraphRAG Kibana | `5602` | ES の中身確認 |
| Neo4j Browser | `7474` | グラフ構造の確認 |
| Neo4j Bolt | `7687` | Neo4j ドライバー接続 |

## GraphRAG の使い方

### API

| 操作 | メソッド | URL |
|---|---|---|
| ヘルスチェック | `GET` | `http://localhost:8080/health` |
| 取り込み | `POST` | `http://localhost:8080/ingest` |
| 検索 | `POST` / `GET` | `http://localhost:8080/search` |
| プロバイダー確認 | `GET` | `http://localhost:8080/providers` |

`/search` は `category` フィルターに対応しています。

GET 例:

```bash
curl "http://localhost:8080/search?query=セキュリティ&category=技術文書"
```

POST 例:

```json
{
  "query": "セキュリティ",
  "top_k": 5,
  "category": "技術文書"
}
```

### GROWI から取り込む

`graphrag/.env` に `GROWI_URL` と `GROWI_API_KEY` を設定した上で、API を呼びます。

```bash
curl -s -X POST http://localhost:8080/ingest-growi \
  -H "Content-Type: application/json" \
  -d '{"page_id": "12345", "category": "技術文書"}'
```

> `page_id` は GROWI ページ URL 末尾の数字、または管理画面で確認できます。

### Dify から GraphRAG を呼ぶ

Dify ワークフローでは HTTP Request ノードを使い、次の URL を指定します。

```text
http://host.docker.internal:8080/search
```

## LLM / Embed プロバイダー設定

`graphrag/.env` で切り替えます。

| フェーズ | Embed | LLM | 次元数 |
|---|---|---|---:|
| PoC | Bedrock Titan Embed v2 | Bedrock Claude Haiku | 1024 |
| 本番寄り | Ollama `nomic-embed-text` | Ollama `llama3` | 768 |

注意:
Embed モデルを切り替えるとベクトル次元数が変わるため、Elasticsearch インデックスを作り直して再取り込みが必要です。

## よく使う確認先

- GraphRAG Swagger UI: `http://localhost:8080/docs`
- Kibana: `http://localhost:5602`
- Neo4j Browser: `http://localhost:7474`

## トラブルシューティング

### `host.docker.internal` が解決できない

Linux 環境で解決できない場合はホスト IP を確認して置き換えてください。

```bash
hostname -I | awk '{print $1}'
```

### Elasticsearch が起動しない

`vm.max_map_count` が小さいと Elasticsearch が落ちます。

```bash
sudo sysctl -w vm.max_map_count=262144
```

### GraphRAG API のログを見たい

```bash
make logs-graphrag
```

### Dify ログイン後に 401 になる

`dify/.env` の URL 系設定を見直してください。

```text
CONSOLE_API_URL=http://localhost:80
CONSOLE_WEB_URL=http://localhost:80
```

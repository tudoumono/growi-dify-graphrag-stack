# サービス URL 一覧

## メインサービス

| システム | URL | ユーザー | パスワード | 備考 |
|----------|-----|----------|------------|------|
| Growi | http://localhost:3300 | 初回アクセス時に設定 | 初回アクセス時に設定 | ナレッジWiki |
| Dify | http://localhost:80 | 初回アクセス時に設定 | 初回アクセス時に設定 | LLMアプリプラットフォーム |
| Langfuse | http://localhost:3100 | 初回アクセス時に設定 | 初回アクセス時に設定 | LLM可観測性 |
| GraphRAG API | http://localhost:8080 | - | - | GraphRAGバックエンド（認証なし） |

## Langfuse 補助サービス

| サービス | URL | ユーザー | パスワード | 備考 |
|----------|-----|----------|------------|------|
| MinIO (S3互換) | http://localhost:3190 | - | - | ストレージ（外部公開用） |
| MinIO Console | http://localhost:3191 | `minio` | `langfuse/.env の MINIO_ROOT_PASSWORD` | 管理画面（localhost限定） |
| ClickHouse | http://localhost:8123 | `clickhouse` | `langfuse/.env の CLICKHOUSE_PASSWORD` | 分析DB（localhost限定） |
| PostgreSQL | localhost:5432 | `postgres` | `langfuse/.env の POSTGRES_PASSWORD` | DB（localhost限定） |

> Langfuse 補助サービスは Langfuse が内部で自動接続するため、通常は手動ログイン不要。

## GraphRAG 補助サービス

| サービス | URL | ユーザー | パスワード | 備考 |
|----------|-----|----------|------------|------|
| Elasticsearch | http://localhost:9201 | `elastic` | `graphrag` | 検索エンジン |
| Kibana | http://localhost:5602 | `elastic` | `graphrag` | ES管理画面（日本語UI） |
| Neo4j Browser | http://localhost:7474 | `neo4j` | `dify-graphrag` | グラフDB管理画面 |

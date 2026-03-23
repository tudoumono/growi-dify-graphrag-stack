# Dify / GROWI / Langfuse / GraphRAG 初回セットアップガイド

## 1. はじめに

### このガイドの使い方

**上から順番にコマンドを実行すれば、環境が完成します。**

途中でコピーしたAPIキーやパスワードはメモ帳など手元に控えながら進めてください。
手順書自体にAPIキーは記載しません。発行したらすぐ次のステップで使い切る構成になっています。

### 4つのサービスの役割

```
GROWI（ドキュメント作成・管理）
    │
    └─ ingest.py でドキュメントを投入
            │
            ▼
    GraphRAG API（ドキュメントの検索エンジン）
            │
            └─ Dify ワークフローから HTTP で呼び出し
                    │
                    ▼
            Dify（LLMアプリの実行基盤）
                    │
                    └─ トレース・ログを自動送信
                            │
                            ▼
                    Langfuse（AIの動作を記録・分析）
```

| サービス | 役割 | URL |
|----------|------|-----|
| GROWI | ナレッジ Wiki（ドキュメントの正本） | http://localhost:3300 |
| Dify | チャット UI / ワークフロー実行 | http://localhost:80 |
| Langfuse | LLMの動作記録・コスト・評価 | http://localhost:3100 |
| GraphRAG API | ハイブリッド検索（ES + Neo4j） | http://localhost:8080 |

### 所要時間の目安

| フェーズ | 目安 |
|----------|------|
| 事前確認・ファイル準備 | 15〜30分 |
| 各サービスの起動・初期設定 | 30〜60分 |
| 動作確認 | 15〜30分 |
| 合計 | **1〜2時間** |

---

## 2. 事前確認チェックリスト

### 必要なソフトウェアの確認

以下のコマンドをターミナルで実行し、それぞれバージョンが表示されることを確認します。
エラーが出た場合は、各ツールをインストールしてから先に進んでください。

```bash
# Docker が動いているか
docker --version
docker compose version

# Git
git --version

# Python（3.11 以上）
python3 --version

# uv（Python パッケージマネージャー）
uv --version

# openssl（シークレット生成に使用）
openssl version

# make（Makefile 実行）
make --version
```

### Elasticsearch 用メモリ設定

Elasticsearch は OS のメモリマップ数の設定が小さいと起動に失敗します。
起動前に必ず以下を実行してください。

```bash
# 現在の値を確認
sysctl vm.max_map_count

# 262144 未満の場合は以下で設定（再起動後にリセットされます）
sudo sysctl -w vm.max_map_count=262144
```

> **再起動後も維持したい場合（Linux）**
> `/etc/sysctl.conf` に `vm.max_map_count=262144` を追記してください。
>
> **WSL2 の場合**
> WSL2 は再起動のたびにリセットされます。毎回上記コマンドを実行するか、
> 起動スクリプトに追記することを推奨します。

---

## 3. ファイル準備（.env の作成）

### リポジトリのクローン

```bash
git clone <リポジトリURL> DIfy_Growi_Langfuse
cd DIfy_Growi_Langfuse
```

### GROWI の .env を作成

```bash
cd growi
cp .env.example .env

# シークレットを自動生成して .env に書き込む
sed -i "s|PASSWORD_SEED=changeme|PASSWORD_SEED=$(openssl rand -base64 24 | tr -d '/')|" .env
sed -i "s|SECRET_TOKEN=changeme|SECRET_TOKEN=$(openssl rand -hex 32)|" .env

# 内容を確認（値が changeme のままでないこと）
cat .env
cd ..
```

### Dify の .env を作成

```bash
cp dify/.env.example dify/.env
```

> Dify の `.env.example` はそのままでもローカル起動できます。
> LLM プロバイダーの設定は起動後に GUI から行います。

### Langfuse の .env を作成

Langfuse には `.env.example` がないため、以下のコマンドで直接作成します。

```bash
# MinIO パスワードを先に生成して変数に保存
MINIO_PASS=$(openssl rand -base64 16 | tr -d '/+=')

cat > langfuse/.env << EOF
# Langfuse 設定
NEXTAUTH_URL=http://localhost:3100
NEXTAUTH_SECRET=$(openssl rand -base64 32)

# 暗号化キー（必ず 64 文字の16進数）
ENCRYPTION_KEY=$(openssl rand -hex 32)
SALT=$(openssl rand -base64 32)

# PostgreSQL
POSTGRES_USER=postgres
POSTGRES_PASSWORD=$(openssl rand -base64 16 | tr -d '/+=')
POSTGRES_DB=postgres
DATABASE_URL=postgresql://postgres:\${POSTGRES_PASSWORD}@postgres:5432/postgres

# Redis
REDIS_AUTH=$(openssl rand -base64 16 | tr -d '/+=')

# ClickHouse
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=$(openssl rand -base64 16 | tr -d '/+=')
CLICKHOUSE_MIGRATION_URL=clickhouse://clickhouse:9000
CLICKHOUSE_URL=http://clickhouse:8123

# MinIO（S3 互換ストレージ）
# ※ MINIO_ROOT_PASSWORD と S3_SECRET_ACCESS_KEY は必ず同じ値にすること
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=${MINIO_PASS}
LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID=minio
LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=${MINIO_PASS}
LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT=http://minio:9000
LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE=true
LANGFUSE_S3_EVENT_UPLOAD_BUCKET=langfuse
LANGFUSE_S3_EVENT_UPLOAD_REGION=auto
LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID=minio
LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY=${MINIO_PASS}
LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT=http://localhost:3190
LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE=true
LANGFUSE_S3_MEDIA_UPLOAD_BUCKET=langfuse
LANGFUSE_S3_MEDIA_UPLOAD_REGION=auto
EOF
```

> **注意**: `ENCRYPTION_KEY` は必ず 64 文字の16進数である必要があります。
> 上記コマンドで自動生成される値は条件を満たしています。

### GraphRAG の .env を作成

```bash
cp graphrag/.env.example graphrag/.env
```

LLM プロバイダーを確認・設定します（デフォルトは AWS Bedrock）。

```bash
# .env の内容を確認
cat graphrag/.env
```

AWS Bedrock を使う場合、`AWS_PROFILE` がローカルの設定と一致しているか確認してください。

```bash
# AWS 認証が通っているか確認（Bedrock を使う場合）
aws sts get-caller-identity --profile sandbox
```

---

## 4. GROWI の起動と初期設定

### 起動

```bash
make up-growi
```

起動には 1〜2 分かかります。以下のコマンドでログを確認できます。

```bash
make logs-growi
# 「GROWI は起動しました」のようなメッセージが出たら Ctrl+C で抜ける
```

### 管理者アカウントの作成

1. ブラウザで http://localhost:3300 を開く
2. 「新規登録」画面が表示されるので、**最初に登録したユーザーが管理者**になります
3. メールアドレス・ユーザー名・パスワードを入力して登録

### GROWI API キーの発行

Dify から GROWI のドキュメントを取り込む際に使用します。

1. 右上のユーザーアイコン → 「ユーザー設定」をクリック
2. 左メニューの「API 設定」をクリック
3. 「API トークンを発行」ボタンをクリック
4. 表示されたトークンをコピーして **手元のメモ帳に控える**
5. 以下のコマンドで `graphrag/.env` に書き込む

```bash
# YOUR_GROWI_API_KEY の部分をコピーしたキーに置き換えて実行
sed -i "s|^# GROWI_API_KEY=.*|GROWI_API_KEY=YOUR_GROWI_API_KEY|" graphrag/.env

# または直接エディタで編集
# graphrag/.env の末尾に GROWI_API_KEY=<発行したキー> を追記
```

---

## 5. Langfuse の起動と初期設定

### 起動

```bash
make up-langfuse
```

Langfuse は複数のコンテナ（PostgreSQL, Redis, ClickHouse, MinIO）を含むため、
起動完了まで **3〜5 分**かかります。

```bash
make logs-langfuse
# langfuse-web が "Ready on http://localhost:3000" と表示されたら完了
```

### アカウントの作成とプロジェクト作成

1. ブラウザで http://localhost:3100 を開く
2. 「Sign up」をクリックし、メールアドレス・パスワードを登録
3. ログイン後、「New Project」をクリックしてプロジェクトを作成
   - プロジェクト名の例: `dify-local`

### API キーの発行

Dify との連携に使います。発行後すぐに次のステップで使います。

1. 左メニューの「Settings」→「API Keys」を開く
2. 「Create new API keys」をクリック
3. **Secret Key と Public Key の両方をコピーして手元に控える**
4. Langfuse の **Host URL** も控える: `http://localhost:3100`

> **次のステップですぐ使います。メモ帳に一時的に控えておいてください。**

---

## 6. Dify の起動と初期設定

### 起動

```bash
make up-dify
```

```bash
make logs-dify
# api コンテナのログに "Running on http://0.0.0.0:5001" が出たら完了
```

### 管理者アカウントの作成

1. ブラウザで http://localhost:80 を開く
2. 初回アクセス時に管理者登録画面が表示される
3. メールアドレス・パスワードを入力して登録・ログイン

### LLM プロバイダーの設定

1. 右上のアイコン → 「設定」をクリック
2. 「モデルプロバイダー」→ 使用するプロバイダーを選択
   - **AWS Bedrock を使う場合**: `Amazon Bedrock` を選択 → アクセスキーを入力
   - **Anthropic を使う場合**: `Anthropic` を選択 → APIキーを入力
3. 「保存」をクリックし、接続テストが成功することを確認

### テスト用アプリの作成と Langfuse 連携設定

Dify の Langfuse 連携は**アプリごとに設定**します。

1. 「スタジオ」→「アプリを作成」→ タイプは「チャットボット」を選択
2. アプリ名を入力して作成（例: `テスト用チャット`）
3. アプリが開いたら右上の「...」メニュー → 「監視」をクリック
4. 「Langfuse」の「設定」をクリック
5. **前のステップで控えた値**を入力する
   - `Public Key`: Langfuse で発行した Public Key
   - `Secret Key`: Langfuse で発行した Secret Key
   - `Host`: `http://localhost:3100`
6. 「保存」をクリック

> これで Dify でチャットするたびに Langfuse にトレースが自動記録されます。
> APIキーはドキュメントに残さず、入力後は手元のメモからも削除して構いません。

---

## 7. GraphRAG の起動と初期設定

### Python 仮想環境の作成

GraphRAG の CLI（`ingest.py`）をローカルで実行するための Python 環境を作ります。
**Python 3.11** を使用します。

```bash
cd graphrag

# 仮想環境を作成
uv venv --python 3.11 .venv

# 仮想環境を有効化
source .venv/bin/activate

# 依存パッケージをインストール
uv pip install --python .venv/bin/python -r requirements.txt

cd ..
```

> `~/.cache/uv` への書き込みエラーが出た場合は、先頭に
> `UV_CACHE_DIR=/tmp/uv-cache` を付けて実行してください。

### 起動

```bash
make up-graphrag
```

GraphRAG は Elasticsearch と Neo4j が完全に起動してから API が立ち上がります。
**5〜10 分**かかる場合があります。

```bash
make logs-graphrag
# graphrag-api コンテナに "Application startup complete." が出たら完了
```

### 起動確認

```bash
curl http://localhost:8080/health
# {"status":"ok"} が返ってきたら成功
```

---

## 8. 動作確認（エンドツーエンド）

### 全サービスの起動確認

```bash
make status
```

全コンテナが `Up` または `running` 状態になっていることを確認します。

### GROWI にテストページを作成

1. http://localhost:3300 を開く
2. 左メニューの「+」→「新規ページ作成」をクリック
3. パス: `/テスト/サンプルドキュメント`
4. 内容に適当なテキストを入力して「保存」

### GraphRAG へドキュメントを取り込む（ingest）

GROWI のページを GraphRAG の検索エンジンに登録します。

```bash
cd graphrag
source .venv/bin/activate

# GROWI のページIDを確認（URLの末尾の数字）
# 例: http://localhost:3300/テスト/サンプルドキュメント のページIDを確認

python ingest.py \
  --url http://localhost:3300 \
  --page-id <GROWIのページID> \
  --api-key <GROWIのAPIキー> \
  --category テスト

cd ..
```

> ページIDは GROWI のページURLや管理画面で確認できます。

### GraphRAG の検索をテスト

```bash
curl "http://localhost:8080/search?query=テスト&category=テスト"
# 先ほど取り込んだドキュメントの内容が返ってきたら成功
```

### Dify から GraphRAG を呼び出す

1. http://localhost:80 を開き、作成したアプリを開く
2. 「ワークフロー」タブを開く
3. 「HTTP リクエスト」ノードを追加
4. URL に以下を設定:
   ```
   http://host.docker.internal:8080/search
   ```
5. メソッド: `GET`、パラメータに `query={{ユーザーの入力}}` を設定
6. 「実行」して GraphRAG からの検索結果が返ることを確認

### Langfuse にトレースが記録されているか確認

1. http://localhost:3100 を開く
2. 左メニューの「Tracing」をクリック
3. Dify でチャットした記録が表示されていれば成功

> **トレースがすぐ表示されない場合**
> Langfuse は MinIO → ClickHouse → 画面反映という非同期処理のため、
> チャット送信から **数秒〜30秒程度** 遅れて表示されます。
> 少し待ってからページを更新してみてください。

---

## 9. 日常操作クイックリファレンス

### 起動・停止コマンド

| 操作 | コマンド |
|------|----------|
| GROWI 起動 | `make up-growi` |
| Langfuse 起動 | `make up-langfuse` |
| Dify 起動 | `make up-dify` |
| GraphRAG 起動 | `make up-graphrag` |
| **全サービス一括起動** | `make up-all` |
| GROWI 停止 | `make down-growi` |
| Langfuse 停止 | `make down-langfuse` |
| Dify 停止 | `make down-dify` |
| GraphRAG 停止 | `make down-graphrag` |
| **全サービス一括停止** | `make down-all` |
| 状態確認 | `make status` |

### ログ確認

```bash
make logs-growi
make logs-langfuse
make logs-dify
make logs-graphrag
```

### アクセス URL 一覧

| サービス | URL | 備考 |
|----------|-----|------|
| GROWI | http://localhost:3300 | ナレッジ Wiki |
| Dify | http://localhost:80 | LLMアプリ |
| Langfuse | http://localhost:3100 | トレース確認 |
| GraphRAG API | http://localhost:8080 | REST API |
| GraphRAG Swagger | http://localhost:8080/docs | API仕様書 |
| Kibana | http://localhost:5602 | ES中身確認（elastic / graphrag） |
| Neo4j Browser | http://localhost:7474 | グラフDB確認 |

> Neo4j Browser の接続 URL: `bolt://localhost:7687`（ユーザー: `neo4j`）

---

## 10. トラブルシューティング

### Elasticsearch が起動しない

**症状**: GraphRAG のログに `max virtual memory areas vm.max_map_count [...] is too low` が出る

```bash
sudo sysctl -w vm.max_map_count=262144
make down-graphrag
make up-graphrag
```

### `host.docker.internal` が解決できない

**症状**: Dify ワークフローから GraphRAG を呼び出すと名前解決エラーになる

Linux 環境では `host.docker.internal` が使えない場合があります。

```bash
# ホストの IP アドレスを確認
hostname -I | awk '{print $1}'
```

Dify ワークフローの HTTP リクエストノードの URL を、上記の IP アドレスに置き換えてください。
例: `http://192.168.1.100:8080/search`

### Dify ログイン後に 401 エラーになる

**症状**: ログインはできるが API 呼び出しで 401 が返る

`dify/.env` の URL 設定が実際のアクセス URL と一致していることを確認します。

```bash
grep -E "CONSOLE_API_URL|CONSOLE_WEB_URL|APP_WEB_URL" dify/.env
```

ローカルで使う場合は以下の値になっているか確認してください。

```
CONSOLE_API_URL=http://localhost:80
CONSOLE_WEB_URL=http://localhost:80
APP_WEB_URL=http://localhost:80
```

### メモリ不足でサービスが落ちる

**症状**: コンテナが突然停止する、または起動してすぐ落ちる

全サービスを同時に起動すると 8GB 以上のメモリが必要です。
WSL2 の場合は `.wslconfig` でメモリ上限を増やしてください。

Windows の `%USERPROFILE%\.wslconfig` を編集:

```ini
[wsl2]
memory=10GB
```

設定後、PowerShell で WSL2 を再起動します。

```powershell
wsl --shutdown
```

### GraphRAG の Embed モデルを変更した後に検索がヒットしない

Embed モデルを変更するとベクトルの次元数が変わり、既存のインデックスと不整合が起きます。
インデックスを削除して再取り込みが必要です。

```bash
# Kibana（http://localhost:5602）の Dev Tools で以下を実行
DELETE /graphrag_chunks

# その後 ingest.py で再取り込み
```

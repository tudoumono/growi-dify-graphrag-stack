FROM python:3.11-slim

WORKDIR /app

# curl: healthcheck 用 / git: official ファイル削除時の Git コミット用
RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

# Git コミット時の author 設定（DELETE /documents 官式ファイル削除で使用）
ENV GIT_AUTHOR_NAME="GraphRAG Bot"
ENV GIT_AUTHOR_EMAIL="graphrag@localhost"
ENV GIT_COMMITTER_NAME="GraphRAG Bot"
ENV GIT_COMMITTER_EMAIL="graphrag@localhost"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

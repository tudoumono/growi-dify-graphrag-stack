#!/bin/bash
# GraphRAG インデックス・グラフDB 一括リセット
# Elasticsearch インデックスと Neo4j の全データを削除します。
# 次回 ingest 時にインデックスは自動再作成されます。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

# .env からパスワードを読み込む
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[エラー] .env が見つかりません: $ENV_FILE"
  exit 1
fi

ES_PASSWORD=$(grep "^GRAPHRAG_ES_PASSWORD=" "$ENV_FILE" | cut -d= -f2)
NEO4J_PASSWORD=$(grep "^NEO4J_PASSWORD=" "$ENV_FILE" | cut -d= -f2)

if [[ -z "$ES_PASSWORD" || -z "$NEO4J_PASSWORD" ]]; then
  echo "[エラー] .env から GRAPHRAG_ES_PASSWORD または NEO4J_PASSWORD を読み込めませんでした"
  exit 1
fi

echo "=== Elasticsearch インデックス削除 ==="
RESULT=$(curl -s -o /dev/null -w "%{http_code}" \
  -X DELETE "http://localhost:9201/graphrag_chunks" \
  -u "elastic:${ES_PASSWORD}")

if [[ "$RESULT" == "200" ]]; then
  echo "削除完了"
elif [[ "$RESULT" == "404" ]]; then
  echo "インデックスが存在しないためスキップ"
else
  echo "[エラー] HTTP $RESULT"
  exit 1
fi

echo ""
echo "=== Neo4j グラフDB 全データ削除 ==="
docker exec neo4j cypher-shell \
  -u neo4j -p "${NEO4J_PASSWORD}" \
  "MATCH (n) DETACH DELETE n;" \
  && echo "削除完了"

echo ""
echo "=== リセット完了 ==="
echo "次回 ingest 時にインデックスは自動再作成されます"

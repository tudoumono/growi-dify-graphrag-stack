"""
GraphRAG API - Elasticsearch と Neo4j を使ったハイブリッド検索 API
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from elasticsearch import Elasticsearch
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from langchain_text_splitters import RecursiveCharacterTextSplitter
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

from providers import EmbedProvider, LLMProvider, get_embed_provider, get_llm_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GraphRAG API", version="0.2.0")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )

ES_HOST = os.environ["ELASTICSEARCH_HOST"]
ES_PORT = os.environ.get("ELASTICSEARCH_PORT", "9200")
ES_USER = os.environ.get("ELASTICSEARCH_USERNAME", "elastic")
ES_PASS = os.environ["ELASTICSEARCH_PASSWORD"]
ES_INDEX = os.environ.get("ELASTICSEARCH_INDEX", "graphrag_chunks")

NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_USER = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.environ["NEO4J_PASSWORD"]

CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "120"))

_embed_provider: EmbedProvider | None = None
_llm_provider: LLMProvider | None = None


def compact_dict(values: dict[str, Any]) -> dict[str, Any]:
    """None を除外して保存用の dict を作る"""
    return {key: value for key, value in values.items() if value is not None}


def metadata_json(metadata: dict[str, Any]) -> str | None:
    if not metadata:
        return None
    return json.dumps(metadata, ensure_ascii=False)


@app.on_event("startup")
def startup() -> None:
    global _embed_provider, _llm_provider
    _embed_provider = get_embed_provider()
    _llm_provider = get_llm_provider()
    logger.info(
        "プロバイダー初期化完了 embed=%s(%d次元) llm=%s chunk=%d overlap=%d",
        os.environ.get("EMBED_PROVIDER", "bedrock"),
        _embed_provider.dims,
        os.environ.get("LLM_PROVIDER", "bedrock"),
        CHUNK_SIZE,
        CHUNK_OVERLAP,
    )
    # スキーマ移行: source_document_id のない旧 RELATED_TO を削除
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH ()-[r:RELATED_TO]->() WHERE r.source_document_id IS NULL "
                "DELETE r RETURN count(r) AS deleted"
            )
            deleted = result.single()["deleted"]
            if deleted > 0:
                logger.info("旧スキーマの RELATED_TO を %d 件削除しました", deleted)
    except Exception as exc:
        logger.warning("Neo4j スキーマ移行をスキップしました: %s", exc)
    finally:
        driver.close()


def embed_provider() -> EmbedProvider:
    assert _embed_provider is not None
    return _embed_provider


def llm_provider() -> LLMProvider:
    assert _llm_provider is not None
    return _llm_provider


def get_es_client() -> Elasticsearch:
    return Elasticsearch(f"http://{ES_HOST}:{ES_PORT}", basic_auth=(ES_USER, ES_PASS))


def get_neo4j_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))


def ensure_es_index(es: Elasticsearch) -> None:
    if es.indices.exists(index=ES_INDEX):
        return

    mapping = {
        "mappings": {
            "properties": {
                "chunk_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "title": {"type": "text"},
                "text": {"type": "text"},
                "url": {"type": "keyword"},
                "chunk_index": {"type": "integer"},
                "category": {"type": "keyword"},
                "source": {"type": "keyword"},
                "tags": {"type": "keyword"},
                "language": {"type": "keyword"},
                "created_at": {
                    "type": "date",
                    "format": "strict_date_optional_time||epoch_millis",
                },
                "updated_at": {
                    "type": "date",
                    "format": "strict_date_optional_time||epoch_millis",
                },
                "metadata": {"type": "flattened"},
                "embedding": {
                    "type": "dense_vector",
                    "dims": embed_provider().dims,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        }
    }
    es.indices.create(index=ES_INDEX, body=mapping)
    logger.info("ES インデックス '%s' を作成しました", ES_INDEX)


def extract_entities(text: str) -> list[dict[str, Any]]:
    prompt = (
        "以下のテキストから固有表現（人名・組織・概念・場所など）を抽出し、"
        "JSON 配列で返してください。\n"
        'フォーマット: [{"name":"...","canonical_name":"...","type":"Person|Organization|Concept|Location|Other"}]\n'
        "説明は不要です。JSON のみ返してください。\n\n"
        f"テキスト:\n{text[:2000]}"
    )
    raw = llm_provider().generate(prompt)
    try:
        if "```" in raw:
            raw = raw.split("```")[1].removeprefix("json").strip()
        return json.loads(raw)
    except Exception:
        logger.warning("エンティティ抽出の JSON パース失敗: %s", raw)
        return []


def extract_relations(entities: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    if len(entities) < 2:
        return []

    entity_names = [entity["canonical_name"] for entity in entities if entity.get("canonical_name")]
    if len(entity_names) < 2:
        return []

    prompt = (
        "以下のエンティティリストとテキストを元に、エンティティ間の関係を抽出してください。\n"
        'フォーマット: [{"from":"...","to":"...","relation_type":"..."}]\n'
        "relation_type は動詞句で表現してください。\n"
        "JSON のみ返してください。\n\n"
        f"エンティティ: {json.dumps(entity_names, ensure_ascii=False)}\n"
        f"テキスト:\n{text[:2000]}"
    )
    raw = llm_provider().generate(prompt)
    try:
        if "```" in raw:
            raw = raw.split("```")[1].removeprefix("json").strip()
        return json.loads(raw)
    except Exception:
        logger.warning("関係抽出の JSON パース失敗: %s", raw)
        return []


class IngestRequest(BaseModel):
    document_id: str
    title: str
    url: str
    text: str
    category: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    document_id: str
    chunk_id: str
    url: str
    score: float


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    category: str | None = None
    source: str | None = None
    language: str | None = None


class SearchResponse(BaseModel):
    es_hits: list[dict[str, Any]]
    graph_hits: list[dict[str, Any]]
    merged_context: str
    citations: list[Citation]


class ProviderInfo(BaseModel):
    embed_provider: str
    embed_dims: int
    llm_provider: str
    chunk_size: int
    chunk_overlap: int


def document_properties(req: IngestRequest) -> dict[str, Any]:
    # None を含めて全フィールドを返す: SET d = $props で完全置き換えするため
    return {
        "id": req.document_id,
        "title": req.title,
        "url": req.url,
        "category": req.category,
        "source": req.source,
        "tags": req.tags or None,
        "language": req.language,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
        "metadata_json": metadata_json(req.metadata),
    }


def chunk_document(req: IngestRequest) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    return splitter.split_text(req.text)


def build_es_filters(req: SearchRequest) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    if req.category:
        filters.append({"term": {"category": req.category}})
    if req.source:
        filters.append({"term": {"source": req.source}})
    if req.language:
        filters.append({"term": {"language": req.language}})
    return filters


def perform_search(req: SearchRequest) -> SearchResponse:
    es = get_es_client()
    driver = get_neo4j_driver()
    try:
        return _perform_search_inner(es, driver, req)
    finally:
        driver.close()


def _perform_search_inner(es: Elasticsearch, driver: Any, req: SearchRequest) -> SearchResponse:
    knn = {
        "field": "embedding",
        "query_vector": embed_provider().embed(req.query),
        "k": req.top_k,
        "num_candidates": req.top_k * 10,
    }
    es_filters = build_es_filters(req)
    if es_filters:
        knn["filter"] = es_filters

    es_response = es.search(
        index=ES_INDEX,
        body={
            "knn": knn,
            "_source": [
                "chunk_id",
                "document_id",
                "title",
                "text",
                "url",
                "chunk_index",
                "category",
                "source",
                "language",
            ],
        },
    )

    es_hits = [
        {
            "chunk_id": hit["_source"]["chunk_id"],
            "document_id": hit["_source"]["document_id"],
            "title": hit["_source"]["title"],
            "text": hit["_source"]["text"],
            "url": hit["_source"]["url"],
            "category": hit["_source"].get("category"),
            "source": hit["_source"].get("source"),
            "language": hit["_source"].get("language"),
            "score": hit["_score"],
        }
        for hit in es_response["hits"]["hits"]
    ]

    seed_chunk_ids = [hit["chunk_id"] for hit in es_hits]
    graph_hits: list[dict[str, Any]] = []

    if seed_chunk_ids:
        with driver.session() as session:
            result = session.run(
                """
                UNWIND $chunk_ids AS cid
                MATCH (c:Chunk {id: cid})-[:MENTIONS]->(e:Entity)
                WITH DISTINCT e LIMIT 20
                MATCH (e)<-[:MENTIONS]-(related:Chunk)
                WHERE NOT related.id IN $chunk_ids
                  AND ($category IS NULL OR related.category = $category)
                  AND ($source IS NULL OR related.source = $source)
                  AND ($language IS NULL OR related.language = $language)
                MATCH (d:Document {id: related.document_id})
                RETURN DISTINCT
                    related.id AS chunk_id,
                    related.document_id AS document_id,
                    related.text AS text,
                    d.title AS title,
                    d.url AS url,
                    related.category AS category,
                    related.source AS source,
                    related.language AS language,
                    e.canonical_name AS via_entity,
                    e.type AS entity_type
                LIMIT $limit
                """,
                chunk_ids=seed_chunk_ids,
                category=req.category,
                source=req.source,
                language=req.language,
                limit=req.top_k * 2,
            )
            graph_hits = [dict(record) for record in result]

    seen: set[str] = set()
    context_parts: list[str] = []
    citations: list[Citation] = []

    for hit in es_hits:
        if hit["chunk_id"] in seen:
            continue
        seen.add(hit["chunk_id"])
        context_parts.append(f"[{hit['title']}]\n{hit['text']}")
        citations.append(
            Citation(
                document_id=hit["document_id"],
                chunk_id=hit["chunk_id"],
                url=hit["url"],
                score=hit["score"],
            )
        )

    for hit in graph_hits:
        if hit["chunk_id"] in seen:
            continue
        seen.add(hit["chunk_id"])
        context_parts.append(f"[{hit['title']} ※ '{hit['via_entity']}' 経由]\n{hit['text']}")
        citations.append(
            Citation(
                document_id=hit["document_id"],
                chunk_id=hit["chunk_id"],
                url=hit.get("url") or "",
                score=0.0,
            )
        )

    return SearchResponse(
        es_hits=es_hits,
        graph_hits=graph_hits,
        merged_context="\n\n---\n\n".join(context_parts),
        citations=citations,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/providers", response_model=ProviderInfo)
def providers_info() -> ProviderInfo:
    return ProviderInfo(
        embed_provider=os.environ.get("EMBED_PROVIDER", "bedrock"),
        embed_dims=embed_provider().dims,
        llm_provider=os.environ.get("LLM_PROVIDER", "bedrock"),
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    """
    ドキュメントを ES（ベクトル）と Neo4j（グラフ）へ取り込む。
    同じ document_id で再取り込みした場合、不要になった古いチャンクは削除される。
    """
    es = get_es_client()
    ensure_es_index(es)
    driver = get_neo4j_driver()
    chunks = chunk_document(req)
    new_chunk_count = len(chunks)
    stored_chunks: list[str] = []
    doc_props = document_properties(req)

    try:
        with driver.session() as session:
            session.run(
                "MERGE (d:Document {id: $id}) SET d = $props",
                id=req.document_id,
                props=doc_props,
            )

            # 再取り込み時: このドキュメント由来の RELATED_TO のみを安全に削除
            session.run(
                "MATCH ()-[r:RELATED_TO {source_document_id: $document_id}]->() DELETE r",
                document_id=req.document_id,
            )

            for index, chunk_text in enumerate(chunks):
                chunk_id = f"{req.document_id}-chunk-{index}"
                embedding = embed_provider().embed(chunk_text)
                chunk_props = compact_dict(
                    {
                        "chunk_id": chunk_id,
                        "document_id": req.document_id,
                        "title": req.title,
                        "text": chunk_text,
                        "url": req.url,
                        "chunk_index": index,
                        "category": req.category,
                        "source": req.source,
                        "tags": req.tags or None,
                        "language": req.language,
                        "created_at": req.created_at,
                        "updated_at": req.updated_at,
                        "metadata": req.metadata or None,
                        "embedding": embedding,
                    }
                )
                es.index(index=ES_INDEX, id=chunk_id, document=chunk_props)

                # None を含めて全フィールドを渡す: SET c = $props で完全置き換えし
                # 前回あったオプション項目（category 等）が今回 None なら削除される
                neo4j_chunk_props = {
                    "id": chunk_id,
                    "document_id": req.document_id,
                    "text": chunk_text,
                    "chunk_index": index,
                    "category": req.category,
                    "source": req.source,
                    "tags": req.tags or None,
                    "language": req.language,
                    "created_at": req.created_at,
                    "updated_at": req.updated_at,
                    "metadata_json": metadata_json(req.metadata),
                }
                session.run(
                    "MERGE (c:Chunk {id: $id}) "
                    "SET c = $props "
                    "WITH c MATCH (d:Document {id: $document_id}) "
                    "MERGE (d)-[:HAS_CHUNK]->(c)",
                    id=chunk_id,
                    document_id=req.document_id,
                    props=neo4j_chunk_props,
                )
                # 再取り込み時: 古い MENTIONS を削除してから新しいエンティティを付け直す
                session.run(
                    "MATCH (c:Chunk {id: $id})-[r:MENTIONS]->() DELETE r",
                    id=chunk_id,
                )

                entities = extract_entities(chunk_text)
                for entity in entities:
                    canonical = entity.get("canonical_name") or entity.get("name")
                    if not canonical:
                        continue
                    session.run(
                        "MERGE (e:Entity {canonical_name: $canonical_name}) "
                        "SET e.name = $name, e.type = $type "
                        "WITH e MATCH (c:Chunk {id: $chunk_id}) "
                        "MERGE (c)-[:MENTIONS]->(e)",
                        canonical_name=canonical,
                        name=entity.get("name", canonical),
                        type=entity.get("type", "Other"),
                        chunk_id=chunk_id,
                    )

                relations = extract_relations(entities, chunk_text)
                for relation in relations:
                    session.run(
                        "MATCH (e1:Entity {canonical_name: $from_name}) "
                        "MATCH (e2:Entity {canonical_name: $to_name}) "
                        "MERGE (e1)-[r:RELATED_TO {relation_type: $relation_type, source_document_id: $doc_id}]->(e2)",
                        from_name=relation.get("from", ""),
                        to_name=relation.get("to", ""),
                        relation_type=relation.get("relation_type", "related"),
                        doc_id=req.document_id,
                    )

                stored_chunks.append(chunk_id)

            # 再取り込み時: chunk_index >= new_chunk_count の古いチャンクを Neo4j から削除
            session.run(
                "MATCH (c:Chunk) WHERE c.document_id = $document_id AND c.chunk_index >= $count "
                "DETACH DELETE c",
                document_id=req.document_id,
                count=new_chunk_count,
            )

    finally:
        driver.close()
    # 失敗時は再取り込みで回復する方針。ES は es.index() で既に上書き済みのため
    # ロールバックすると旧データも失われるため行わない。

    # 再取り込み時: ES からも古いチャンクを削除
    # Neo4j 側はすでに確定済みのため、ES 削除に失敗しても再取り込みで修復可能
    stale_chunks_removed = True
    try:
        es.delete_by_query(
            index=ES_INDEX,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"document_id": req.document_id}},
                            {"range": {"chunk_index": {"gte": new_chunk_count}}},
                        ]
                    }
                }
            },
        )
    except Exception:
        stale_chunks_removed = False
        logger.warning(
            "旧チャンクの ES 削除に失敗しました (document_id=%s)。再取り込みで解消されます。",
            req.document_id,
        )

    return {
        "document_id": req.document_id,
        "chunks_stored": len(stored_chunks),
        "chunk_ids": stored_chunks,
        "stale_chunks_removed": stale_chunks_removed,
    }


@app.post("/search", response_model=SearchResponse)
def search_post(req: SearchRequest) -> SearchResponse:
    return perform_search(req)


@app.get("/search", response_model=SearchResponse)
def search_get(
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=20),
    category: str | None = None,
    source: str | None = None,
    language: str | None = None,
) -> SearchResponse:
    return perform_search(
        SearchRequest(
            query=query,
            top_k=top_k,
            category=category,
            source=source,
            language=language,
        )
    )

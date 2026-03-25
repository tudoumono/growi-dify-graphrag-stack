"""
GraphRAG API - Elasticsearch と Neo4j を使ったハイブリッド検索 API

このファイルの大きな処理は 2 系統あります。

1. /ingest
   受け取った 1 つの文書をチャンクに分割し、Elasticsearch と Neo4j の両方へ保存する。
   検索時に速く引くための「ベクトル検索用データ」と、
   関連文脈をたどるための「グラフ構造」を同時に作る。

2. /search
   まず Elasticsearch で意味的に近いチャンクを探し、
   次に Neo4j で「同じエンティティを含む別チャンク」をたどって補助コンテキストを増やす。

将来の改修では、まず /ingest と /search の順序を頭に入れてから読むと追いやすい。
"""

from __future__ import annotations

# Python 標準ライブラリ。
# json: LLM の JSON 出力や metadata の文字列化に使う
# logging: エラーや起動情報をログに出す
# os: 環境変数から接続先やモデル設定を読む
# Any: 「いろいろな型が入る」ことを型ヒントで表す
import hashlib
import json
import logging
import os
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 外部ライブラリ。
# Elasticsearch: ベクトル検索用の ES クライアント
# FastAPI / Query / Request: API エンドポイントとリクエスト定義
# JSONResponse: エラー時の JSON レスポンス返却
# RecursiveCharacterTextSplitter: 長文を chunk に分割
# GraphDatabase: Neo4j 接続ドライバー生成
# BaseModel / Field: API の入出力データ構造を定義
from elasticsearch import Elasticsearch
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from langchain_text_splitters import RecursiveCharacterTextSplitter
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

# プロジェクト内モジュール。
# providers.py から「埋め込みモデル」と「LLM」の抽象型・生成関数を読み込む。
# 実体は Bedrock / Gemini / Ollama のいずれかだが、
# main.py 側は違いを意識せず同じ呼び方で使える。
from providers import EmbedProvider, LLMProvider, get_embed_provider, get_llm_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GraphRAG API", version="0.2.0")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # FastAPI 全体の最終エラーハンドラ。
    # 個別処理で拾えなかった例外をログに残し、API 利用側へ 500 エラーを返す。
    # 開発中に「どこで失敗したか」を追う入口にもなる。
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
INGEST_INPUT_ROOT = os.environ.get("INGEST_INPUT_ROOT", "/input")
TEMP_DOC_TTL_HOURS = int(os.environ.get("TEMP_DOC_TTL_HOURS", "24"))
GROWI_URL = os.environ.get("GROWI_URL", "")
GROWI_API_KEY = os.environ.get("GROWI_API_KEY", "")

# ジョブ管理テーブル（単一プロセス前提のインメモリ dict）
# 将来 --workers N 等の複数プロセス構成にする場合は Redis 等の外部ストアへ移行する
jobs: dict[str, dict] = {}

_embed_provider: EmbedProvider | None = None
_llm_provider: LLMProvider | None = None


def compact_dict(values: dict[str, Any]) -> dict[str, Any]:
    """None を除外して保存用の dict を作る"""
    # ES 保存時に使う整形関数。
    # None を除いておくと、不要な空項目を保存せずに済む。
    return {key: value for key, value in values.items() if value is not None}


def metadata_json(metadata: dict[str, Any]) -> str | None:
    # Neo4j では dict をそのまま扱いにくいため、metadata を JSON 文字列へ変換する。
    # metadata が空なら None を返し、「値なし」として保存する。
    if not metadata:
        return None
    return json.dumps(metadata, ensure_ascii=False)


@app.on_event("startup")
def startup() -> None:
    # 起動時の順番:
    # 1. 埋め込みモデルと LLM のプロバイダーを初期化する
    # 2. 現在の設定値（次元数、chunk サイズなど）をログに出す
    # 3. 旧スキーマ由来の不要な RELATED_TO を掃除する
    #
    # 将来、プロバイダー追加や初期化順の変更をするならこの関数から確認する。
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
    # startup() で初期化済みの埋め込みプロバイダーを返す。
    # ここで返る実体は Bedrock / Gemini / Ollama のいずれか。
    # assert は「startup 前に呼ばれていないこと」の簡易チェックで、
    # まだ初期化されていなければ即座に異常に気づけるようにしている。
    assert _embed_provider is not None
    return _embed_provider


def llm_provider() -> LLMProvider:
    # startup() で初期化済みの LLM プロバイダーを返す。
    # entity 抽出や relation 抽出は毎回この関数経由で同じ設定の LLM を使う。
    # こちらも assert により、初期化漏れを早い段階で検出する。
    assert _llm_provider is not None
    return _llm_provider


def get_es_client() -> Elasticsearch:
    # Elasticsearch に接続するクライアントを都度生成する。
    # 役割は「ベクトル検索用の本文チャンクを保存・検索すること」。
    # 接続先は環境変数 ELASTICSEARCH_HOST / PORT / USER / PASSWORD で切り替わる。
    return Elasticsearch(f"http://{ES_HOST}:{ES_PORT}", basic_auth=(ES_USER, ES_PASS))


def get_neo4j_driver():
    # Neo4j に接続するドライバーを生成する。
    # 役割は「Document / Chunk / Entity のグラフ構造を保存し、
    # 検索時に関連チャンクをたどること」。
    # ES が“近い文章を探す担当”なら、Neo4j は“つながりを広げる担当”。
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))


def ensure_es_index(es: Elasticsearch) -> None:
    if es.indices.exists(index=ES_INDEX):
        return

    # ES 側には「チャンク本文」と「ベクトル」を保存する。
    # 特に embedding.dims は埋め込みモデルと一致している必要がある。
    # ここを変えた場合は、既存インデックスを作り直して再取り込みが必要。
    #
    # category フィールドは path_hierarchy tokenizer で階層分解してインデックスする。
    # 例: "contracts/nda" → "contracts" と "contracts/nda" の両方でヒットするようになる。
    # prefix クエリ不要で term クエリのまま高速に階層検索できる。
    mapping = {
        "settings": {
            "analysis": {
                "analyzer": {
                    "path_analyzer": {
                        "tokenizer": "path_tokenizer"
                    }
                },
                "tokenizer": {
                    "path_tokenizer": {
                        "type": "path_hierarchy",
                        "delimiter": "/"
                    }
                }
            }
        },
        "mappings": {
            "properties": {
                "chunk_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "title": {"type": "text"},
                "text": {"type": "text"},
                "url": {"type": "keyword"},
                "source_ref": {"type": "keyword"},
                "chunk_index": {"type": "integer"},
                "category": {
                    "type": "text",
                    "analyzer": "path_analyzer",
                    "fields": {
                        "keyword": {"type": "keyword"}
                    }
                },
                "scope": {"type": "keyword"},
                "expires_at": {
                    "type": "date",
                    "format": "strict_date_optional_time||epoch_millis",
                },
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
    # 取り込み時の中盤ステップ:
    # 1. チャンク本文を LLM に渡す
    # 2. エンティティ一覧を JSON で返させる
    # 3. パースに失敗したら空配列にして取り込み自体は継続する
    #
    # この実装では「取り込み失敗」より「多少情報が欠けても保存完了」を優先している。
    # 品質改善したい場合は、まずこのプロンプトと JSON パース失敗率を確認する。
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
    # 関係抽出はエンティティが 2 件以上ある時だけ実行する。
    # 先にエンティティ抽出を済ませ、その結果を relation 抽出の入力に使うため、
    # ingest 内では「entity -> relation」の順番が固定。
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
    source_ref: str = ""
    text: str
    category: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # scope: "official"（input/ 正本）または "temporary"（/tmp 一時）
    scope: str = "official"
    # expires_at: temporary 時のみ設定。UTC ISO8601 文字列（例: "2026-03-25T10:00:00Z"）
    expires_at: str | None = None


class Citation(BaseModel):
    document_id: str
    chunk_id: str
    url: str
    score: float


class SearchRequest(BaseModel):
    query: str
    # scope: "official"（デフォルト）/ "temporary" / "all"
    # 期限切れは scope に関わらず常に除外される
    scope: str = "official"
    top_k: int = 5
    category: str | None = None
    source: str | None = None
    language: str | None = None


class IngestGrowiRequest(BaseModel):
    page_path: str


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


def document_properties(req: IngestRequest, content_hash: str) -> dict[str, Any]:
    # None を含めて全フィールドを返す: SET d = $props で完全置き換えするため
    # Document ノード用のプロパティを 1 箇所で組み立てる関数。
    # 将来、文書単位の属性を増やすならまずここを触る。
    return {
        "id": req.document_id,
        "title": req.title,
        "url": req.url,
        "source_ref": req.source_ref or None,
        "category": req.category,
        "source": req.source,
        "tags": req.tags or None,
        "language": req.language,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
        "metadata_json": metadata_json(req.metadata),
        "content_hash": content_hash,
        "scope": req.scope,
        "expires_at": req.expires_at,
    }


def chunk_document(req: IngestRequest) -> list[str]:
    # チャンク分割は ingest の最初の主要処理。
    # CHUNK_SIZE を大きくすると 1 チャンクの情報量は増えるが、
    # 検索の粒度は粗くなる。CHUNK_OVERLAP は文脈の切れ目を減らすための重なり。
    # 検索精度の調整で最初に触ることが多い。
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    return splitter.split_text(req.text)


def build_es_filters(req: SearchRequest) -> list[dict[str, Any]]:
    # 検索時のフィルタを ES の filter 形式へ変換する。
    # scope フィルタ: scope=all の場合は term フィルタを追加しない
    # expires_at フィルタ: scope に関わらず常に適用（期限切れは検索結果に出さない）
    filters: list[dict[str, Any]] = []
    if req.scope != "all":
        filters.append({"term": {"scope": req.scope}})
    # expires_at IS NULL OR expires_at > now
    filters.append({
        "bool": {
            "should": [
                {"bool": {"must_not": {"exists": {"field": "expires_at"}}}},
                {"range": {"expires_at": {"gt": "now"}}},
            ],
            "minimum_should_match": 1,
        }
    })
    if req.category:
        filters.append({"term": {"category": req.category}})
    if req.source:
        filters.append({"term": {"source": req.source}})
    if req.language:
        filters.append({"term": {"language": req.language}})
    return filters


def perform_search(req: SearchRequest) -> SearchResponse:
    # /search の薄いラッパー関数。
    # 接続の生成とクローズだけを担当し、実際の検索ロジックは _perform_search_inner に寄せる。
    # テスト時には inner を直接呼ぶとロジックだけ検証しやすい。
    es = get_es_client()
    driver = get_neo4j_driver()
    try:
        return _perform_search_inner(es, driver, req)
    finally:
        driver.close()


def _perform_search_inner(es: Elasticsearch, driver: Any, req: SearchRequest) -> SearchResponse:
    # /search の順番:
    # 1. クエリを embedding 化する
    # 2. Elasticsearch に kNN 検索を投げる
    # 3. 上位チャンクを seed として保持する
    # 4. seed が触れている Entity を Neo4j でたどる
    # 5. 共有 Entity を持つ別チャンクを graph_hits として集める
    # 6. ES 結果 -> グラフ結果の順で重複除去しながら merged_context を作る
    #
    # つまり検索の主役は ES、Neo4j は「検索結果を広げる補助役」。
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
            # ここでは RELATED_TO ではなく、共有エンティティ経由で関連チャンクを取っている。
            # 探索深さは実質 1 ホップ相当:
            # seed chunk -> Entity -> related chunk
            #
            # 将来「2 ホップ以上」にしたい場合はこの Cypher を拡張する。
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            result = session.run(
                """
                UNWIND $chunk_ids AS cid
                MATCH (c:Chunk {id: cid})-[:MENTIONS]->(e:Entity)
                WITH DISTINCT e LIMIT 20
                MATCH (e)<-[:MENTIONS]-(related:Chunk)
                WHERE NOT related.id IN $chunk_ids
                  AND ($scope = 'all' OR related.scope = $scope)
                  AND (related.expires_at IS NULL OR related.expires_at > $now)
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
                scope=req.scope,
                now=now_str,
                category=req.category,
                source=req.source,
                language=req.language,
                limit=req.top_k * 2,
            )
            graph_hits = [dict(record) for record in result]

    seen: set[str] = set()
    context_parts: list[str] = []
    citations: list[Citation] = []

    # merged_context は ES の直接ヒットを先に並べる。
    # その後にグラフで補った文脈を足すことで、
    # 「質問に近い本文」を先頭に置いたまま関連情報を追加できる。
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
    # コンテナや監視から呼ばれる生存確認用エンドポイント。
    # 「アプリが応答できるか」を最小限で返す。
    return {"status": "ok"}


@app.get("/providers", response_model=ProviderInfo)
def providers_info() -> ProviderInfo:
    # 現在どの埋め込みモデル / LLM / chunk 設定で動いているかを返す。
    # 動作確認や、設定ミス切り分けの確認口として使う。
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
    content_hash が一致する場合はスキップして即座に返す。

    処理の順番:
    0. content_hash を計算し、未変更なら即座にスキップして返す
    1. ES インデックスがなければ作成する
    2. 文書本文を chunk に分割する
    3. Document ノードを作成または更新する
    4. 再取り込み前提で旧 RELATED_TO を削除する
    5. 各 chunk ごとに:
       - embedding を生成して ES に保存
       - Chunk ノードを Neo4j に保存
       - MENTIONS を張り直す
       - entity を抽出する
       - relation を抽出して RELATED_TO を作る
    6. 今回の chunk 数を超える古い chunk を Neo4j から削除する
    7. 同じく古い chunk を ES から削除する

    改良時の見どころ:
    - 精度を変えたい: chunk サイズ、抽出プロンプト、検索 Cypher
    - 性能を変えたい: LLM 呼び出し回数、ES の k 値、Neo4j 探索範囲
    - 整合性を変えたい: 再取り込み時の削除順と失敗時の復旧方針
    """
    # Step 0. content_hash を計算して未変更チェックを行う
    content_hash = hashlib.sha256(req.text.encode("utf-8")).hexdigest()
    skip_driver = get_neo4j_driver()
    try:
        with skip_driver.session() as session:
            result = session.run(
                "MATCH (d:Document {id: $id}) RETURN d.content_hash AS hash",
                id=req.document_id,
            )
            record = result.single()
            if record and record["hash"] == content_hash:
                existing = session.run(
                    "MATCH (d:Document {id: $id})-[:HAS_CHUNK]->(c:Chunk) "
                    "RETURN c.id AS chunk_id ORDER BY c.chunk_index",
                    id=req.document_id,
                )
                chunk_ids = [r["chunk_id"] for r in existing]
                logger.info("スキップ (未変更): %s", req.document_id)
                return {
                    "document_id": req.document_id,
                    "chunks_stored": 0,
                    "chunk_ids": chunk_ids,
                    "skipped": True,
                    "reason": "content unchanged",
                }
    finally:
        skip_driver.close()

    es = get_es_client()
    ensure_es_index(es)
    driver = get_neo4j_driver()
    chunks = chunk_document(req)
    new_chunk_count = len(chunks)
    stored_chunks: list[str] = []
    doc_props = document_properties(req, content_hash)

    try:
        with driver.session() as session:
            # Step 1. 文書メタデータ自体を Document ノードとして保存する。
            # 文書タイトルや URL を後で graph_hits に付け直す時にも使う。
            session.run(
                "MERGE (d:Document {id: $id}) SET d = $props",
                id=req.document_id,
                props=doc_props,
            )

            # Step 2. 再取り込み前に、この文書が作った RELATED_TO だけ掃除する。
            # 他文書由来の relation まで消さないよう source_document_id で絞る。
            session.run(
                "MATCH ()-[r:RELATED_TO {source_document_id: $document_id}]->() DELETE r",
                document_id=req.document_id,
            )

            for index, chunk_text in enumerate(chunks):
                # Step 3. 1 chunk ずつ同じ順番で処理する。
                # 3-1. chunk_id を決める
                # 3-2. embedding を作る
                # 3-3. ES に保存する
                # 3-4. Neo4j の Chunk ノードを保存する
                # 3-5. MENTIONS を削除して張り直す
                # 3-6. entity を抽出して Entity ノードに結ぶ
                # 3-7. relation を抽出して Entity 間に RELATED_TO を作る
                chunk_id = f"{req.document_id}-chunk-{index}"
                embedding = embed_provider().embed(chunk_text)
                chunk_props = compact_dict(
                    {
                        "chunk_id": chunk_id,
                        "document_id": req.document_id,
                        "title": req.title,
                        "text": chunk_text,
                        "url": req.url,
                        "source_ref": req.source_ref or None,
                        "chunk_index": index,
                        "category": req.category,
                        "source": req.source,
                        "tags": req.tags or None,
                        "language": req.language,
                        "created_at": req.created_at,
                        "updated_at": req.updated_at,
                        "metadata": req.metadata or None,
                        "embedding": embedding,
                        "scope": req.scope,
                        "expires_at": req.expires_at,
                    }
                )
                es.index(index=ES_INDEX, id=chunk_id, document=chunk_props)

                # Neo4j 側は SET c = $props にして完全置き換えする。
                # これにより前回取り込み時に存在した category 等が、
                # 今回は未指定なら素直に消える。
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
                    "scope": req.scope,
                    "expires_at": req.expires_at,
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
                # 再取り込み時に entity 抽出結果が変わることがあるため、
                # 先に古い MENTIONS を消してから最新の結果を付け直す。
                session.run(
                    "MATCH (c:Chunk {id: $id})-[r:MENTIONS]->() DELETE r",
                    id=chunk_id,
                )

                entities = extract_entities(chunk_text)
                for entity in entities:
                    canonical = entity.get("canonical_name") or entity.get("name")
                    if not canonical:
                        continue
                    # entity は canonical_name を軸に名寄せする簡易実装。
                    # 同義語や表記揺れをより厳密に扱いたい場合はここを改良する。
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
                    # relation もこの chunk 由来であることを source_document_id に残す。
                    # 再取り込み時に安全に削除し直すための識別子。
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

            # Step 4. 今回の chunk 数より後ろにある古い chunk を Neo4j から削除する。
            # 例: 前回 10 chunk、今回 7 chunk なら index 7-9 が不要になる。
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

    # Step 5. ES 側も同じ条件で古い chunk を削除する。
    # ここは後処理に分けており、もし失敗しても再取り込みで回復する設計。
    # つまり「完全なトランザクション整合性」より「運用しやすい回復性」を優先している。
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
    # JSON ボディで検索したい呼び出し元向けの POST 版。
    # Dify や他アプリから API として呼ぶ時はこちらが使いやすい。
    return perform_search(req)


@app.get("/search", response_model=SearchResponse)
def search_get(
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=20),
    category: str | None = None,
    source: str | None = None,
    language: str | None = None,
) -> SearchResponse:
    # ブラウザや curl で試しやすい GET 版。
    # クエリ文字列を SearchRequest に詰め替え、内部処理は POST 版と同じにしている。
    return perform_search(
        SearchRequest(
            query=query,
            top_k=top_k,
            category=category,
            source=source,
            language=language,
        )
    )


def cleanup_expired_temp() -> None:
    """期限切れ temporary ドキュメントを ES・Neo4j・/tmp から物理削除する。
    /ingest-temp 呼び出し時に毎回実行する。
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    es = get_es_client()

    # --- 期限切れ temporary のドキュメントを ES から取得（ファイルパス参照に必要）---
    try:
        expired_resp = es.search(
            index=ES_INDEX,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"scope": "temporary"}},
                            {"range": {"expires_at": {"lte": now_str}}},
                        ]
                    }
                },
                "collapse": {"field": "document_id"},
                "_source": ["document_id", "metadata"],
                "size": 1000,
            },
        )
        expired_docs = {
            hit["_source"]["document_id"]: hit["_source"].get("metadata", {})
            for hit in expired_resp["hits"]["hits"]
        }
    except Exception as exc:
        logger.warning("cleanup_expired_temp: ES 検索エラー: %s", exc)
        return

    if not expired_docs:
        return

    # --- /tmp ファイルを削除 ---
    for doc_id, metadata in expired_docs.items():
        temp_path_str = metadata.get("temp_file_path")
        if temp_path_str:
            try:
                Path(temp_path_str).unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("cleanup_expired_temp: ファイル削除エラー %s: %s", temp_path_str, exc)

    # --- ES: 期限切れ temporary を delete_by_query ---
    try:
        es.delete_by_query(
            index=ES_INDEX,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"scope": "temporary"}},
                            {"range": {"expires_at": {"lte": now_str}}},
                        ]
                    }
                }
            },
        )
    except Exception as exc:
        logger.warning("cleanup_expired_temp: ES 削除エラー: %s", exc)

    # --- Neo4j: 期限切れ temporary の Chunk・Document を削除 ---
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            for doc_id in expired_docs:
                session.run(
                    "MATCH (c:Chunk {document_id: $id})-[r:MENTIONS]->() DELETE r",
                    id=doc_id,
                )
                session.run(
                    "MATCH (d:Document {id: $id})-[r:HAS_CHUNK]->(c:Chunk) DELETE r, c",
                    id=doc_id,
                )
                session.run(
                    "MATCH (d:Document {id: $id}) DELETE d",
                    id=doc_id,
                )
    except Exception as exc:
        logger.warning("cleanup_expired_temp: Neo4j 削除エラー: %s", exc)
    finally:
        driver.close()

    logger.info("cleanup_expired_temp: %d 件の期限切れ temporary を削除しました", len(expired_docs))


@app.post("/ingest-temp")
async def ingest_temp(file: UploadFile = File(...)) -> dict[str, Any]:
    """一時ファイルを取り込む。input/ には保存しない。TTL 後に自動削除される。

    処理の順番:
    1. 期限切れ temporary をクリーンアップ（毎回実行）
    2. /tmp/graphrag_temp/ にファイルを保存
    3. build_*_payload() で payload を生成
    4. document_id を tmp-{uuid8}-{stem} に上書き
    5. scope=temporary, expires_at=now+TTL を設定
    6. ES・Neo4j に書き込む
    """
    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".md", ".txt"}:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsupported_file_type", "allowed": [".pdf", ".md", ".txt"]},
        )

    # 期限切れ temporary をクリーンアップ
    cleanup_expired_temp()

    # /tmp/graphrag_temp/ にファイルを保存
    temp_dir = Path("/tmp/graphrag_temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    uuid8 = uuid.uuid4().hex[:8]
    temp_path = temp_dir / f"{uuid8}_{filename}"
    content = await file.read()
    temp_path.write_bytes(content)

    # build_*_payload() でペイロードを組み立てる（SystemExit はキャッチして 422 に変換）
    from ingest import build_markdown_payload, build_pdf_payload, build_txt_payload
    try:
        if ext == ".pdf":
            payload = build_pdf_payload(temp_path, temp_dir)
        elif ext == ".md":
            payload = build_markdown_payload(temp_path, temp_dir)
        else:
            payload = build_txt_payload(temp_path, temp_dir)
    except SystemExit:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail={"error": "file_parse_error", "message": "ファイルの解析に失敗しました"},
        )

    # document_id を一時ファイル用プレフィックスで上書き（build_*_payload() は変更しない）
    stem = Path(filename).stem
    payload["document_id"] = f"tmp-{uuid8}-{stem}"

    # temp_file_path を metadata に追加（DELETE 時のファイル特定に使用）
    payload.setdefault("metadata", {})["temp_file_path"] = str(temp_path)

    # scope / expires_at を設定
    expires_dt = datetime.now(timezone.utc) + timedelta(hours=TEMP_DOC_TTL_HOURS)
    payload["scope"] = "temporary"
    payload["expires_at"] = expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    result = ingest(IngestRequest(**payload))

    return {
        "status": "ok",
        "document_id": payload["document_id"],
        "scope": "temporary",
        "expires_at": payload["expires_at"],
        "skipped": result.get("skipped", False),
    }


def run_ingest_dir(job_id: str) -> None:
    """input/ ディレクトリをスキャンして各ファイルを取り込む（BackgroundTasks で実行）。

    処理の順番:
    1. INGEST_INPUT_ROOT 配下を再帰スキャン
    2. 対象拡張子（.pdf/.md/.txt）ごとに build_*_payload() を呼ぶ
    3. scope="official", expires_at=None を付与して IngestRequest 化
    4. ingest() 内部ロジックを直接呼び出す（HTTP は介さない）
    5. processed / skipped / failed をカウントしてジョブを更新
    """
    # ingest.py の build_*_payload() を利用する。
    # sys.exit(1) が残っているため SystemExit をキャッチして失敗扱いにする。
    # Phase 5a で build_*_payload() が ValueError/RuntimeError に移行後、このキャッチは削除する。
    from ingest import build_markdown_payload, build_pdf_payload, build_txt_payload

    input_root = Path(INGEST_INPUT_ROOT).resolve()
    if not input_root.exists():
        jobs[job_id].update({
            "status": "failed",
            "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "errors": [f"INGEST_INPUT_ROOT が存在しません: {input_root}"],
        })
        return

    processed = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    supported = {".pdf", ".md", ".txt"}
    all_files = sorted(f for f in input_root.rglob("*") if f.is_file())
    targets = [f for f in all_files if f.suffix.lower() in supported]

    for f in targets:
        ext = f.suffix.lower()
        try:
            if ext == ".pdf":
                payload = build_pdf_payload(f, input_root)
            elif ext == ".md":
                payload = build_markdown_payload(f, input_root)
            elif ext == ".txt":
                payload = build_txt_payload(f, input_root)
            else:
                skipped += 1
                continue

            payload["scope"] = "official"
            payload["expires_at"] = None

            result = ingest(IngestRequest(**payload))
            if result.get("skipped"):
                skipped += 1
            else:
                processed += 1
        except SystemExit:
            # build_*_payload() の sys.exit(1) をキャッチ（Phase 5a でリファクタ予定）
            failed += 1
            errors.append(f"失敗: {f.name}")
        except Exception as exc:
            failed += 1
            errors.append(f"{f.name}: {exc}")
            logger.warning("ingest-dir ファイル処理エラー: %s %s", f, exc)

        # 進捗をジョブに随時反映
        jobs[job_id].update({"processed": processed, "skipped": skipped, "failed": failed})

    jobs[job_id].update({
        "status": "done",
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    })
    logger.info("ingest-dir 完了: job_id=%s processed=%d skipped=%d failed=%d", job_id, processed, skipped, failed)


@app.post("/ingest-dir", status_code=202)
def ingest_dir_start(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """input/ ディレクトリを非同期スキャンして取り込みを開始する。

    同時実行制御: 実行中ジョブが存在する場合は 409 Conflict を返す。
    ジョブ管理: jobs dict（インメモリ）で状態を保持。再起動でリセットされる。
    """
    # 1時間以上前のジョブをクリーンアップ
    cutoff_ts = datetime.now(timezone.utc).timestamp() - 3600
    stale = [jid for jid, job in jobs.items() if job.get("created_at_ts", 0) < cutoff_ts]
    for jid in stale:
        del jobs[jid]

    # 実行中チェック → 409
    running = [jid for jid, job in jobs.items() if job["status"] == "running"]
    if running:
        raise HTTPException(
            status_code=409,
            detail={"error": "job_already_running", "running_job_id": running[0]},
        )

    job_id = str(uuid.uuid4())
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "started_at": now_str,
        "finished_at": None,
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "created_at_ts": datetime.now(timezone.utc).timestamp(),
    }
    background_tasks.add_task(run_ingest_dir, job_id)

    return {"job_id": job_id, "status": "running", "message": "ingest-dir started"}


@app.get("/ingest-job/{job_id}")
def get_ingest_job(job_id: str) -> dict[str, Any]:
    """ジョブ状態を返す。存在しない job_id は 404 を返す。"""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail={"error": "job_not_found"})
    job = jobs[job_id]
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job.get("finished_at"),
        "processed": job["processed"],
        "skipped": job["skipped"],
        "failed": job["failed"],
        "errors": job.get("errors", []),
    }


@app.delete("/documents/{document_id}")
def delete_document(document_id: str) -> dict[str, Any]:
    """ドキュメントを ES・Neo4j から削除する。scope × source で挙動が異なる。

    - official file: ES/Neo4j のみ削除（:ro マウントのためファイル削除は Phase 5c）
    - official growi: ES/Neo4j のみ削除（Growi 本体が正本のためファイル削除しない）
    - temporary: ES/Neo4j に加えて /tmp の一時ファイルも削除
    """
    es = get_es_client()

    # ドキュメント存在確認（ES から scope・source・metadata を取得）
    try:
        resp = es.search(
            index=ES_INDEX,
            body={
                "query": {"term": {"document_id": document_id}},
                "collapse": {"field": "document_id"},
                "_source": ["document_id", "scope", "source", "metadata"],
                "size": 1,
            },
        )
        es_hit = resp["hits"]["hits"][0]["_source"] if resp["hits"]["hits"] else None
    except Exception:
        es_hit = None

    # Neo4j でも存在確認
    neo4j_exists = False
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (d:Document {id: $id}) RETURN d.id LIMIT 1",
                id=document_id,
            )
            neo4j_exists = result.single() is not None
    except Exception:
        pass
    finally:
        driver.close()

    if es_hit is None and not neo4j_exists:
        raise HTTPException(status_code=404, detail={"error": "document_not_found"})

    scope = (es_hit or {}).get("scope", "official")
    metadata = (es_hit or {}).get("metadata", {})
    file_deleted = False

    # temporary の場合: /tmp のファイルを削除
    if scope == "temporary":
        temp_path_str = metadata.get("temp_file_path")
        if temp_path_str:
            try:
                Path(temp_path_str).unlink(missing_ok=True)
                file_deleted = True
            except Exception as exc:
                logger.warning("DELETE: 一時ファイル削除エラー %s: %s", temp_path_str, exc)

    # ES から削除
    try:
        es.delete_by_query(
            index=ES_INDEX,
            body={"query": {"term": {"document_id": document_id}}},
        )
    except Exception as exc:
        logger.warning("DELETE: ES 削除エラー %s: %s", document_id, exc)

    # Neo4j から削除（3ステップ: MENTIONS → HAS_CHUNK+Chunk → Document）
    # Entity ノードは他ドキュメントから参照されている可能性があるため削除しない
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            session.run(
                "MATCH (c:Chunk {document_id: $id})-[r:MENTIONS]->() DELETE r",
                id=document_id,
            )
            session.run(
                "MATCH (d:Document {id: $id})-[r:HAS_CHUNK]->(c:Chunk) DELETE r, c",
                id=document_id,
            )
            session.run(
                "MATCH (d:Document {id: $id}) DELETE d",
                id=document_id,
            )
    except Exception as exc:
        logger.warning("DELETE: Neo4j 削除エラー %s: %s", document_id, exc)
    finally:
        driver.close()

    return {
        "status": "ok",
        "document_id": document_id,
        "scope": scope,
        "file_deleted": file_deleted,
        "git_committed": False,
    }


@app.post("/documents/{document_id}/reingest")
def reingest_document(document_id: str) -> dict[str, Any]:
    """mismatch 状態のドキュメントを再取り込みして整合性を修復する。

    scope × source による 3 ケース分岐:
    - official file: source_ref から input/ のファイルを再取り込み
    - official growi: metadata.growi_page_id を使って Growi API から再取得
    - temporary: metadata.temp_file_path でファイルの存在確認 → 再取り込み or 409
    """
    from ingest import build_growi_payload, build_markdown_payload, build_pdf_payload, build_txt_payload

    es = get_es_client()

    # ES からドキュメント情報を取得
    try:
        resp = es.search(
            index=ES_INDEX,
            body={
                "query": {"term": {"document_id": document_id}},
                "collapse": {"field": "document_id"},
                "_source": ["document_id", "scope", "source", "source_ref", "metadata"],
                "size": 1,
            },
        )
        es_src = resp["hits"]["hits"][0]["_source"] if resp["hits"]["hits"] else None
    except Exception:
        es_src = None

    # ES になければ Neo4j から取得
    if es_src is None:
        driver = get_neo4j_driver()
        try:
            with driver.session() as session:
                result = session.run(
                    "MATCH (d:Document {id: $id}) "
                    "RETURN d.scope AS scope, d.source AS source, d.source_ref AS source_ref LIMIT 1",
                    id=document_id,
                )
                record = result.single()
        finally:
            driver.close()
        if not record:
            raise HTTPException(status_code=404, detail={"error": "document_not_found"})
        scope = record["scope"] or "official"
        source = record["source"]
        source_ref = record["source_ref"]
        metadata: dict[str, Any] = {}
    else:
        scope = es_src.get("scope", "official")
        source = es_src.get("source")
        source_ref = es_src.get("source_ref")
        metadata = es_src.get("metadata", {})

    # --- temporary: temp_file_path でファイル存在確認 ---
    if scope == "temporary":
        temp_path_str = metadata.get("temp_file_path")
        if not temp_path_str or not Path(temp_path_str).exists():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "temp_file_expired",
                    "message": "一時ファイルは既に削除されています。再アップロードしてください",
                },
            )
        temp_path = Path(temp_path_str)
        ext = temp_path.suffix.lower()
        try:
            if ext == ".pdf":
                payload = build_pdf_payload(temp_path, temp_path.parent)
            elif ext == ".md":
                payload = build_markdown_payload(temp_path, temp_path.parent)
            else:
                payload = build_txt_payload(temp_path, temp_path.parent)
        except SystemExit:
            raise HTTPException(status_code=422, detail={"error": "file_parse_error"})
        payload["document_id"] = document_id
        payload.setdefault("metadata", {})["temp_file_path"] = temp_path_str
        expires_dt = datetime.now(timezone.utc) + timedelta(hours=TEMP_DOC_TTL_HOURS)
        payload["scope"] = "temporary"
        payload["expires_at"] = expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- official growi: metadata.growi_page_id を使用 ---
    elif source == "growi":
        if not GROWI_URL or not GROWI_API_KEY:
            raise HTTPException(status_code=503, detail={"error": "growi_not_configured"})
        page_id = metadata.get("growi_page_id")
        if not page_id:
            raise HTTPException(
                status_code=422,
                detail={"error": "growi_page_id_not_found_in_metadata"},
            )
        try:
            payload = build_growi_payload(GROWI_URL, page_id, GROWI_API_KEY)
        except Exception as exc:
            raise HTTPException(status_code=500, detail={"error": "growi_api_error", "detail": str(exc)})
        payload.setdefault("metadata", {})["growi_page_id"] = page_id
        payload["scope"] = "official"
        payload["expires_at"] = None

    # --- official file: source_ref から input/ のファイルを読む ---
    else:
        if not source_ref:
            raise HTTPException(status_code=422, detail={"error": "source_ref_not_found"})
        input_root = Path(INGEST_INPUT_ROOT).resolve()
        file_path = (input_root / source_ref).resolve()
        # パストラバーサル防止
        try:
            file_path.relative_to(input_root)
        except ValueError:
            raise HTTPException(status_code=400, detail={"error": "invalid_source_ref"})
        if not file_path.exists():
            raise HTTPException(
                status_code=422,
                detail={"error": "source_file_not_found", "path": source_ref},
            )
        ext = file_path.suffix.lower()
        try:
            if ext == ".pdf":
                payload = build_pdf_payload(file_path, input_root)
            elif ext == ".md":
                payload = build_markdown_payload(file_path, input_root)
            else:
                payload = build_txt_payload(file_path, input_root)
        except SystemExit:
            raise HTTPException(status_code=422, detail={"error": "file_parse_error"})
        payload["scope"] = "official"
        payload["expires_at"] = None

    ingest(IngestRequest(**payload))
    return {"status": "ok", "document_id": document_id, "scope": scope}


@app.post("/ingest-growi")
def ingest_growi_endpoint(req: IngestGrowiRequest) -> dict[str, Any]:
    """Growi のページパスを受け取り、page_id に解決して official として取り込む。

    処理の順番:
    1. GROWI_URL / GROWI_API_KEY の確認（未設定なら 503）
    2. Growi API で page_path → page_id に解決（見つからなければ 404）
    3. build_growi_payload() でペイロードを組み立て
    4. metadata["growi_page_id"] を追加（/reingest 時の再取得に使用）
    5. scope=official, expires_at=null で ES・Neo4j に書き込む
    """
    import urllib.error
    import urllib.request as urlreq

    from ingest import build_growi_payload

    if not GROWI_URL or not GROWI_API_KEY:
        raise HTTPException(status_code=503, detail={"error": "growi_not_configured"})

    # page_path → page_id を解決（Growi API: GET /_api/v3/pages?path=<path>）
    encoded_path = urllib.parse.quote(req.page_path)
    endpoint = f"{GROWI_URL.rstrip('/')}/_api/v3/pages?path={encoded_path}"
    api_req = urlreq.Request(endpoint, headers={"Authorization": f"Bearer {GROWI_API_KEY}"})
    try:
        with urlreq.urlopen(api_req) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(status_code=404, detail={"error": "growi_page_not_found"})
        raise HTTPException(status_code=500, detail={"error": "growi_api_error", "detail": str(e)})
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "growi_connection_error", "detail": str(e)})

    # レスポンス構造: {"page": {...}} または {"pages": [...]}
    page = data.get("page") or (data.get("pages") or [{}])[0]
    page_id = str(page.get("_id") or page.get("id", ""))
    if not page_id:
        raise HTTPException(status_code=404, detail={"error": "growi_page_not_found"})

    try:
        payload = build_growi_payload(GROWI_URL, page_id, GROWI_API_KEY)
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": "growi_api_error", "detail": str(exc)})

    # growi_page_id を metadata に保存（/reingest 時に source_ref 解析の代わりに使用）
    payload.setdefault("metadata", {})["growi_page_id"] = page_id
    payload["scope"] = "official"
    payload["expires_at"] = None

    result = ingest(IngestRequest(**payload))

    return {
        "status": "ok",
        "document_id": payload["document_id"],
        "page_id": page_id,
        "scope": "official",
        "skipped": result.get("skipped", False),
    }


@app.get("/documents")
def list_documents(
    scope: str = Query("official"),
) -> list[dict[str, Any]]:
    """ES と Neo4j の両方を突合してドキュメント一覧を返す。

    scope パラメータ: "official" / "temporary" / "all"
    期限切れ temporary は常に除外（クリーンアップ前でも同様）。
    status: "ok"（両方に存在）/ "mismatch"（どちらか片方のみ）
    """
    if scope not in ("official", "temporary", "all"):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_scope", "allowed": ["official", "temporary", "all"]},
        )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- ES: document_id を collapse で 1件/document に絞り込む ---
    es = get_es_client()
    ensure_es_index(es)

    # expires_at IS NULL OR expires_at > now を常に適用
    es_filters: list[dict[str, Any]] = [
        {
            "bool": {
                "should": [
                    {"bool": {"must_not": {"exists": {"field": "expires_at"}}}},
                    {"range": {"expires_at": {"gt": now_str}}},
                ],
                "minimum_should_match": 1,
            }
        }
    ]
    if scope != "all":
        es_filters.append({"term": {"scope": scope}})

    es_resp = es.search(
        index=ES_INDEX,
        body={
            "query": {"bool": {"filter": es_filters}},
            "collapse": {"field": "document_id"},
            "_source": ["document_id", "scope", "expires_at", "source_ref", "category"],
            "size": 1000,
        },
    )

    es_docs: dict[str, dict[str, Any]] = {}
    for hit in es_resp["hits"]["hits"]:
        src = hit["_source"]
        doc_id = src["document_id"]
        es_docs[doc_id] = {
            "document_id": doc_id,
            "source_ref": src.get("source_ref"),
            "category": src.get("category"),
            "scope": src.get("scope", "official"),
            "expires_at": src.get("expires_at"),
        }

    # --- Neo4j: Document ノードを scope + expires_at フィルタ付きで取得 ---
    neo4j_doc_ids: set[str] = set()
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (d:Document)
                WHERE ($scope = 'all' OR d.scope = $scope)
                  AND (d.expires_at IS NULL OR d.expires_at > $now)
                RETURN d.id AS document_id
                """,
                scope=scope,
                now=now_str,
            )
            neo4j_doc_ids = {record["document_id"] for record in result}
    finally:
        driver.close()

    # --- 突合して status を付与 ---
    all_doc_ids = set(es_docs.keys()) | neo4j_doc_ids
    result_list: list[dict[str, Any]] = []
    for doc_id in sorted(all_doc_ids):
        in_es = doc_id in es_docs
        in_neo4j = doc_id in neo4j_doc_ids
        status = "ok" if (in_es and in_neo4j) else "mismatch"

        if in_es:
            meta = es_docs[doc_id]
        else:
            # Neo4j にしか存在しない場合: document_id のみ、他フィールドは null
            meta = {"document_id": doc_id, "source_ref": None, "category": None, "scope": None, "expires_at": None}

        result_list.append({**meta, "in_es": in_es, "in_neo4j": in_neo4j, "status": status})

    return result_list

"""
埋め込み（Embed）と LLM のプロバイダー抽象レイヤー。

環境変数で切り替え可能:
  EMBED_PROVIDER=bedrock  -> Amazon Bedrock Titan Embed v2 (1024次元)
  EMBED_PROVIDER=ollama   -> Ollama nomic-embed-text 等 (768次元など)
  EMBED_PROVIDER=gemini   -> Google Gemini text-embedding-004 (768次元)

  LLM_PROVIDER=bedrock    -> Amazon Bedrock Claude Haiku
  LLM_PROVIDER=ollama     -> Ollama llama3 等
  LLM_PROVIDER=gemini     -> Google Gemini 2.0 Flash

注意:
  Embed プロバイダーを切り替えると ES インデックスの次元数が変わるため、
  インデックスの再作成とドキュメントの再取り込みが必要。
"""

from __future__ import annotations

import abc
import json
import os
import time

import boto3
import httpx
from google import genai
from google.genai import types as genai_types


class EmbedProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def dims(self) -> int:
        """ベクトル次元数を返す"""

    @abc.abstractmethod
    def embed(self, text: str) -> list[float]:
        """テキストをベクトル化する"""


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    def generate(self, prompt: str) -> str:
        """プロンプトを受け取りテキストを生成する"""


class BedrockEmbedProvider(EmbedProvider):
    def __init__(self, model: str, region: str) -> None:
        self._model = model
        self._client = boto3.client("bedrock-runtime", region_name=region)

    @property
    def dims(self) -> int:
        return 1024

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text, "dimensions": 1024, "normalize": True})
        response = self._client.invoke_model(
            modelId=self._model,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(response["body"].read())["embedding"]


class BedrockLLMProvider(LLMProvider):
    def __init__(self, model: str, region: str) -> None:
        self._model = model
        self._client = boto3.client("bedrock-runtime", region_name=region)

    def generate(self, prompt: str) -> str:
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        response = self._client.invoke_model(
            modelId=self._model,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"].strip()


class GeminiEmbedProvider(EmbedProvider):
    def __init__(self, api_key: str, model: str = "gemini-embedding-001") -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(api_version="v1beta"),
        )
        self._model = model

    @property
    def dims(self) -> int:
        return 3072

    def embed(self, text: str) -> list[float]:
        for attempt in range(5):
            try:
                result = self._client.models.embed_content(model=self._model, contents=text)
                return result.embeddings[0].values
            except Exception as e:
                if attempt == 4:
                    raise
                wait = 2 ** attempt  # 1, 2, 4, 8秒
                time.sleep(wait)
        raise RuntimeError("embed: 最大リトライ回数に達しました")


class GeminiLLMProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(api_version="v1beta"),
        )
        self._model = model

    def generate(self, prompt: str) -> str:
        for attempt in range(5):
            try:
                response = self._client.models.generate_content(model=self._model, contents=prompt)
                return response.text.strip()
            except Exception as e:
                if attempt == 4:
                    raise
                wait = 2 ** attempt
                time.sleep(wait)
        raise RuntimeError("generate: 最大リトライ回数に達しました")


class OllamaEmbedProvider(EmbedProvider):
    def __init__(self, model: str, url: str, dims: int) -> None:
        self._model = model
        self._url = url.rstrip("/")
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, text: str) -> list[float]:
        response = httpx.post(
            f"{self._url}/api/embeddings",
            json={"model": self._model, "prompt": text},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()["embedding"]


class OllamaLLMProvider(LLMProvider):
    def __init__(self, model: str, url: str) -> None:
        self._model = model
        self._url = url.rstrip("/")

    def generate(self, prompt: str) -> str:
        response = httpx.post(
            f"{self._url}/api/generate",
            json={"model": self._model, "prompt": prompt, "stream": False},
            timeout=120.0,
        )
        response.raise_for_status()
        return response.json()["response"].strip()


def get_embed_provider() -> EmbedProvider:
    """EMBED_PROVIDER 環境変数に応じたプロバイダーを返す"""
    provider = os.environ.get("EMBED_PROVIDER", "bedrock")

    if provider == "gemini":
        return GeminiEmbedProvider(
            api_key=os.environ["GEMINI_API_KEY"],
            model=os.environ.get("GEMINI_EMBED_MODEL", "text-embedding-004"),
        )

    if provider == "ollama":
        return OllamaEmbedProvider(
            model=os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
            url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"),
            dims=int(os.environ.get("EMBED_DIMS", "768")),
        )

    return BedrockEmbedProvider(
        model=os.environ.get("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0"),
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )


def get_llm_provider() -> LLMProvider:
    """LLM_PROVIDER 環境変数に応じたプロバイダーを返す"""
    provider = os.environ.get("LLM_PROVIDER", "bedrock")

    if provider == "gemini":
        return GeminiLLMProvider(
            api_key=os.environ["GEMINI_API_KEY"],
            model=os.environ.get("GEMINI_LLM_MODEL", "gemini-2.0-flash"),
        )

    if provider == "ollama":
        return OllamaLLMProvider(
            model=os.environ.get("OLLAMA_LLM_MODEL", "llama3"),
            url=os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"),
        )

    return BedrockLLMProvider(
        model=os.environ.get(
            "BEDROCK_LLM_MODEL",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ),
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )

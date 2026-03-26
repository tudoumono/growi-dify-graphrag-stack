"""
ドキュメント取り込みユーティリティ（ライブラリモジュール）

Phase 5c で CLI インターフェースを廃止し、main.py から直接呼び出す形に変更。

移行先:
  ingest.py pdf/md/txt  →  POST /ingest-temp（一時）または input/ に置いて POST /ingest-dir
  ingest.py growi       →  POST /ingest-growi
  ingest.py input-dir   →  POST /ingest-dir（Phase 4 で移行済み）

公開関数:
  build_growi_payload()   : Growi API からページを取得して payload dict を返す
  build_pdf_payload()     : PDF ファイルを読んで payload dict を返す
  build_markdown_payload(): Markdown ファイルを読んで payload dict を返す
  build_txt_payload()     : テキストファイルを読んで payload dict を返す
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any


def extract_pdf_text(pdf_path: str) -> str:
    """PDF ファイルからテキストを抽出する。

    Raises:
        ImportError: pdfplumber がインストールされていない場合
        RuntimeError: テキストを抽出できなかった場合（スキャン PDF 等）
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError(
            "pdfplumber が見つかりません。pip install pdfplumber を実行してください。"
        ) from exc

    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                texts.append(text)
    return "\n\n".join(texts)


def build_growi_payload(growi_url: str, page_id: str, api_key: str) -> dict[str, Any]:
    """Growi REST API からページ情報を取得して payload dict を返す。送信はしない。

    Raises:
        RuntimeError: Growi API への接続失敗またはレスポンス解析失敗
    """
    endpoint = f"{growi_url.rstrip('/')}/_api/v3/page?pageId={page_id}"
    req = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req) as res:
        data = json.loads(res.read())

    page = data.get("page", {})
    page_path = page.get("path", "")
    return {
        "document_id": f"growi-{page_id}",
        "title": page_path or f"page-{page_id}",
        "url": f"{growi_url.rstrip('/')}{page_path}",
        "source_ref": f"growi-{page_id}",
        "text": page.get("revision", {}).get("body", ""),
        "source": "growi",
    }


def build_pdf_payload(
    pdf_path: Path,
    input_root: Path,
    title: str | None = None,
    category: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """PDF ファイルを読んで payload dict を返す。送信はしない。

    Raises:
        ValueError: ファイルが input_root の外にある場合
        RuntimeError: テキストを抽出できなかった場合
    """
    pdf_path = pdf_path.resolve()

    try:
        relative = pdf_path.relative_to(input_root)
    except ValueError as exc:
        raise ValueError(
            f"ファイルが入力ルート ({input_root}) の外にあります: {pdf_path}"
        ) from exc

    stem_path = str(relative.with_suffix("")).replace("/", "_").replace("\\", "_")
    document_id = f"pdf-{stem_path}"

    text = extract_pdf_text(str(pdf_path))
    if not text.strip():
        raise RuntimeError(
            "テキストを抽出できませんでした。スキャン PDF の場合は OCR が必要です。"
        )

    dir_str = str(relative.parent).replace("\\", "/")
    if dir_str == ".":
        dir_str = ""
    auto_metadata: dict[str, Any] = {
        "source_type": "pdf",
        "filename": pdf_path.name,
        "dir": dir_str,
        "path": str(relative).replace("\\", "/"),
    }

    resolved_category = category or dir_str

    payload: dict[str, Any] = {
        "document_id": document_id,
        "title": title or pdf_path.stem,
        "url": "",
        "source_ref": str(relative).replace("\\", "/"),
        "text": text,
        "source": "pdf",
        "category": resolved_category or None,
        "metadata": auto_metadata,
    }
    if language:
        payload["language"] = language

    return payload


def build_markdown_payload(
    md_path: Path,
    input_root: Path,
    title: str | None = None,
    category: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Markdown ファイルを読んで payload dict を返す。送信はしない。

    Raises:
        ValueError: ファイルが input_root の外にある場合
        RuntimeError: ファイルが空の場合
    """
    md_path = md_path.resolve()

    try:
        relative = md_path.relative_to(input_root)
    except ValueError as exc:
        raise ValueError(
            f"ファイルが入力ルート ({input_root}) の外にあります: {md_path}"
        ) from exc

    stem_path = str(relative.with_suffix("")).replace("/", "_").replace("\\", "_")
    document_id = f"md-{stem_path}"

    text = md_path.read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"テキストが空です: {md_path}")

    dir_str = str(relative.parent).replace("\\", "/")
    if dir_str == ".":
        dir_str = ""
    auto_metadata: dict[str, Any] = {
        "source_type": "markdown",
        "filename": md_path.name,
        "dir": dir_str,
        "path": str(relative).replace("\\", "/"),
    }

    resolved_category = category or dir_str

    payload: dict[str, Any] = {
        "document_id": document_id,
        "title": title or md_path.stem,
        "url": "",
        "source_ref": str(relative).replace("\\", "/"),
        "text": text,
        "source": "markdown",
        "category": resolved_category or None,
        "metadata": auto_metadata,
    }
    if language:
        payload["language"] = language

    return payload


def build_txt_payload(
    txt_path: Path,
    input_root: Path,
    title: str | None = None,
    category: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """テキストファイルを読んで payload dict を返す。送信はしない。

    Raises:
        ValueError: ファイルが input_root の外にある場合
        RuntimeError: ファイルが空の場合
    """
    txt_path = txt_path.resolve()

    try:
        relative = txt_path.relative_to(input_root)
    except ValueError as exc:
        raise ValueError(
            f"ファイルが入力ルート ({input_root}) の外にあります: {txt_path}"
        ) from exc

    stem_path = str(relative.with_suffix("")).replace("/", "_").replace("\\", "_")
    document_id = f"txt-{stem_path}"

    text = txt_path.read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"テキストが空です: {txt_path}")

    dir_str = str(relative.parent).replace("\\", "/")
    if dir_str == ".":
        dir_str = ""
    auto_metadata: dict[str, Any] = {
        "source_type": "txt",
        "filename": txt_path.name,
        "dir": dir_str,
        "path": str(relative).replace("\\", "/"),
    }

    resolved_category = category or dir_str

    payload: dict[str, Any] = {
        "document_id": document_id,
        "title": title or txt_path.stem,
        "url": "",
        "source_ref": str(relative).replace("\\", "/"),
        "text": text,
        "source": "txt",
        "category": resolved_category or None,
        "metadata": auto_metadata,
    }
    if language:
        payload["language"] = language

    return payload

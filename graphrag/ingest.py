"""
ドキュメント取り込み CLI

使い方（Growi ページ）:
  python ingest.py growi --url http://localhost:3300 --page-id 12345 --api-key YOUR_KEY
  python ingest.py growi --url http://localhost:3300 --page-id 12345 --api-key YOUR_KEY \
                         --category 技術文書

使い方（PDF ファイル）:
  python ingest.py pdf --file /path/to/document.pdf
  python ingest.py pdf --file /path/to/document.pdf --category 技術文書 --title "任意のタイトル"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any


def fetch_growi_page(growi_url: str, page_id: str, api_key: str) -> dict[str, Any]:
    """Growi REST API からページ情報を取得する"""
    endpoint = f"{growi_url.rstrip('/')}/_api/v3/page?pageId={page_id}"
    req = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req) as res:
        data = json.loads(res.read())

    page = data.get("page", {})
    return {
        "document_id": f"growi-{page_id}",
        "title": page.get("path", f"page-{page_id}"),
        "url": f"{growi_url.rstrip('/')}{page.get('path', '')}",
        "text": page.get("revision", {}).get("body", ""),
        "source": "growi",
    }


def extract_pdf_text(pdf_path: str) -> str:
    """PDF ファイルからテキストを抽出する"""
    try:
        import pdfplumber
    except ImportError:
        print("pdfplumber が見つかりません。pip install pdfplumber を実行してください。", file=sys.stderr)
        sys.exit(1)

    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                texts.append(text)
    return "\n\n".join(texts)


def post_to_graphrag_api(graphrag_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """GraphRAG API の /ingest エンドポイントに送信する"""
    import urllib.error
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{graphrag_url.rstrip('/')}/ingest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}\n{detail}") from e


def send_and_print(graphrag_url: str, payload: dict[str, Any]) -> None:
    """payload を GraphRAG API に送信して結果を表示する"""
    print(f"  タイトル : {payload['title']}")
    print(f"  文字数   : {len(payload['text'])} 文字")
    if payload.get("category"):
        print(f"  カテゴリ : {payload['category']}")
    print(f"GraphRAG API ({graphrag_url}) に送信中...")

    try:
        result = post_to_graphrag_api(graphrag_url, payload)
    except RuntimeError as exc:
        print("", file=sys.stderr)
        print("=== GraphRAG API エラー ===", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("===========================", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n[エラー] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"完了: {result['chunks_stored']} チャンクを格納しました")
    for chunk_id in result["chunk_ids"]:
        print(f"  - {chunk_id}")


def cmd_growi(args: argparse.Namespace) -> None:
    print(f"Growi ページ {args.page_id} を取得中...")
    try:
        payload = fetch_growi_page(args.url, args.page_id, args.api_key)
        if args.category:
            payload["category"] = args.category
        if args.language:
            payload["language"] = args.language
    except Exception as exc:
        print(f"Growi からのページ取得に失敗しました: {exc}", file=sys.stderr)
        sys.exit(1)

    send_and_print(args.graphrag_url, payload)


def cmd_pdf(args: argparse.Namespace) -> None:
    pdf_path = Path(args.file)
    if not pdf_path.exists():
        print(f"ファイルが見つかりません: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"PDF を読み込み中: {pdf_path} ...")
    text = extract_pdf_text(str(pdf_path))
    if not text.strip():
        print("テキストを抽出できませんでした。スキャン PDF の場合は OCR が必要です。", file=sys.stderr)
        sys.exit(1)

    title = args.title or pdf_path.stem
    document_id = f"pdf-{pdf_path.stem}"

    payload: dict[str, Any] = {
        "document_id": document_id,
        "title": title,
        "url": pdf_path.name,
        "text": text,
        "source": "pdf",
    }
    if args.category:
        payload["category"] = args.category
    if args.language:
        payload["language"] = args.language

    send_and_print(args.graphrag_url, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="ドキュメントを GraphRAG に取り込む")
    parser.add_argument(
        "--graphrag-url",
        default="http://localhost:8080",
        help="GraphRAG API の URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # growi サブコマンド
    growi_parser = subparsers.add_parser("growi", help="Growi ページを取り込む")
    growi_parser.add_argument("--url", required=True, help="Growi の URL (例: http://localhost:3300)")
    growi_parser.add_argument("--page-id", required=True, help="Growi のページ ID")
    growi_parser.add_argument("--api-key", required=True, help="Growi の API キー")
    growi_parser.add_argument("--category", help="GraphRAG 側のカテゴリ")
    growi_parser.add_argument("--language", help="ドキュメント言語 (例: ja)")

    # pdf サブコマンド
    pdf_parser = subparsers.add_parser("pdf", help="PDF ファイルを取り込む")
    pdf_parser.add_argument("--file", required=True, help="PDF ファイルのパス")
    pdf_parser.add_argument("--title", help="ドキュメントのタイトル（省略時はファイル名）")
    pdf_parser.add_argument("--category", help="GraphRAG 側のカテゴリ")
    pdf_parser.add_argument("--language", help="ドキュメント言語 (例: ja)")

    args = parser.parse_args()

    if args.command == "growi":
        cmd_growi(args)
    elif args.command == "pdf":
        cmd_pdf(args)


if __name__ == "__main__":
    main()

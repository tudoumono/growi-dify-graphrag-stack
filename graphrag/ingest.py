"""
ドキュメント取り込み CLI

使い方（Growi ページ）:
  python ingest.py growi --url http://localhost:3300 --page-id 12345 --api-key YOUR_KEY
  python ingest.py growi --url http://localhost:3300 --page-id 12345 --api-key YOUR_KEY \
                         --category 技術文書

使い方（PDF ファイル）:
  python ingest.py pdf --file /path/to/document.pdf
  python ingest.py pdf --file /path/to/document.pdf --category 技術文書 --title "任意のタイトル"

このファイルは「投入前の入口」を担当する。

- growi サブコマンド:
  1. Growi API からページ本文を取得
  2. GraphRAG API /ingest に送信

- pdf サブコマンド:
  1. PDF ファイルを読み込む
  2. ページごとの文字列を抽出
  3. GraphRAG API /ingest に送信

GraphRAG 本体の保存ロジックは main.py 側にあり、このファイルは
「どこから本文を集め、どんな JSON に組み立てて API に渡すか」を読む場所。

環境変数:
  INGEST_INPUT_ROOT: 取り込みファイルのルートディレクトリ（必須）
    document_id はこのディレクトリからの相対パスをもとに生成される。
    例: INGEST_INPUT_ROOT=/workspace/input
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any



def get_input_root() -> Path:
    """環境変数 INGEST_INPUT_ROOT を読んで Path を返す。未設定ならエラー終了。"""
    root = os.environ.get("INGEST_INPUT_ROOT")
    if not root:
        print(
            "環境変数 INGEST_INPUT_ROOT が設定されていません。\n"
            "例: export INGEST_INPUT_ROOT=/workspace/input",
            file=sys.stderr,
        )
        sys.exit(1)
    return Path(root).resolve()


def build_growi_payload(growi_url: str, page_id: str, api_key: str) -> dict[str, Any]:
    """Growi REST API からページ情報を取得して payload dict を返す。送信はしない。

    Growi 取り込みの順番:
    1. pageId 付きで Growi REST API を呼ぶ
    2. レスポンスから path / revision.body を読む
    3. GraphRAG /ingest が受け取れる payload 形式にそろえる
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


def extract_pdf_text(pdf_path: str) -> str:
    """PDF ファイルからテキストを抽出する"""
    # PDF 取り込みでは最初に本文テキストを平文化する。
    # ここでは OCR は行わないため、画像ベースの PDF は抽出できない。
    # OCR 対応を追加したい場合の改修起点はこの関数。
    try:
        import pdfplumber
    except ImportError:
        print("pdfplumber が見つかりません。pip install pdfplumber を実行してください。", file=sys.stderr)
        sys.exit(1)

    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # ページ順に連結することで、後続の chunk 分割も元の文書順を保てる。
            text = page.extract_text()
            if text:
                texts.append(text)
    return "\n\n".join(texts)


def post_to_graphrag_api(graphrag_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """GraphRAG API の /ingest エンドポイントに送信する"""
    import urllib.error

    # main.py の /ingest は JSON を前提にしているため、
    # ここで payload をそのまま POST する。
    # 送る項目は document_id / title / url / text / source など。
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
    # CLI 実行時の見え方:
    # 1. 何を送るか表示する
    # 2. /ingest に送る
    # 3. 成功なら chunk 数と chunk_id を表示する
    # 4. 失敗なら HTTP エラー本文まで表示する
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
    # Growi 取り込みの全体順:
    # 1. Growi から本文を取得
    # 2. 任意の付加情報（category / language）を payload に追加
    # 3. GraphRAG API に送る
    print(f"Growi ページ {args.page_id} を取得中...")
    try:
        payload = build_growi_payload(args.url, args.page_id, args.api_key)
        if args.category:
            payload["category"] = args.category
        if args.language:
            payload["language"] = args.language
    except Exception as exc:
        print(f"Growi からのページ取得に失敗しました: {exc}", file=sys.stderr)
        sys.exit(1)

    send_and_print(args.graphrag_url, payload)


def build_pdf_payload(
    pdf_path: Path,
    input_root: Path,
    title: str | None = None,
    category: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """PDF ファイルを読んで payload dict を返す。送信はしない。

    PDF 取り込みの順番:
    1. input_root からの相対パスを求める
    2. 相対パスをもとに document_id を生成する
    3. PDF から本文テキストを抽出する
    4. payload を組み立てて返す
    """
    pdf_path = pdf_path.resolve()

    # input_root の外にあるファイルは受け付けない
    try:
        relative = pdf_path.relative_to(input_root)
    except ValueError:
        print(
            f"ファイルが INGEST_INPUT_ROOT ({input_root}) の外にあります: {pdf_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    # パス区切り "/" を "_" に変換して document_id を生成する
    # 例: contracts/nda/sample.pdf → "pdf-contracts_nda_sample"
    stem_path = str(relative.with_suffix("")).replace("/", "_").replace("\\", "_")
    document_id = f"pdf-{stem_path}"

    print(f"PDF を読み込み中: {pdf_path} ...")
    text = extract_pdf_text(str(pdf_path))
    if not text.strip():
        print("テキストを抽出できませんでした。スキャン PDF の場合は OCR が必要です。", file=sys.stderr)
        sys.exit(1)

    # フォルダ構造情報を metadata に自動付与する
    dir_str = str(relative.parent).replace("\\", "/")
    if dir_str == ".":
        dir_str = ""
    auto_metadata: dict[str, Any] = {
        "source_type": "pdf",
        "filename": pdf_path.name,
        "dir": dir_str,
        "path": str(relative).replace("\\", "/"),
    }

    # カテゴリ: 明示指定があれば優先、なければ親ディレクトリパスを自動推定
    resolved_category = category or dir_str

    # payload は API 契約そのもの。
    # PDF 以外の入力元を増やす場合も、この形に合わせれば main.py 側は再利用できる。
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

    将来の拡張ポイント:
    - フロントマター（--- で囲まれた YAML）から title / category / tags を自動抽出
    - # ヘッダー構造をチャンク境界のヒントとして使う
    """
    md_path = md_path.resolve()

    try:
        relative = md_path.relative_to(input_root)
    except ValueError:
        print(
            f"ファイルが INGEST_INPUT_ROOT ({input_root}) の外にあります: {md_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    stem_path = str(relative.with_suffix("")).replace("/", "_").replace("\\", "_")
    document_id = f"md-{stem_path}"

    print(f"Markdown を読み込み中: {md_path} ...")
    text = md_path.read_text(encoding="utf-8")
    if not text.strip():
        print(f"テキストが空です: {md_path}", file=sys.stderr)
        sys.exit(1)

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
    """テキストファイルを読んで payload dict を返す。送信はしない。"""
    txt_path = txt_path.resolve()

    try:
        relative = txt_path.relative_to(input_root)
    except ValueError:
        print(
            f"ファイルが INGEST_INPUT_ROOT ({input_root}) の外にあります: {txt_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    stem_path = str(relative.with_suffix("")).replace("/", "_").replace("\\", "_")
    document_id = f"txt-{stem_path}"

    print(f"テキストファイルを読み込み中: {txt_path} ...")
    text = txt_path.read_text(encoding="utf-8")
    if not text.strip():
        print(f"テキストが空です: {txt_path}", file=sys.stderr)
        sys.exit(1)

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


def cmd_pdf(args: argparse.Namespace) -> None:
    # PDF 取り込みの全体順:
    # 1. ファイルの存在確認
    # 2. build_pdf_payload() で payload を組み立てる
    # 3. GraphRAG API に送る
    pdf_path = Path(args.file)
    if not pdf_path.exists():
        print(f"ファイルが見つかりません: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    input_root = get_input_root()
    payload = build_pdf_payload(
        pdf_path=pdf_path,
        input_root=input_root,
        title=args.title,
        category=args.category,
        language=args.language,
    )
    send_and_print(args.graphrag_url, payload)


def cmd_md(args: argparse.Namespace) -> None:
    md_path = Path(args.file)
    if not md_path.exists():
        print(f"ファイルが見つかりません: {md_path}", file=sys.stderr)
        sys.exit(1)

    input_root = get_input_root()
    payload = build_markdown_payload(
        md_path=md_path,
        input_root=input_root,
        title=args.title,
        category=args.category,
        language=args.language,
    )
    send_and_print(args.graphrag_url, payload)


def cmd_txt(args: argparse.Namespace) -> None:
    txt_path = Path(args.file)
    if not txt_path.exists():
        print(f"ファイルが見つかりません: {txt_path}", file=sys.stderr)
        sys.exit(1)

    input_root = get_input_root()
    payload = build_txt_payload(
        txt_path=txt_path,
        input_root=input_root,
        title=args.title,
        category=args.category,
        language=args.language,
    )
    send_and_print(args.graphrag_url, payload)


def cmd_input_dir(args: argparse.Namespace) -> None:
    # input-dir 取り込みの全体順:
    # 1. INGEST_INPUT_ROOT 配下を再帰スキャンして対応ファイルを列挙する
    # 2. 未対応拡張子はスキップして警告を出す
    # 3. 各ファイルを拡張子に応じた build_*_payload() で処理して送信する
    input_root = get_input_root()
    supported = {".pdf", ".md", ".txt"}

    all_files = sorted(f for f in input_root.rglob("*") if f.is_file())
    targets = []
    for f in all_files:
        if f.suffix.lower() in supported:
            targets.append(f)
        else:
            print(f"[スキップ] 未対応形式: {f.relative_to(input_root)}", file=sys.stderr)

    if not targets:
        print("取り込み対象のファイルが見つかりませんでした。", file=sys.stderr)
        sys.exit(1)

    print(f"{len(targets)} ファイルを取り込みます...")
    for f in targets:
        ext = f.suffix.lower()
        try:
            if ext == ".pdf":
                payload = build_pdf_payload(f, input_root)
            elif ext == ".md":
                payload = build_markdown_payload(f, input_root)
            elif ext == ".txt":
                payload = build_txt_payload(f, input_root)
            send_and_print(args.graphrag_url, payload)
        except SystemExit:
            # 個別ファイルのエラーは警告に留めて次のファイルへ進む
            print(f"[エラー] {f.relative_to(input_root)} をスキップしました", file=sys.stderr)


def main() -> None:
    # CLI の入口。
    # まず共通オプションを定義し、その後に入力元ごとのサブコマンドを分ける。
    # 新しい入力元（例: HTML, Notion, S3）を追加するならここにサブコマンドを足す。
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

    # md サブコマンド
    md_parser = subparsers.add_parser("md", help="Markdown ファイルを取り込む")
    md_parser.add_argument("--file", required=True, help="Markdown ファイルのパス")
    md_parser.add_argument("--title", help="ドキュメントのタイトル（省略時はファイル名）")
    md_parser.add_argument("--category", help="GraphRAG 側のカテゴリ")
    md_parser.add_argument("--language", help="ドキュメント言語 (例: ja)")

    # txt サブコマンド
    txt_parser = subparsers.add_parser("txt", help="テキストファイルを取り込む")
    txt_parser.add_argument("--file", required=True, help="テキストファイルのパス")
    txt_parser.add_argument("--title", help="ドキュメントのタイトル（省略時はファイル名）")
    txt_parser.add_argument("--category", help="GraphRAG 側のカテゴリ")
    txt_parser.add_argument("--language", help="ドキュメント言語 (例: ja)")

    # input-dir サブコマンド
    subparsers.add_parser("input-dir", help="INGEST_INPUT_ROOT 配下を一括取り込む")

    args = parser.parse_args()

    if args.command == "growi":
        cmd_growi(args)
    elif args.command == "pdf":
        cmd_pdf(args)
    elif args.command == "md":
        cmd_md(args)
    elif args.command == "txt":
        cmd_txt(args)
    elif args.command == "input-dir":
        cmd_input_dir(args)


if __name__ == "__main__":
    main()

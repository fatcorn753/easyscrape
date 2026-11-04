"""easyscrape のコマンドラインエントリポイント。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easyscrape.extract import FieldSpec
from easyscrape.job import ScrapeJob, run_job
from easyscrape.output import guess_format, warn_personal_info, write_rows

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="easyscrape",
        description="URL と CSS セレクタを指定して、Web ページから情報を CSV / JSON に取り出します。",
        epilog=(
            "例:\n"
            "  easyscrape https://example.com/news --item 'li.article' "
            "--field 'title=h2' --field 'link=a@href' -o news.csv\n"
            "  easyscrape -i   (対話モード)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", help="取得する URL（複数指定可）")
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="名前=セレクタ[@属性]",
        help="取り出す項目。HTML は CSS セレクタ（@属性で属性値）、JSON はドット区切りのキーパス",
    )
    parser.add_argument(
        "--item",
        metavar="セレクタ",
        help="1 件分の要素を指定する。指定すると一致する要素ごとに 1 行。"
        "省略するとページ全体で 1 行",
    )
    parser.add_argument(
        "--data",
        action="append",
        default=[],
        metavar="キー=値",
        help="POST のフォーム値（指定すると POST で送る）。JSON 検索 API 向け",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="名前: 値",
        help="追加の HTTP ヘッダー（例: 'X-Requested-With: XMLHttpRequest'）",
    )
    parser.add_argument(
        "--records",
        metavar="パス",
        help="JSON 応答で行の配列を指すドット区切りのパス（例: result.all）",
    )
    parser.add_argument("-o", "--output", metavar="ファイル", help="出力先（.json で JSON、それ以外は CSV）。省略で標準出力")
    parser.add_argument("--format", choices=["csv", "json"], help="出力形式を拡張子によらず指定する")
    parser.add_argument("--delay", type=float, default=1.0, help="同じホストへの連続アクセスの間隔（秒, 既定 1.0）")
    parser.add_argument("--force", action="store_true", help="出力先が既にあっても上書きする")
    parser.add_argument("-i", "--interactive", action="store_true", help="対話モードで設定を入力する")
    return parser


def _parse_form(items: list[str]) -> dict[str, str]:
    form: dict[str, str] = {}
    for text in items:
        if "=" not in text:
            raise ValueError(f"'キー=値' の形式で指定してください: {text}")
        key, value = text.split("=", 1)
        form[key.strip()] = value
    return form


def _parse_headers(items: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for text in items:
        if ":" not in text:
            raise ValueError(f"'名前: 値' の形式で指定してください: {text}")
        name, value = text.split(":", 1)
        headers[name.strip()] = value.strip()
    return headers


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def interactive_config() -> argparse.Namespace:
    """対話モードで設定を集め、通常オプションと同じ形の Namespace を返す。"""
    print("easyscrape 対話モード（空行で省略）")
    raw_urls = _ask("取得する URL（カンマ区切り）: ")
    urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    item = _ask("1 件分の要素のセレクタ（例: li.article。省略可）: ") or None
    fields: list[str] = []
    print("取り出す項目を入力（例: title=h2 / link=a@href）。空行で終了")
    while True:
        field = _ask("  項目: ")
        if not field:
            break
        fields.append(field)
    output = _ask("出力先ファイル（省略で画面表示）: ") or None
    return argparse.Namespace(
        urls=urls,
        field=fields,
        item=item,
        output=output,
        format=None,
        delay=1.0,
        force=False,
        data=[],
        header=[],
        records=None,
    )


def run(args: argparse.Namespace) -> int:
    if not args.urls:
        print("エラー: URL を指定してください。", file=sys.stderr)
        return EXIT_USAGE
    if not args.field:
        print("エラー: --field を 1 つ以上指定してください。", file=sys.stderr)
        return EXIT_USAGE
    try:
        fields = [FieldSpec.parse(text) for text in args.field]
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.output and Path(args.output).exists() and not args.force:
        print(f"エラー: {args.output} は既に存在します。--force で上書きしてください。", file=sys.stderr)
        return EXIT_USAGE

    try:
        form = _parse_form(args.data)
        headers = _parse_headers(args.header)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_USAGE

    job = ScrapeJob(
        urls=args.urls,
        fields=fields,
        item=args.item,
        records=args.records,
        form=form,
        headers=headers,
        delay=args.delay,
    )
    result = run_job(job, on_progress=lambda msg: print(msg, file=sys.stderr))
    for error in result.errors:
        print(error, file=sys.stderr)
    failed = len(result.errors)
    rows = result.rows

    if not rows:
        print("結果が 0 行でした。セレクタを確認してください。", file=sys.stderr)
        return EXIT_PARTIAL

    warn_personal_info(rows)
    fmt = args.format or guess_format(args.output)
    write_rows(rows, args.output, fmt)
    if args.output:
        print(f"保存: {args.output}（{len(rows)} 行）", file=sys.stderr)
    return EXIT_PARTIAL if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interactive:
        args = interactive_config()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

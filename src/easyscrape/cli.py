"""easyscrape のコマンドラインエントリポイント。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easyscrape.extract import FieldSpec
from easyscrape.job import ScrapeJob, run_job
from easyscrape.output import append_rows, guess_format, read_rows, warn_personal_info, write_rows
from easyscrape.urlpattern import expand_urls
from easyscrape.watch import WatchStatus, make_plan, run_watch

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
    parser.add_argument(
        "urls",
        nargs="*",
        help="取得する URL（複数指定可）。{1..10}（連番）、{01..12}（ゼロ埋め）、{a,b}（列挙）で複数の URL に展開できる",
    )
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
        "--embedded",
        metavar="セレクタ",
        help="HTML 内の埋め込み JSON の場所（例: script#__NEXT_DATA__）。--records と --field はその JSON 内の位置",
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
    parser.add_argument("--every", metavar="間隔", help="繰り返し取得の間隔（例: 30m、2h。10 分以上）。-o が必要")
    parser.add_argument("--for", dest="span", metavar="期間", help="繰り返す期間（例: 24h、3d。最長 7 日）")
    parser.add_argument("--times", type=int, metavar="回数", help="繰り返す回数（--for の代わりに指定）")
    parser.add_argument("--key", metavar="列名", help="--only-changes で同じ行かを見分ける列（url 列と組み合わせて判定）")
    parser.add_argument("--only-changes", action="store_true", help="新規または内容が変わった行だけを保存する（--key が必要）")
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
        embedded=None,
        every=None,
        span=None,
        times=None,
        key=None,
        only_changes=False,
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

    watching = bool(args.every or args.span or args.times is not None or args.only_changes)
    if not watching and args.output and Path(args.output).exists() and not args.force:
        print(f"エラー: {args.output} は既に存在します。--force で上書きしてください。", file=sys.stderr)
        return EXIT_USAGE

    try:
        form = _parse_form(args.data)
        headers = _parse_headers(args.header)
        urls = expand_urls(args.urls)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if len(urls) != len(args.urls):
        print(f"URL を {len(urls)} 件に展開しました。", file=sys.stderr)

    job = ScrapeJob(
        urls=urls,
        fields=fields,
        item=args.item,
        records=args.records,
        embedded=args.embedded,
        form=form,
        headers=headers,
        delay=args.delay,
    )
    if watching:
        return run_watching(args, job, fields)
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
    try:
        write_rows(rows, args.output, fmt)
    except OSError as exc:
        print(f"エラー: 保存できませんでした: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.output:
        print(f"保存: {args.output}（{len(rows)} 行）", file=sys.stderr)
    return EXIT_PARTIAL if failed else EXIT_OK


def run_watching(args: argparse.Namespace, job: ScrapeJob, fields: list[FieldSpec]) -> int:
    """一定期間、繰り返し取得して -o のファイルに追記する。"""
    if not args.output:
        print("エラー: 繰り返し取得には -o（保存先）を指定してください。", file=sys.stderr)
        return EXIT_USAGE
    try:
        plan = make_plan(args.every, args.span, args.times)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_USAGE
    names = {spec.name for spec in fields} | {"url"}
    if args.only_changes and not args.key:
        print("エラー: --only-changes には --key（同じ行を見分ける列）が必要です。", file=sys.stderr)
        return EXIT_USAGE
    if args.key and args.key not in names:
        print(f"エラー: --key の列が項目にありません: {args.key}（使える列: {', '.join(sorted(names))}）", file=sys.stderr)
        return EXIT_USAGE
    fmt = args.format or guess_format(args.output)
    warned = False

    def sink(rows: list[dict[str, str]]) -> None:
        nonlocal warned
        if not warned:
            warn_personal_info(rows)
            warned = True
        append_rows(rows, args.output, fmt)

    status = WatchStatus()
    minutes = round(plan.every / 60)
    print(
        f"{plan.cycles} 回、約 {minutes} 分おきに取得して {args.output} に追記します（止めるには Ctrl-C）。",
        file=sys.stderr,
    )
    try:
        run_watch(job, plan, sink, status, key=args.key, only_changes=args.only_changes,
                  log=lambda msg: print(msg, file=sys.stderr),
                  seed_rows=read_rows(args.output, fmt) if args.only_changes else None)
    except KeyboardInterrupt:
        print("中断しました。", file=sys.stderr)
        return EXIT_OK
    print(f"終了: {status.done} 回実行、{status.rows_saved} 行を保存しました。", file=sys.stderr)
    if status.state == "failed":
        return EXIT_USAGE
    # 変更なしで保存 0 行でも、取得できていれば正常（定期実行で毎回エラー扱いにしない）
    return EXIT_OK if status.rows_fetched else EXIT_PARTIAL


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interactive:
        args = interactive_config()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

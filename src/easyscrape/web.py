"""ブラウザで使うローカル画面。`easyscrape-ui` で起動し、127.0.0.1 だけで待ち受ける。

フォームに URL と取り出す項目を入力して実行し、結果を表示・CSV / JSON で保存する。
"""

from __future__ import annotations

import html
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from easyscrape.extract import FieldSpec
from easyscrape.job import ScrapeJob, ScrapeResult, run_job
from easyscrape.output import render, warn_personal_info
from easyscrape.cli import _parse_form, _parse_headers

HOST = "127.0.0.1"
DEFAULT_PORT = 8790
MAX_URLS = 50
MIN_DELAY = 1.0  # 利用者が設定しても、同じサイトへの連続アクセスは 1 秒未満にしない
PREVIEW_ROWS = 200
KEEP_RESULTS = 5  # 直近の結果だけをメモリに残す（CSV 保存時に再取得しないため）

PRESETS = [
    {
        "label": "KADOKAWA 文庫の新刊（JSON）",
        "urls": "https://www.kadokawa.co.jp/product/search/",
        "item": "",
        "fields": "itemCode=itemCode\ntitle=title\nperson=person\nrelease_date=release_date\nproduct_form_id=product_form_id",
        "records": "result.all",
        "data": "mgenre=51\nreleaseDate=1\nsize=20\npageno=1\npageno_book=1",
        "headers": "X-Requested-With: XMLHttpRequest",
    },
    {
        "label": "KADOKAWA 文庫の新刊一覧（HTML の例）",
        "urls": "https://www.kadokawa.co.jp/category/bunko/",
        "item": ".book-thumb",
        "fields": "title=.book-detail-title.pc-only\nauthor=.book-detail-text\nlink=a@href",
        "records": "",
        "data": "",
        "headers": "",
    },
]


@dataclass
class FormInput:
    """フォームの値。エラーがあれば errors に入る。"""

    urls: list[str]
    item: str | None
    fields_text: str
    records: str | None
    data_text: str
    headers_text: str
    delay: float
    fields: list[FieldSpec]
    form: dict[str, str]
    headers: dict[str, str]
    errors: list[str]


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_input(values: dict[str, str]) -> FormInput:
    errors: list[str] = []
    urls = _lines(values.get("urls", ""))
    if not urls:
        errors.append("URL を 1 行に 1 つずつ入力してください。")
    elif len(urls) > MAX_URLS:
        errors.append(f"URL は {MAX_URLS} 件までです。")
    for url in urls:
        if not url.startswith(("http://", "https://")):
            errors.append(f"http:// か https:// で始まる URL を入力してください: {url}")

    fields_text = values.get("fields", "")
    fields: list[FieldSpec] = []
    for line in _lines(fields_text):
        try:
            fields.append(FieldSpec.parse(line))
        except ValueError as exc:
            errors.append(str(exc))
    if not fields:
        errors.append("取り出す項目を 1 行に 1 つ入力してください（例: title=h2）。")

    data_text = values.get("data", "")
    headers_text = values.get("headers", "")
    form: dict[str, str] = {}
    headers: dict[str, str] = {}
    try:
        form = _parse_form(_lines(data_text))
    except ValueError as exc:
        errors.append(str(exc))
    try:
        headers = _parse_headers(_lines(headers_text))
    except ValueError as exc:
        errors.append(str(exc))

    try:
        delay = max(MIN_DELAY, float(values.get("delay", "") or MIN_DELAY))
    except ValueError:
        delay = MIN_DELAY
        errors.append("待ち時間は数字で入力してください。")

    return FormInput(
        urls=urls,
        item=values.get("item", "").strip() or None,
        fields_text=fields_text,
        records=values.get("records", "").strip() or None,
        data_text=data_text,
        headers_text=headers_text,
        delay=delay,
        fields=fields,
        form=form,
        headers=headers,
        errors=errors,
    )


class ResultStore:
    """直近の結果を token で引けるように保持する。"""

    def __init__(self, keep: int = KEEP_RESULTS) -> None:
        self._keep = keep
        self._items: OrderedDict[str, ScrapeResult] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, result: ScrapeResult) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._items[token] = result
            while len(self._items) > self._keep:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> ScrapeResult | None:
        with self._lock:
            return self._items.get(token)


STORE = ResultStore()


# ---- HTML 生成。取得した値はすべて html.escape を通す ----

STYLE = """
body{font-family:system-ui,-apple-system,"Hiragino Sans",sans-serif;margin:0;background:#f6f7f9;color:#1d1d1f}
main{max-width:960px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 16px}
section{background:#fff;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
label{display:block;font-weight:600;margin:12px 0 4px}
small{color:#6e6e73;display:block;margin-top:4px;line-height:1.5}
textarea,input[type=text],input[type=number]{width:100%;box-sizing:border-box;font:14px/1.5 ui-monospace,Menlo,monospace;padding:8px;border:1px solid #c7c7cc;border-radius:6px}
textarea{min-height:90px}
button{font:inherit;padding:10px 18px;border:0;border-radius:8px;background:#0a66c2;color:#fff;cursor:pointer;margin-right:8px;margin-top:12px}
button.sub{background:#e5e5ea;color:#1d1d1f}
.errors{background:#fff1f0;border:1px solid #ffa39e;border-radius:8px;padding:12px 16px;margin-bottom:16px}
.errors li{margin:4px 0}
.notice{background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;padding:10px 14px;margin-bottom:16px;line-height:1.6}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border-bottom:1px solid #e5e5ea;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f2f2f7;position:sticky;top:0}
td{word-break:break-all}
.presets button{background:#fff;color:#0a66c2;border:1px solid #0a66c2;margin-right:6px}
"""

PRESET_SCRIPT = """
const PRESETS = %s;
function applyPreset(i){
  const p = PRESETS[i];
  for (const k of ['urls','item','fields','records','data','headers']) {
    document.getElementById(k).value = p[k];
  }
}
"""


def _page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style></head>
<body><main>{body}</main></body></html>"""
    return doc.encode("utf-8")


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def form_page(values: dict[str, str], errors: list[str] | None = None) -> bytes:
    error_html = ""
    if errors:
        items = "".join(f"<li>{_esc(e)}</li>" for e in errors)
        error_html = f'<div class="errors"><ul>{items}</ul></div>'
    preset_buttons = "".join(
        f'<button type="button" class="sub" onclick="applyPreset({i})">{_esc(p["label"])}</button>'
        for i, p in enumerate(PRESETS)
    )
    body = f"""
<h1>かんたんスクレイピング</h1>
{error_html}
<section class="presets">
  <strong>サンプルを入れる</strong><br>
  {preset_buttons}
</section>
<form method="post" action="/run">
<section>
  <label for="urls">取得するページの URL（1 行に 1 つ）</label>
  <textarea id="urls" name="urls">{_esc(values.get("urls", ""))}</textarea>

  <label for="item">1 件分の要素（任意）</label>
  <input type="text" id="item" name="item" value="{_esc(values.get("item", ""))}">
  <small>一覧ページで 1 冊分・1 記事分を囲む要素を指定します（例: <code>li.article</code>）。
  空欄なら、ページ全体で 1 行になります。JSON の場合は使いません。</small>

  <label for="fields">取り出す項目（1 行に 1 つ）</label>
  <textarea id="fields" name="fields">{_esc(values.get("fields", ""))}</textarea>
  <small>書き方: <code>項目名=場所</code>。場所は HTML なら CSS セレクタ（例: <code>h2</code>）、
  末尾に <code>@href</code> などを付けると属性値を取ります。JSON ならキー名（例: <code>title</code>）。</small>

  <label for="records">JSON の場合: 行が入っている場所（任意）</label>
  <input type="text" id="records" name="records" value="{_esc(values.get("records", ""))}">
  <small>例: <code>result.all</code>。HTML のページでは空欄のままにします。</small>
</section>
<section>
  <label for="data">POST で送る値（任意・1 行に 1 つ）</label>
  <textarea id="data" name="data">{_esc(values.get("data", ""))}</textarea>
  <small>検索結果を取る API 向けです。書き方: <code>キー=値</code>。普通のページでは空欄のままにします。</small>

  <label for="headers">追加の HTTP ヘッダー（任意・1 行に 1 つ）</label>
  <textarea id="headers" name="headers">{_esc(values.get("headers", ""))}</textarea>
  <small>書き方: <code>名前: 値</code>。通常は空欄のままで大丈夫です。</small>

  <label for="delay">同じサイトへの待ち時間（秒）</label>
  <input type="number" id="delay" name="delay" min="1" step="0.5" value="{_esc(values.get("delay", "1"))}">
  <small>1 秒未満にはできません。サイトによっては robots.txt の指定でさらに長くなります。</small>
</section>
<button type="submit">取得する</button>
</form>
<script>{PRESET_SCRIPT % json.dumps(PRESETS, ensure_ascii=False)}</script>
"""
    return _page("かんたんスクレイピング", body)


def _hidden(values: dict[str, str]) -> str:
    return "".join(
        f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">' for k, v in values.items()
    )


def result_page(values: dict[str, str], result: ScrapeResult, token: str) -> bytes:
    rows = result.rows
    columns = list(rows[0].keys()) if rows else []
    errors_html = ""
    if result.errors:
        items = "".join(f"<li>{_esc(e)}</li>" for e in result.errors)
        errors_html = f'<div class="errors"><ul>{items}</ul></div>'
    if rows:
        head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
        body_rows = "".join(
            "<tr>" + "".join(f"<td>{_esc(row.get(c, ''))}</td>" for c in columns) + "</tr>"
            for row in rows[:PREVIEW_ROWS]
        )
        table = f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body_rows}</tbody></table></div>'
        preview_note = ""
        if len(rows) > PREVIEW_ROWS:
            preview_note = f"<p>画面には先頭 {PREVIEW_ROWS} 行だけ表示しています。保存すると全行が入ります。</p>"
    else:
        table = "<p>取得できた行はありませんでした。項目の場所やセレクタを確認してください。</p>"
        preview_note = ""
    body = f"""
<h1>取得結果: {len(rows)} 行</h1>
{errors_html}
<section>
  <form method="post" action="/download" style="display:inline">
    {_hidden({"token": token, "format": "csv"})}
    <button type="submit">CSV で保存</button>
  </form>
  <form method="post" action="/download" style="display:inline">
    {_hidden({"token": token, "format": "json"})}
    <button type="submit" class="sub">JSON で保存</button>
  </form>
  <form method="post" action="/run" style="display:inline">
    {_hidden(values)}
    <button type="submit" class="sub">設定に戻って編集</button>
  </form>
</section>
<section>
  {preview_note}
  {table}
</section>
"""
    return _page("取得結果", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "easyscrape-ui"

    def log_message(self, format: str, *args) -> None:  # 標準出力を汚さない
        return

    # 別サイトのページから localhost へ送られるリクエストを拒否する
    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in {f"{HOST}:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def _send(self, status: int, payload: bytes, content_type: str = "text/html; charset=utf-8", extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(min(length, 1024 * 1024)).decode("utf-8", "replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send(403, _page("403", "<h1>403</h1>"))
            return
        if self.path not in ("/", "/index.html"):
            self._send(404, _page("404", "<h1>404</h1>"))
            return
        self._send(200, form_page(dict(PRESETS[0])))

    def do_POST(self) -> None:
        if not self._host_ok():
            self._send(403, _page("403", "<h1>403</h1>"))
            return
        values = self._read_form()
        if self.path == "/run":
            self._run(values)
        elif self.path == "/download":
            self._download(values)
        else:
            self._send(404, _page("404", "<h1>404</h1>"))

    def _run(self, values: dict[str, str]) -> None:
        form_input = parse_input(values)
        if form_input.errors:
            self._send(200, form_page(values, form_input.errors))
            return
        job = ScrapeJob(
            urls=form_input.urls,
            fields=form_input.fields,
            item=form_input.item,
            records=form_input.records,
            form=form_input.form,
            headers=form_input.headers,
            delay=form_input.delay,
        )
        result = run_job(job)
        warn_personal_info(result.rows, stream=sys.stderr)
        token = STORE.put(result)
        self._send(200, result_page(values, result, token))

    def _download(self, values: dict[str, str]) -> None:
        result = STORE.get(values.get("token", ""))
        if result is None:
            self._send(404, form_page({}, ["保存できる結果がありません（期限切れ）。もう一度取得してください。"]))
            return
        fmt = values.get("format", "csv")
        if fmt not in ("csv", "json"):
            fmt = "csv"
        payload = render(result.rows, fmt).encode("utf-8")
        content_type = "application/json; charset=utf-8" if fmt == "json" else "text/csv; charset=utf-8"
        filename = f"easyscrape.{fmt}"
        self._send(
            200,
            payload,
            content_type,
            {"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def make_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, port), Handler)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    port = int(args[0]) if args else DEFAULT_PORT
    try:
        server = make_server(port)
    except OSError as exc:
        print(f"ポート {port} を開けませんでした（別のアプリが使用中）: {exc}\n別のポート番号を指定してください: easyscrape-ui 8791", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_port}/"
    print(f"easyscrape を起動しました: {url}（終了するには Ctrl+C）")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

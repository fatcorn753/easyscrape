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
from easyscrape.fetch import FetchError, Fetcher
from easyscrape.picker import FRAME_CSP, sanitize, suggest
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
        "label": "総務省 報道資料一覧",
        "urls": "https://www.soumu.go.jp/menu_news/s-news/index.html",
        "item": "table.tableList tr:has(td)",
        "fields": "date=td.nw\ntitle=td a\nlink=td a@href\ndept=td:nth-of-type(3)",
        "records": "",
        "data": "",
        "headers": "",
    },
    {
        "label": "環境省 報道発表一覧",
        "urls": "https://www.env.go.jp/press/index.html",
        "item": "li.c-news-link__item",
        "fields": "dept=span.c-tag\ntitle=a.c-news-link__link\nlink=a.c-news-link__link@href",
        "records": "",
        "data": "",
        "headers": "",
    },
    {
        "label": "KADOKAWA 文庫の一覧（HTML の例）",
        "urls": "https://www.kadokawa.co.jp/category/bunko/",
        "item": ".book-thumb",
        "fields": "title=.book-detail-title.pc-only\nauthor=.book-detail-text\nlink=a@href",
        "records": "",
        "data": "",
        "headers": "",
    },
    {
        "label": "KADOKAWA 文庫の新刊（API・上級）",
        "urls": "https://www.kadokawa.co.jp/product/search/",
        "item": "",
        "fields": "itemCode=itemCode\ntitle=title\nperson=person\nrelease_date=release_date\nproduct_form_id=product_form_id",
        "records": "result.all",
        "data": "mgenre=51\nreleaseDate=1\nsize=20\npageno=1\npageno_book=1",
        "headers": "X-Requested-With: XMLHttpRequest",
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
PICK_FETCHER = Fetcher(delay=MIN_DELAY)


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
.pick-bar{display:flex;gap:8px;margin-top:8px}
.pick-bar input{flex:1;font:14px system-ui,sans-serif;padding:8px;border:1px solid #c7c7cc;border-radius:6px}
.pick-bar button{margin:0}
.pick-mode{margin-top:10px;line-height:2}
.pick-mode label{display:inline;font-weight:400;margin-right:14px}
h2.step{font-size:17px;margin:0 0 6px}
details.advanced{background:#fff;border-radius:10px;padding:12px 16px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
details.advanced summary{cursor:pointer;font-weight:600}
.suggest{margin-top:10px}
.suggest table{margin-top:8px}
.suggest input[type=text]{font:14px system-ui,sans-serif}
.suggest input[type=checkbox]{width:auto}
#pick-frame{width:100%;height:520px;border:1px solid #c7c7cc;border-radius:8px;margin-top:10px;background:#fff}
.status{margin:8px 0 0;color:#1d1d1f;font-size:14px}
.status.warn{color:#c93400}
"""

PRESET_SCRIPT = """
const PRESETS = %s;
function applyPreset(i){
  const p = PRESETS[i];
  for (const k of ['urls','item','fields','records','data','headers']) {
    document.getElementById(k).value = p[k];
  }
  document.getElementById('item').dispatchEvent(new Event('change'));
}
"""

PICK_SCRIPT = r"""
(function () {
  const frame = document.getElementById('pick-frame');
  const status = document.getElementById('pick-status');
  const itemBox = document.getElementById('item');
  const fieldsBox = document.getElementById('fields');
  const urlsBox = document.getElementById('urls');
  const suggestBox = document.getElementById('suggest');
  const NAME_OK = /^[^=@\s]+$/;

  function say(text, warn) {
    status.textContent = text;
    status.className = warn ? 'status warn' : 'status';
  }
  function mode() {
    return document.querySelector('input[name=pickmode]:checked').value;
  }
  function sendInit() {
    if (!frame.contentWindow) return;
    frame.contentWindow.postMessage(
      { source: 'easyscrape-parent', type: 'init', item: itemBox.value.trim() || null }, '*');
  }
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  // ---- 1. ページを開く ----
  function load() {
    const url = document.getElementById('pick-url').value.trim();
    if (!/^https?:\/\//.test(url)) {
      say('http:// か https:// で始まる URL を入力してください', true);
      return;
    }
    urlsBox.value = url;
    say('読み込み中…');
    suggestBox.textContent = '';
    frame.src = '/pick-frame?url=' + encodeURIComponent(url);
  }
  document.getElementById('pick-load').addEventListener('click', load);
  itemBox.addEventListener('change', sendInit);

  // ---- 2. 自動提案 ----
  function suggest() {
    const url = document.getElementById('pick-url').value.trim();
    if (!/^https?:\/\//.test(url)) {
      say('先に URL を入力して「読み込む」を押してください', true);
      return;
    }
    suggestBox.textContent = '候補を探しています…';
    fetch('/suggest?url=' + encodeURIComponent(url), { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(renderSuggest)
      .catch(function () { suggestBox.textContent = '候補を取得できませんでした。'; });
  }
  function renderSuggest(data) {
    suggestBox.textContent = '';
    if (data.error) {
      suggestBox.appendChild(el('p', data.error, 'status warn'));
      return;
    }
    const candidates = data.candidates || [];
    if (!candidates.length) {
      suggestBox.appendChild(el('p', '一覧を自動で見つけられませんでした。' +
        'ページを開いて「一覧の 1 件」をクリックするか、サンプルを試してください。' +
        '（JavaScript で後から表示されるページは取れない場合があります）', 'status warn'));
      return;
    }
    const pick = el('select');
    candidates.forEach(function (c, i) {
      const sample = c.fields.length ? c.fields[0].preview : '';
      const opt = el('option', '候補 ' + (i + 1) + '：' + c.count + ' 件' + (sample ? '（例: ' + sample.slice(0, 30) + '）' : ''));
      opt.value = String(i);
      pick.appendChild(opt);
    });
    const area = el('div');
    suggestBox.appendChild(el('p', '一覧の候補を選んでください（件数が正しいものを選びます）'));
    suggestBox.appendChild(pick);
    suggestBox.appendChild(area);
    pick.addEventListener('change', function () { renderCandidate(candidates[Number(pick.value)], area); });
    renderCandidate(candidates[0], area);
  }
  function renderCandidate(c, area) {
    area.textContent = '';
    const useItem = el('button', 'この一覧を使う');
    useItem.type = 'button';
    useItem.className = 'sub';
    useItem.addEventListener('click', function () {
      itemBox.value = c.item;
      sendInit();
      say('一覧の 1 件を設定しました');
    });
    area.appendChild(useItem);

    const table = el('table');
    const head = el('tr');
    ['使う', '項目名', '例（1 件目）', '取れた件数'].forEach(function (t) { head.appendChild(el('th', t)); });
    table.appendChild(el('thead')).appendChild(head);
    const body = el('tbody');
    const rows = [];
    c.fields.forEach(function (f) {
      const tr = el('tr');
      const check = el('input');
      check.type = 'checkbox';
      check.checked = true;
      const name = el('input');
      name.type = 'text';
      name.value = f.name;
      const td1 = el('td'); td1.appendChild(check);
      const td2 = el('td'); td2.appendChild(name);
      tr.appendChild(td1);
      tr.appendChild(td2);
      tr.appendChild(el('td', f.preview));
      tr.appendChild(el('td', f.coverage + ' / ' + c.count));
      body.appendChild(tr);
      rows.push({ check: check, name: name, field: f });
    });
    table.appendChild(body);
    const wrap = el('div', undefined, 'table-wrap');
    wrap.appendChild(table);
    area.appendChild(wrap);

    const apply = el('button', '選んだ項目を「3」に入れる（置き換え）');
    apply.type = 'button';
    apply.addEventListener('click', function () {
      const lines = [];
      for (const row of rows) {
        if (!row.check.checked) continue;
        const name = row.name.value.trim();
        if (!NAME_OK.test(name)) {
          say('項目名に空白・=・@ は使えません: ' + name, true);
          return;
        }
        const attr = row.field.attr ? '@' + row.field.attr : '';
        lines.push(name + '=' + row.field.selector + attr);
      }
      if (!lines.length) {
        say('項目を 1 つ以上選んでください', true);
        return;
      }
      itemBox.value = c.item;
      fieldsBox.value = lines.join('\n');
      sendInit();
      say('「3. 取り出す内容」に ' + lines.length + ' 件入れました。内容を確認して「取得する」を押してください');
    });
    area.appendChild(apply);
  }
  document.getElementById('suggest-run').addEventListener('click', suggest);

  // ---- 3. クリックで選んだ内容を受け取る ----
  function addField(name, selector) {
    const current = fieldsBox.value.replace(/\s+$/, '');
    fieldsBox.value = (current ? current + '\n' : '') + name + '=' + selector;
  }

  window.addEventListener('message', function (e) {
    if (e.source !== frame.contentWindow) return;
    const d = e.data;
    if (!d || d.source !== 'easyscrape') return;
    if (d.type === 'ready') {
      sendInit();
      say('クリックして取り出したいものを選んでください（緑の枠が取り出される範囲）');
      suggest();
      return;
    }
    if (d.type === 'error') {
      say(d.message, true);
      return;
    }
    if (d.type !== 'pick') return;

    const m = mode();
    if (m === 'item') {
      if (!d.item) {
        say('繰り返しの要素が見つかりませんでした。一覧の 1 件の中をクリックしてください', true);
        return;
      }
      itemBox.value = d.item.sel;
      sendInit();
      say('一覧の 1 件: ' + d.item.sel + '（一覧 ' + d.item.count + ' 件）');
      return;
    }
    if (d.outside) {
      say('「一覧の 1 件」の中をクリックしてください', true);
      return;
    }
    let selector = d.field;
    let suffix = '';
    if (m === 'href') {
      if (!d.anchor) {
        say('リンクが見つかりません。リンクの文字をクリックしてください', true);
        return;
      }
      selector = d.anchor;
      suffix = '@href';
    }
    if (!selector) {
      say('この要素は指定できませんでした。別の場所をクリックしてください', true);
      return;
    }
    const name = prompt('この項目の名前（英数字。例: title, link, date）', m === 'href' ? 'link' : 'title');
    if (!name || !name.trim()) {
      say('追加をキャンセルしました');
      return;
    }
    if (!NAME_OK.test(name.trim())) {
      say('項目名に空白・=・@ は使えません', true);
      return;
    }
    addField(name.trim(), selector + suffix);
    say('追加: ' + name.trim() + '=' + selector + suffix + '（一致 ' + d.count + ' 件 / 例: ' + (d.text || '') + '）');
  });
})();
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
  <strong>サンプルで試す</strong><br>
  {preset_buttons}
</section>

<section>
  <h2 class="step">1. ページを開く</h2>
  <small>取りたい情報が載っているページの URL を入れて「読み込む」。</small>
  <div class="pick-bar">
    <input type="text" id="pick-url" placeholder="https://..." value="{_esc(_first_line(values.get("urls", "")))}">
    <button type="button" id="pick-load">読み込む</button>
  </div>
  <p id="pick-status" class="status">読み込み前</p>
  <iframe id="pick-frame" sandbox="allow-scripts" title="取得するページ"></iframe>
  <small>ページ上の目印をクリックすると、下の「3. 取り出す内容」に自動で入ります。</small>
  <div class="pick-mode">
    <label><input type="radio" name="pickmode" value="item" checked> 一覧の 1 件（同じ形で並ぶもの全体）を選ぶ</label>
    <label><input type="radio" name="pickmode" value="text"> 文字を取り出す</label>
    <label><input type="radio" name="pickmode" value="href"> リンク先（URL）を取り出す</label>
  </div>
</section>

<section>
  <h2 class="step">2. 自動で候補を出す</h2>
  <button type="button" id="suggest-run" class="sub">候補を自動で探す</button>
  <div id="suggest" class="suggest"></div>
</section>

<form method="post" action="/run">
<section>
  <h2 class="step">3. 取り出す内容</h2>
  <label for="urls">取得するページの URL（1 行に 1 つ）</label>
  <textarea id="urls" name="urls">{_esc(values.get("urls", ""))}</textarea>

  <label for="item">一覧の 1 件（任意）</label>
  <input type="text" id="item" name="item" value="{_esc(values.get("item", ""))}">
  <small>同じ形で並ぶ 1 件分の目印。一覧でない単独のページなら空欄のまま。</small>

  <label for="fields">取り出す項目（1 行に 1 つ）</label>
  <textarea id="fields" name="fields">{_esc(values.get("fields", ""))}</textarea>
  <small>書き方: <code>項目名=場所</code>。例: <code>title=h2</code>、リンクは <code>link=a@href</code>。</small>
</section>

<details class="advanced">
  <summary>詳細設定（API・JSON・待ち時間）</summary>
  <label for="records">JSON の場合: 行が入っている場所</label>
  <input type="text" id="records" name="records" value="{_esc(values.get("records", ""))}">
  <small>例: <code>result.all</code>。普通のページでは空欄。</small>

  <label for="data">POST で送る値（1 行に 1 つ）</label>
  <textarea id="data" name="data">{_esc(values.get("data", ""))}</textarea>
  <small>書き方: <code>キー=値</code>。検索 API 向け。普通のページでは空欄。</small>

  <label for="headers">追加の HTTP ヘッダー（1 行に 1 つ）</label>
  <textarea id="headers" name="headers">{_esc(values.get("headers", ""))}</textarea>
  <small>書き方: <code>名前: 値</code>。通常は空欄。</small>

  <label for="delay">同じサイトへの待ち時間（秒）</label>
  <input type="number" id="delay" name="delay" min="1" step="0.5" value="{_esc(values.get("delay", "1"))}">
  <small>1 秒未満にはできません。サイトの robots.txt でさらに長くなる場合があります。</small>
</section>
<button type="submit">取得する</button>
</form>
<script>{PRESET_SCRIPT % json.dumps(PRESETS, ensure_ascii=False)}</script>
<script>{PICK_SCRIPT}</script>
"""
    return _page("かんたんスクレイピング", body)


def _first_line(text: str) -> str:
    lines = _lines(text)
    return lines[0] if lines else ""


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
        if host not in {f"{HOST}:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            return False
        # 別サイトのページからの要求（DNS の書き換え・CSRF）を拒否する
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).hostname not in (HOST, "localhost"):
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

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
        if self.path in ("/", "/index.html"):
            self._send(200, form_page(dict(PRESETS[0])))
            return
        if self.path.startswith("/pick-frame"):
            self._pick_frame()
            return
        if self.path.startswith("/suggest"):
            self._suggest()
            return
        self._send(404, _page("404", "<h1>404</h1>"))

    def _suggest(self) -> None:
        url = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("url", [""])[-1].strip()
        if not url.startswith(("http://", "https://")):
            self._json(400, {"error": "http:// か https:// で始まる URL を入力してください。"})
            return
        try:
            text, content_type = PICK_FETCHER.fetch(url)
        except FetchError as exc:
            self._json(200, {"error": f"ページを取得できませんでした: {exc}"})
            return
        if content_type and "html" not in content_type.lower():
            self._json(200, {"error": "HTML のページではないため、候補は出せません。"})
            return
        self._json(200, suggest(text))

    def _json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8", {"X-Content-Type-Options": "nosniff"})

    def _pick_frame(self) -> None:
        query = urllib.parse.urlsplit(self.path).query
        url = urllib.parse.parse_qs(query).get("url", [""])[-1].strip()
        if not url.startswith(("http://", "https://")):
            self._send(400, _page("エラー", "<p>http:// か https:// で始まる URL を指定してください。</p>"))
            return
        try:
            text, content_type = PICK_FETCHER.fetch(url)
        except FetchError as exc:
            message = html.escape(str(exc))
            self._send(200, _page("エラー", f"<p>ページを取得できませんでした: {message}</p>"))
            return
        if "html" not in content_type.lower() and content_type:
            self._send(200, _page("エラー", "<p>HTML のページではないため、クリックでは選べません。</p>"))
            return
        self._send(
            200,
            sanitize(text, url).encode("utf-8"),
            "text/html; charset=utf-8",
            {"Content-Security-Policy": FRAME_CSP, "X-Content-Type-Options": "nosniff"},
        )

    def do_POST(self) -> None:
        if not self._host_ok():
            self._send(403, _page("403", "<h1>403</h1>"))
            return
        values = self._read_form()
        try:
            if self.path == "/run":
                self._run(values)
            elif self.path == "/download":
                self._download(values)
            else:
                self._send(404, _page("404", "<h1>404</h1>"))
        except Exception as exc:  # 予期しない失敗でも接続を切らず、理由を画面に出す
            print(f"内部エラー: {exc!r}", file=sys.stderr)
            self._send(500, form_page(values, [f"処理中に問題が発生しました: {exc}"]))

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

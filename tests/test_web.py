import http.client
import re
import threading
import urllib.parse
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from easyscrape import web

TARGET_HTML = """<html><body>
<ul>
  <li class="item"><h2>&lt;b&gt;記事A&lt;/b&gt;</h2><a href="/a">読む</a></li>
  <li class="item"><h2>記事B</h2><a href="/b">読む</a></li>
</ul></body></html>"""


@pytest.fixture
def target(tmp_path):
    (tmp_path / "list.html").write_text(TARGET_HTML, encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a, **k: None
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def ui():
    server = web.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _request(server, method, path, body=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{server.server_port}"}
    payload = None
    if body is not None:
        payload = urllib.parse.urlencode(body)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    result = (resp.status, dict(resp.getheaders()), data.decode("utf-8"))
    conn.close()
    return result


def test_parse_input_validation():
    parsed = web.parse_input({"urls": "", "fields": "", "delay": "0"})
    assert any("URL" in e for e in parsed.errors)
    assert any("項目" in e for e in parsed.errors)
    assert parsed.delay == web.MIN_DELAY


def test_parse_input_rejects_non_http_url():
    parsed = web.parse_input({"urls": "file:///etc/passwd", "fields": "t=h1"})
    assert any("http" in e for e in parsed.errors)


def test_parse_input_ok():
    parsed = web.parse_input({
        "urls": "https://example.com\n\nhttps://example.org",
        "item": "li.x",
        "fields": "title=h2\nlink=a@href",
        "delay": "2",
    })
    assert parsed.errors == []
    assert parsed.urls == ["https://example.com", "https://example.org"]
    assert [f.name for f in parsed.fields] == ["title", "link"]
    assert parsed.delay == 2.0


def test_form_page_escapes_user_input():
    page = web.form_page({"urls": '"><script>alert(1)</script>'}).decode("utf-8")
    assert "<script>alert(1)</script>" not in page.split("<script>")[0]
    assert "&lt;script&gt;" in page


def test_rejects_foreign_host(ui):
    status, _, _ = _request(ui, "GET", "/", host="evil.example")
    assert status == 403


def test_get_form(ui):
    status, headers, body = _request(ui, "GET", "/")
    assert status == 200
    assert "取得する" in body


def test_run_and_download_csv(ui, target):
    values = {
        "urls": f"{target}/list.html",
        "item": "li.item",
        "fields": "title=h2\nlink=a@href",
        "delay": "1",
    }
    status, _, body = _request(ui, "POST", "/run", values)
    assert status == 200
    assert "取得結果: 2 行" in body
    # 記事 A のタグは生の HTML として解釈されず、文字として表示される
    assert "&lt;b&gt;記事A&lt;/b&gt;" in body
    assert "<b>記事A</b>" not in body

    token = re.search(r'name="token" value="([^"]+)"', body).group(1)
    status, headers, csv_body = _request(ui, "POST", "/download", {"token": token, "format": "csv"})
    assert status == 200
    assert headers["Content-Disposition"] == 'attachment; filename="easyscrape.csv"'
    lines = csv_body.splitlines()
    assert lines[0] == "url,title,link"
    assert len(lines) == 3


def test_download_unknown_token(ui):
    status, _, body = _request(ui, "POST", "/download", {"token": "nope", "format": "csv"})
    assert status == 404
    assert "期限切れ" in body

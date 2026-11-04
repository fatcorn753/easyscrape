import json
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from easyscrape.cli import main
from easyscrape.extract import FieldSpec, extract_rows
from easyscrape.fetch import Fetcher, RobotsDenied
from easyscrape.output import render, warn_personal_info

PAGE = """<html><body>
<ul>
  <li class="article"><h2>記事A</h2><a href="/a">読む</a></li>
  <li class="article"><h2>記事B</h2><a href="/b">読む</a></li>
</ul>
<p class="contact">連絡: taro@example.com</p>
</body></html>"""


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    (tmp_path / "private.html").write_text("<p>secret</p>", encoding="utf-8")
    (tmp_path / "robots.txt").write_text("User-agent: *\nDisallow: /private.html\n", encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a, **k: None
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_field_parse_text_and_attr():
    assert FieldSpec.parse("title=h2") == FieldSpec("title", "h2", None)
    assert FieldSpec.parse("link=a.x@href") == FieldSpec("link", "a.x", "href")


@pytest.mark.parametrize("bad", ["title", "=h2", "title="])
def test_field_parse_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        FieldSpec.parse(bad)


def test_extract_rows_per_item():
    fields = [FieldSpec.parse("title=h2"), FieldSpec.parse("link=a@href")]
    rows = extract_rows(PAGE, fields, "li.article")
    assert rows == [
        {"title": "記事A", "link": "/a"},
        {"title": "記事B", "link": "/b"},
    ]


def test_extract_rows_whole_page_first_match():
    rows = extract_rows(PAGE, [FieldSpec.parse("title=h2")], None)
    assert rows == [{"title": "記事A"}]


def test_missing_selector_gives_empty_string():
    rows = extract_rows(PAGE, [FieldSpec.parse("price=.price")], None)
    assert rows == [{"price": ""}]


def test_render_csv_and_json():
    rows = [{"url": "u", "title": "記事A"}]
    assert render(rows, "csv").splitlines()[0] == "url,title"
    assert json.loads(render(rows, "json")) == rows


def test_warn_personal_info(capsys):
    import io

    buf = io.StringIO()
    warn_personal_info([{"c": "taro@example.com 03-1234-5678"}], stream=buf)
    out = buf.getvalue()
    assert "メールアドレス" in out
    assert "電話番号" in out


def test_robots_denied(site):
    fetcher = Fetcher(delay=0)
    with pytest.raises(RobotsDenied):
        fetcher.get_html(site + "/private.html")
    assert "記事A" in fetcher.get_html(site + "/index.html")


def test_cli_end_to_end_csv(site, tmp_path):
    out = tmp_path / "out.csv"
    code = main([
        site + "/index.html",
        "--item", "li.article",
        "--field", "title=h2",
        "--field", "link=a@href",
        "-o", str(out),
        "--delay", "0",
    ])
    assert code == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "url,title,link"
    assert lines[1].endswith(",記事A,/a")
    assert len(lines) == 3


def test_cli_json_and_robots_partial(site, tmp_path):
    out = tmp_path / "out.json"
    code = main([
        site + "/index.html",
        site + "/private.html",
        "--field", "title=h2",
        "-o", str(out),
        "--delay", "0",
    ])
    assert code == 1  # 禁止 URL があったため部分失敗
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == [{"url": site + "/index.html", "title": "記事A"}]


def test_cli_refuses_overwrite(site, tmp_path):
    out = tmp_path / "exists.csv"
    out.write_text("keep", encoding="utf-8")
    code = main([site + "/index.html", "--field", "title=h2", "-o", str(out)])
    assert code == 2
    assert out.read_text(encoding="utf-8") == "keep"


def test_cli_usage_errors(capsys):
    assert main([]) == 2
    assert main(["https://example.com"]) == 2
    assert main(["https://example.com", "--field", "bad"]) == 2

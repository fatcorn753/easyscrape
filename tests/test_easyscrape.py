import json
import threading
import urllib.robotparser
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler

import pytest

from easyscrape.cli import main
from easyscrape.extract import FieldSpec, extract_json_rows, extract_rows
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
    server.server_close()


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


@pytest.mark.parametrize("value", ["090-1234-5678", "0312345678", "(03) 1234-5678"])
def test_phone_detected(value):
    import io

    buf = io.StringIO()
    warn_personal_info([{"c": value}], stream=buf)
    assert "電話番号" in buf.getvalue()


@pytest.mark.parametrize("value", [
    "/product/322605001487/",  # 商品コード
    "9784041175361",           # ISBN-13
    "https://example.com/p/12345678901234",
])
def test_phone_not_detected(value):
    import io

    buf = io.StringIO()
    warn_personal_info([{"c": value}], stream=buf)
    assert buf.getvalue() == ""


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
    assert lines[1].endswith(",記事A," + site + "/a")  # 相対 URL は絶対 URL に直る
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


JSON_BODY = {
    "count": {"book": "2"},
    "result": {
        "all": [
            {"itemCode": "1", "title": "天地明察 上", "person": "冲方丁", "release_date": "2026-10-23"},
            {"itemCode": "2", "title": "天地明察 下", "person": "冲方丁", "release_date": "2026-10-23"},
        ]
    },
}


@pytest.fixture
def json_api():
    """POST のフォーム値を受け取り、JSON を返すローカル API。"""
    import json as _json

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = _json.dumps(JSON_BODY, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **k):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api"
    server.shutdown()
    server.server_close()


def test_extract_json_rows_records_path():
    text = json.dumps(JSON_BODY, ensure_ascii=False)
    fields = [FieldSpec.parse("title=title"), FieldSpec.parse("date=release_date")]
    rows = extract_json_rows(text, fields, "result.all")
    assert rows == [
        {"title": "天地明察 上", "date": "2026-10-23"},
        {"title": "天地明察 下", "date": "2026-10-23"},
    ]


def test_extract_json_rows_missing_and_nested():
    text = json.dumps({"a": [{"b": {"c": 1}}, {"b": None}]})
    fields = [FieldSpec.parse("c=b.c"), FieldSpec.parse("first=a.0.b.c")]
    assert extract_json_rows(text, fields, "a") == [
        {"c": "1", "first": ""},
        {"c": "", "first": ""},
    ]


def test_extract_json_rows_bad_records_path():
    with pytest.raises(ValueError):
        extract_json_rows(json.dumps(JSON_BODY), [FieldSpec.parse("t=title")], "result.nope")


def test_cli_json_post_end_to_end(json_api, tmp_path):
    out = tmp_path / "api.csv"
    code = main([
        json_api,
        "--data", "pageno=1",
        "--data", "releaseDate=1",
        "--records", "result.all",
        "--field", "title=title",
        "--field", "person=person",
        "-o", str(out),
        "--delay", "0",
    ])
    assert code == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "url,title,person"
    assert lines[1].endswith(",天地明察 上,冲方丁")
    assert len(lines) == 3


def test_crawl_delay_respected():
    fetcher = Fetcher(delay=1.0)
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Crawl-delay: 20"])
    fetcher._robots["https://example.test"] = parser
    assert fetcher._interval("https://example.test/a") == 20.0


def test_decode_body_charsets():
    from easyscrape.fetch import decode_body

    sjis = "総務省 報道資料".encode("cp932")
    assert decode_body(sjis, "text/html; charset=Shift_JIS") == "総務省 報道資料"
    meta = b'<meta charset="Shift_JIS"><p>' + sjis + b"</p>"
    assert "総務省" in decode_body(meta, "text/html")
    utf8 = "天地明察".encode("utf-8")
    assert decode_body(utf8, "text/html; charset=utf-8") == "天地明察"
    assert decode_body(utf8, "") == "天地明察"


def test_run_job_continues_after_failed_url(site):
    from easyscrape.job import ScrapeJob, run_job

    job = ScrapeJob(
        urls=["http://127.0.0.1:9/unreachable", site + "/index.html"],
        fields=[FieldSpec.parse("title=h2")],
        item="li.article",
        delay=0,
    )
    result = run_job(job)
    assert len(result.errors) == 1
    assert [r["title"] for r in result.rows] == ["記事A", "記事B"]


def test_extract_rows_skips_empty_items():
    html = "<table><tr><th>発表日</th></tr><tr><td class='d'>2026年11月4日</td></tr></table>"
    rows = extract_rows(html, [FieldSpec.parse("date=td.d")], "tr")
    assert rows == [{"date": "2026年11月4日"}]


def test_bad_selector_is_error_not_crash():
    from easyscrape.job import ScrapeJob, run_job

    result = run_job(ScrapeJob(urls=[], fields=[FieldSpec.parse("t=h2[")]))
    assert result.rows == []
    with pytest.raises(ValueError, match="セレクタが正しくありません"):
        extract_rows("<h2>x</h2>", [FieldSpec.parse("t=h2[")], None)


def test_json_detected_by_body_when_content_type_is_text():
    rows = extract_json_rows('{"items":[{"t":"a"}]}', [FieldSpec.parse("t=t")], "items")
    assert rows == [{"t": "a"}]
    from easyscrape.extract import looks_like_json

    assert looks_like_json('{"a":1}', "text/plain")
    assert not looks_like_json("<html>", "text/html")


def test_output_to_directory_is_clean_error(site, tmp_path, capsys):
    code = main([site + "/index.html", "--field", "title=h2", "-o", str(tmp_path), "--force", "--delay", "0"])
    assert code == 2
    assert "保存できませんでした" in capsys.readouterr().err

"""HTTP 取得と robots.txt の確認。"""

from __future__ import annotations

import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

import certifi

# 連絡先を含めない。用途が分かる固定の名前だけを送る。
USER_AGENT = "easyscrape/0.1 (personal scraping tool)"
DEFAULT_TIMEOUT = 15.0
# python.org 版や uv の Python は macOS の証明書ストアを使わないため、certifi の CA 束を使う
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
MAX_BYTES = 10 * 1024 * 1024


_META_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)


def decode_body(body: bytes, content_type: str = "") -> str:
    """文字コードを判定して復号する。

    優先順: Content-Type の charset → 本文先頭の <meta charset> / XML 宣言 → UTF-8。
    日本の官公庁サイトは Shift_JIS（実際は cp932）の例が多い。
    """
    charset = None
    match = re.search(r"charset=([\w.:-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    else:
        sniffed = _META_CHARSET.search(body[:4096])
        if sniffed:
            charset = sniffed.group(1).decode("ascii", "replace")
    if charset:
        name = charset.lower()
        # Windows の日本語文字を含むため、Shift_JIS は cp932 として扱う
        if name in ("shift_jis", "shift-jis", "sjis", "x-sjis", "ms_kanji"):
            name = "cp932"
        try:
            return body.decode(name, "replace")
        except LookupError:
            pass
    return body.decode("utf-8", "replace")


class FetchError(Exception):
    """取得に失敗したとき、利用者に見せるメッセージを持つ例外。"""


class RobotsDenied(FetchError):
    """robots.txt で取得が禁止されている URL。"""


class Fetcher:
    """同一ホストへの連続アクセスに間隔を空けながら取得する。

    robots.txt は常に確認する。取得できなかった場合（404 など）は
    取得を許可する。これは一般的な crawler の扱いに合わせている。
    robots.txt の Crawl-delay が --delay より長ければ、そちらを使う。
    """

    def __init__(self, delay: float = 1.0, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.delay = delay
        self.timeout = timeout
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                body, _ = self._open(origin + "/robots.txt")
                parser.parse(decode_body(body).splitlines())
                self._robots[origin] = parser
            except FetchError:
                # robots.txt が無い・読めない場合は制限なしとして扱う
                self._robots[origin] = None
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def _open(
        self, url: str, form: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[bytes, str]:
        data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        request = urllib.request.Request(url, data=data, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=SSL_CONTEXT) as response:
                content_type = response.headers.get("Content-Type", "")
                return response.read(MAX_BYTES), content_type
        except urllib.error.HTTPError as exc:
            raise FetchError(f"HTTP {exc.code}: {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"取得失敗: {url} ({exc})") from exc

    def interval(self, url: str) -> float:
        """このホストへの次のアクセスまでの待ち時間（robots.txt の Crawl-delay を含む）。"""
        parser = self._robots_for(url)
        crawl_delay = parser.crawl_delay(USER_AGENT) if parser is not None else None
        return max(self.delay, float(crawl_delay or 0))

    def _wait_for_host(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            remaining = self.interval(url) - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    def fetch(
        self, url: str, form: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[str, str]:
        """本文と Content-Type を返す。form を渡すと POST で送る。"""
        if not self.allowed(url):
            raise RobotsDenied(f"robots.txt により取得禁止: {url}")
        self._wait_for_host(url)
        body, content_type = self._open(url, form, headers)
        return decode_body(body, content_type), content_type

    def get_html(self, url: str) -> str:
        text, _ = self.fetch(url)
        return text

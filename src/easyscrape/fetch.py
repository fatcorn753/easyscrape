"""HTTP 取得と robots.txt の確認。"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

# 連絡先を含めない。用途が分かる固定の名前だけを送る。
USER_AGENT = "easyscrape/0.1 (personal scraping tool)"
DEFAULT_TIMEOUT = 15.0
MAX_BYTES = 10 * 1024 * 1024


class FetchError(Exception):
    """取得に失敗したとき、利用者に見せるメッセージを持つ例外。"""


class RobotsDenied(FetchError):
    """robots.txt で取得が禁止されている URL。"""


class Fetcher:
    """同一ホストへの連続アクセスに間隔を空けながら HTML を取得する。

    robots.txt は常に確認する。取得できなかった場合（404 など）は
    取得を許可する。これは一般的な crawler の扱いに合わせている。
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
                body = self._open(origin + "/robots.txt").decode("utf-8", "replace")
                parser.parse(body.splitlines())
                self._robots[origin] = parser
            except FetchError:
                # robots.txt が無い・読めない場合は制限なしとして扱う
                self._robots[origin] = None
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def _open(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(MAX_BYTES)
        except urllib.error.HTTPError as exc:
            raise FetchError(f"HTTP {exc.code}: {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"取得失敗: {url} ({exc})") from exc

    def _wait_for_host(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            remaining = self.delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    def get_html(self, url: str) -> str:
        if not self.allowed(url):
            raise RobotsDenied(f"robots.txt により取得禁止: {url}")
        self._wait_for_host(url)
        body = self._open(url)
        return body.decode("utf-8", "replace")

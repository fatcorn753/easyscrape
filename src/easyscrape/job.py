"""取得から行の抽出までをまとめた処理。CLI と Web UI の両方から使う。"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from easyscrape.extract import FieldSpec, extract_embedded_rows, extract_json_rows, extract_rows, looks_like_json
from easyscrape.fetch import FetchError, Fetcher


@dataclass
class ScrapeJob:
    urls: list[str]
    fields: list[FieldSpec]
    item: str | None = None
    records: str | None = None
    form: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 1.0
    embedded: str | None = None  # HTML 内の埋め込み JSON の <script> セレクタ


DETAIL_MAX = 200


def detail_targets(rows: list[dict[str, str]], column: str, limit: int) -> list[str]:
    """一覧の行から、詳細ページの URL を取り出す。

    同じホストで、一覧ページと同じフォルダ以下のものだけを対象にする（別サイトへは行かない）。
    重複は 1 つにまとめ、limit 件で打ち切る。
    """
    targets: list[str] = []
    for row in rows:
        link = row.get(column, "")
        list_url = row.get("url", "")
        if not link.startswith(("http://", "https://")) or not list_url:
            continue
        list_parts = urllib.parse.urlsplit(list_url)
        link_parts = urllib.parse.urlsplit(link)
        if link_parts.netloc != list_parts.netloc:
            continue
        folder = list_parts.path.rsplit("/", 1)[0] + "/"
        if not link_parts.path.startswith(folder):
            continue
        if link in targets:
            continue
        targets.append(link)
        if len(targets) >= min(limit, DETAIL_MAX):
            break
    return targets


def run_detail(
    rows: list[dict[str, str]],
    column: str,
    fields: list[FieldSpec],
    fetcher: Fetcher,
    limit: int,
    on_progress: Callable[[str], None] | None = None,
) -> ScrapeResult:
    """各行の詳細ページを取得し、項目を同じ行に結合する。失敗した詳細は空欄のまま残す。"""
    result = ScrapeResult()
    details: dict[str, dict[str, str]] = {}
    for index, url in enumerate(detail_targets(rows, column, limit), start=1):
        try:
            html, _ = fetcher.fetch(url)
            details[url] = extract_rows(html, fields, None)[0]
        except FetchError as exc:
            result.errors.append(f"スキップ: {exc}")
        except ValueError as exc:
            result.errors.append(f"スキップ: {url}: {exc}")
        if on_progress:
            on_progress(f"詳細 {index}: {url}")
    for row in rows:
        merged = dict(row)
        merged.update(details.get(row.get(column, ""), {}))
        result.rows.append(merged)
    return result


@dataclass
class ScrapeResult:
    rows: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_job(job: ScrapeJob, on_progress: Callable[[str], None] | None = None) -> ScrapeResult:
    """各 URL を取得して行を集める。1 件の失敗は errors に残し、残りは続行する。"""
    fetcher = Fetcher(delay=job.delay)
    result = ScrapeResult()
    for url in job.urls:
        try:
            body, content_type = fetcher.fetch(url, job.form or None, job.headers or None)
        except FetchError as exc:
            result.errors.append(f"スキップ: {exc}")
            continue
        is_json = looks_like_json(body, content_type)
        try:
            if is_json:
                page_rows = extract_json_rows(body, job.fields, job.records)
            elif job.embedded:
                page_rows = extract_embedded_rows(body, job.fields, job.embedded, job.records)
            else:
                page_rows = extract_rows(body, job.fields, job.item)
        except ValueError as exc:
            result.errors.append(f"スキップ: {url}: {exc}")
            continue
        if not is_json:
            # 相対 URL（/a/b.html など）を、取得元のページを基準に絶対 URL へ直す
            link_fields = [f.name for f in job.fields if f.attr in ("href", "src")]
            for row in page_rows:
                for name in link_fields:
                    if row.get(name):
                        row[name] = urljoin(url, row[name])
        result.rows.extend({"url": url, **row} for row in page_rows)
        if on_progress:
            on_progress(f"取得: {url}（{len(result.rows)} 行 累計）")
    return result

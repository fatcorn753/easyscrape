"""取得から行の抽出までをまとめた処理。CLI と Web UI の両方から使う。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from easyscrape.extract import FieldSpec, extract_json_rows, extract_rows, looks_like_json
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

"""URL の規則指定を、複数の URL に展開する。

書式（URL の中に波かっこで書く）:
  {1..10}      連番（1〜10）
  {01..12}     ゼロ埋めの連番（01〜12）
  {1..20..5}   刻み付きの連番（1, 6, 11, 16）
  {10..1}      逆順
  {tokyo,osaka} 列挙
複数書くと組み合わせになる。上記の形に当てはまらない {…} はそのまま URL の一部として扱う。
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

URL_LIMIT = 200

_BRACE = re.compile(r"\{([^{}]*)\}")
_RANGE = re.compile(r"^(\d+)\.\.(\d+)(?:\.\.(\d+))?$")


@dataclass(frozen=True)
class _Part:
    """URL を分けた 1 区間。options があれば展開対象、無ければ固定の文字。"""

    literal: str
    options: tuple[str, ...] | None = None


def _range_options(first: str, last: str, step_text: str | None) -> tuple[str, ...]:
    start, stop = int(first), int(last)
    step = int(step_text) if step_text is not None else 1
    if step == 0:
        raise ValueError("連番の刻みは 1 以上にしてください: {" + f"{first}..{last}..{step_text}" + "}")
    # 先頭が 0 の数（01 など）が端にあれば、桁をそろえる
    pad = max(len(first), len(last)) if (first.startswith("0") and len(first) > 1) or (
        last.startswith("0") and len(last) > 1
    ) else 0
    values = range(start, stop + 1, step) if start <= stop else range(start, stop - 1, -step)
    if len(values) > URL_LIMIT:  # 巨大な範囲を実際に作る前に止める
        raise ValueError(f"連番の範囲が上限（{URL_LIMIT} 件）を超えます: {first}..{last}")
    return tuple(str(v).zfill(pad) for v in values)


def _split(url: str) -> list[_Part]:
    parts: list[_Part] = []
    position = 0
    for match in _BRACE.finditer(url):
        body = match.group(1)
        range_match = _RANGE.match(body)
        if range_match:
            options = _range_options(*range_match.groups())
        elif "," in body:
            options = tuple(item.strip() for item in body.split(","))
            if any(not item for item in options):
                raise ValueError("列挙に空の要素があります: {" + body + "}")
        else:
            continue  # パターンではない波かっこは、そのまま残す
        parts.append(_Part(url[position:match.start()]))
        parts.append(_Part("", options))
        position = match.end()
    parts.append(_Part(url[position:]))
    return parts


def has_pattern(url: str) -> bool:
    return any(part.options is not None for part in _split(url))


def expand_urls(urls: list[str], limit: int = URL_LIMIT) -> list[str]:
    """規則を含む URL を展開する。重複は除き、順序は保つ。展開後が limit を超えたら ValueError。"""
    split_urls = [_split(url) for url in urls]
    total = 0
    for parts in split_urls:
        size = 1
        for part in parts:
            if part.options is not None:
                size *= len(part.options)
        total += size
        if total > limit:
            raise ValueError(f"URL の展開結果が上限（{limit} 件）を超えます。範囲を狭めてください。")
    result: list[str] = []
    seen: set[str] = set()
    for parts in split_urls:
        pools = [part.options if part.options is not None else (part.literal,) for part in parts]
        for combo in itertools.product(*pools):
            url = "".join(combo)
            if url not in seen:
                seen.add(url)
                result.append(url)
    return result

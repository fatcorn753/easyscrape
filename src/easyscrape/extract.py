"""CSS セレクタによる値の抽出。

セレクタの末尾に ``@属性名`` を付けると、テキストではなく属性値を取る。
例: ``a.item@href`` は ``a.item`` の href 属性を取得する。
"""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag


@dataclass(frozen=True)
class FieldSpec:
    name: str
    selector: str
    attr: str | None = None

    @classmethod
    def parse(cls, text: str) -> FieldSpec:
        """``名前=セレクタ[@属性]`` 形式の文字列を解釈する。"""
        if "=" not in text:
            raise ValueError(f"'名前=セレクタ' の形式で指定してください: {text}")
        name, rest = text.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"名前が空です: {text}")
        selector, attr = rest, None
        # 属性指定は最後の @ で区切る（セレクタ中の @ は想定しない）
        if "@" in rest:
            selector, attr = rest.rsplit("@", 1)
            attr = attr.strip() or None
        selector = selector.strip()
        if not selector:
            raise ValueError(f"セレクタが空です: {text}")
        return cls(name=name, selector=selector, attr=attr)


def _value(node: Tag, attr: str | None) -> str:
    if attr is None:
        return node.get_text(" ", strip=True)
    value = node.get(attr, "")
    # class などの複数値属性は list で返るため、空白区切りの文字列に戻す
    if isinstance(value, list):
        return " ".join(value)
    return str(value)


def extract_record(soup: BeautifulSoup | Tag, fields: list[FieldSpec]) -> dict[str, str]:
    """各フィールドについて最初に一致した要素の値を取る。一致しなければ空文字。"""
    record: dict[str, str] = {}
    for spec in fields:
        node = soup.select_one(spec.selector)
        record[spec.name] = _value(node, spec.attr) if node is not None else ""
    return record


def extract_rows(html: str, fields: list[FieldSpec], item_selector: str | None) -> list[dict[str, str]]:
    """ページから行を取り出す。

    item_selector が無い場合は、ページ全体で 1 行（各フィールドは最初の一致）。
    item_selector がある場合は、一致した要素ごとに 1 行。
    """
    soup = BeautifulSoup(html, "html.parser")
    if item_selector is None:
        return [extract_record(soup, fields)]
    return [extract_record(item, fields) for item in soup.select(item_selector)]

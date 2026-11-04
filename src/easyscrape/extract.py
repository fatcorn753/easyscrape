"""CSS セレクタによる値の抽出。

セレクタの末尾に ``@属性名`` を付けると、テキストではなく属性値を取る。
例: ``a.item@href`` は ``a.item`` の href 属性を取得する。
"""

from __future__ import annotations

import json
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


def _select(scope: BeautifulSoup | Tag, selector: str, many: bool = False):
    """CSS セレクタを適用する。構文が不正なら利用者向けの ValueError にする。"""
    try:
        return scope.select(selector) if many else scope.select_one(selector)
    except Exception as exc:  # soupsieve の構文エラーなど
        raise ValueError(f"セレクタが正しくありません: {selector}") from exc


def extract_record(soup: BeautifulSoup | Tag, fields: list[FieldSpec]) -> dict[str, str]:
    """各フィールドについて最初に一致した要素の値を取る。一致しなければ空文字。"""
    record: dict[str, str] = {}
    for spec in fields:
        node = _select(soup, spec.selector)
        record[spec.name] = _value(node, spec.attr) if node is not None else ""
    return record


def _dig(obj: object, path: str) -> object:
    """``a.b.0`` のようなドット区切りのパスで JSON の値を辿る。無ければ None。"""
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def _json_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def looks_like_json(text: str, content_type: str) -> bool:
    """Content-Type が JSON、または本文が { か [ で始まるなら JSON とみなす。

    JSON を text/plain で返す API があるため、Content-Type だけでは判定しない。
    """
    if "json" in content_type.lower():
        return True
    return text.lstrip()[:1] in ("{", "[")


def extract_json_rows(text: str, fields: list[FieldSpec], records_path: str | None) -> list[dict[str, str]]:
    """JSON から行を取り出す。

    records_path（例: ``result.all``）で配列を指す。省略時はルートが配列ならその要素、
    オブジェクトなら 1 行として扱う。フィールドの selector はドット区切りのキーパス。
    """
    data = json.loads(text)
    if records_path is not None:
        data = _dig(data, records_path)
        if not isinstance(data, list):
            raise ValueError(f"--records の位置に配列がありません: {records_path}")
    records = data if isinstance(data, list) else [data]
    rows = []
    for record in records:
        rows.append({spec.name: _json_text(_dig(record, spec.selector)) for spec in fields})
    return rows


def extract_rows(html: str, fields: list[FieldSpec], item_selector: str | None) -> list[dict[str, str]]:
    """ページから行を取り出す。

    item_selector が無い場合は、ページ全体で 1 行（各フィールドは最初の一致）。
    item_selector がある場合は、一致した要素ごとに 1 行（全項目が空の行は除く）。
    """
    soup = BeautifulSoup(html, "html.parser")
    if item_selector is None:
        return [extract_record(soup, fields)]
    # 一覧の見出し行など、どの項目も取れなかった要素は行にしない
    rows = [extract_record(item, fields) for item in _select(soup, item_selector, many=True)]
    return [row for row in rows if any(row.values())]

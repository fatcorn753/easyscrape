"""結果の書き出しと、個人情報らしき値の警告。"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path
from typing import TextIO

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 日本の携帯・固定電話の表記ゆれをざっくり拾う（検出は警告のみ）
PHONE_RE = re.compile(r"0\d{1,4}[-(]?\d{1,4}[-)]?\d{3,4}")


def warn_personal_info(rows: list[dict[str, str]], stream: TextIO = sys.stderr) -> None:
    """メールアドレスや電話番号らしき値があれば警告する。取得自体は止めない。"""
    hits = {"メールアドレス": 0, "電話番号": 0}
    for row in rows:
        for value in row.values():
            hits["メールアドレス"] += len(EMAIL_RE.findall(value))
            hits["電話番号"] += len(PHONE_RE.findall(value))
    for label, count in hits.items():
        if count:
            print(
                f"警告: {label}らしき値を {count} 件検出しました。"
                "利用目的と保管方法を確認してください。",
                file=stream,
            )


def render(rows: list[dict[str, str]], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    if fmt == "csv":
        buffer = io.StringIO()
        if rows:
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return buffer.getvalue()
    raise ValueError(f"未対応の形式: {fmt}")


def guess_format(path: str | None) -> str:
    """拡張子から形式を決める。拡張子が無いときは CSV。"""
    if path and Path(path).suffix.lower() == ".json":
        return "json"
    return "csv"


def write_rows(rows: list[dict[str, str]], path: str | None, fmt: str | None = None) -> None:
    text = render(rows, fmt or guess_format(path))
    if path is None:
        sys.stdout.write(text)
        return
    Path(path).write_text(text, encoding="utf-8", newline="")

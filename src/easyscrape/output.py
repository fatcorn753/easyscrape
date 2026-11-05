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
# 日本の電話番号（ハイフン区切り、または 10〜11 桁の連続）。
# 商品コードや ISBN など、前後に数字・英字・ハイフンが続く数列は除外する
PHONE_RE = re.compile(r"(?<![\w-])(?:\(?0\d{1,4}\)?[ -]?\d{1,4}-\d{4}|0\d{9,10})(?![\w-])")


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


def append_rows(rows: list[dict[str, str]], path: str, fmt: str | None = None) -> None:
    """既存ファイルの末尾に行を足す（繰り返し取得用）。無ければ新規に作る。

    CSV は見出し行が同じときだけ追記する。JSON は配列を読み直して足し、一時ファイル経由で置き換える。
    """
    if not rows:
        return
    target = Path(path)
    fmt = fmt or guess_format(path)
    if fmt == "csv":
        columns = list(rows[0].keys())
        exists = target.exists() and target.stat().st_size > 0
        if exists:
            with target.open(encoding="utf-8", newline="") as handle:
                header = next(csv.reader(handle), [])
            if header != columns:
                raise ValueError(f"既存ファイルの列が今回と違います（既存: {','.join(header)}）。別のファイル名にしてください。")
        with target.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            if not exists:
                writer.writeheader()
            writer.writerows(rows)
        return
    if fmt == "json":
        existing: list = []
        if target.exists() and target.stat().st_size > 0:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                raise ValueError("既存の JSON ファイルが配列ではありません。別のファイル名にしてください。")
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(existing + rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
        temp.replace(target)
        return
    raise ValueError(f"未対応の形式: {fmt}")


def read_rows(path: str, fmt: str | None = None) -> list[dict[str, str]]:
    """保存済みのファイルから行を読む。無い・空・壊れている場合は空のリスト。"""
    target = Path(path)
    if not target.exists() or target.stat().st_size == 0:
        return []
    try:
        if (fmt or guess_format(path)) == "json":
            data = json.loads(target.read_text(encoding="utf-8"))
            return [{k: str(v) for k, v in row.items()} for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        with target.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, ValueError, UnicodeDecodeError):
        return []

"""一定期間、同じ条件で繰り返し取得する。CLI と Web UI の両方から使う。

相手サイトへの負荷を抑えるため、間隔は MIN_EVERY 秒以上、期間は MAX_SPAN 秒以内に制限する。
実行はこのプロセスが動いている間だけ。プロセスを終了すると止まる。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from easyscrape.job import ScrapeJob, ScrapeResult, run_job

MIN_EVERY = 600.0  # 10 分
MAX_SPAN = 7 * 86400.0  # 7 日
FETCHED_AT = "fetched_at"
CHANGE = "change"

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")


def parse_duration(text: str) -> float:
    """``30m``、``24h``、``2d``、``90s`` のような時間を秒にする。"""
    match = _DURATION.match(text.strip().lower())
    if not match:
        raise ValueError(f"時間は 30m（分）、24h（時間）、2d（日）の形式で指定してください: {text}")
    return float(match.group(1)) * _UNITS[match.group(2)]


@dataclass(frozen=True)
class WatchPlan:
    every: float  # 秒
    cycles: int  # 取得する回数（最初の 1 回を含む）


def make_plan(
    every_text: str | None,
    for_text: str | None,
    times: int | None,
    min_every: float = MIN_EVERY,
) -> WatchPlan:
    """間隔と、期間（--for）または回数（--times）から計画を作る。"""
    if not every_text:
        raise ValueError("繰り返しの間隔（--every）を指定してください。")
    if (for_text is None) == (times is None):
        raise ValueError("期間（--for）か回数（--times）のどちらか一方を指定してください。")
    every = parse_duration(every_text)
    if every < min_every:
        raise ValueError(f"間隔は {int(min_every // 60)} 分以上にしてください（相手サイトへの負荷を抑えるため）。")
    if for_text is not None:
        span = parse_duration(for_text)
        cycles = int(span // every) + 1
    else:
        if times is None or times < 1:
            raise ValueError("回数は 1 以上にしてください。")
        cycles = times
    if (cycles - 1) * every > MAX_SPAN:
        raise ValueError(f"期間は最長 {int(MAX_SPAN // 86400)} 日までです。")
    return WatchPlan(every=every, cycles=cycles)


class ChangeTracker:
    """前回までに保存した行と比べ、新規または内容が変わった行だけを返す。"""

    def __init__(self, key: str | None) -> None:
        self.key = key
        self._seen: dict[tuple, tuple] = {}

    @staticmethod
    def _signature(row: dict[str, str]) -> tuple:
        return tuple((k, v) for k, v in row.items() if k not in (FETCHED_AT, CHANGE))

    def _ident(self, row: dict[str, str], signature: tuple) -> tuple:
        key_value = row.get(self.key, "") if self.key else ""
        # キーの値が空の行は、行全体を目印にする（同じ内容なら変更なし）
        return (row.get("url", ""), key_value) if key_value else (row.get("url", ""), "", signature)

    def seed(self, rows: list[dict[str, str]]) -> None:
        """保存済みの行（古い順）を既知の状態として取り込む。再起動しても同じ行を新規扱いにしないため。"""
        for row in rows:
            signature = self._signature(row)
            self._seen[self._ident(row, signature)] = signature

    def filter(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        changed: list[dict[str, str]] = []
        for row in rows:
            signature = self._signature(row)
            ident = self._ident(row, signature)
            previous = self._seen.get(ident)
            if previous == signature:
                continue
            self._seen[ident] = signature
            changed.append({**row, CHANGE: "new" if previous is None else "changed"})
        return changed


@dataclass
class WatchStatus:
    """実行状況。別スレッドの画面から読む。"""

    planned: int = 0
    done: int = 0
    rows_saved: int = 0
    rows_fetched: int = 0  # 取得できた行の合計（保存しなかった行を含む）
    state: str = "waiting"  # waiting / running / done / stopped / failed
    last_run: str = ""
    next_run: str = ""
    messages: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.messages.append(text)
        del self.messages[:-20]  # 直近 20 件だけ保持


def run_watch(
    job: ScrapeJob,
    plan: WatchPlan,
    sink: Callable[[list[dict[str, str]]], None],
    status: WatchStatus,
    key: str | None = None,
    only_changes: bool = False,
    run: Callable[[ScrapeJob], ScrapeResult] = run_job,
    stop: threading.Event | None = None,
    now: Callable[[], datetime] = datetime.now,
    log: Callable[[str], None] | None = None,
    seed_rows: list[dict[str, str]] | None = None,
) -> WatchStatus:
    """plan の回数だけ取得して sink に渡す。stop が立つか、保存に失敗したら止める。"""
    stop = stop or threading.Event()
    tracker = ChangeTracker(key) if only_changes else None
    if tracker is not None and seed_rows:
        tracker.seed(seed_rows)
    status.planned = plan.cycles

    def say(text: str) -> None:
        status.note(text)
        if log:
            log(text)

    started = now()
    for index in range(plan.cycles):
        if stop.is_set():
            break
        status.state = "running"
        status.next_run = ""
        result = run(job)
        stamp = now().isoformat(timespec="seconds")
        status.last_run = stamp
        for error in result.errors:
            say(error)
        rows = [{FETCHED_AT: stamp, **row} for row in result.rows]
        if tracker is not None:
            rows = tracker.filter(rows)
        try:
            if rows:
                sink(rows)
        except (OSError, ValueError) as exc:
            status.state = "failed"
            say(f"保存できませんでした: {exc}")
            return status
        status.done = index + 1
        status.rows_saved += len(rows)
        status.rows_fetched += len(result.rows)
        say(f"{index + 1}/{plan.cycles} 回目: 取得 {len(result.rows)} 行、保存 {len(rows)} 行")
        if index + 1 >= plan.cycles:
            break
        # 取得に時間がかかっても、次の開始時刻は最初の時刻から数える（遅れを積み上げない）
        wait = max(0.0, plan.every * (index + 1) - (now() - started).total_seconds())
        status.state = "waiting"
        status.next_run = datetime.fromtimestamp(now().timestamp() + wait).isoformat(timespec="seconds")
        if stop.wait(wait):
            break
    status.state = "stopped" if stop.is_set() else "done"
    status.next_run = ""
    return status

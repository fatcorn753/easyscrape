import csv
import json
import threading
from datetime import datetime, timedelta

import pytest

from easyscrape.extract import FieldSpec
from easyscrape.job import ScrapeJob, ScrapeResult
from easyscrape.output import append_rows
from easyscrape.urlpattern import URL_LIMIT, expand_urls, has_pattern
from easyscrape.watch import (
    ChangeTracker,
    WatchPlan,
    WatchStatus,
    make_plan,
    parse_duration,
    run_watch,
)


# ---- URL の規則指定 ----

def test_expand_numeric_range_and_padding():
    assert expand_urls(["https://x.test/p{1..3}"]) == ["https://x.test/p1", "https://x.test/p2", "https://x.test/p3"]
    assert expand_urls(["https://x.test/{08..10}.html"]) == [
        "https://x.test/08.html", "https://x.test/09.html", "https://x.test/10.html",
    ]
    assert expand_urls(["a{1..10..4}"]) == ["a1", "a5", "a9"]
    assert expand_urls(["a{3..1}"]) == ["a3", "a2", "a1"]


def test_expand_list_and_combination_and_dedupe():
    assert expand_urls(["https://x.test/{tokyo,osaka}/p{1..2}"]) == [
        "https://x.test/tokyo/p1", "https://x.test/tokyo/p2",
        "https://x.test/osaka/p1", "https://x.test/osaka/p2",
    ]
    assert expand_urls(["a{1..2}", "a{2..3}"]) == ["a1", "a2", "a3"]


def test_expand_leaves_non_patterns_alone():
    url = "https://x.test/search?q={word}&json={}"
    assert expand_urls([url]) == [url]
    assert not has_pattern(url)
    assert has_pattern("https://x.test/{1..2}")


def test_expand_limits_and_errors():
    with pytest.raises(ValueError, match="上限"):
        expand_urls([f"a{{1..{URL_LIMIT + 1}}}"])
    with pytest.raises(ValueError, match="上限"):
        expand_urls(["a{1..20}{1..20}"])  # 組み合わせで 400 件
    with pytest.raises(ValueError, match="上限"):
        expand_urls(["a{1..999999999}"])  # 巨大な範囲は作る前に止まる
    with pytest.raises(ValueError, match="刻み"):
        expand_urls(["a{1..5..0}"])
    with pytest.raises(ValueError, match="空の要素"):
        expand_urls(["a{x,,y}"])


# ---- 時間と計画 ----

def test_parse_duration():
    assert parse_duration("30m") == 1800
    assert parse_duration("24h") == 86400
    assert parse_duration("2d") == 172800
    with pytest.raises(ValueError):
        parse_duration("abc")
    with pytest.raises(ValueError):
        parse_duration("10")


def test_make_plan_rules():
    plan = make_plan("30m", "24h", None)
    assert plan == WatchPlan(every=1800, cycles=49)
    assert make_plan("1h", None, 5) == WatchPlan(every=3600, cycles=5)
    with pytest.raises(ValueError, match="10 分以上"):
        make_plan("5m", "1h", None)
    with pytest.raises(ValueError, match="どちらか一方"):
        make_plan("30m", "1h", 3)
    with pytest.raises(ValueError, match="どちらか一方"):
        make_plan("30m", None, None)
    with pytest.raises(ValueError, match="最長 7 日"):
        make_plan("1h", "8d", None)
    with pytest.raises(ValueError, match="間隔"):
        make_plan(None, "1h", None)


# ---- 変更の検出 ----

def test_change_tracker_new_changed_unchanged():
    tracker = ChangeTracker("id")
    first = tracker.filter([
        {"fetched_at": "t1", "url": "u", "id": "1", "price": "100"},
        {"fetched_at": "t1", "url": "u", "id": "2", "price": "200"},
    ])
    assert [r["change"] for r in first] == ["new", "new"]
    second = tracker.filter([
        {"fetched_at": "t2", "url": "u", "id": "1", "price": "100"},  # 変化なし（時刻だけ違う）
        {"fetched_at": "t2", "url": "u", "id": "2", "price": "250"},  # 変更
        {"fetched_at": "t2", "url": "u", "id": "3", "price": "300"},  # 新規
    ])
    assert [(r["id"], r["change"]) for r in second] == [("2", "changed"), ("3", "new")]


def test_change_tracker_separates_urls_and_empty_key():
    tracker = ChangeTracker("id")
    rows = tracker.filter([
        {"fetched_at": "t", "url": "a", "id": "1", "v": "x"},
        {"fetched_at": "t", "url": "b", "id": "1", "v": "x"},  # 別 URL の同じ id は別の行
    ])
    assert len(rows) == 2
    empty = ChangeTracker("id")
    assert len(empty.filter([{"fetched_at": "t", "url": "a", "id": "", "v": "x"}])) == 1
    assert empty.filter([{"fetched_at": "t2", "url": "a", "id": "", "v": "x"}]) == []


# ---- 追記 ----

def test_append_rows_csv_header_once_and_mismatch(tmp_path):
    path = str(tmp_path / "out.csv")
    append_rows([{"a": "1", "b": "あ"}], path)
    append_rows([{"a": "2", "b": "い"}], path)
    with open(path, encoding="utf-8", newline="") as handle:
        assert list(csv.reader(handle)) == [["a", "b"], ["1", "あ"], ["2", "い"]]
    with pytest.raises(ValueError, match="列が今回と違います"):
        append_rows([{"x": "1"}], path)


def test_append_rows_json(tmp_path):
    path = str(tmp_path / "out.json")
    append_rows([{"a": "1"}], path)
    append_rows([{"a": "2"}], path)
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == [{"a": "1"}, {"a": "2"}]
    assert not (tmp_path / "out.json.tmp").exists()
    (tmp_path / "obj.json").write_text('{"a": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="配列ではありません"):
        append_rows([{"a": "1"}], str(tmp_path / "obj.json"))


# ---- 繰り返し実行 ----

def _job():
    return ScrapeJob(urls=["https://x.test/"], fields=[FieldSpec.parse("id=id")])


def test_run_watch_runs_all_cycles_and_adds_fetched_at():
    calls = []
    saved = []

    def fake_run(job):
        calls.append(1)
        return ScrapeResult(rows=[{"url": "https://x.test/", "id": str(len(calls))}])

    status = WatchStatus()
    run_watch(_job(), WatchPlan(every=0.01, cycles=3), saved.extend, status, run=fake_run)
    assert len(calls) == 3
    assert status.state == "done" and status.done == 3 and status.rows_saved == 3
    assert all("fetched_at" in row for row in saved)
    assert list(saved[0].keys())[0] == "fetched_at"


def test_run_watch_only_changes_saves_just_differences():
    outputs = iter([
        [{"url": "u", "id": "1", "v": "a"}],
        [{"url": "u", "id": "1", "v": "a"}],   # 変化なし
        [{"url": "u", "id": "1", "v": "b"}],   # 変更
    ])
    saved = []
    status = WatchStatus()
    run_watch(_job(), WatchPlan(every=0.01, cycles=3), saved.extend, status, key="id", only_changes=True,
              run=lambda job: ScrapeResult(rows=next(outputs)))
    assert [r["change"] for r in saved] == ["new", "changed"]
    assert status.rows_saved == 2 and status.done == 3


def test_run_watch_stop_event_ends_early():
    stop = threading.Event()
    status = WatchStatus()

    def fake_run(job):
        stop.set()  # 1 回目の後で止める
        return ScrapeResult(rows=[{"url": "u", "id": "1"}])

    run_watch(_job(), WatchPlan(every=60, cycles=5), lambda rows: None, status, run=fake_run, stop=stop)
    assert status.state == "stopped" and status.done == 1


def test_run_watch_sink_failure_stops_with_message():
    def bad_sink(rows):
        raise OSError("ディスクがいっぱい")

    status = WatchStatus()
    run_watch(_job(), WatchPlan(every=0.01, cycles=3), bad_sink, status,
              run=lambda job: ScrapeResult(rows=[{"url": "u", "id": "1"}]))
    assert status.state == "failed" and status.done == 0
    assert any("保存できませんでした" in m for m in status.messages)


def test_run_watch_records_fetch_errors_and_continues():
    status = WatchStatus()
    run_watch(_job(), WatchPlan(every=0.01, cycles=2), lambda rows: None, status,
              run=lambda job: ScrapeResult(errors=["スキップ: 取得失敗"]))
    assert status.done == 2 and status.rows_saved == 0
    assert any("スキップ" in m for m in status.messages)


def test_run_watch_uses_injected_clock_for_timestamp():
    base = datetime(2026, 11, 5, 9, 0, 0)
    ticks = iter(base + timedelta(seconds=i) for i in range(100))
    saved = []
    run_watch(_job(), WatchPlan(every=0.01, cycles=1), saved.extend, WatchStatus(),
              run=lambda job: ScrapeResult(rows=[{"url": "u", "id": "1"}]), now=lambda: next(ticks))
    assert saved[0]["fetched_at"].startswith("2026-11-05T09:00:")


def test_change_tracker_seed_prevents_duplicates_after_restart():
    # 保存済みの行（change 列つき）を取り込むと、同じ内容の行は新規扱いにならない
    tracker = ChangeTracker("id")
    tracker.seed([
        {"fetched_at": "t0", "url": "u", "id": "1", "price": "100", "change": "new"},
        {"fetched_at": "t1", "url": "u", "id": "1", "price": "150", "change": "changed"},
    ])
    rows = tracker.filter([
        {"fetched_at": "t2", "url": "u", "id": "1", "price": "150"},  # 最新の保存内容と同じ
        {"fetched_at": "t2", "url": "u", "id": "2", "price": "20"},
    ])
    assert [(r["id"], r["change"]) for r in rows] == [("2", "new")]


def test_read_rows_handles_missing_empty_and_json(tmp_path):
    from easyscrape.output import read_rows

    assert read_rows(str(tmp_path / "none.csv")) == []
    (tmp_path / "empty.csv").write_text("", encoding="utf-8")
    assert read_rows(str(tmp_path / "empty.csv")) == []
    (tmp_path / "a.json").write_text('[{"a": 1}, "x"]', encoding="utf-8")
    assert read_rows(str(tmp_path / "a.json")) == [{"a": "1"}]
    (tmp_path / "bad.json").write_text("{oops", encoding="utf-8")
    assert read_rows(str(tmp_path / "bad.json")) == []


def test_run_watch_seed_rows(tmp_path):
    saved = []
    run_watch(_job(), WatchPlan(every=0.01, cycles=1), saved.extend, WatchStatus(), key="id", only_changes=True,
              seed_rows=[{"fetched_at": "t0", "url": "u", "id": "1", "v": "a", "change": "new"}],
              run=lambda job: ScrapeResult(rows=[{"url": "u", "id": "1", "v": "a"}, {"url": "u", "id": "2", "v": "b"}]))
    assert [r["id"] for r in saved] == ["2"]

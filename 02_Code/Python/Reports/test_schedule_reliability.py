"""Fixture-based tests for schedule_reliability.py.

Validates calendar bucketing, missing-slot detection, freshness rollup,
event-mix logic, and the bounded-write helper. No filesystem writes
to the real data dir, no network.

Run: python 02_Code/Python/Reports/test_schedule_reliability.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import schedule_reliability as sr  # noqa: E402


def _utc(s: str) -> str:
    """Helper: build an ISO8601-Z string for a 'YYYY-MM-DD HH:MM' UTC arg."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(ts_chi_date: str, slot: str, *, event="schedule", proceeded=True,
            ts_utc=None, run_url=None, sha=None, skip_reason=None) -> dict:
    r = {
        "ts_utc": ts_utc or _utc("2026-05-04 14:00"),
        "ts_chicago": ts_chi_date + " 09:00",
        "chicago_date": ts_chi_date,
        "event_name": event,
        "slot": slot,
        "proceeded": proceeded,
    }
    if run_url:
        r["run_url"] = run_url
    if sha:
        r["commit_sha"] = sha
    if skip_reason:
        r["skip_reason"] = skip_reason
    return r


def test_slot_window_lookup():
    assert sr._slot_for_chicago_hm("08:45") == "morning"
    assert sr._slot_for_chicago_hm("11:59") == "morning"
    assert sr._slot_for_chicago_hm("12:30") == "midday"
    assert sr._slot_for_chicago_hm("15:35") == "close"
    assert sr._slot_for_chicago_hm("23:00") is None


def test_trading_days_skips_weekend():
    days = sr._trading_days(date(2026, 5, 4), 5)  # Mon
    assert days == [
        date(2026, 5, 4),
        date(2026, 5, 1),
        date(2026, 4, 30),
        date(2026, 4, 29),
        date(2026, 4, 28),
    ]


def test_calendar_marks_missing_morning():
    # Five trading days, but Mon has no morning slot delivered.
    today = date(2026, 5, 4)
    runs = []
    days = ["2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01", "2026-05-04"]
    for d in days:
        for slot in ("morning", "midday", "close"):
            if d == "2026-05-04" and slot == "morning":
                continue
            runs.append(_record(d, slot))
    cal = sr.analyze_slot_calendar(runs, today)
    assert cal["missing_count"] == 1, cal
    monday = next(r for r in cal["rows"] if r["date"] == "2026-05-04")
    assert "morning" in monday["missing"], monday


def test_calendar_marks_duplicates():
    today = date(2026, 5, 4)
    runs = [
        _record("2026-05-04", "morning"),
        _record("2026-05-04", "morning"),  # duplicate
        _record("2026-05-04", "midday"),
        _record("2026-05-04", "close"),
    ]
    # Pad earlier days so they aren't reported as missing.
    for d in ("2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01"):
        for slot in ("morning", "midday", "close"):
            runs.append(_record(d, slot))
    cal = sr.analyze_slot_calendar(runs, today)
    assert cal["duplicate_count"] >= 1, cal
    monday = next(r for r in cal["rows"] if r["date"] == "2026-05-04")
    assert "morning" in monday["duplicate"], monday


def test_recency_warn_on_unparsed_as_of():
    out = sr.analyze_recency([], rankings={"as_of": "garbage"})
    fr = next(c for c in out["checks"] if c["name"] == "rankings_freshness")
    assert fr["status"] == "FAIL", fr


def test_event_mix_warns_when_dispatch_dominates():
    base_dt = sr._now_utc() - timedelta(days=2)
    base = base_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [
        _record("2026-05-01", "morning", event="workflow_dispatch", ts_utc=base),
        _record("2026-05-01", "midday", event="workflow_dispatch", ts_utc=base),
        _record("2026-05-01", "close",  event="workflow_dispatch", ts_utc=base),
        _record("2026-05-04", "morning", event="schedule", ts_utc=base),
    ]
    out = sr.analyze_event_mix(runs)
    mix = next(c for c in out["checks"] if c["name"] == "event_mix")
    assert mix["status"] == "WARN", mix


def test_event_mix_ok_when_schedule_dominant():
    base = (sr._now_utc() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [_record("2026-05-04", s, event="schedule", ts_utc=base) for s in ("morning","midday","close")]
    out = sr.analyze_event_mix(runs)
    mix = next(c for c in out["checks"] if c["name"] == "event_mix")
    assert mix["status"] == "OK", mix


def test_skip_pattern_counts():
    base = (sr._now_utc() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [
        _record("2026-05-04", "morning", event="schedule", proceeded=True, ts_utc=base),
        _record("2026-05-04", "morning", event="schedule", proceeded=False, ts_utc=base, skip_reason="morning"),
        _record("2026-05-04", "morning", event="schedule", proceeded=False, ts_utc=base, skip_reason="morning"),
    ]
    out = sr.analyze_skip_pattern(runs)
    assert out["metrics"]["proceed_14d"] == 1
    assert out["metrics"]["skip_14d"] == 2


def test_append_run_record_trims_to_max(monkeypatch=None):
    # Use a tempdir for the cache to avoid touching repo data.
    tmp = tempfile.TemporaryDirectory()
    try:
        original = sr.RUNS_JSONL
        sr.RUNS_JSONL = Path(tmp.name) / "workflow_runs.jsonl"
        sr.DATA_REPORTS_DIR = Path(tmp.name)

        # Write 600 records, trimmed to 500.
        for i in range(600):
            sr.append_run_record({
                "ts_utc": (sr._now_utc() - timedelta(seconds=600 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ts_chicago": "2026-05-04 09:00",
                "chicago_date": "2026-05-04",
                "event_name": "schedule",
                "slot": "morning",
                "proceeded": True,
            })
        with sr.RUNS_JSONL.open() as f:
            lines = f.readlines()
        assert len(lines) == sr.MAX_RUN_HISTORY, len(lines)
    finally:
        sr.RUNS_JSONL = original
        sr.DATA_REPORTS_DIR = original.parent
        tmp.cleanup()


def test_append_run_record_drops_old():
    tmp = tempfile.TemporaryDirectory()
    try:
        original = sr.RUNS_JSONL
        sr.RUNS_JSONL = Path(tmp.name) / "workflow_runs.jsonl"
        sr.DATA_REPORTS_DIR = Path(tmp.name)
        # Old record: 200 days ago -> dropped (HISTORY_DAYS=90)
        old_ts = (sr._now_utc() - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_ts = sr._now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        # Seed file directly with one old record.
        sr.RUNS_JSONL.parent.mkdir(parents=True, exist_ok=True)
        sr.RUNS_JSONL.write_text(json.dumps({"ts_utc": old_ts, "slot": "morning"}) + "\n")
        sr.append_run_record({"ts_utc": new_ts, "slot": "morning", "proceeded": True})
        rows = sr.load_runs_jsonl()
        # Only the fresh one survives.
        assert len(rows) == 1, rows
        assert rows[0]["ts_utc"] == new_ts
    finally:
        sr.RUNS_JSONL = original
        sr.DATA_REPORTS_DIR = original.parent
        tmp.cleanup()


def test_render_html_contains_calendar_and_overall():
    # Build a minimal report.
    runs = [_record("2026-05-04", s) for s in ("morning", "midday", "close")]
    rankings = {"as_of": (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d ") + "09:00 AM CDT"}
    cal = sr.analyze_slot_calendar(runs, date(2026, 5, 4))
    sections = {
        "calendar": {"checks": [{"name": "missing_slots", "status": "OK", "message": "ok"}],
                     "metrics": {"calendar": cal}, "status": "OK"},
        "recency": {"checks": [{"name": "rankings_freshness", "status": "OK", "message": "fresh"}],
                    "metrics": {}, "status": "OK"},
    }
    report = {
        "generated_at": "2026-05-04T20:00:00Z",
        "generated_at_chicago": "2026-05-04 03:00 PM CDT",
        "overall": "OK",
        "sections": sections,
    }
    html = sr._render_html(report)
    assert "Schedule Reliability" in html
    assert "Recent Slot Delivery" in html
    assert "morning" in html


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"FAIL: {failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

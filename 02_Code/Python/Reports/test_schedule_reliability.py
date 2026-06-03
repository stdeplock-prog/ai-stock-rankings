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


def _sections_with_today(today_str: str, *, hits: dict, missing: list,
                         missing_count: int, age_h: float = 0.5,
                         is_weekend: bool = False,
                         last_event: str = "schedule",
                         last_ts_chicago: str | None = None) -> dict:
    """Build a minimal sections dict for compute_effective_overall tests.

    age_h is the live rankings freshness (the dominant signal post-fix).
    last_ts_chicago lets a test pin the most-recent proceeding run's
    Chicago timestamp, used by the current-slot-coverage check when the
    data is NOT fresh.
    """
    last_run = {"event_name": last_event, "slot": "morning",
                "ts_chicago": last_ts_chicago or (today_str + " 09:00")}
    return {
        "calendar": {"metrics": {"calendar": {
            "rows": [
                {"date": "2026-04-29",
                 "slot_hits": {"morning": 0, "midday": 0, "close": 0},
                 "missing": ["morning", "midday", "close"]},
                {"date": today_str, "slot_hits": hits, "missing": missing},
            ],
            "missing_count": missing_count, "duplicate_count": 0,
            "lookback_days": 5,
        }}},
        "recency": {"metrics": {
            "rankings_age_hours": age_h,
            "is_weekend": is_weekend,
            "last_run": last_run,
        }},
    }


def test_compute_effective_recovered_when_fresh_despite_missing_slot():
    # The 2026-06-02 scenario: a delayed morning run lands in the midday
    # window so the calendar flags 'midday' missing, but the live data is
    # fresh. Effective must downgrade FAIL -> WARN (recovered), NOT stay
    # FAIL just because one bookkeeping slot shows missing.
    today = sr._to_chicago(sr._now_utc()).date().strftime("%Y-%m-%d")
    secs = _sections_with_today(today,
                                 hits={"morning": 1, "midday": 0, "close": 1},
                                 missing=["midday"], missing_count=5, age_h=0.2)
    eff = sr.compute_effective_overall("FAIL", secs)
    assert eff["effective"] == "WARN", eff
    assert eff["recovered"] is True
    assert eff["rankings_fresh"] is True
    assert "fresh" in eff["reason"].lower()


def test_compute_effective_recovered_when_today_fully_satisfied_and_fresh():
    today = sr._to_chicago(sr._now_utc()).date().strftime("%Y-%m-%d")
    secs = _sections_with_today(today,
                                 hits={"morning": 1, "midday": 1, "close": 1},
                                 missing=[], missing_count=3, age_h=0.2)
    eff = sr.compute_effective_overall("FAIL", secs)
    assert eff["effective"] == "WARN", eff
    assert eff["recovered"] is True


def test_compute_effective_active_fail_when_data_stale_and_slot_missing():
    # Stale data + the current expected slot has no run today => active FAIL.
    today = sr._to_chicago(sr._now_utc()).date().strftime("%Y-%m-%d")
    secs = _sections_with_today(today,
                                 hits={"morning": 0, "midday": 0, "close": 0},
                                 missing=["morning", "midday", "close"],
                                 missing_count=3, age_h=10.0,
                                 last_ts_chicago="2026-04-29 09:00")
    eff = sr.compute_effective_overall("FAIL", secs)
    assert eff["effective"] == "FAIL", eff
    assert eff["recovered"] is False
    assert "stale" in eff["reason"].lower()


def test_compute_effective_active_fail_when_data_stale():
    today = sr._to_chicago(sr._now_utc()).date().strftime("%Y-%m-%d")
    secs = _sections_with_today(today,
                                 hits={"morning": 1, "midday": 1, "close": 1},
                                 missing=[], missing_count=3,
                                 age_h=10.0)  # weekday => >= 6.0h is stale
    eff = sr.compute_effective_overall("FAIL", secs)
    assert eff["effective"] == "FAIL", eff
    assert eff["recovered"] is False
    assert "stale" in eff["reason"].lower()


def test_compute_effective_active_fail_when_no_refresh_on_record():
    # No last_run and no parseable age => cannot establish a refresh =>
    # active FAIL.
    today = sr._to_chicago(sr._now_utc()).date().strftime("%Y-%m-%d")
    secs = {
        "calendar": {"metrics": {"calendar": {
            "rows": [{"date": today,
                      "slot_hits": {"morning": 0, "midday": 0, "close": 0},
                      "missing": ["morning", "midday", "close"]}],
            "missing_count": 3, "duplicate_count": 0, "lookback_days": 5,
        }}},
        "recency": {"metrics": {
            "rankings_age_hours": None, "is_weekend": False, "last_run": {},
        }},
    }
    eff = sr.compute_effective_overall("FAIL", secs)
    assert eff["effective"] == "FAIL", eff
    assert eff["recovered"] is False


def test_compute_effective_passes_through_when_raw_ok_or_warn():
    today = sr._to_chicago(sr._now_utc()).date().strftime("%Y-%m-%d")
    secs = _sections_with_today(today,
                                 hits={"morning": 1, "midday": 1, "close": 1},
                                 missing=[], missing_count=0)
    assert sr.compute_effective_overall("OK", secs)["effective"] == "OK"
    # WARN raw (e.g. duplicate-only diagnostic) with fresh data stays WARN,
    # never escalates.
    warn = sr.compute_effective_overall("WARN", secs)
    assert warn["effective"] == "WARN"
    assert warn["recovered"] is False


def test_compute_effective_current_slot_helper():
    # Pre-morning weekday -> no expected slot.
    pre = datetime(2026, 6, 1, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert sr._current_expected_slot(pre) is None
    # Inside morning window.
    mid_morning = datetime(2026, 6, 1, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert sr._current_expected_slot(mid_morning) == "morning"
    # Gap between morning and midday windows -> most recent passed slot.
    gap = datetime(2026, 6, 1, 12, 15, tzinfo=timezone(timedelta(hours=-5)))
    assert sr._current_expected_slot(gap) == "morning"
    # After close -> close.
    evening = datetime(2026, 6, 1, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert sr._current_expected_slot(evening) == "close"
    # Weekend -> None.
    sat = datetime(2026, 6, 6, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert sr._current_expected_slot(sat) is None


def test_build_report_emits_overall_raw_and_effective():
    """Build a minimal report end-to-end and confirm both fields land."""
    today = sr._to_chicago(sr._now_utc()).date().strftime("%Y-%m-%d")
    # Stub out IO so build_report doesn't read the real repo's history.
    saved = (sr.RUNS_JSONL, sr.load_rankings)
    try:
        tmp = tempfile.TemporaryDirectory()
        sr.RUNS_JSONL = Path(tmp.name) / "workflow_runs.jsonl"
        sr.RUNS_JSONL.parent.mkdir(parents=True, exist_ok=True)
        # Three slot hits today, two prior weekdays empty -> raw FAIL,
        # effective WARN (recovered).
        runs = [_record(today, s, ts_utc=sr._now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"))
                for s in ("morning", "midday", "close")]
        sr.RUNS_JSONL.write_text("\n".join(json.dumps(r) for r in runs) + "\n")
        # Stub rankings as fresh
        sr.load_rankings = lambda: {  # type: ignore[assignment]
            "as_of": (sr._to_chicago(sr._now_utc())).strftime("%Y-%m-%d ")
                     + "09:00 AM " + ("CDT" if sr._to_chicago(sr._now_utc()).utcoffset().total_seconds() == -5 * 3600 else "CST")
        }
        report = sr.build_report()
        assert "overall_raw" in report
        assert "overall_effective" in report
        # If today is satisfied and fresh, FAIL raw downgrades to WARN.
        if report["overall_raw"] == "FAIL":
            assert report["overall_effective"] in ("WARN", "FAIL"), report
            if report["overall_effective"] == "WARN":
                assert report["effective"]["recovered"] is True
    finally:
        sr.RUNS_JSONL, sr.load_rankings = saved
        try:
            tmp.cleanup()
        except Exception:
            pass


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
        "overall_raw": "OK",
        "overall_effective": "OK",
        "effective": {"effective": "OK", "recovered": False,
                      "reason": "all good"},
        "sections": sections,
    }
    html = sr._render_html(report)
    assert "Schedule Reliability" in html
    assert "Recent Slot Delivery" in html
    assert "morning" in html
    # Both raw + effective surfaced in header.
    assert "Effective" in html and "Raw history" in html


def test_render_html_shows_recovered_banner():
    sections = {
        "calendar": {"checks": [], "metrics": {"calendar": {
            "rows": [{"date": "2026-05-06",
                      "slot_hits": {"morning": 1, "midday": 1, "close": 1},
                      "missing": []}],
            "missing_count": 3, "lookback_days": 5,
        }}, "status": "FAIL"},
        "recency": {"checks": [], "metrics": {
            "rankings_age_hours": 0.2, "is_weekend": False,
        }, "status": "OK"},
    }
    report = {
        "generated_at": "2026-05-06T20:00:00Z",
        "generated_at_chicago": "2026-05-06 03:00 PM CDT",
        "overall": "FAIL",
        "overall_raw": "FAIL",
        "overall_effective": "WARN",
        "effective": {"effective": "WARN", "recovered": True,
                      "reason": "today satisfied, history has misses"},
        "sections": sections,
    }
    html = sr._render_html(report)
    assert "diagnostic" in html.lower(), html[:600]
    assert "today satisfied" in html


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

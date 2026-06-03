"""Fixture-based tests for midday_health_check.py.

Exercises the status-rollup logic for the documented cases:
  * all-OK happy path
  * stale rankings.json -> overall FAIL
  * data_quality FAIL on rankings -> overall FAIL via critical-section rule
  * schedule_reliability FAIL but today satisfied + manual dispatch ->
    overall WARN (recovered downgrade)
  * SUPP coverage degraded -> WARN
  * parity FAIL stays WARN (advisory)

No filesystem writes to the real data dir, no network. Builds a fresh
fixture set per case and calls the analyzers directly so the rules are
verifiable without the wider workflow.

Run: python 02_Code/Python/Reports/test_midday_health_check.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import midday_health_check as mhc  # noqa: E402


# ------------------- fixture builders -------------------


def _now_chi_str() -> str:
    """Format the current time as the rankings as_of label so the
    freshness check sees age ~0h regardless of when the test runs."""
    now_utc = datetime.now(timezone.utc)
    chi = mhc._to_chicago(now_utc)
    label = "CDT" if chi.utcoffset().total_seconds() == -5 * 3600 else "CST"
    return chi.strftime("%Y-%m-%d %I:%M %p ") + label


def _today_chi_date_str() -> str:
    return mhc._to_chicago(datetime.now(timezone.utc)).date().strftime("%Y-%m-%d")


def fresh_rankings(rows: int = 100) -> dict:
    return {
        "as_of": _now_chi_str(),
        "open_date": _today_chi_date_str(),
        "is_open_run": False,
        "rows": [{"ticker": f"T{i}"} for i in range(rows)],
    }


def stale_rankings(hours: int = 48) -> dict:
    """Rankings marked older than the weekday FAIL threshold."""
    now_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    chi = mhc._to_chicago(now_utc)
    label = "CDT" if chi.utcoffset().total_seconds() == -5 * 3600 else "CST"
    return {
        "as_of": chi.strftime("%Y-%m-%d %I:%M %p ") + label,
        "open_date": chi.strftime("%Y-%m-%d"),
        "rows": [{"ticker": "T1"}],
    }


def healthy_dq() -> dict:
    return {
        "generated_at": "2026-05-05T17:00:00Z",
        "overall": "OK",
        "sections": {
            "rankings": {"present": True, "checks": [
                {"name": "row_count", "status": "OK", "message": "100"},
            ]},
            "watchlist": {"present": True, "checks": []},
            "tasks": {"present": True, "checks": []},
        },
    }


def fail_dq_rankings() -> dict:
    return {
        "generated_at": "2026-05-05T17:00:00Z",
        "overall": "FAIL",
        "sections": {
            "rankings": {"present": True, "checks": [
                {"name": "row_count", "status": "FAIL", "message": "0 rows"},
            ]},
            "watchlist": {"present": True, "checks": []},
            "tasks": {"present": True, "checks": []},
        },
    }


def schedule_ok() -> dict:
    today = _today_chi_date_str()
    return {
        "overall": "OK",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today,
                          "slot_hits": {"morning": 1, "midday": 1, "close": 1},
                          "missing": [], "duplicate": []}],
                "missing_count": 0, "duplicate_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "schedule", "slot": "midday",
                "ts_chicago": today + " 12:32",
            }}},
        },
    }


def schedule_fail_recovered() -> dict:
    """FAIL overall driven by historical missing slots, but TODAY is
    fully covered AND the latest run is workflow_dispatch."""
    today = _today_chi_date_str()
    return {
        "overall": "FAIL",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [
                    {"date": "2026-04-29",
                     "slot_hits": {"morning": 0, "midday": 0, "close": 0},
                     "missing": ["morning", "midday", "close"], "duplicate": []},
                    {"date": today,
                     "slot_hits": {"morning": 1, "midday": 1, "close": 1},
                     "missing": [], "duplicate": []},
                ],
                "missing_count": 3, "duplicate_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "workflow_dispatch", "slot": "manual",
                "ts_chicago": today + " 12:32",
            }}},
        },
    }


def schedule_fail_today_missing() -> dict:
    """FAIL where today still has missing slots — should NOT be recovered."""
    today = _today_chi_date_str()
    return {
        "overall": "FAIL",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today,
                          "slot_hits": {"morning": 0, "midday": 0, "close": 0},
                          "missing": ["morning", "midday", "close"], "duplicate": []}],
                "missing_count": 3, "duplicate_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {}}},
        },
    }


def watchlist_ok() -> dict:
    return {
        "as_of": _now_chi_str(),
        "rows": [],
        "source_meta": {
            "scored": 143, "unavailable_count": 1,
            "supp_summary": {"total": 76, "full_fundamentals": 65,
                             "price_only": 9, "technical_only": 2,
                             "metadata_only": 0, "eodhd_enriched": 0,
                             "unavailable": 1},
            "yfinance_info_cache": {"cache_hit_fresh": 45, "cache_miss": 31},
        },
    }


def watchlist_degraded() -> dict:
    return {
        "as_of": _now_chi_str(),
        "rows": [],
        "source_meta": {
            "scored": 143,
            "supp_summary": {"total": 76, "full_fundamentals": 5,
                             "price_only": 60, "technical_only": 11,
                             "metadata_only": 0, "eodhd_enriched": 0},
            "yfinance_info_cache": {"cache_hit_fresh": 0, "cache_miss": 76},
        },
    }


def benchmark_ok() -> dict:
    return {
        "snapshots_kept": 3,
        "snapshot_summary": {"horizons": {
            "1d": {"completed": 2, "pending": 0, "buckets": {}},
        }},
        "findings": [
            {"name": "sector_concentration:main_top10", "status": "WARN",
             "message": "Financial Services 40%"},
        ],
    }


def parity_warn_supp() -> dict:
    return {
        "overall": "WARN",
        "cross_group_parity": {"status": "OK", "by_field": {}},
        "verdicts": {},
    }


def parity_fail_low_risk() -> dict:
    return {
        "overall": "FAIL",
        "cross_group_parity": {"status": "FAIL", "by_field": {
            "low_risk": {"status": "FAIL", "message": "delta -2.1"},
        }},
        "verdicts": {},
    }


def parity_low_risk_known_bias() -> dict:
    """Parity report shaped as it lands once low_risk drift is recognised
    as selection bias: low_risk row demoted to WARN with known_bias=True,
    overall demoted from FAIL to WARN, and `low_risk_bias_known` flag set."""
    return {
        "overall": "WARN",
        "low_risk_bias_known": True,
        "cross_group_parity": {
            "status": "WARN",
            "by_field": {
                "low_risk": {
                    "status": "WARN", "raw_status": "FAIL",
                    "known_bias": True,
                    "message": "delta -2.28 — explained by selection bias",
                },
            },
            "low_risk_bias": {"is_known_bias": True,
                              "reason": "selection bias", "drift_verdict": "selection_bias"},
        },
        "verdicts": {},
    }


# ------------------- analyzer-level tests -------------------


def test_freshness_ok_for_now_dated_rankings():
    sec = mhc.analyze_data_freshness(fresh_rankings())
    assert sec["status"] == "OK", sec
    assert sec["metrics"]["today_live"] is True


def test_freshness_fail_when_rankings_missing():
    sec = mhc.analyze_data_freshness(None)
    assert sec["status"] == "FAIL", sec


def test_freshness_fail_when_stale_weekday():
    """Skip the weekday assertion when the test happens to run over a
    weekend — the threshold deliberately relaxes there."""
    sec = mhc.analyze_data_freshness(stale_rankings(hours=48))
    if sec["metrics"].get("is_weekend"):
        assert sec["status"] in ("OK", "WARN"), sec
    else:
        assert sec["status"] == "FAIL", sec


def test_dq_ok_passes_through():
    sec = mhc.analyze_data_quality(healthy_dq())
    assert sec["status"] == "OK", sec
    assert not sec["metrics"]["critical_section_fail"]


def test_dq_critical_section_fail_promoted():
    sec = mhc.analyze_data_quality(fail_dq_rankings())
    assert sec["status"] == "FAIL", sec
    assert sec["metrics"]["critical_section_fail"] is True


def test_dq_missing_input_warns():
    sec = mhc.analyze_data_quality(None)
    assert sec["status"] == "WARN", sec


def test_schedule_recovered_downgrade_to_warn():
    sec = mhc.analyze_schedule_reliability(schedule_fail_recovered())
    assert sec["status"] == "WARN", sec
    assert sec["metrics"]["recovered"] is True
    assert sec["metrics"]["overall_raw"] == "FAIL"


def test_schedule_fail_today_missing_stays_fail():
    sec = mhc.analyze_schedule_reliability(schedule_fail_today_missing())
    assert sec["status"] == "FAIL", sec
    assert sec["metrics"]["recovered"] is False


def test_schedule_ok_passes_through():
    sec = mhc.analyze_schedule_reliability(schedule_ok())
    assert sec["status"] == "OK", sec


def test_watchlist_ok_clean_summary():
    sec = mhc.analyze_watchlist(watchlist_ok())
    assert sec["status"] == "OK", sec


def test_watchlist_degraded_warns():
    sec = mhc.analyze_watchlist(watchlist_degraded())
    assert sec["status"] == "WARN", sec
    assert sec["metrics"]["supp_degraded_pct"] >= 0.5


def test_benchmark_ok():
    sec = mhc.analyze_benchmark(benchmark_ok())
    assert sec["status"] == "OK", sec


def test_benchmark_missing_warns():
    sec = mhc.analyze_benchmark(None)
    assert sec["status"] == "WARN", sec


def test_parity_warn_propagates():
    sec = mhc.analyze_parity(parity_warn_supp())
    assert sec["status"] == "WARN", sec


def test_parity_fail_demoted_to_warn():
    sec = mhc.analyze_parity(parity_fail_low_risk())
    assert sec["status"] == "WARN", sec
    assert "low_risk" in sec["metrics"]["fail_fields"]


def test_parity_low_risk_known_bias_message_explains_not_blocker():
    """When low_risk_bias_known=True and low_risk is the only parity
    issue (already demoted to WARN inside parity), the rollup message
    should mention selection bias and NOT call low_risk a 'blocker'."""
    sec = mhc.analyze_parity(parity_low_risk_known_bias())
    assert sec["status"] == "WARN", sec
    assert sec["metrics"]["low_risk_bias_known"] is True
    assert "low_risk" in sec["metrics"]["known_bias_fields"]
    assert sec["metrics"]["fail_fields"] == [], sec["metrics"]
    msg = sec["checks"][0]["message"]
    assert "selection bias" in msg.lower(), msg
    assert "blocker" not in msg.lower(), msg


def test_parity_low_risk_known_bias_with_real_blocker_keeps_blocker_message():
    """If a different field is genuinely FAIL (not low_risk), parity
    should still surface that field as a blocker."""
    par = parity_low_risk_known_bias()
    par["overall"] = "FAIL"  # something else really is a blocker
    par["cross_group_parity"]["status"] = "FAIL"
    par["cross_group_parity"]["by_field"]["ai_score"] = {
        "status": "FAIL", "message": "delta +2.1"
    }
    sec = mhc.analyze_parity(par)
    assert "ai_score" in sec["metrics"]["fail_fields"]
    msg = sec["checks"][0]["message"]
    assert "ai_score" in msg, msg


def test_schedule_uses_report_provided_effective():
    """When the schedule report supplies overall_effective, midday should
    prefer it over the inline heuristic."""
    today = _today_chi_date_str()
    sr_rep = {
        "overall": "FAIL",
        "overall_raw": "FAIL",
        "overall_effective": "WARN",
        "effective": {"effective": "WARN", "recovered": True,
                      "reason": "today satisfied"},
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today,
                          "slot_hits": {"morning": 1, "midday": 1, "close": 1},
                          "missing": []}],
                "missing_count": 3, "duplicate_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "schedule",  # not workflow_dispatch — prior
                                            # heuristic would NOT recover here
                "ts_chicago": today + " 12:32",
            }}},
        },
    }
    sec = mhc.analyze_schedule_reliability(sr_rep)
    assert sec["status"] == "WARN", sec
    assert sec["metrics"]["overall_effective"] == "WARN"
    assert sec["metrics"]["recovered"] is True


def test_schedule_falls_back_to_heuristic_for_old_reports():
    """Old schedule_reliability.json without overall_effective should still
    work via the legacy recovery heuristic."""
    today = _today_chi_date_str()
    sr_rep = {
        "overall": "FAIL",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today,
                          "slot_hits": {"morning": 1, "midday": 1, "close": 1},
                          "missing": []}],
                "missing_count": 3, "duplicate_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "workflow_dispatch",
                "ts_chicago": today + " 12:32",
            }}},
        },
    }
    sec = mhc.analyze_schedule_reliability(sr_rep)
    assert sec["status"] == "WARN", sec
    assert sec["metrics"]["recovered"] is True


def test_schedule_active_failure_when_today_missing():
    today = _today_chi_date_str()
    sr_rep = {
        "overall": "FAIL",
        "overall_raw": "FAIL",
        "overall_effective": "FAIL",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today,
                          "slot_hits": {"morning": 0, "midday": 0, "close": 0},
                          "missing": ["morning", "midday", "close"]}],
                "missing_count": 3, "duplicate_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {}}},
        },
    }
    sec = mhc.analyze_schedule_reliability(sr_rep)
    assert sec["status"] == "FAIL", sec


def test_schedule_fresh_but_one_slot_missing_is_warn_not_fail():
    """The 2026-06-02 scenario: a delayed morning run landed in the midday
    window, so the calendar flags 'midday' missing, but live data is fresh.
    The schedule report supplies overall_effective=WARN/recovered. Midday
    must read WARN (not FAIL) so the dashboard does not show a false outage
    while data is current."""
    today = _today_chi_date_str()
    sr_rep = {
        "overall": "FAIL",
        "overall_raw": "FAIL",
        "overall_effective": "WARN",
        "effective": {"effective": "WARN", "recovered": True,
                      "current_slot": "close", "current_slot_covered": True,
                      "rankings_fresh": True,
                      "reason": "live data fresh; diagnostic slot gaps"},
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today,
                          "slot_hits": {"morning": 1, "midday": 0, "close": 1},
                          "missing": ["midday"]}],
                "missing_count": 5, "duplicate_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "schedule",
                "ts_chicago": today + " 18:04",
            }}},
        },
    }
    sec = mhc.analyze_schedule_reliability(sr_rep)
    assert sec["status"] == "WARN", sec
    assert sec["metrics"]["recovered"] is True


# ------------------- end-to-end via build_report -------------------


def _patch_inputs(monkeypatch_targets: dict, tmp: Path) -> None:
    """Write fixture JSONs into tmp and point the module's path constants
    at them. Caller restores after the test finishes."""
    for attr, payload in monkeypatch_targets.items():
        path = tmp / f"{attr}.json"
        if payload is None:
            # Leave file absent so _load_json returns None.
            setattr(mhc, attr, path)
            continue
        path.write_text(json.dumps(payload), encoding="utf-8")
        setattr(mhc, attr, path)


def _restore_inputs(saved: dict) -> None:
    for attr, val in saved.items():
        setattr(mhc, attr, val)


def _run_with_fixtures(rankings, watchlist, dq, sr, bench, par) -> dict:
    saved = {a: getattr(mhc, a) for a in (
        "RANKINGS_FILE", "WATCHLIST_FILE", "DATA_QUALITY_FILE",
        "SCHEDULE_RELIABILITY_FILE", "BENCHMARK_FILE", "PARITY_FILE",
    )}
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _patch_inputs({
                "RANKINGS_FILE": rankings,
                "WATCHLIST_FILE": watchlist,
                "DATA_QUALITY_FILE": dq,
                "SCHEDULE_RELIABILITY_FILE": sr,
                "BENCHMARK_FILE": bench,
                "PARITY_FILE": par,
            }, tmp)
            return mhc.build_report()
    finally:
        _restore_inputs(saved)


def test_e2e_all_ok():
    rep = _run_with_fixtures(
        fresh_rankings(), watchlist_ok(), healthy_dq(),
        schedule_ok(), benchmark_ok(), {"overall": "OK", "cross_group_parity": {"status": "OK", "by_field": {}}},
    )
    assert rep["overall"] == "OK", rep["overall"]
    assert "Fresh" in rep["summary"]
    assert "Data quality OK" in rep["summary"]


def test_e2e_stale_data_fail():
    rep = _run_with_fixtures(
        stale_rankings(hours=72), watchlist_ok(), healthy_dq(),
        schedule_ok(), benchmark_ok(), {"overall": "OK", "cross_group_parity": {"status": "OK", "by_field": {}}},
    )
    if not rep["sections"]["data_freshness"]["metrics"].get("is_weekend"):
        assert rep["overall"] == "FAIL", rep["overall"]


def test_e2e_recovered_schedule_warn():
    rep = _run_with_fixtures(
        fresh_rankings(), watchlist_ok(), healthy_dq(),
        schedule_fail_recovered(), benchmark_ok(),
        {"overall": "OK", "cross_group_parity": {"status": "OK", "by_field": {}}},
    )
    assert rep["overall"] == "WARN", rep["overall"]
    assert "FAIL/recovered" in rep["summary"], rep["summary"]


def test_e2e_critical_dq_fail():
    rep = _run_with_fixtures(
        fresh_rankings(), watchlist_ok(), fail_dq_rankings(),
        schedule_ok(), benchmark_ok(),
        {"overall": "OK", "cross_group_parity": {"status": "OK", "by_field": {}}},
    )
    assert rep["overall"] == "FAIL", rep["overall"]


def test_e2e_summary_format():
    rep = _run_with_fixtures(
        fresh_rankings(), watchlist_ok(), healthy_dq(),
        schedule_fail_recovered(), benchmark_ok(),
        parity_fail_low_risk(),
    )
    summary = rep["summary"]
    for part in ("Fresh", "Data quality", "Watchlist", "Schedule", "Benchmark"):
        assert part in summary, f"missing {part!r} in {summary!r}"


def test_stamp_task_round_trip(tmp_path=None):
    """Stamp tasks.json fixture and confirm the row is updated."""
    saved_tasks = mhc.TASKS_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            tasks_path = Path(td) / "tasks.json"
            tasks_path.write_text(json.dumps({"tasks": [
                {"id": "midday-health-check", "name": "Midday Health Check",
                 "status": "Not Run", "summary": "placeholder"},
                {"id": "other", "status": "OK", "summary": "x"},
            ]}), encoding="utf-8")
            mhc.TASKS_FILE = tasks_path
            report = {"overall": "WARN",
                      "generated_at_chicago": "2026-05-05 12:32 PM CDT",
                      "summary": "Fresh 12:32 PM CT · all good"}
            mhc._stamp_task(report)
            data = json.loads(tasks_path.read_text(encoding="utf-8"))
            row = next(r for r in data["tasks"] if r["id"] == "midday-health-check")
            assert row["status"] == "warn", row
            assert row["summary"] == "Fresh 12:32 PM CT · all good"
            assert row["last_run"] == "2026-05-05 12:32 PM CDT"
            assert row["report_url"] == mhc.REPORT_URL
            other = next(r for r in data["tasks"] if r["id"] == "other")
            assert other["status"] == "OK", other
    finally:
        mhc.TASKS_FILE = saved_tasks


def test_html_render_smoke():
    rep = _run_with_fixtures(
        fresh_rankings(), watchlist_ok(), healthy_dq(),
        schedule_ok(), benchmark_ok(),
        {"overall": "OK", "cross_group_parity": {"status": "OK", "by_field": {}}},
    )
    html = mhc._render_html(rep)
    assert "<html" in html and "</html>" in html
    assert "Midday Health Check" in html
    assert "Summary:" in html


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
        print(f"\nFAIL: {failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

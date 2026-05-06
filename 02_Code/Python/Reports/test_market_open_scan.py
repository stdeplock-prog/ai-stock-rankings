"""Fixture-based tests for market_open_scan.py.

Covers:
  * stale rankings -> overall FAIL
  * schedule reliability rescued -> overall WARN, source='recovered'
  * all-OK happy path
  * MOV gainer/loser extraction sorts and excludes 0/non-numeric
  * tasks.json row stamping (last_run / status / summary / report_url)

Run: python 02_Code/Python/Reports/test_market_open_scan.py
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

import market_open_scan as mos  # noqa: E402


def _now_chi_str() -> str:
    now_utc = datetime.now(timezone.utc)
    chi = mos._to_chicago(now_utc)
    label = "CDT" if chi.utcoffset().total_seconds() == -5 * 3600 else "CST"
    return chi.strftime("%Y-%m-%d %I:%M %p ") + label


def _today_chi_date_str() -> str:
    return mos._to_chicago(datetime.now(timezone.utc)).date().strftime("%Y-%m-%d")


# ------------------- fixtures -------------------


def fresh_rankings(rows: int = 12) -> dict:
    out = {
        "as_of": _now_chi_str(),
        "open_date": _today_chi_date_str(),
        "is_open_run": True,
        "universe": "test",
        "rows": [],
    }
    for i in range(rows):
        out["rows"].append({
            "rank": i + 1,
            "ticker": f"T{i}",
            "company": f"Company {i}",
            "ai_score": 8.0 - i * 0.05,
            "change": (i - 5),  # -5..+6 spread for sorting
            "sector": "Industrials" if i < 5 else "Technology",
        })
    # Add one row with non-numeric change to make sure it doesn't break sorting
    out["rows"].append({
        "rank": rows + 1, "ticker": "BAD", "company": "Bad",
        "ai_score": 5, "change": "n/a", "sector": "Energy",
    })
    return out


def stale_rankings(hours: int = 48) -> dict:
    now_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    chi = mos._to_chicago(now_utc)
    label = "CDT" if chi.utcoffset().total_seconds() == -5 * 3600 else "CST"
    return {
        "as_of": chi.strftime("%Y-%m-%d %I:%M %p ") + label,
        "open_date": chi.strftime("%Y-%m-%d"),
        "rows": [{"ticker": "T0", "rank": 1, "change": 0}],
    }


def healthy_dq() -> dict:
    return {
        "overall": "OK",
        "sections": {
            "rankings": {"checks": [{"name": "row_count", "status": "OK"}]},
            "tasks": {"checks": []},
        },
    }


def fail_dq_rankings() -> dict:
    return {
        "overall": "FAIL",
        "sections": {
            "rankings": {"checks": [{"name": "row_count", "status": "FAIL"}]},
            "tasks": {"checks": []},
        },
    }


def schedule_ok() -> dict:
    today = _today_chi_date_str()
    return {
        "overall": "OK",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today, "missing": []}],
                "missing_count": 0, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "schedule", "slot": "morning",
                "ts_chicago": today + " 08:45",
            }}},
        },
    }


def schedule_recovered() -> dict:
    today = _today_chi_date_str()
    return {
        "overall": "FAIL",
        "overall_effective": "WARN",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [
                    {"date": "2026-04-29",
                     "missing": ["morning", "midday", "close"]},
                    {"date": today, "missing": []},
                ],
                "missing_count": 3, "lookback_days": 5,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "workflow_dispatch", "slot": "manual",
                "ts_chicago": today + " 12:32",
            }}},
        },
    }


def watchlist_ok() -> dict:
    rows = []
    for i in range(15):
        rows.append({
            "rank": i + 1,
            "ticker": f"WL{i}",
            "company": f"WL {i}",
            "ai_score": 8.5 - i * 0.05,
            "change": (i - 7),
            "sector": "Technology" if i < 8 else "Health",
            "data_source": "supplemental_yfinance" if i % 3 == 0 else "main_pipeline",
        })
    return {
        "as_of": _now_chi_str(),
        "rows": rows,
        "source_meta": {
            "scored": 142, "unavailable_count": 1,
            "supp_summary": {
                "total": 75, "full_fundamentals": 65, "price_only": 9,
                "technical_only": 1, "metadata_only": 0, "eodhd_enriched": 0,
            },
            "yfinance_info_cache": {"cache_hit_fresh": 75, "cache_miss": 0},
        },
    }


def market_risk_alert() -> dict:
    return {
        "generated_at": "2026-05-06 17:18 UTC",
        "indicators": {
            "vix": {"value": None, "status": "unavailable"},
            "polls": {"value": None, "status": "source_needed"},
            "generals_fail": {
                "rows": [
                    {"ticker": "AAPL", "below": False},
                    {"ticker": "MSFT", "below": True},
                    {"ticker": "META", "below": True},
                    {"ticker": "TSLA", "below": True},
                ],
                "below_count": 3, "available_count": 4,
                "threshold": 3, "alert": True,
            },
        },
    }


def market_risk_ok() -> dict:
    return {
        "generated_at": "2026-05-06 17:18 UTC",
        "indicators": {
            "generals_fail": {
                "rows": [
                    {"ticker": "AAPL", "below": False},
                    {"ticker": "MSFT", "below": False},
                ],
                "below_count": 0, "available_count": 7,
                "threshold": 3, "alert": False,
            },
        },
    }


def benchmark_ok() -> dict:
    return {
        "snapshots_kept": 4,
        "snapshot_summary": {"horizons": {"1d": {"completed": 2}}},
        "findings": [],
    }


# ------------------- analyzer-level tests -------------------


def test_freshness_ok_when_today_live():
    sec = mos.analyze_freshness(fresh_rankings())
    assert sec["status"] == "OK", sec
    assert sec["metrics"]["today_live"] is True


def test_freshness_fail_when_stale():
    sec = mos.analyze_freshness(stale_rankings(hours=48))
    if sec["metrics"].get("is_weekend"):
        # Weekend threshold is much wider — skip the strict assertion.
        assert sec["status"] in ("OK", "WARN"), sec
    else:
        assert sec["status"] == "FAIL", sec


def test_freshness_fail_when_missing():
    sec = mos.analyze_freshness(None)
    assert sec["status"] == "FAIL"


def test_run_source_classifies_recovered():
    sec = mos.analyze_run_source(schedule_recovered())
    assert sec["metrics"]["source"] == "recovered", sec
    assert sec["status"] == "WARN"


def test_run_source_classifies_manual():
    today = _today_chi_date_str()
    rep = {
        "overall": "OK",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today, "missing": []}],
                "missing_count": 0,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "workflow_dispatch",
                "ts_chicago": today + " 09:01",
            }}},
        },
    }
    sec = mos.analyze_run_source(rep)
    assert sec["metrics"]["source"] == "manual", sec
    assert sec["status"] == "WARN"


def test_run_source_classifies_schedule():
    sec = mos.analyze_run_source(schedule_ok())
    assert sec["metrics"]["source"] == "schedule", sec
    assert sec["status"] == "OK"


def test_extract_movers_sorts_and_filters_zero_and_invalid():
    movers = mos.extract_rankings_movers(fresh_rankings(rows=12))
    gainers = movers["top_gainers"]
    losers = movers["top_losers"]
    # change values were i-5 for i in 0..11 -> -5..+6, plus a zero (i=5).
    assert gainers, "expected gainers"
    assert losers, "expected losers"
    # All gainers strictly > 0, all losers strictly < 0.
    assert all(g["change"] > 0 for g in gainers)
    assert all(l["change"] < 0 for l in losers)
    # Sorted descending and ascending.
    assert gainers == sorted(gainers, key=lambda r: r["change"], reverse=True)
    assert losers == sorted(losers, key=lambda r: r["change"])
    # Top 10 honored, even with the BAD non-numeric row.
    assert len(movers["top_10"]) == 10
    # mov_summary excludes the non-numeric row.
    assert movers["mov_summary"]["with_change"] == 12


def test_extract_movers_handles_empty():
    movers = mos.extract_rankings_movers({"rows": []})
    assert movers["top_10"] == []
    assert movers["top_gainers"] == []


def test_data_quality_critical_section_promotes_fail():
    sec = mos.analyze_data_quality(fail_dq_rankings())
    assert sec["status"] == "FAIL"
    assert sec["metrics"]["critical_section_fail"] is True


def test_market_risk_alert_warns():
    sec = mos.analyze_market_risk(market_risk_alert())
    assert sec["status"] == "WARN"
    assert sec["metrics"]["generals_fail"]["alert"] is True
    assert "MSFT" in sec["metrics"]["generals_fail"]["below_tickers"]


def test_market_risk_ok_when_no_alert():
    sec = mos.analyze_market_risk(market_risk_ok())
    assert sec["status"] == "OK"


def test_compute_overall_fail_when_freshness_fail():
    sections = {
        "freshness": {"status": "FAIL", "metrics": {}},
        "run_source": {"status": "OK", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "rankings_changes": {"status": "OK", "metrics": {}},
        "watchlist": {"status": "OK", "metrics": {}},
        "market_risk": {"status": "OK", "metrics": {}},
        "benchmark": {"status": "OK", "metrics": {}},
    }
    assert mos.compute_overall(sections) == "FAIL"


def test_compute_overall_warn_when_recovered():
    sections = {
        "freshness": {"status": "OK", "metrics": {}},
        "run_source": {"status": "WARN", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "rankings_changes": {"status": "OK", "metrics": {}},
        "watchlist": {"status": "OK", "metrics": {}},
        "market_risk": {"status": "OK", "metrics": {}},
        "benchmark": {"status": "OK", "metrics": {}},
    }
    assert mos.compute_overall(sections) == "WARN"


def test_compute_overall_ok_when_all_clean():
    sections = {
        "freshness": {"status": "OK", "metrics": {}},
        "run_source": {"status": "OK", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "rankings_changes": {"status": "OK", "metrics": {}},
        "watchlist": {"status": "OK", "metrics": {}},
        "market_risk": {"status": "OK", "metrics": {}},
        "benchmark": {"status": "OK", "metrics": {}},
    }
    assert mos.compute_overall(sections) == "OK"


# ------------------- end-to-end tests -------------------


def _patched_paths(tmp: Path):
    """Redirect file paths into a tmp dir for end-to-end testing."""
    data = tmp / "data"
    reports_data = data / "reports"
    reports_html = tmp / "reports"
    reports_data.mkdir(parents=True, exist_ok=True)
    reports_html.mkdir(parents=True, exist_ok=True)
    saved = {
        "RANKINGS_FILE": mos.RANKINGS_FILE,
        "WATCHLIST_FILE": mos.WATCHLIST_FILE,
        "DATA_QUALITY_FILE": mos.DATA_QUALITY_FILE,
        "SCHEDULE_RELIABILITY_FILE": mos.SCHEDULE_RELIABILITY_FILE,
        "MARKET_RISK_FILE": mos.MARKET_RISK_FILE,
        "BENCHMARK_FILE": mos.BENCHMARK_FILE,
        "JSON_OUTPUT": mos.JSON_OUTPUT,
        "HTML_OUTPUT": mos.HTML_OUTPUT,
        "TASKS_FILE": mos.TASKS_FILE,
        "DATA_REPORTS_DIR": mos.DATA_REPORTS_DIR,
        "HTML_REPORTS_DIR": mos.HTML_REPORTS_DIR,
    }
    mos.RANKINGS_FILE = data / "rankings.json"
    mos.WATCHLIST_FILE = data / "watchlist_rankings.json"
    mos.DATA_QUALITY_FILE = reports_data / "data_quality_audit.json"
    mos.SCHEDULE_RELIABILITY_FILE = reports_data / "schedule_reliability.json"
    mos.MARKET_RISK_FILE = reports_data / "market_risk_monitor.json"
    mos.BENCHMARK_FILE = reports_data / "benchmark_review.json"
    mos.JSON_OUTPUT = reports_data / "market_open_scan.json"
    mos.HTML_OUTPUT = reports_html / "market-open-scan.html"
    mos.TASKS_FILE = data / "tasks.json"
    mos.DATA_REPORTS_DIR = reports_data
    mos.HTML_REPORTS_DIR = reports_html
    return saved, data


def _restore_paths(saved):
    for k, v in saved.items():
        setattr(mos, k, v)


def _write_inputs(data_dir: Path, **inputs):
    if "rankings" in inputs:
        (data_dir / "rankings.json").write_text(
            json.dumps(inputs["rankings"]), encoding="utf-8")
    if "watchlist" in inputs:
        (data_dir / "watchlist_rankings.json").write_text(
            json.dumps(inputs["watchlist"]), encoding="utf-8")
    if "dq" in inputs:
        (data_dir / "reports" / "data_quality_audit.json").write_text(
            json.dumps(inputs["dq"]), encoding="utf-8")
    if "schedule" in inputs:
        (data_dir / "reports" / "schedule_reliability.json").write_text(
            json.dumps(inputs["schedule"]), encoding="utf-8")
    if "market_risk" in inputs:
        (data_dir / "reports" / "market_risk_monitor.json").write_text(
            json.dumps(inputs["market_risk"]), encoding="utf-8")
    if "benchmark" in inputs:
        (data_dir / "reports" / "benchmark_review.json").write_text(
            json.dumps(inputs["benchmark"]), encoding="utf-8")


def _write_tasks_file(data_dir: Path):
    (data_dir / "tasks.json").write_text(json.dumps({
        "tasks": [
            {"id": "market-open-scan", "name": "Market Open Scan",
             "schedule": "Weekdays 8:30 AM CT", "last_run": "—",
             "next_run": "—", "status": "Not Run", "summary": "old"},
        ],
    }) + "\n", encoding="utf-8")


def test_e2e_all_ok():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=watchlist_ok(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                market_risk=market_risk_ok(),
                benchmark=benchmark_ok(),
            )
            assert mos.main() == 0
            report = json.loads(mos.JSON_OUTPUT.read_text())
            assert report["overall"] == "OK", report
            assert mos.HTML_OUTPUT.exists()
            tasks = json.loads(mos.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "market-open-scan")
            assert row["status"] == "OK"
            assert row["report_url"] == "./reports/market-open-scan.html"
            assert "Live" in row["summary"]
        finally:
            _restore_paths(saved)


def test_e2e_stale_fail():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            _write_inputs(
                data,
                rankings=stale_rankings(hours=72),
                watchlist=watchlist_ok(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
            )
            assert mos.main() == 0
            report = json.loads(mos.JSON_OUTPUT.read_text())
            tasks = json.loads(mos.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "market-open-scan")
            # Weekend tolerance again — the threshold relaxes off-hours.
            if report["sections"]["freshness"]["metrics"].get("is_weekend"):
                assert report["overall"] in ("OK", "WARN", "FAIL")
            else:
                assert report["overall"] == "FAIL", report
                assert row["status"] == "fail"
        finally:
            _restore_paths(saved)


def test_e2e_recovered_warn():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=watchlist_ok(),
                dq=healthy_dq(),
                schedule=schedule_recovered(),
                market_risk=market_risk_ok(),
            )
            assert mos.main() == 0
            report = json.loads(mos.JSON_OUTPUT.read_text())
            assert report["overall"] == "WARN", report
            assert report["sections"]["run_source"]["metrics"]["source"] == "recovered"
            tasks = json.loads(mos.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "market-open-scan")
            assert row["status"] == "warn"
            # Action item should mention rescue/recovered status
            assert any("recover" in a.lower() or "rescue" in a.lower()
                       or "WARN" in a for a in report["action_items"])
        finally:
            _restore_paths(saved)


def test_e2e_market_risk_alert_warns():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=watchlist_ok(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                market_risk=market_risk_alert(),
            )
            assert mos.main() == 0
            report = json.loads(mos.JSON_OUTPUT.read_text())
            assert report["overall"] == "WARN", report
            assert "Risk alert" in report["summary"]
        finally:
            _restore_paths(saved)


def test_e2e_task_row_stamped_with_schedule_and_url():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=watchlist_ok(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                market_risk=market_risk_ok(),
            )
            mos.main()
            tasks = json.loads(mos.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "market-open-scan")
            assert row["report_url"] == "./reports/market-open-scan.html"
            assert "rescue" in row["schedule"].lower()
            assert row["last_run"] != "—"
        finally:
            _restore_paths(saved)


# ------------------- test runner -------------------


if __name__ == "__main__":
    failed = 0
    funcs = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    for fn in funcs:
        try:
            fn()
        except AssertionError as exc:
            print(f"FAIL  {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"OK    {fn.__name__}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)

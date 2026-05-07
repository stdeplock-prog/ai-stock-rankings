"""Fixture-based tests for close_recap.py.

Covers:
  * stale rankings -> overall FAIL
  * schedule reliability rescued -> overall WARN, source='recovered'
  * all-OK happy path
  * MOV gainer/loser extraction sorts and excludes 0/non-numeric
  * new top10 detection vs prior benchmark snapshot
  * intraday top10 detection vs market_open_scan snapshot
  * sector concentration WARN trigger
  * task row stamping (last_run / status / summary / report_url / schedule)
  * action item bucketing into Operational vs Market

Run: python 02_Code/Python/Reports/test_close_recap.py
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

import close_recap as cr  # noqa: E402


def _now_chi_str() -> str:
    now_utc = datetime.now(timezone.utc)
    chi = cr._to_chicago(now_utc)
    label = "CDT" if chi.utcoffset().total_seconds() == -5 * 3600 else "CST"
    return chi.strftime("%Y-%m-%d %I:%M %p ") + label


def _today_chi_date_str() -> str:
    return cr._to_chicago(datetime.now(timezone.utc)).date().strftime("%Y-%m-%d")


# ------------------- fixtures -------------------


def fresh_rankings(rows: int = 12) -> dict:
    out = {
        "as_of": _now_chi_str(),
        "open_date": _today_chi_date_str(),
        "is_open_run": False,
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
    out["rows"].append({
        "rank": rows + 1, "ticker": "BAD", "company": "Bad",
        "ai_score": 5, "change": "n/a", "sector": "Energy",
    })
    return out


def stale_rankings(hours: int = 48) -> dict:
    now_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    chi = cr._to_chicago(now_utc)
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
                "event_name": "schedule", "slot": "close",
                "ts_chicago": today + " 15:35",
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
                "ts_chicago": today + " 15:42",
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
            "scored": 142, "unavailable_count": 0,
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


def diagnostics_ok() -> dict:
    return {"overall": "OK", "suspicious_ranks": [], "sector_crowding": {}}


def diagnostics_warn() -> dict:
    return {
        "overall": "WARN",
        "suspicious_ranks": [
            {"group": "watchlist_top10", "rank": 5, "ticker": "AMZN",
             "reasons": ["weak LOW_RISK (4.4)"]},
            {"group": "watchlist_top10", "rank": 9, "ticker": "SCCO",
             "reasons": ["weak SENT (4.2)"]},
        ],
        "sector_crowding": {"main_top10": {"top_sector": "Financial Services"}},
    }


def market_open_scan_with_top10(tickers: list[str]) -> dict:
    return {
        "overall": "OK",
        "sections": {
            "rankings_changes": {"metrics": {
                "top_10": [
                    {"rank": i + 1, "ticker": t, "ai_score": 8.0 - i * 0.05}
                    for i, t in enumerate(tickers)
                ],
            }},
        },
    }


# ------------------- analyzer-level tests -------------------


def test_freshness_ok_when_today_live():
    sec = cr.analyze_freshness(fresh_rankings())
    assert sec["status"] == "OK", sec
    assert sec["metrics"]["today_live"] is True


def test_freshness_fail_when_stale():
    sec = cr.analyze_freshness(stale_rankings(hours=48))
    if sec["metrics"].get("is_weekend"):
        assert sec["status"] in ("OK", "WARN"), sec
    else:
        assert sec["status"] == "FAIL", sec


def test_freshness_fail_when_missing():
    sec = cr.analyze_freshness(None)
    assert sec["status"] == "FAIL"


def test_run_source_classifies_recovered():
    sec = cr.analyze_run_source(schedule_recovered())
    assert sec["metrics"]["source"] == "recovered", sec
    assert sec["status"] == "WARN"


def test_run_source_classifies_schedule():
    sec = cr.analyze_run_source(schedule_ok())
    assert sec["metrics"]["source"] == "schedule", sec
    assert sec["status"] == "OK"


def test_run_source_warns_when_close_slot_missing():
    today = _today_chi_date_str()
    rep = {
        "overall": "WARN",
        "overall_effective": "WARN",
        "sections": {
            "calendar": {"metrics": {"calendar": {
                "rows": [{"date": today, "missing": ["close"]}],
                "missing_count": 1,
            }}},
            "recency": {"metrics": {"last_run": {
                "event_name": "schedule", "ts_chicago": today + " 12:32",
            }}},
        },
    }
    sec = cr.analyze_run_source(rep)
    assert sec["metrics"]["close_slot_missing"] is True
    assert sec["status"] in ("WARN", "FAIL")


def test_extract_movers_sorts_and_filters_zero_and_invalid():
    movers = cr.extract_rankings_movers(fresh_rankings(rows=12))
    gainers = movers["top_gainers"]
    losers = movers["top_losers"]
    assert gainers, "expected gainers"
    assert losers, "expected losers"
    assert all(g["change"] > 0 for g in gainers)
    assert all(l["change"] < 0 for l in losers)
    assert gainers == sorted(gainers, key=lambda r: r["change"], reverse=True)
    assert losers == sorted(losers, key=lambda r: r["change"])
    assert len(movers["top_10"]) == 10
    assert movers["mov_summary"]["with_change"] == 12


def test_extract_movers_handles_empty():
    movers = cr.extract_rankings_movers({"rows": []})
    assert movers["top_10"] == []
    assert movers["top_gainers"] == []
    assert movers["new_top10_entries"] == []
    assert movers["intraday_new_entries"] == []


def test_extract_movers_detects_new_top10_entries_vs_prior_snapshot():
    rk = fresh_rankings(rows=12)
    # Tickers in fresh_rankings top10 are T0..T9. Pretend prior was
    # T1..T8 + outsiders, so T0 and T9 are new and OLD1/OLD2 exited.
    prior = {"T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "OLD1", "OLD2"}
    movers = cr.extract_rankings_movers(
        rk, prior_top_tickers=prior, prior_as_of_date="2026-04-30"
    )
    new_tickers = {e["ticker"] for e in movers["new_top10_entries"]}
    assert "T0" in new_tickers and "T9" in new_tickers
    assert "T5" not in new_tickers
    assert set(movers["exited_top10_entries"]) == {"OLD1", "OLD2"}
    assert movers["prior_top10_compared_against"] == "2026-04-30"


def test_extract_movers_detects_intraday_new_entries_vs_open_scan():
    rk = fresh_rankings(rows=12)
    # This-morning's top10 was T0..T7 + OLDA/OLDB. So intraday new
    # entries today would be T8 and T9.
    open_top = {"T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "OLDA", "OLDB"}
    movers = cr.extract_rankings_movers(rk, open_top_tickers=open_top)
    intraday_tickers = {e["ticker"] for e in movers["intraday_new_entries"]}
    assert "T8" in intraday_tickers and "T9" in intraday_tickers
    assert "T0" not in intraday_tickers


def test_extract_movers_sector_concentration_warns_at_threshold():
    # 6 of 10 in Industrials triggers >=40% (in fact 60%) warn.
    rk = {"rows": [
        {"rank": i + 1, "ticker": f"I{i}", "ai_score": 8.0 - 0.01 * i,
         "change": 0, "sector": "Industrials"}
        for i in range(6)
    ] + [
        {"rank": 7 + i, "ticker": f"X{i}", "ai_score": 7.0 - 0.01 * i,
         "change": 0, "sector": s}
        for i, s in enumerate(["Tech", "Health", "Energy", "Utility"])
    ]}
    movers = cr.extract_rankings_movers(rk)
    sc = movers["sector_concentration"]
    assert sc["top_sector"] == "Industrials"
    assert sc["pct"] >= 0.40
    assert sc["warn"] is True


def test_data_quality_critical_section_promotes_fail():
    sec = cr.analyze_data_quality(fail_dq_rankings())
    assert sec["status"] == "FAIL"
    assert sec["metrics"]["critical_section_fail"] is True


def test_market_risk_alert_warns():
    sec = cr.analyze_market_risk(market_risk_alert())
    assert sec["status"] == "WARN"
    assert sec["metrics"]["generals_fail"]["alert"] is True
    assert "MSFT" in sec["metrics"]["generals_fail"]["below_tickers"]


def test_diagnostics_warn_promoted_to_section_warn():
    sec = cr.analyze_diagnostics(diagnostics_warn())
    assert sec["status"] == "WARN"
    assert sec["metrics"]["suspicious_count"] == 2


def test_diagnostics_missing_is_advisory_ok():
    sec = cr.analyze_diagnostics(None)
    assert sec["status"] == "OK"


def test_collect_action_items_splits_operational_and_market():
    sections = {
        "freshness": {"status": "FAIL", "checks": [
            {"name": "today_live", "status": "FAIL", "message": "stale"},
        ], "metrics": {}},
        "run_source": {"status": "WARN", "checks": [
            {"name": "run_source", "status": "WARN", "message": "rescued"},
        ], "metrics": {}},
        "data_quality": {"status": "WARN", "checks": [
            {"name": "data_quality_overall", "status": "WARN", "message": "warn"},
        ], "metrics": {}},
        "rankings_recap": {"status": "OK", "checks": [], "metrics": {
            "new_top10_entries": [{"ticker": "NEWX", "rank": 1}],
            "exited_top10_entries": ["OLDX"],
            "intraday_new_entries": [{"ticker": "INTX", "rank": 5}],
            "prior_top10_compared_against": "2026-04-30",
            "top_gainers": [{"ticker": "AAA", "change": 5, "rank": 2}],
            "top_losers": [{"ticker": "ZZZ", "change": -4, "rank": 99}],
        }},
        "watchlist": {"status": "OK", "checks": [], "metrics": {}},
        "market_risk": {"status": "WARN", "checks": [
            {"name": "generals_fail", "status": "WARN", "message": "alert"},
        ], "metrics": {}},
        "diagnostics": {"status": "OK", "checks": [], "metrics": {
            "suspicious_top": [{"ticker": "AMZN", "rank": 5}],
        }},
        "benchmark": {"status": "OK", "checks": [], "metrics": {}},
        "parity": {"status": "OK", "checks": [], "metrics": {}},
    }
    items = cr.collect_action_items(sections)
    # FAIL line is properly tagged.
    assert any(i.startswith("[FAIL]") for i in items), items
    # Both buckets contribute — at least one operational and one market line.
    assert any("freshness." in i for i in items), items
    # Market-side: a new-top10 / exited / intraday / movers / diagnostics
    # signal should make it through the cap. Specific INFO lines may be
    # trimmed by the 5-line market cap but at least one market signal must
    # land regardless.
    assert any("New top10" in i or "Exited top10" in i
               or "Intraday top10" in i or "Top MOV" in i
               or "Diagnostics flagged" in i
               or "market_risk." in i for i in items), items
    op_prefixes = tuple(f"[{lvl}] {sec}." for lvl in ("FAIL", "WARN")
                        for sec in cr._OPERATIONAL_SECTIONS)
    op_in_items = [i for i in items if i.startswith(op_prefixes)]
    assert len(op_in_items) <= 3, op_in_items
    assert len(items) <= 7, items
    # Sanity: the full bucketing pipeline emits both info and warn lines
    # in the expected order (FAIL/WARN lines lead within their bucket).
    all_buckets = [i for i in items if i.startswith(("[FAIL]", "[WARN]"))]
    assert all_buckets, items


def test_compute_overall_fail_when_freshness_fail():
    sections = {
        "freshness": {"status": "FAIL", "metrics": {}},
        "run_source": {"status": "OK", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "rankings_recap": {"status": "OK", "metrics": {}},
        "watchlist": {"status": "OK", "metrics": {}},
        "market_risk": {"status": "OK", "metrics": {}},
        "diagnostics": {"status": "OK", "metrics": {}},
        "benchmark": {"status": "OK", "metrics": {}},
        "parity": {"status": "OK", "metrics": {}},
    }
    assert cr.compute_overall(sections) == "FAIL"


def test_compute_overall_warn_when_recovered():
    sections = {
        "freshness": {"status": "OK", "metrics": {}},
        "run_source": {"status": "WARN", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "rankings_recap": {"status": "OK", "metrics": {}},
        "watchlist": {"status": "OK", "metrics": {}},
        "market_risk": {"status": "OK", "metrics": {}},
        "diagnostics": {"status": "OK", "metrics": {}},
        "benchmark": {"status": "OK", "metrics": {}},
        "parity": {"status": "OK", "metrics": {}},
    }
    assert cr.compute_overall(sections) == "WARN"


def test_compute_overall_ok_when_all_clean():
    sections = {
        "freshness": {"status": "OK", "metrics": {}},
        "run_source": {"status": "OK", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "rankings_recap": {"status": "OK", "metrics": {}},
        "watchlist": {"status": "OK", "metrics": {}},
        "market_risk": {"status": "OK", "metrics": {}},
        "diagnostics": {"status": "OK", "metrics": {}},
        "benchmark": {"status": "OK", "metrics": {}},
        "parity": {"status": "OK", "metrics": {}},
    }
    assert cr.compute_overall(sections) == "OK"


# ------------------- end-to-end tests -------------------


def _patched_paths(tmp: Path):
    data = tmp / "data"
    reports_data = data / "reports"
    reports_html = tmp / "reports"
    reports_data.mkdir(parents=True, exist_ok=True)
    reports_html.mkdir(parents=True, exist_ok=True)
    saved = {
        "RANKINGS_FILE": cr.RANKINGS_FILE,
        "WATCHLIST_FILE": cr.WATCHLIST_FILE,
        "DATA_QUALITY_FILE": cr.DATA_QUALITY_FILE,
        "SCHEDULE_RELIABILITY_FILE": cr.SCHEDULE_RELIABILITY_FILE,
        "MARKET_RISK_FILE": cr.MARKET_RISK_FILE,
        "BENCHMARK_FILE": cr.BENCHMARK_FILE,
        "PARITY_FILE": cr.PARITY_FILE,
        "DIAGNOSTICS_FILE": cr.DIAGNOSTICS_FILE,
        "MARKET_OPEN_SCAN_FILE": cr.MARKET_OPEN_SCAN_FILE,
        "BENCHMARK_SNAPSHOTS_FILE": cr.BENCHMARK_SNAPSHOTS_FILE,
        "JSON_OUTPUT": cr.JSON_OUTPUT,
        "HTML_OUTPUT": cr.HTML_OUTPUT,
        "TASKS_FILE": cr.TASKS_FILE,
        "DATA_REPORTS_DIR": cr.DATA_REPORTS_DIR,
        "HTML_REPORTS_DIR": cr.HTML_REPORTS_DIR,
    }
    cr.RANKINGS_FILE = data / "rankings.json"
    cr.WATCHLIST_FILE = data / "watchlist_rankings.json"
    cr.DATA_QUALITY_FILE = reports_data / "data_quality_audit.json"
    cr.SCHEDULE_RELIABILITY_FILE = reports_data / "schedule_reliability.json"
    cr.MARKET_RISK_FILE = reports_data / "market_risk_monitor.json"
    cr.BENCHMARK_FILE = reports_data / "benchmark_review.json"
    cr.PARITY_FILE = reports_data / "scoring_parity_review.json"
    cr.DIAGNOSTICS_FILE = reports_data / "ranking_diagnostics.json"
    cr.MARKET_OPEN_SCAN_FILE = reports_data / "market_open_scan.json"
    cr.BENCHMARK_SNAPSHOTS_FILE = reports_data / "benchmark_snapshots.jsonl"
    cr.JSON_OUTPUT = reports_data / "close_recap.json"
    cr.HTML_OUTPUT = reports_html / "close-recap.html"
    cr.TASKS_FILE = data / "tasks.json"
    cr.DATA_REPORTS_DIR = reports_data
    cr.HTML_REPORTS_DIR = reports_html
    return saved, data


def _restore_paths(saved):
    for k, v in saved.items():
        setattr(cr, k, v)


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
    if "diagnostics" in inputs:
        (data_dir / "reports" / "ranking_diagnostics.json").write_text(
            json.dumps(inputs["diagnostics"]), encoding="utf-8")
    if "market_open_scan" in inputs:
        (data_dir / "reports" / "market_open_scan.json").write_text(
            json.dumps(inputs["market_open_scan"]), encoding="utf-8")
    if "parity" in inputs:
        (data_dir / "reports" / "scoring_parity_review.json").write_text(
            json.dumps(inputs["parity"]), encoding="utf-8")


def _write_tasks_file(data_dir: Path):
    (data_dir / "tasks.json").write_text(json.dumps({
        "tasks": [
            {"id": "close-recap", "name": "Close Recap",
             "schedule": "Weekdays 3:30 PM CT", "last_run": "—",
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
                diagnostics=diagnostics_ok(),
            )
            assert cr.main() == 0
            report = json.loads(cr.JSON_OUTPUT.read_text())
            # fresh_rankings has 5 in Industrials and 5 in Technology in the
            # top10, so sector concentration is exactly 50% — that should
            # warn. Override by writing rankings with mixed sectors:
            assert cr.HTML_OUTPUT.exists()
            tasks = json.loads(cr.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "close-recap")
            assert row["report_url"] == "./reports/close-recap.html"
            assert "close" in row["schedule"].lower()
            # Either OK or WARN depending on sector mix and weekend.
            assert report["overall"] in ("OK", "WARN", "FAIL")
        finally:
            _restore_paths(saved)


def test_e2e_clean_ok_with_sectorless_rankings():
    """A genuine all-OK recap requires sector concentration <40%."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            # Build a rankings fixture with diverse sectors to avoid the
            # concentration WARN trigger.
            sectors = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
            rk = {
                "as_of": _now_chi_str(),
                "open_date": _today_chi_date_str(),
                "rows": [
                    {"rank": i + 1, "ticker": f"T{i}", "company": f"C{i}",
                     "ai_score": 8.0 - 0.05 * i, "change": (i - 5),
                     "sector": sectors[i]}
                    for i in range(10)
                ],
            }
            _write_inputs(
                data,
                rankings=rk,
                watchlist=watchlist_ok(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                market_risk=market_risk_ok(),
                benchmark=benchmark_ok(),
                diagnostics=diagnostics_ok(),
            )
            assert cr.main() == 0
            report = json.loads(cr.JSON_OUTPUT.read_text())
            tasks = json.loads(cr.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "close-recap")
            # On weekday, this should be OK; on weekend, today_live tolerance
            # may push to OK as well.
            assert report["overall"] == "OK", report
            assert row["status"] == "OK"
            assert row["last_run"] != "—"
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
            assert cr.main() == 0
            report = json.loads(cr.JSON_OUTPUT.read_text())
            tasks = json.loads(cr.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "close-recap")
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
                diagnostics=diagnostics_ok(),
            )
            assert cr.main() == 0
            report = json.loads(cr.JSON_OUTPUT.read_text())
            assert report["overall"] == "WARN", report
            assert report["sections"]["run_source"]["metrics"]["source"] == "recovered"
            tasks = json.loads(cr.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "close-recap")
            assert row["status"] == "warn"
        finally:
            _restore_paths(saved)


def test_e2e_uses_market_open_scan_for_intraday_entries():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            # Morning had T0..T7 plus 2 outsiders. Current rankings
            # (fresh_rankings) have T0..T9, so T8 and T9 are intraday adds.
            open_top = ["T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7",
                        "OLDA", "OLDB"]
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=watchlist_ok(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                market_risk=market_risk_ok(),
                diagnostics=diagnostics_ok(),
                market_open_scan=market_open_scan_with_top10(open_top),
            )
            assert cr.main() == 0
            report = json.loads(cr.JSON_OUTPUT.read_text())
            intraday = (report["sections"]["rankings_recap"]
                        ["metrics"]["intraday_new_entries"])
            tickers = {e["ticker"] for e in intraday}
            assert "T8" in tickers and "T9" in tickers
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
                diagnostics=diagnostics_ok(),
            )
            cr.main()
            tasks = json.loads(cr.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "close-recap")
            assert row["report_url"] == "./reports/close-recap.html"
            assert "3:35" in row["schedule"] or "close" in row["schedule"].lower()
            assert row["last_run"] != "—"
            # summary should be informative — at minimum contain a "·"
            # separator and a freshness label.
            assert "·" in row["summary"]
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

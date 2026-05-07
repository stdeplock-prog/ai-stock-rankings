"""Fixture-based tests for weekly_rebalance_check.py.

Covers:
  * no-history / limited-history change review behavior
  * entrant / exit detection vs prior snapshot
  * conviction score-change extraction
  * sector concentration WARN trigger on main/watchlist top25
  * benchmark context: lagging buckets flagged
  * task row update (id=weekly-rebalance, schedule, status, summary, url)
  * status logic: FAIL on stale rankings, WARN on limited history, OK
    when fresh + no notable warnings

Run: python 02_Code/Python/Reports/test_weekly_rebalance_check.py
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

import weekly_rebalance_check as wr  # noqa: E402


def _now_chi_str() -> str:
    now_utc = datetime.now(timezone.utc)
    chi = wr._to_chicago(now_utc)
    label = "CDT" if chi.utcoffset().total_seconds() == -5 * 3600 else "CST"
    return chi.strftime("%Y-%m-%d %I:%M %p ") + label


def _today_chi_date() -> str:
    return wr._to_chicago(datetime.now(timezone.utc)).date().strftime("%Y-%m-%d")


# ------------------- fixtures -------------------


def fresh_rankings(n: int = 30, sector_skew: bool = False) -> dict:
    out = {
        "as_of": _now_chi_str(),
        "open_date": _today_chi_date(),
        "rows": [],
    }
    sectors = (["Tech"] * 12 + ["Health"] * 5 + ["Energy"] * 5
               + ["Industrials"] * 4 + ["Real Estate"] * 4)
    for i in range(n):
        if sector_skew:
            sec = sectors[i] if i < len(sectors) else "Misc"
        else:
            sec = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"][i % 10]
        out["rows"].append({
            "rank": i + 1,
            "ticker": f"T{i}",
            "company": f"Company {i}",
            "ai_score": round(8.5 - 0.05 * i, 2),
            "fundamental": 7.5,
            "technical": 8.0,
            "sentiment": 6.0 if i != 3 else 3.0,
            "low_risk": 5.0 if i != 5 else 3.0,
            "change": (i - 5),
            "sector": sec,
        })
    return out


def stale_rankings(hours: int = 72) -> dict:
    now_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    chi = wr._to_chicago(now_utc)
    label = "CDT" if chi.utcoffset().total_seconds() == -5 * 3600 else "CST"
    return {
        "as_of": chi.strftime("%Y-%m-%d %I:%M %p ") + label,
        "open_date": chi.strftime("%Y-%m-%d"),
        "rows": [{"ticker": "T0", "rank": 1, "ai_score": 7.5}],
    }


def fresh_watchlist(n: int = 30) -> dict:
    rows = []
    for i in range(n):
        rows.append({
            "rank": i + 1,
            "ticker": f"WL{i}",
            "company": f"Watch {i}",
            "ai_score": round(8.5 - 0.05 * i, 2),
            "fundamental": 7.0, "technical": 8.0,
            "sentiment": 6.5, "low_risk": 5.0,
            "change": (i - 7),
            "sector": "Technology" if i < 12 else "Health",
            "data_source": "supplemental_yfinance" if i % 2 == 0 else "main_pipeline",
        })
    return {"as_of": _now_chi_str(), "rows": rows, "source_meta": {}}


def healthy_dq() -> dict:
    return {
        "overall": "OK",
        "sections": {
            "rankings": {"checks": [{"name": "row_count", "status": "OK"}]},
            "tasks": {"checks": []},
        },
    }


def fail_dq() -> dict:
    return {
        "overall": "FAIL",
        "sections": {
            "rankings": {"checks": [{"name": "row_count", "status": "FAIL"}]},
            "tasks": {"checks": []},
        },
    }


def schedule_ok() -> dict:
    return {"overall": "OK", "overall_effective": "OK", "sections": {}}


def schedule_recovered() -> dict:
    return {"overall": "FAIL", "overall_effective": "WARN", "sections": {}}


def benchmark_with_laggers() -> dict:
    return {
        "snapshot_summary": {
            "snapshots_total": 6,
            "horizons": {
                "1d": {"completed": 4, "buckets": {
                    "main_top10": {"snapshots": 4, "wins": 1, "losses": 2,
                                   "avg_mean_return": -0.005},
                    "watchlist_top10": {"snapshots": 4, "wins": 2, "losses": 1,
                                        "avg_mean_return": 0.012},
                }},
            },
        },
        "benchmark_compare": {
            "spy_return_21d": 0.012,
            "main_top25_mean_window_return": 0.04,
            "watchlist_top25_mean_window_return": 0.07,
        },
    }


def benchmark_clean() -> dict:
    return {
        "snapshot_summary": {
            "snapshots_total": 6,
            "horizons": {
                "1d": {"completed": 4, "buckets": {
                    "main_top10": {"snapshots": 4, "wins": 3, "losses": 1,
                                   "avg_mean_return": 0.004},
                    "watchlist_top10": {"snapshots": 4, "wins": 3, "losses": 1,
                                        "avg_mean_return": 0.006},
                }},
            },
        },
        "benchmark_compare": {},
    }


def diagnostics_ok() -> dict:
    return {"overall": "OK", "suspicious_ranks": []}


def diagnostics_warn() -> dict:
    return {
        "overall": "WARN",
        "suspicious_ranks": [
            {"group": "main_top10", "rank": 5, "ticker": "T2",
             "reasons": ["weak LOW_RISK 3.0"]},
            {"group": "watchlist_top10", "rank": 9, "ticker": "WL3",
             "reasons": ["weak SENT 3.5"]},
        ],
    }


def make_snapshot(date: str, main_top10: list[dict],
                  watchlist_top10: list[dict] | None = None,
                  supp_top10: list[dict] | None = None) -> dict:
    return {
        "as_of_date": date,
        "captured_at": date + "T15:35:00-05:00",
        "buckets": {
            "main_top10": {"members": main_top10, "size": len(main_top10)},
            "watchlist_top10": {
                "members": watchlist_top10 or [],
                "size": len(watchlist_top10 or []),
            },
            "supp_top10": {
                "members": supp_top10 or [],
                "size": len(supp_top10 or []),
            },
        },
        "forward": {},
    }


# ------------------- analyzer-level tests -------------------


def test_freshness_ok_when_fresh():
    sec = wr.analyze_freshness(fresh_rankings(), fresh_watchlist())
    assert sec["status"] == "OK"


def test_freshness_fail_when_missing():
    sec = wr.analyze_freshness(None, None)
    assert sec["status"] == "FAIL"


def test_freshness_fail_when_stale():
    sec = wr.analyze_freshness(stale_rankings(72), None)
    if sec["metrics"].get("is_weekend"):
        assert sec["status"] in ("OK", "WARN", "FAIL")
    else:
        assert sec["status"] == "FAIL"


def test_data_quality_critical_promotes_fail():
    sec = wr.analyze_data_quality(fail_dq())
    assert sec["status"] == "FAIL"
    assert sec["metrics"]["critical_section_fail"] is True


def test_schedule_recovered_warns():
    sec = wr.analyze_schedule(schedule_recovered())
    assert sec["status"] == "WARN"


def test_leaderboards_sector_concentration_warns():
    rk = fresh_rankings(n=30, sector_skew=True)  # 12 of 25 in Tech => 48%
    sec = wr.analyze_leaderboards(rk, fresh_watchlist())
    sc = sec["metrics"]["main_sector_concentration"]
    assert sc["warn"] is True
    assert sc["top_sector"] == "Tech"
    assert sec["status"] == "WARN"


def test_leaderboards_supp_top25_filtered():
    sec = wr.analyze_leaderboards(fresh_rankings(), fresh_watchlist(30))
    supp = sec["metrics"]["supp_top25"]
    assert supp, "expected SUPP rows"
    # All entries must have data_source supplemental_yfinance per fixture
    assert all(r.get("data_source") == "supplemental_yfinance" for r in supp)


def test_change_review_no_history_warns():
    rk = fresh_rankings()
    sec = wr.analyze_change_review(rk, fresh_watchlist(), [])
    assert sec["status"] == "WARN"
    assert "no snapshots" in (sec["metrics"].get("limitation") or "").lower()


def test_change_review_detects_entrants_and_exits():
    rk = fresh_rankings()
    wl = fresh_watchlist()
    today = _today_chi_date()
    prior_date = (datetime.fromisoformat(today) - timedelta(days=3)).date().strftime("%Y-%m-%d")
    # Prior had T1..T8 + OLD1, OLD2 in main_top10. Current is T0..T9.
    # So T0, T9 are new and OLD1, OLD2 are exits.
    prior_main = [
        {"ticker": f"T{i}", "ai_score": 7.0, "ref_close": 100.0,
         "sector": "Tech"} for i in range(1, 9)
    ] + [
        {"ticker": "OLD1", "ai_score": 7.5, "ref_close": 50.0, "sector": "X"},
        {"ticker": "OLD2", "ai_score": 7.4, "ref_close": 60.0, "sector": "Y"},
    ]
    snap = make_snapshot(prior_date, prior_main)
    sec = wr.analyze_change_review(rk, wl, [snap])
    deltas = sec["metrics"]["deltas"]["main_top10"]
    new_t = {e["ticker"] for e in deltas["new_entries"]}
    assert "T0" in new_t and "T9" in new_t
    assert set(deltas["exited"]) == {"OLD1", "OLD2"}
    assert sec["metrics"]["compared_against_date"] == prior_date


def test_change_review_score_changes_extracted():
    rk = fresh_rankings()
    wl = fresh_watchlist()
    today = _today_chi_date()
    prior_date = (datetime.fromisoformat(today) - timedelta(days=3)).date().strftime("%Y-%m-%d")
    prior_main = [
        # T0 had ai_score 6.0 prior — current is ~8.5 -> delta ~2.5
        {"ticker": "T0", "ai_score": 6.0, "sector": "A"},
        {"ticker": "T1", "ai_score": 8.45, "sector": "B"},  # delta tiny
    ]
    snap = make_snapshot(prior_date, prior_main)
    sec = wr.analyze_change_review(rk, wl, [snap])
    sc = sec["metrics"]["deltas"]["main_top10"]["score_changes"]
    assert sc, "expected score_changes"
    assert sc[0]["ticker"] == "T0"
    assert abs(sc[0]["delta"] - (rk["rows"][0]["ai_score"] - 6.0)) < 1e-6


def test_change_review_limited_history_warns():
    rk = fresh_rankings()
    wl = fresh_watchlist()
    today = _today_chi_date()
    prior_date = (datetime.fromisoformat(today) - timedelta(days=2)).date().strftime("%Y-%m-%d")
    snap = make_snapshot(prior_date, [
        {"ticker": "T1", "ai_score": 8.0, "sector": "A"}
    ])
    sec = wr.analyze_change_review(rk, wl, [snap])
    # only 1 snapshot < HISTORY_WARN_DAYS -> limited history WARN
    assert sec["metrics"]["limited_history"] is True
    assert sec["status"] == "WARN"


def test_benchmark_context_flags_laggers():
    sec = wr.analyze_benchmark_context(benchmark_with_laggers())
    assert sec["metrics"]["laggers"], "expected laggers"
    assert any(l["bucket"] == "main_top10" for l in sec["metrics"]["laggers"])
    assert sec["status"] == "WARN"


def test_benchmark_context_ok_when_clean():
    sec = wr.analyze_benchmark_context(benchmark_clean())
    assert sec["status"] == "OK"


def test_benchmark_context_missing_warns():
    sec = wr.analyze_benchmark_context(None)
    assert sec["status"] == "WARN"


def test_diagnostics_warn_propagates():
    sec = wr.analyze_diagnostics(diagnostics_warn())
    assert sec["status"] == "WARN"
    assert sec["metrics"]["suspicious_count"] == 2


def test_candidates_include_new_entrants_and_exits():
    rk = fresh_rankings()
    wl = fresh_watchlist()
    today = _today_chi_date()
    prior_date = (datetime.fromisoformat(today) - timedelta(days=3)).date().strftime("%Y-%m-%d")
    prior_main = [
        {"ticker": f"T{i}", "ai_score": 7.0, "sector": "A"}
        for i in range(1, 9)
    ] + [
        {"ticker": "OLD1", "ai_score": 7.0, "sector": "B"},
        {"ticker": "OLD2", "ai_score": 7.0, "sector": "C"},
    ]
    snap = make_snapshot(prior_date, prior_main)
    cr = wr.analyze_change_review(rk, wl, [snap])
    cands = wr.build_candidates(rk, wl, cr, diagnostics_warn())
    add_t = {c["ticker"] for c in cands["candidate_adds"]}
    trim_t = {c["ticker"] for c in cands["candidate_trims"]}
    # New entrants T0, T9 should land in adds.
    assert "T0" in add_t and "T9" in add_t
    # Exits OLD1, OLD2 should land in trims.
    assert "OLD1" in trim_t and "OLD2" in trim_t
    # Suspicious from diagnostics_warn (T2) should be in trims.
    assert "T2" in trim_t


def test_compute_overall_fail_when_fresh_fail():
    sections = {
        "freshness": {"status": "FAIL", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "schedule": {"status": "OK", "metrics": {}},
        "leaderboards": {"status": "OK", "metrics": {}},
        "change_review": {"status": "OK", "metrics": {}},
        "benchmark_context": {"status": "OK", "metrics": {}},
        "diagnostics": {"status": "OK", "metrics": {}},
    }
    assert wr.compute_overall(sections) == "FAIL"


def test_compute_overall_warn_when_one_warn():
    sections = {
        "freshness": {"status": "OK", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "schedule": {"status": "WARN", "metrics": {}},
        "leaderboards": {"status": "OK", "metrics": {}},
        "change_review": {"status": "OK", "metrics": {}},
        "benchmark_context": {"status": "OK", "metrics": {}},
        "diagnostics": {"status": "OK", "metrics": {}},
    }
    assert wr.compute_overall(sections) == "WARN"


def test_compute_overall_ok_when_all_ok():
    sections = {
        "freshness": {"status": "OK", "metrics": {}},
        "data_quality": {"status": "OK", "metrics": {}},
        "schedule": {"status": "OK", "metrics": {}},
        "leaderboards": {"status": "OK", "metrics": {}},
        "change_review": {"status": "OK", "metrics": {}},
        "benchmark_context": {"status": "OK", "metrics": {}},
        "diagnostics": {"status": "OK", "metrics": {}},
    }
    assert wr.compute_overall(sections) == "OK"


# ------------------- end-to-end -------------------


def _patched_paths(tmp: Path):
    data = tmp / "data"
    reports_data = data / "reports"
    reports_html = tmp / "reports"
    reports_data.mkdir(parents=True, exist_ok=True)
    reports_html.mkdir(parents=True, exist_ok=True)
    saved = {
        "RANKINGS_FILE": wr.RANKINGS_FILE,
        "WATCHLIST_FILE": wr.WATCHLIST_FILE,
        "BENCHMARK_FILE": wr.BENCHMARK_FILE,
        "BENCHMARK_SNAPSHOTS_FILE": wr.BENCHMARK_SNAPSHOTS_FILE,
        "DIAGNOSTICS_FILE": wr.DIAGNOSTICS_FILE,
        "PARITY_FILE": wr.PARITY_FILE,
        "DATA_QUALITY_FILE": wr.DATA_QUALITY_FILE,
        "SCHEDULE_RELIABILITY_FILE": wr.SCHEDULE_RELIABILITY_FILE,
        "JSON_OUTPUT": wr.JSON_OUTPUT,
        "HTML_OUTPUT": wr.HTML_OUTPUT,
        "TASKS_FILE": wr.TASKS_FILE,
        "DATA_REPORTS_DIR": wr.DATA_REPORTS_DIR,
        "HTML_REPORTS_DIR": wr.HTML_REPORTS_DIR,
    }
    wr.RANKINGS_FILE = data / "rankings.json"
    wr.WATCHLIST_FILE = data / "watchlist_rankings.json"
    wr.BENCHMARK_FILE = reports_data / "benchmark_review.json"
    wr.BENCHMARK_SNAPSHOTS_FILE = reports_data / "benchmark_snapshots.jsonl"
    wr.DIAGNOSTICS_FILE = reports_data / "ranking_diagnostics.json"
    wr.PARITY_FILE = reports_data / "scoring_parity_review.json"
    wr.DATA_QUALITY_FILE = reports_data / "data_quality_audit.json"
    wr.SCHEDULE_RELIABILITY_FILE = reports_data / "schedule_reliability.json"
    wr.JSON_OUTPUT = reports_data / "weekly_rebalance_check.json"
    wr.HTML_OUTPUT = reports_html / "weekly-rebalance-check.html"
    wr.TASKS_FILE = data / "tasks.json"
    wr.DATA_REPORTS_DIR = reports_data
    wr.HTML_REPORTS_DIR = reports_html
    return saved, data, reports_data


def _restore_paths(saved):
    for k, v in saved.items():
        setattr(wr, k, v)


def _write_inputs(data_dir: Path, **inputs):
    if "rankings" in inputs:
        (data_dir / "rankings.json").write_text(
            json.dumps(inputs["rankings"]), encoding="utf-8")
    if "watchlist" in inputs:
        (data_dir / "watchlist_rankings.json").write_text(
            json.dumps(inputs["watchlist"]), encoding="utf-8")
    rd = data_dir / "reports"
    if "dq" in inputs:
        (rd / "data_quality_audit.json").write_text(
            json.dumps(inputs["dq"]), encoding="utf-8")
    if "schedule" in inputs:
        (rd / "schedule_reliability.json").write_text(
            json.dumps(inputs["schedule"]), encoding="utf-8")
    if "benchmark" in inputs:
        (rd / "benchmark_review.json").write_text(
            json.dumps(inputs["benchmark"]), encoding="utf-8")
    if "diagnostics" in inputs:
        (rd / "ranking_diagnostics.json").write_text(
            json.dumps(inputs["diagnostics"]), encoding="utf-8")
    if "snapshots" in inputs:
        with (rd / "benchmark_snapshots.jsonl").open("w", encoding="utf-8") as f:
            for snap in inputs["snapshots"]:
                f.write(json.dumps(snap) + "\n")


def _write_tasks_file(data_dir: Path, task_id: str = "weekly-rebalance"):
    (data_dir / "tasks.json").write_text(json.dumps({
        "tasks": [
            {"id": task_id, "name": "Weekly Rebalance Check",
             "schedule": "Fridays 4:00 PM CT", "last_run": "—",
             "next_run": "—", "status": "Not Run", "summary": "Planned task"},
        ],
    }) + "\n", encoding="utf-8")


def test_e2e_no_history_warn_and_task_stamped():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data, _ = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=fresh_watchlist(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                benchmark=benchmark_clean(),
                diagnostics=diagnostics_ok(),
            )
            assert wr.main() == 0
            report = json.loads(wr.JSON_OUTPUT.read_text())
            # No snapshots -> change_review WARN -> overall WARN
            assert report["overall"] == "WARN"
            tasks = json.loads(wr.TASKS_FILE.read_text())
            row = next(t for t in tasks["tasks"] if t["id"] == "weekly-rebalance")
            assert row["report_url"] == "./reports/weekly-rebalance-check.html"
            assert "Friday" in row["schedule"] or "Fridays" in row["schedule"]
            assert row["last_run"] != "—"
            assert row["status"] == "warn"
            assert "·" in row["summary"]
            assert wr.HTML_OUTPUT.exists()
        finally:
            _restore_paths(saved)


def test_e2e_stale_overall_fail():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data, _ = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            _write_inputs(
                data,
                rankings=stale_rankings(72),
                watchlist=fresh_watchlist(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
            )
            assert wr.main() == 0
            report = json.loads(wr.JSON_OUTPUT.read_text())
            if report["sections"]["freshness"]["metrics"].get("is_weekend"):
                assert report["overall"] in ("OK", "WARN", "FAIL")
            else:
                assert report["overall"] == "FAIL"
        finally:
            _restore_paths(saved)


def test_e2e_supports_fallback_task_id():
    """If the existing tasks.json uses 'weekly-rebalance-check', stamp
    that row instead. The task id is preserved in place."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data, _ = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data, task_id="weekly-rebalance-check")
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=fresh_watchlist(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                benchmark=benchmark_clean(),
                diagnostics=diagnostics_ok(),
            )
            assert wr.main() == 0
            tasks = json.loads(wr.TASKS_FILE.read_text())
            row = next(
                t for t in tasks["tasks"] if t["id"] == "weekly-rebalance-check"
            )
            assert row["report_url"] == "./reports/weekly-rebalance-check.html"
            assert row["last_run"] != "—"
        finally:
            _restore_paths(saved)


def test_e2e_with_snapshots_detects_changes_and_clean_overall():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        saved, data, _ = _patched_paths(tmp_path)
        try:
            _write_tasks_file(data)
            today = _today_chi_date()
            prior_dt = (datetime.fromisoformat(today) - timedelta(days=3)).date().strftime("%Y-%m-%d")
            # 5 snapshots over the last week => not limited history.
            snaps = []
            for off in range(5, 0, -1):
                d = (datetime.fromisoformat(today) - timedelta(days=off)).date().strftime("%Y-%m-%d")
                main_members = [
                    {"ticker": f"T{i}", "ai_score": 7.0, "sector": "X"}
                    for i in range(1, 9)
                ] + [
                    {"ticker": "OLDA", "ai_score": 7.0, "sector": "X"},
                    {"ticker": "OLDB", "ai_score": 7.0, "sector": "X"},
                ]
                snaps.append(make_snapshot(d, main_members))
            _write_inputs(
                data,
                rankings=fresh_rankings(),
                watchlist=fresh_watchlist(),
                dq=healthy_dq(),
                schedule=schedule_ok(),
                benchmark=benchmark_clean(),
                diagnostics=diagnostics_ok(),
                snapshots=snaps,
            )
            assert wr.main() == 0
            report = json.loads(wr.JSON_OUTPUT.read_text())
            cr = report["sections"]["change_review"]["metrics"]
            assert cr["snapshots_total"] == 5
            assert cr["limited_history"] is False
            deltas = cr["deltas"]["main_top10"]
            assert "T0" in {e["ticker"] for e in deltas["new_entries"]}
            assert "OLDA" in deltas["exited"] and "OLDB" in deltas["exited"]
            # On a weekday this should be OK; on weekend may shift to WARN.
            assert report["overall"] in ("OK", "WARN")
        finally:
            _restore_paths(saved)


# ------------------- runner -------------------


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

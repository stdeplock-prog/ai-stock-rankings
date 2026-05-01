"""Fixture-based tests for data_quality_audit.py.

Validates parsing, threshold logic, freshness math, and rollup levels
against synthetic in-memory payloads. No filesystem writes, no network.

Run: python 02_Code/Python/Reports/test_data_quality_audit.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

# Make the audit module importable regardless of CWD.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import data_quality_audit as dqa  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _as_of_minutes_ago(minutes: int) -> str:
    """Format a CDT/CST as_of string for a moment `minutes` ago in Chicago."""
    now_utc = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    # Mimic the pipeline's naive seasonal offset: CDT in months 3-10, CST otherwise.
    offset_h = -5 if 3 <= now_utc.month <= 10 else -6
    label = "CDT" if offset_h == -5 else "CST"
    chi = now_utc.astimezone(timezone(timedelta(hours=offset_h)))
    return chi.strftime("%Y-%m-%d %I:%M %p") + " " + label


def test_parse_as_of_roundtrip():
    s = "2026-05-01 11:06 AM CDT"
    dt = dqa._parse_as_of(s)
    assert dt is not None, "expected parse to succeed"
    assert dt.tzinfo is not None, "expected aware datetime"
    # 11:06 AM CDT == 16:06 UTC
    assert dt.hour == 16 and dt.minute == 6, f"unexpected utc time: {dt}"

    assert dqa._parse_as_of(None) is None
    assert dqa._parse_as_of("garbage") is None
    assert dqa._parse_as_of("2026-05-01 11:06 AM EST") is None


def test_threshold_check_levels():
    ok = dqa._threshold_check("x", 1, 100, warn_pct=0.05, fail_pct=0.20)
    warn = dqa._threshold_check("x", 10, 100, warn_pct=0.05, fail_pct=0.20)
    bad = dqa._threshold_check("x", 30, 100, warn_pct=0.05, fail_pct=0.20)
    empty = dqa._threshold_check("x", 0, 0, warn_pct=0.05, fail_pct=0.20)
    assert ok["status"] == "OK", ok
    assert warn["status"] == "WARN", warn
    assert bad["status"] == "FAIL", bad
    assert empty["status"] == "OK", empty


def test_freshness_check_recent_ok():
    fresh = _as_of_minutes_ago(30)
    out = dqa._freshness_check("rankings_freshness", fresh)
    # 30min on a weekday should be OK; on a weekend morning still OK.
    assert out["status"] in ("OK",), f"expected OK on fresh as_of, got {out}"


def test_freshness_check_unparseable_warns():
    out = dqa._freshness_check("rankings_freshness", "not a date")
    assert out["status"] == "WARN", out


def test_audit_rankings_minimal_ok():
    payload = {
        "as_of": _as_of_minutes_ago(15),
        "open_date": "2026-05-01",
        "is_open_run": False,
        "universe": "Test",
        "rows": [
            {
                "ticker": f"T{i}",
                "market_cap": "1B",
                "sector": "Information Technology",
                "industry": "Software",
                "closes": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                "change": (i % 5) - 2,  # mix of +/-/0
                "ai_score": 5.0,
                "fundamental": 5.0,
                "technical": 5.0,
                "sentiment": 5.0,
                "low_risk": 5.0,
                "swing_score": 5.0,
            }
            for i in range(100)
        ],
    }
    out = dqa.audit_rankings(payload)
    assert out["status"] == "OK", f"expected OK rollup, got {out['status']}: {out['checks']}"
    assert out["metrics"]["row_count"] == 100
    assert out["metrics"]["missing_sector"] == 0


def test_audit_rankings_flags_missing_sector_and_short_count():
    rows = [
        {"ticker": "A", "market_cap": "", "sector": "", "industry": "",
         "closes": [], "change": None}
    ]
    payload = {"as_of": _as_of_minutes_ago(10), "rows": rows}
    out = dqa.audit_rankings(payload)
    # Row count != 100, missing market_cap, sector, industry, short sparkline,
    # missing scores — should at minimum WARN, more likely FAIL.
    assert out["status"] in ("WARN", "FAIL"), f"expected non-OK, got {out['status']}"
    assert out["metrics"]["closes_short_count"] == 1


def test_audit_rankings_missing_payload_fails():
    out = dqa.audit_rankings(None)
    assert out["status"] == "FAIL", out
    assert out["present"] is False


def test_audit_watchlist_unavailable_spike_fail():
    payload = {
        "as_of": _as_of_minutes_ago(10),
        "rows": [
            {"ticker": f"T{i}", "data_source": "main_pipeline", "source": "csv",
             "market_cap": "1B", "sector": "X"}
            for i in range(50)
        ],
        "unavailable": [{"input": f"BAD{i}"} for i in range(20)],
        "source_meta": {
            "csv_count": 70, "tradingview_count": 10, "combined_unique": 70,
            "supp_summary": {"total": 0, "full_fundamentals": 0,
                             "metadata_only": 0, "price_only": 0,
                             "technical_only": 0, "unavailable": 0},
        },
    }
    out = dqa.audit_watchlist(payload)
    spike = next(c for c in out["checks"] if c["name"] == "unavailable_spike")
    assert spike["status"] == "FAIL", spike


def test_audit_watchlist_supp_full_fundamentals_warn():
    payload = {
        "as_of": _as_of_minutes_ago(10),
        "rows": [],
        "unavailable": [],
        "source_meta": {
            "combined_unique": 100,
            "supp_summary": {"total": 50, "full_fundamentals": 25,
                             "price_only": 20, "technical_only": 5,
                             "metadata_only": 0, "unavailable": 0},
        },
    }
    out = dqa.audit_watchlist(payload)
    fund = next(c for c in out["checks"] if c["name"] == "supp_full_fundamentals")
    assert fund["status"] == "WARN", fund


def test_audit_tasks_all_not_run_fails():
    payload = {"tasks": [{"id": "a", "status": "Not Run"},
                         {"id": "b", "status": "Not Run"}]}
    out = dqa.audit_tasks(payload, rankings_as_of=None)
    assert out["status"] == "FAIL", out
    names = [c["name"] for c in out["checks"] if c["status"] == "FAIL"]
    assert "tasks_all_not_run" in names, names


def test_audit_tasks_stale_report_metadata_warns():
    rankings_dt = datetime.now(timezone.utc)
    stale_last_run = "2026-04-25 09:00 AM CDT"  # well over 24h before now
    payload = {"tasks": [
        {"id": "report-1", "status": "OK", "last_run": stale_last_run,
         "report_url": "./reports/x.html"},
        {"id": "report-2", "status": "Not Run"},
    ]}
    out = dqa.audit_tasks(payload, rankings_as_of=rankings_dt)
    stale = next(c for c in out["checks"] if c["name"] == "stale_report_metadata")
    assert stale["status"] == "WARN", stale
    assert "report-1" in (stale.get("data", {}).get("ids") or [])


def test_overall_rollup_is_worst_section():
    sections = {
        "a": {"status": "OK"},
        "b": {"status": "WARN"},
        "c": {"status": "OK"},
    }
    assert dqa._build_overall(sections) == "WARN"
    sections["c"] = {"status": "FAIL"}
    assert dqa._build_overall(sections) == "FAIL"


def test_render_html_contains_sections_and_levels():
    report = {
        "generated_at": "2026-05-01T17:00:00Z",
        "overall": "WARN",
        "sections": {
            "rankings": {
                "status": "WARN",
                "metrics": {"row_count": 100},
                "checks": [{"name": "row_count", "status": "OK", "message": "100 rows"}],
            }
        },
    }
    html = dqa._render_html(report)
    assert "Data Quality Audit" in html
    assert "WARN" in html
    assert "row_count" in html


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
        fail(f"{failed} test(s) failed")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

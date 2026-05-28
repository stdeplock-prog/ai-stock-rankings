"""Fixture-based tests for market_risk_monitor.py.

Covers Cboe put/call HTML parsing, conservative classification thresholds,
graceful fallback on fetch/parse failure, and HTML rendering wiring. No
network calls — fetch is bypassed by passing fixture HTML directly.

Run: python 02_Code/Python/Reports/test_market_risk_monitor.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import market_risk_monitor as mrm  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# Minimal fixture mirroring the structure observed on cboe.com (table rows
# with adjacent <td> for label and value). Real-world classes/attributes vary,
# so the test fixture intentionally uses a leaner attribute set than prod.
CBOE_FIXTURE_HTML = """
<html><body>
<table>
  <tr><td class="tw-align-middle">TOTAL PUT/CALL RATIO</td><td class="tw-align-middle tw-text-right">0.83</td></tr>
  <tr><td>INDEX PUT/CALL RATIO</td><td>1.08</td></tr>
  <tr><td>EXCHANGE TRADED PRODUCTS PUT/CALL RATIO</td><td>1.28</td></tr>
  <tr><td>EQUITY PUT/CALL RATIO</td><td>0.43</td></tr>
  <tr><td>CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO</td><td>0.28</td></tr>
  <tr><td>SPX + SPXW PUT/CALL RATIO</td><td>1.18</td></tr>
  <tr><td>OEX PUT/CALL RATIO</td><td>0.55</td></tr>
</table>
</body></html>
"""


def test_parse_extracts_all_known_series():
    ratios = mrm._parse_cboe_html(CBOE_FIXTURE_HTML)
    expected = {
        "total": 0.83,
        "index": 1.08,
        "etp": 1.28,
        "equity": 0.43,
        "vix": 0.28,
        "spx": 1.18,
    }
    for key, val in expected.items():
        if key not in ratios:
            fail(f"missing series '{key}' in parse output")
        if abs(ratios[key] - val) > 1e-9:
            fail(f"series '{key}': expected {val}, got {ratios[key]}")


def test_parse_handles_garbage_html():
    ratios = mrm._parse_cboe_html("<html>no data here</html>")
    if ratios:
        fail(f"expected empty dict on unrelated HTML, got {ratios}")


def test_parse_handles_empty_string():
    if mrm._parse_cboe_html("") != {}:
        fail("expected empty dict on empty HTML")


def test_classify_equity_low_is_warn():
    label, alert, kind = mrm._classify_equity_pc(0.35)
    if not alert or kind != "warn":
        fail(f"equity 0.35 should warn (speculative), got {label}/{alert}/{kind}")


def test_classify_equity_high_is_warn():
    label, alert, kind = mrm._classify_equity_pc(1.25)
    if not alert or kind != "warn":
        fail(f"equity 1.25 should warn (defensive), got {label}/{alert}/{kind}")


def test_classify_equity_neutral_is_pass():
    label, alert, kind = mrm._classify_equity_pc(0.70)
    if alert or kind != "pass":
        fail(f"equity 0.70 should pass (neutral), got {label}/{alert}/{kind}")


def test_classify_equity_boundary_low_inclusive():
    # 0.40 should still warn (<=)
    _, alert, _ = mrm._classify_equity_pc(0.40)
    if not alert:
        fail("equity 0.40 should warn at lower boundary")


def test_classify_equity_boundary_high_inclusive():
    _, alert, _ = mrm._classify_equity_pc(1.20)
    if not alert:
        fail("equity 1.20 should warn at upper boundary")


def test_build_put_call_with_fixture():
    p = mrm._build_put_call(html=CBOE_FIXTURE_HTML)
    if p["status"] not in ("ok", "warn"):
        fail(f"unexpected status {p['status']}")
    if p["value"] != 0.43:
        fail(f"expected equity headline 0.43, got {p['value']}")
    # 0.43 is in neutral range -> ok
    if p["status"] != "ok":
        fail(f"equity 0.43 should classify as ok, got {p['status']}")
    if "ratios" not in p or len(p["ratios"]) < 6:
        fail(f"expected >=6 ratios, got {p.get('ratios')}")
    if p["source"] != mrm.CBOE_DAILY_STATS_URL:
        fail("expected source URL to be CBOE_DAILY_STATS_URL")
    if not p.get("fetched_at"):
        fail("expected fetched_at timestamp")


def test_build_put_call_fetch_failure_falls_back():
    # Empty html -> source_needed, mirrors fetch failure path.
    p = mrm._build_put_call(html="")
    if p["status"] != "source_needed":
        fail(f"expected source_needed on empty html, got {p['status']}")
    if p["value"] is not None:
        fail("expected value=None on fallback")
    if p["source"] != mrm.CBOE_DAILY_STATS_URL:
        fail("source URL should still be reported on fallback")


def test_build_put_call_parse_failure_falls_back():
    p = mrm._build_put_call(html="<html>unrelated</html>")
    if p["status"] != "source_needed":
        fail(f"expected source_needed on unparseable html, got {p['status']}")


def test_render_includes_put_call_table_when_ok():
    # Build minimal payload mirroring build_report() output, with fixture P/C.
    payload = {
        "generated_at": "2026-05-28 00:00 UTC",
        "indicators": {
            "polls": mrm._placeholder("POLLS Indicator"),
            "adr": mrm._placeholder("ADR Indicator"),
            "vix": {"value": 15.0, "status": "normal", "alert": False},
            "ndr": mrm._placeholder("NDR Indicator"),
            "put_call_ratio": mrm._build_put_call(html=CBOE_FIXTURE_HTML),
            "generals_fail": {
                "rows": [],
                "below_count": 0,
                "available_count": 0,
                "threshold": 3,
                "alert": False,
            },
        },
    }
    html = mrm.render_html(payload)
    if "Put/Call Ratios — Cboe Daily" not in html:
        fail("expected put/call detail section in rendered HTML")
    if "0.43" not in html:
        fail("expected equity ratio 0.43 in rendered HTML")
    if "0.83" not in html:
        fail("expected total ratio 0.83 in rendered HTML")


def test_render_falls_back_when_source_needed():
    payload = {
        "generated_at": "2026-05-28 00:00 UTC",
        "indicators": {
            "polls": mrm._placeholder("POLLS Indicator"),
            "adr": mrm._placeholder("ADR Indicator"),
            "vix": {"value": 15.0, "status": "normal", "alert": False},
            "ndr": mrm._placeholder("NDR Indicator"),
            "put_call_ratio": mrm._build_put_call(html=""),
            "generals_fail": {
                "rows": [], "below_count": 0, "available_count": 0,
                "threshold": 3, "alert": False,
            },
        },
    }
    html = mrm.render_html(payload)
    if "Put/Call Ratios — Cboe Daily" in html:
        fail("detail table should be suppressed on source_needed status")
    if "Source needed" not in html:
        fail("expected 'Source needed' pill when fetch fails")


def test_summary_includes_equity_pc_when_present():
    payload = {
        "generated_at": "2026-05-28 00:00 UTC",
        "indicators": {
            "polls": mrm._placeholder("POLLS Indicator"),
            "adr": mrm._placeholder("ADR Indicator"),
            "vix": {"value": 15.0, "status": "normal", "alert": False},
            "ndr": mrm._placeholder("NDR Indicator"),
            "put_call_ratio": mrm._build_put_call(html=CBOE_FIXTURE_HTML),
            "generals_fail": {
                "rows": [], "below_count": 0, "available_count": 0,
                "threshold": 3, "alert": False,
            },
        },
    }
    status, summary = mrm._summary_from_payload(payload)
    if "Equity P/C 0.43" not in summary:
        fail(f"expected equity P/C in summary, got: {summary}")
    # 0.43 is neutral -> OK
    if status != "OK":
        fail(f"expected OK status with neutral P/C, got {status}")


def test_summary_warn_when_equity_pc_extreme():
    # Hand-craft a put_call_ratio dict simulating an extreme value.
    payload = {
        "generated_at": "2026-05-28 00:00 UTC",
        "indicators": {
            "polls": mrm._placeholder("POLLS Indicator"),
            "adr": mrm._placeholder("ADR Indicator"),
            "vix": {"value": 15.0, "status": "normal", "alert": False},
            "ndr": mrm._placeholder("NDR Indicator"),
            "put_call_ratio": {
                "ratios": {"equity": 0.30},
                "value": 0.30,
                "status": "warn",
                "alert": True,
                "label": "Low — speculative",
                "note": "",
                "source": mrm.CBOE_DAILY_STATS_URL,
                "fetched_at": "2026-05-28 00:00 UTC",
            },
            "generals_fail": {
                "rows": [], "below_count": 0, "available_count": 0,
                "threshold": 3, "alert": False,
            },
        },
    }
    status, summary = mrm._summary_from_payload(payload)
    if status != "warn":
        fail(f"expected warn status on extreme P/C, got {status}")
    if "0.30" not in summary:
        fail(f"expected extreme value in summary, got: {summary}")


TESTS = [
    test_parse_extracts_all_known_series,
    test_parse_handles_garbage_html,
    test_parse_handles_empty_string,
    test_classify_equity_low_is_warn,
    test_classify_equity_high_is_warn,
    test_classify_equity_neutral_is_pass,
    test_classify_equity_boundary_low_inclusive,
    test_classify_equity_boundary_high_inclusive,
    test_build_put_call_with_fixture,
    test_build_put_call_fetch_failure_falls_back,
    test_build_put_call_parse_failure_falls_back,
    test_render_includes_put_call_table_when_ok,
    test_render_falls_back_when_source_needed,
    test_summary_includes_equity_pc_when_present,
    test_summary_warn_when_equity_pc_extreme,
]


def main() -> int:
    for t in TESTS:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\n{len(TESTS)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

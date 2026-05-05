"""Fixture-based tests for low_risk_drift_review.py.

Validates the verdict logic for the four expected outcomes
(selection_bias / data_gap / formula_issue / mixed / indeterminate),
the per-group stats, and the same-ticker overlap signal. No filesystem
writes, no network.

Run: python 02_Code/Python/Reports/test_low_risk_drift_review.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import low_risk_drift_review as lrd  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# ---------- distribution / market_cap helpers ----------


def test_distribution_basic():
    d = lrd.distribution([1, 2, 3, 4, 5])
    assert d["n"] == 5 and d["mean"] == 3.0 and d["median"] == 3, d
    assert d["p25"] == 2.0 and d["p75"] == 4.0, d


def test_distribution_ignores_nulls_and_nan():
    nan = float("nan")
    d = lrd.distribution([1.0, None, "x", nan, 5.0])
    assert d["n"] == 2 and d["null_count"] == 3, d
    assert d["mean"] == 3.0 and d["min"] == 1.0 and d["max"] == 5.0, d


def test_distribution_empty():
    d = lrd.distribution([])
    assert d["n"] == 0 and d["mean"] is None and d["null_count"] == 0, d


def test_market_cap_to_float():
    assert lrd._market_cap_to_float("9.58B") == 9.58e9
    assert lrd._market_cap_to_float("4.17T") == 4.17e12
    assert lrd._market_cap_to_float("732.4M") == 732.4e6
    assert lrd._market_cap_to_float("500K") == 500e3
    assert lrd._market_cap_to_float(None) is None
    assert lrd._market_cap_to_float("") is None
    assert lrd._market_cap_to_float("not-a-cap") is None
    assert lrd._market_cap_to_float(1234.5) == 1234.5


# ---------- split_groups ----------


def test_split_groups_partitions_by_data_source():
    rankings = {"rows": [{"ticker": "A", "rank": 1}, {"ticker": "B", "rank": 2}]}
    watchlist = {"rows": [
        {"ticker": "A", "data_source": "main_pipeline"},
        {"ticker": "C", "data_source": "supplemental_yfinance"},
    ]}
    g = lrd.split_groups(rankings, watchlist)
    assert len(g["main_rankings"]) == 2
    assert len(g["watchlist_main_pipeline"]) == 1
    assert len(g["watchlist_supp"]) == 1
    assert len(g["main_top10"]) == 2  # only 2 main rows


# ---------- coverage_metrics ----------


def test_coverage_metrics_atr_and_vol():
    rows = [
        {"atr_pct": 0.03, "vol_bucket": "Med", "swing_score": 6.0, "market_cap": "10B"},
        {"atr_pct": None, "vol_bucket": "", "swing_score": None, "market_cap": None},
        {"atr_pct": 0.05, "vol_bucket": "High", "swing_score": 7.0, "market_cap": "1.2T"},
    ]
    cov = lrd.coverage_metrics(rows)
    assert cov["row_count"] == 3
    assert cov["atr_pct"]["null"] == 1
    assert cov["vol_bucket"]["null"] == 1
    assert cov["swing_score"]["null"] == 1
    assert cov["market_cap"]["null"] == 1


# ---------- overlap_analysis: formula issue signal ----------


def test_overlap_no_diff_when_low_risk_matches():
    main = [{"ticker": "A", "low_risk": 7.5}, {"ticker": "B", "low_risk": 6.0}]
    wl_main = [{"ticker": "A", "low_risk": 7.5}, {"ticker": "B", "low_risk": 6.0}]
    o = lrd.overlap_analysis(main, wl_main)
    assert o["shared_count"] == 2
    assert o["differing_count"] == 0
    assert o["max_abs_delta"] == 0.0


def test_overlap_flags_diff_when_same_ticker_drifts():
    main = [{"ticker": "A", "low_risk": 7.5}, {"ticker": "B", "low_risk": 6.0}]
    # B has different low_risk in watchlist file — formula or pipeline drift
    wl_main = [{"ticker": "A", "low_risk": 7.5}, {"ticker": "B", "low_risk": 4.0}]
    o = lrd.overlap_analysis(main, wl_main)
    assert o["shared_count"] == 2
    assert o["differing_count"] == 1
    assert o["max_abs_delta"] == 2.0
    assert o["top_diffs"][0]["ticker"] == "B"


# ---------- verdict: selection_bias ----------


def _mk_main_rows(n=50, low_risk=7.5):
    return [
        {"ticker": f"M{i}", "low_risk": low_risk, "atr_pct": 0.02,
         "vol_bucket": "Low" if i < n // 2 else "Med",
         "swing_score": 6.0, "market_cap": "100B",
         "sector": "Industrials" if i % 2 == 0 else "Financial Services",
         "data_source": "main_pipeline", "rank": i + 1}
        for i in range(n)
    ]


def _mk_wl_main_rows(n=30, low_risk=5.4):
    return [
        {"ticker": f"W{i}", "low_risk": low_risk, "atr_pct": 0.06,
         "vol_bucket": "High",
         "swing_score": 5.5, "market_cap": "5B",
         "sector": "Technology",
         "data_source": "main_pipeline"}
        for i in range(n)
    ]


def test_verdict_selection_bias_when_universe_mix_differs():
    rankings = {"rows": _mk_main_rows()}
    watchlist = {"rows": _mk_wl_main_rows()}
    report = lrd.build_report(rankings, watchlist)
    v = report["verdict"]
    assert v["verdict"] == "selection_bias", v
    assert v["selection_bias_flag"]["triggered"] is True
    # No shared tickers between M* and W*, so formula flag must be off.
    assert v["formula_flag"]["triggered"] is False
    # All risk inputs are populated, so data_gap must be off.
    assert v["data_gap_flag"]["triggered"] is False
    # Recommendations should mention "Leave the low_risk formula unchanged".
    assert any("Leave the low_risk formula unchanged" in r for r in report["recommendations"]), report["recommendations"]


# ---------- verdict: data_gap ----------


def test_verdict_data_gap_when_risk_inputs_missing():
    rankings = {"rows": _mk_main_rows()}
    # Watchlist with same universe shape (so no selection_bias) but missing
    # atr_pct on most rows.
    wl = []
    for i in range(40):
        wl.append({
            "ticker": f"M{i}",  # shared ticker, same low_risk → no formula flag
            "low_risk": 7.5,
            "atr_pct": None,    # the data gap
            "vol_bucket": "Low" if i < 20 else "Med",
            "swing_score": 6.0,
            "market_cap": "100B",
            "sector": "Industrials" if i % 2 == 0 else "Financial Services",
            "data_source": "main_pipeline",
        })
    report = lrd.build_report(rankings, {"rows": wl})
    v = report["verdict"]
    assert v["data_gap_flag"]["triggered"] is True
    assert "data_gap" in v["flags"], v
    # Should NOT flip selection_bias on (universe mix ≈ same)
    assert v["selection_bias_flag"]["triggered"] is False, v
    # Should NOT flag formula issue because shared tickers' low_risk matches.
    assert v["formula_flag"]["triggered"] is False, v
    assert v["verdict"] == "data_gap", v
    assert any("missing risk inputs" in r for r in report["recommendations"]), report["recommendations"]


# ---------- verdict: formula_issue ----------


def test_verdict_formula_issue_when_same_ticker_diverges():
    main_rows = _mk_main_rows(n=50, low_risk=7.5)
    # Same tickers in watchlist but with materially different low_risk.
    wl = []
    for i in range(50):
        wl.append({
            "ticker": f"M{i}",
            "low_risk": 5.0,    # 2.5pt drift on same ticker — clear formula signal
            "atr_pct": 0.02,
            "vol_bucket": "Low" if i < 25 else "Med",
            "swing_score": 6.0,
            "market_cap": "100B",
            "sector": "Industrials" if i % 2 == 0 else "Financial Services",
            "data_source": "main_pipeline",
        })
    report = lrd.build_report({"rows": main_rows}, {"rows": wl})
    v = report["verdict"]
    assert v["formula_flag"]["triggered"] is True, v
    assert v["formula_flag"]["severity"] == "FAIL", v
    assert v["verdict"] == "formula_issue", v
    assert any("STOP" in r or "formula or transformation difference" in r
               for r in report["recommendations"]), report["recommendations"]


# ---------- verdict: indeterminate when nothing fires ----------


def test_verdict_indeterminate_when_no_flags():
    rankings = {"rows": _mk_main_rows()}
    # Nearly-identical second universe → no selection_bias, no data_gap, no
    # shared tickers (so formula_flag stays off).
    wl = []
    for i in range(30):
        wl.append({
            "ticker": f"X{i}",
            "low_risk": 7.4,
            "atr_pct": 0.02,
            "vol_bucket": "Low" if i < 15 else "Med",
            "swing_score": 6.0,
            "market_cap": "100B",
            "sector": "Industrials" if i % 2 == 0 else "Financial Services",
            "data_source": "main_pipeline",
        })
    report = lrd.build_report(rankings, {"rows": wl})
    v = report["verdict"]
    assert v["verdict"] == "indeterminate", v


# ---------- verdict: mixed when multiple flags fire ----------


def test_verdict_mixed_when_data_gap_plus_selection_bias():
    rankings = {"rows": _mk_main_rows()}
    # Watchlist is materially more speculative AND missing atr on most rows.
    wl = []
    for i in range(40):
        wl.append({
            "ticker": f"W{i}",
            "low_risk": 5.0,
            "atr_pct": None,        # data gap
            "vol_bucket": "High",   # high-vol → bias signal
            "swing_score": 5.0,
            "market_cap": "5B",
            "sector": "Technology", # speculative → bias signal
            "data_source": "main_pipeline",
        })
    report = lrd.build_report(rankings, {"rows": wl})
    v = report["verdict"]
    assert v["verdict"] == "mixed", v
    assert "data_gap" in v["flags"] and "selection_bias" in v["flags"], v


# ---------- group stats sanity ----------


def test_per_group_low_risk_stats_match_inputs():
    rankings = {"rows": [{"ticker": f"T{i}", "low_risk": float(i), "rank": i}
                          for i in range(11)]}  # 0..10, mean=5
    watchlist = {"rows": []}
    report = lrd.build_report(rankings, watchlist)
    main = report["groups"]["main_rankings"]["low_risk"]
    assert main["n"] == 11, main
    assert main["mean"] == 5.0, main
    assert main["median"] == 5, main
    # top10 slice — first 10 rows by rank, low_risk 0..9 → mean 4.5
    top10 = report["groups"]["main_top10"]["low_risk"]
    assert top10["n"] == 10
    assert top10["mean"] == 4.5, top10


# ---------- HTML smoke ----------


def test_render_html_smoke():
    rankings = {"rows": _mk_main_rows()}
    watchlist = {"rows": _mk_wl_main_rows()}
    report = lrd.build_report(rankings, watchlist)
    html = lrd._render_html(report)
    assert "Low-Risk Drift Review" in html
    assert "selection_bias" in html
    assert "<table" in html
    # Speculative sector evidence should appear in the verdict block.
    assert "speculative_sector_share" in html


def test_build_report_handles_missing_inputs():
    report = lrd.build_report(None, None, None)
    assert report["inputs"]["rankings_present"] is False
    assert report["inputs"]["watchlist_present"] is False
    assert report["verdict"]["verdict"] == "indeterminate", report["verdict"]


# ---------- recommendations always include normalize note ----------


def test_recommendations_always_mentions_normalize_followup():
    rankings = {"rows": _mk_main_rows()}
    watchlist = {"rows": _mk_wl_main_rows()}
    report = lrd.build_report(rankings, watchlist)
    assert any("normalize" in r.lower() for r in report["recommendations"]), report["recommendations"]


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

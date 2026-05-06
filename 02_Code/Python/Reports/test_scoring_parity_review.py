"""Fixture-based tests for scoring_parity_review.py.

Validates distribution math, verdict thresholds, group splitting, cross-group
parity, and SUPP example selection. No filesystem writes, no network.

Run: python 02_Code/Python/Reports/test_scoring_parity_review.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scoring_parity_review as spr  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# ---------- distribution() ----------


def test_distribution_empty():
    d = spr.distribution([])
    assert d == {"n": 0, "null_count": 0, "mean": None, "median": None, "min": None, "max": None}, d


def test_distribution_basic_stats():
    d = spr.distribution([1, 2, 3, 4, 5])
    assert d["n"] == 5
    assert d["null_count"] == 0
    assert d["mean"] == 3.0
    assert d["median"] == 3
    assert d["min"] == 1
    assert d["max"] == 5


def test_distribution_even_count_median():
    d = spr.distribution([1, 2, 3, 4])
    assert d["median"] == 2.5, d


def test_distribution_ignores_non_numeric_and_nan():
    nan = float("nan")
    d = spr.distribution([1.0, None, "x", nan, 3.0])
    assert d["n"] == 2, d
    assert d["null_count"] == 3, d
    assert d["mean"] == 2.0
    assert d["min"] == 1.0
    assert d["max"] == 3.0


def test_distribution_all_nulls():
    d = spr.distribution([None, None, "x"])
    assert d["n"] == 0
    assert d["null_count"] == 3
    assert d["mean"] is None


# ---------- verdict_for_component() ----------


def test_verdict_main_full_coverage_ok():
    v = spr.verdict_for_component("main_rankings", "ai_score", present=100, total=100)
    assert v["status"] == "OK", v


def test_verdict_main_warn_threshold():
    v = spr.verdict_for_component("main_rankings", "ai_score", present=90, total=100)
    assert v["status"] == "WARN", v  # 10% null


def test_verdict_main_fail_threshold():
    v = spr.verdict_for_component("main_rankings", "ai_score", present=70, total=100)
    assert v["status"] == "FAIL", v  # 30% null


def test_verdict_supp_partial_by_design_caps_at_warn():
    # 100% null fundamental on SUPP → WARN, not FAIL.
    v = spr.verdict_for_component("watchlist_supp", "fundamental", present=0, total=50)
    assert v["status"] == "WARN", v
    assert "partial-by-design" in (v.get("rationale") or v.get("message") or "")


def test_verdict_supp_ai_score_uses_full_thresholds():
    v = spr.verdict_for_component("watchlist_supp", "ai_score", present=10, total=50)
    assert v["status"] == "FAIL", v  # 80% null on a required field


def test_verdict_zero_total_is_ok():
    v = spr.verdict_for_component("main_rankings", "ai_score", present=0, total=0)
    assert v["status"] == "OK", v


# ---------- split_groups() ----------


def test_split_groups_separates_supp_and_main():
    rankings = {"rows": [{"ticker": "AAA"}, {"ticker": "BBB"}]}
    watchlist = {"rows": [
        {"ticker": "X", "data_source": "main_pipeline"},
        {"ticker": "Y", "data_source": "supplemental_yfinance"},
        {"ticker": "Z", "data_source": "supplemental_csv"},
        {"ticker": "Q", "data_source": None},
    ]}
    g = spr.split_groups(rankings, watchlist)
    assert len(g["main_rankings"]) == 2
    assert len(g["watchlist_main_pipeline"]) == 1
    assert len(g["watchlist_supp"]) == 2  # both supplemental_* variants
    assert g["watchlist_main_pipeline"][0]["ticker"] == "X"


# ---------- coverage() ----------


def test_coverage_score_present_and_distributions():
    rows = [
        {"ai_score": 5.0, "fundamental": 4.0, "technical": 6.0, "sentiment": None,
         "low_risk": 5.5, "swing_score": 6.0, "data_source": "main_pipeline"},
        {"ai_score": 7.0, "fundamental": None, "technical": 8.0, "sentiment": None,
         "low_risk": 6.0, "swing_score": 5.0, "data_source": "main_pipeline",
         "ai_score_basis": "supp_technical_only"},
    ]
    cov = spr.coverage(rows)
    assert cov["row_count"] == 2
    assert cov["score_present"]["ai_score"] == 2
    assert cov["score_present"]["fundamental"] == 1
    assert cov["score_null"]["sentiment"] == 2
    assert cov["distributions"]["ai_score"]["mean"] == 6.0
    # Provenance picked up
    prov = cov["provenance"]["ai_score_basis"]
    assert prov["values"].get("supp_technical_only") == 1


# ---------- cross_group_parity() ----------


def test_cross_group_parity_ok_when_means_match():
    main_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    cross = spr.cross_group_parity({
        "main_rankings": main_cov,
        "watchlist_main_pipeline": wlm_cov,
    })
    assert cross["status"] == "OK", cross


def test_cross_group_parity_warn_at_moderate_drift():
    main_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov["distributions"]["ai_score"] = {"mean": 5.9}  # +0.9 delta
    cross = spr.cross_group_parity({
        "main_rankings": main_cov,
        "watchlist_main_pipeline": wlm_cov,
    })
    assert cross["status"] == "WARN", cross
    assert cross["by_field"]["ai_score"]["status"] == "WARN"


def test_cross_group_parity_fail_at_large_drift():
    main_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov["distributions"]["ai_score"] = {"mean": 7.0}  # +2.0 delta
    cross = spr.cross_group_parity({
        "main_rankings": main_cov,
        "watchlist_main_pipeline": wlm_cov,
    })
    assert cross["status"] == "FAIL", cross


# ---------- supp_examples() ----------


def test_supp_examples_picks_technical_only_and_missing_fund_sorted_by_ai():
    rows = [
        # Eligible: technical-only basis
        {"ticker": "T1", "ai_score": 9.5, "ai_score_basis": "supp_technical_only",
         "fundamental": None, "technical": 9.5},
        # Eligible: no fundamental even without explicit basis
        {"ticker": "T2", "ai_score": 8.0, "ai_score_basis": None,
         "fundamental": None, "technical": 8.0},
        # Not eligible: full data
        {"ticker": "T3", "ai_score": 7.0, "ai_score_basis": None,
         "fundamental": 7.0, "technical": 7.0},
        # Not eligible: ai_score missing
        {"ticker": "T4", "ai_score": None, "ai_score_basis": "supp_technical_only",
         "fundamental": None, "technical": 5.0},
    ]
    out = spr.supp_examples(rows, limit=10)
    tickers = [r["ticker"] for r in out]
    assert tickers == ["T1", "T2"], tickers


def test_supp_examples_respects_limit():
    rows = [
        {"ticker": f"T{i}", "ai_score": 9.0 - i * 0.1,
         "ai_score_basis": "supp_technical_only", "fundamental": None}
        for i in range(20)
    ]
    out = spr.supp_examples(rows, limit=5)
    assert len(out) == 5
    # Highest ai_score first
    assert out[0]["ticker"] == "T0"


# ---------- recommendations() ----------


def test_recommendations_warn_when_supp_fundamentals_majority_missing():
    group_cov = {
        "watchlist_supp": {
            "row_count": 50,
            "score_null": {"fundamental": 50},
            "provenance": {"eodhd_fundamentals": {"values": {"bool:False": 50}}},
        }
    }
    verdicts = {"main_rankings": {"status": "OK"},
                "watchlist_main_pipeline": {"status": "OK"}}
    cross = {"status": "OK", "by_field": {}}
    recs = spr.recommendations(group_cov, verdicts, cross)
    joined = " ".join(recs)
    assert "Do NOT blend" in joined, recs
    assert "EODHD" in joined, recs


def test_recommendations_clean_path_when_all_ok():
    group_cov = {
        "watchlist_supp": {
            "row_count": 50,
            "score_null": {"fundamental": 5},
            "provenance": {"eodhd_fundamentals": {"values": {"bool:True": 45, "bool:False": 5}}},
        }
    }
    verdicts = {"main_rankings": {"status": "OK"},
                "watchlist_main_pipeline": {"status": "OK"}}
    cross = {"status": "OK", "by_field": {}}
    recs = spr.recommendations(group_cov, verdicts, cross)
    assert any("All checks OK" in r for r in recs), recs


# ---------- build_report() / overall rollup ----------


def test_build_report_overall_fail_when_rankings_missing():
    report = spr.build_report(None, {"rows": []})
    assert report["overall"] == "FAIL", report["overall"]
    assert report["inputs"]["rankings_present"] is False


def test_build_report_overall_warn_when_watchlist_missing():
    rankings = {"rows": [
        {"ai_score": 5, "fundamental": 5, "technical": 5, "sentiment": 5,
         "low_risk": 5, "swing_score": 5} for _ in range(20)
    ]}
    report = spr.build_report(rankings, None)
    # Overall must not be OK because watchlist is missing
    assert report["overall"] in ("WARN", "FAIL"), report["overall"]


def test_build_report_with_realistic_split():
    rankings = {"as_of": "2026-05-04 12:00 PM CDT", "rows": [
        {"ticker": f"T{i}", "ai_score": 6.0, "fundamental": 6.0, "technical": 6.0,
         "sentiment": 6.0, "low_risk": 6.0, "swing_score": 6.0}
        for i in range(50)
    ]}
    watchlist = {"as_of": "2026-05-04 12:00 PM CDT", "rows": [
        # main_pipeline rows (full coverage)
        *({"ticker": f"M{i}", "ai_score": 6.1, "fundamental": 6.1, "technical": 6.1,
           "sentiment": 6.1, "low_risk": 6.1, "swing_score": 6.1,
           "data_source": "main_pipeline"} for i in range(20)),
        # SUPP rows (technical-only by design)
        *({"ticker": f"S{i}", "ai_score": 8.0,
           "ai_score_basis": "supp_technical_only",
           "technical": 8.0, "fundamental": None, "sentiment": None,
           "low_risk": None, "swing_score": None,
           "data_source": "supplemental_yfinance",
           "eodhd_fundamentals": False, "eodhd_deferred": False}
          for i in range(15)),
    ]}
    report = spr.build_report(rankings, watchlist)
    assert report["groups"]["main_rankings"]["row_count"] == 50
    assert report["groups"]["watchlist_main_pipeline"]["row_count"] == 20
    assert report["groups"]["watchlist_supp"]["row_count"] == 15
    # SUPP has all fundamentals null but should not push overall to FAIL.
    supp_v = report["verdicts"]["watchlist_supp"]
    assert supp_v["components"]["fundamental"]["status"] == "WARN"
    assert supp_v["components"]["ai_score"]["status"] == "OK"
    # SUPP examples should populate
    assert len(report["supp_examples"]) > 0
    # Cross-group parity should be OK (means within 0.1 of each other)
    assert report["cross_group_parity"]["status"] == "OK"


# ---------- low_risk selection-bias demote ----------


def _drift_report(verdict="selection_bias", max_abs_delta=0.0,
                  formula_triggered=False, data_gap_triggered=False,
                  shared_count=4, differing_count=0,
                  confidence="medium") -> dict:
    """Helper: minimal low_risk_drift_review.json shape."""
    return {
        "verdict": {
            "verdict": verdict,
            "confidence": confidence,
            "formula_flag": {"triggered": formula_triggered, "severity": "OK"},
            "data_gap_flag": {"triggered": data_gap_triggered},
            "selection_bias_flag": {"triggered": verdict == "selection_bias"},
        },
        "overlap_main_vs_watchlist": {
            "shared_count": shared_count,
            "differing_count": differing_count,
            "max_abs_delta": max_abs_delta,
        },
    }


def test_low_risk_bias_known_when_drift_says_selection_bias():
    bias = spr.low_risk_bias_known(_drift_report())
    assert bias["is_known_bias"] is True, bias
    assert "selection bias" in bias["reason"].lower()


def test_low_risk_bias_not_known_without_drift_report():
    bias = spr.low_risk_bias_known(None)
    assert bias["is_known_bias"] is False, bias


def test_low_risk_bias_not_known_when_formula_flag_triggered():
    bias = spr.low_risk_bias_known(_drift_report(formula_triggered=True))
    assert bias["is_known_bias"] is False, bias


def test_low_risk_bias_not_known_when_same_ticker_diverges():
    bias = spr.low_risk_bias_known(_drift_report(max_abs_delta=0.6))
    assert bias["is_known_bias"] is False, bias


def test_low_risk_bias_not_known_for_data_gap_or_formula_verdict():
    bias = spr.low_risk_bias_known(_drift_report(verdict="data_gap"))
    assert bias["is_known_bias"] is False, bias
    bias = spr.low_risk_bias_known(_drift_report(verdict="formula_issue"))
    assert bias["is_known_bias"] is False, bias


def test_cross_group_parity_demotes_low_risk_fail_when_known_bias():
    # Big low_risk drift would normally FAIL (>=1.5), but known_bias drops
    # the row to WARN and tags the entry.
    main_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov["distributions"]["low_risk"] = {"mean": 2.5}  # -2.5 delta
    cross = spr.cross_group_parity({
        "main_rankings": main_cov,
        "watchlist_main_pipeline": wlm_cov,
    }, drift=_drift_report())
    lr = cross["by_field"]["low_risk"]
    assert lr["status"] == "WARN", lr
    assert lr.get("known_bias") is True, lr
    assert lr.get("raw_status") == "FAIL", lr
    # No other field should have FAIL, so the rollup is WARN, not FAIL.
    assert cross["status"] == "WARN", cross
    assert cross["low_risk_bias"]["is_known_bias"] is True


def test_cross_group_parity_keeps_fail_when_other_field_diverges():
    # ai_score diverges by 2.0 (FAIL) — this is a real parity blocker and
    # must remain FAIL even if low_risk is also drifting and bias is known.
    main_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov["distributions"]["low_risk"] = {"mean": 2.5}
    wlm_cov["distributions"]["ai_score"] = {"mean": 7.0}
    cross = spr.cross_group_parity({
        "main_rankings": main_cov,
        "watchlist_main_pipeline": wlm_cov,
    }, drift=_drift_report())
    assert cross["status"] == "FAIL", cross
    assert cross["by_field"]["ai_score"]["status"] == "FAIL"
    # low_risk still demoted individually
    assert cross["by_field"]["low_risk"]["status"] == "WARN"


def test_cross_group_parity_keeps_low_risk_fail_when_formula_diverges():
    # Same-ticker formula divergence => bias not known => low_risk stays FAIL.
    main_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov = {"distributions": {f: {"mean": 5.0} for f in spr.SCORE_FIELDS}}
    wlm_cov["distributions"]["low_risk"] = {"mean": 2.5}
    cross = spr.cross_group_parity({
        "main_rankings": main_cov,
        "watchlist_main_pipeline": wlm_cov,
    }, drift=_drift_report(formula_triggered=True, max_abs_delta=1.2))
    lr = cross["by_field"]["low_risk"]
    assert lr["status"] == "FAIL", lr
    assert not lr.get("known_bias"), lr
    assert cross["status"] == "FAIL", cross


def test_build_report_low_risk_bias_known_only_warn():
    """Low-risk-only parity FAIL with selection_bias drift -> overall WARN."""
    rankings = {"as_of": "2026-05-04 12:00 PM CDT", "rows": [
        {"ticker": f"T{i}", "ai_score": 6.0, "fundamental": 6.0,
         "technical": 6.0, "sentiment": 6.0, "low_risk": 7.5,
         "swing_score": 6.0}
        for i in range(50)
    ]}
    watchlist = {"as_of": "2026-05-04 12:00 PM CDT", "rows": [
        {"ticker": f"M{i}", "ai_score": 6.0, "fundamental": 6.0,
         "technical": 6.0, "sentiment": 6.0,
         "low_risk": 5.0,  # -2.5 delta vs main; would FAIL without bias
         "swing_score": 6.0,
         "data_source": "main_pipeline"}
        for i in range(20)
    ]}
    report = spr.build_report(rankings, watchlist, drift=_drift_report())
    assert report["low_risk_bias_known"] is True
    cgp = report["cross_group_parity"]
    assert cgp["status"] == "WARN", cgp
    assert cgp["by_field"]["low_risk"]["known_bias"] is True
    # Overall is at most WARN when only low_risk drifted and bias is known.
    assert report["overall"] in ("OK", "WARN"), report["overall"]


def test_build_report_real_formula_issue_still_fail():
    """Same-ticker per-ticker divergence (drift verdict NOT selection_bias)
    must keep low_risk FAIL and overall FAIL."""
    rankings = {"as_of": "2026-05-04 12:00 PM CDT", "rows": [
        {"ticker": f"T{i}", "ai_score": 6.0, "fundamental": 6.0,
         "technical": 6.0, "sentiment": 6.0, "low_risk": 7.5,
         "swing_score": 6.0}
        for i in range(50)
    ]}
    watchlist = {"as_of": "2026-05-04 12:00 PM CDT", "rows": [
        {"ticker": f"M{i}", "ai_score": 6.0, "fundamental": 6.0,
         "technical": 6.0, "sentiment": 6.0, "low_risk": 5.0,
         "swing_score": 6.0, "data_source": "main_pipeline"}
        for i in range(20)
    ]}
    drift = _drift_report(verdict="formula_issue", formula_triggered=True,
                          max_abs_delta=1.5)
    report = spr.build_report(rankings, watchlist, drift=drift)
    assert report["low_risk_bias_known"] is False
    assert report["cross_group_parity"]["status"] == "FAIL"
    assert report["overall"] == "FAIL"


def test_render_html_smoke():
    report = {
        "generated_at": "2026-05-04T17:00:00Z",
        "overall": "WARN",
        "inputs": {"rankings_present": True, "watchlist_present": True,
                   "rankings_as_of": "2026-05-04 12:00 PM CDT",
                   "watchlist_as_of": "2026-05-04 12:00 PM CDT"},
        "groups": {
            "main_rankings": {
                "row_count": 1,
                "score_present": {f: 1 for f in spr.SCORE_FIELDS},
                "score_null": {f: 0 for f in spr.SCORE_FIELDS},
                "score_present_pct": {f: 1.0 for f in spr.SCORE_FIELDS},
                "distributions": {f: {"n": 1, "null_count": 0, "mean": 5,
                                       "median": 5, "min": 5, "max": 5}
                                   for f in spr.SCORE_FIELDS},
                "provenance": {"data_source": {"values": {"main_pipeline": 1},
                                                "field_absent_rows": 0}},
            },
            "watchlist_main_pipeline": {
                "row_count": 0, "score_present": {f: 0 for f in spr.SCORE_FIELDS},
                "score_null": {f: 0 for f in spr.SCORE_FIELDS},
                "score_present_pct": {f: None for f in spr.SCORE_FIELDS},
                "distributions": {f: {"n": 0, "null_count": 0, "mean": None,
                                       "median": None, "min": None, "max": None}
                                   for f in spr.SCORE_FIELDS},
                "provenance": {},
            },
            "watchlist_supp": {
                "row_count": 0, "score_present": {f: 0 for f in spr.SCORE_FIELDS},
                "score_null": {f: 0 for f in spr.SCORE_FIELDS},
                "score_present_pct": {f: None for f in spr.SCORE_FIELDS},
                "distributions": {f: {"n": 0, "null_count": 0, "mean": None,
                                       "median": None, "min": None, "max": None}
                                   for f in spr.SCORE_FIELDS},
                "provenance": {},
            },
        },
        "verdicts": {
            g: {"status": "OK",
                "components": {f: {"status": "OK", "message": "ok"}
                               for f in spr.SCORE_FIELDS}}
            for g in ("main_rankings", "watchlist_main_pipeline", "watchlist_supp")
        },
        "cross_group_parity": {"status": "OK", "by_field": {}},
        "supp_examples": [],
        "recommendations": ["All checks OK"],
    }
    html = spr._render_html(report)
    assert "Scoring Parity Review" in html
    assert "watchlist_supp" in html
    assert "WARN" in html


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

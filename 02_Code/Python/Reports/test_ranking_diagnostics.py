"""Fixture-based tests for ranking_diagnostics.py.

Validates driver detection, weak-spot detection, suspicious-rank flags,
sector crowding, alternate weighting sensitivity (including missing-data
handling), benchmark context shape, and overall verdict logic. No
filesystem writes, no network.

Run: python 02_Code/Python/Reports/test_ranking_diagnostics.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ranking_diagnostics as rd  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# ---------- compute_mov_pct ----------


def test_mov_pct_basic_positive():
    assert rd.compute_mov_pct([100.0, 110.0]) == 0.1


def test_mov_pct_basic_negative():
    v = rd.compute_mov_pct([100.0, 90.0])
    assert v == -0.1, v


def test_mov_pct_handles_short_or_invalid():
    assert rd.compute_mov_pct(None) is None
    assert rd.compute_mov_pct([]) is None
    assert rd.compute_mov_pct([100.0]) is None
    assert rd.compute_mov_pct([0.0, 5.0]) is None  # zero divisor
    assert rd.compute_mov_pct([None, 10.0]) is None


# ---------- detect_drivers ----------


def test_detect_drivers_picks_high_components_in_score_order():
    row = {"fundamental": 8.5, "technical": 9.2, "sentiment": 7.0,
           "low_risk": 8.1, "swing_score": 6.0}
    d = rd.detect_drivers(row)
    # technical (9.2) > fundamental (8.5) > low_risk (8.1); SENT/SWING below 8.
    assert d == ["TECH", "FUND", "LOW_RISK"], d


def test_detect_drivers_empty_when_no_high_components():
    row = {"fundamental": 5.0, "technical": 7.5, "sentiment": 6.0}
    assert rd.detect_drivers(row) == []


def test_detect_drivers_ignores_missing():
    row = {"fundamental": None, "technical": 9.0,
           "sentiment": None, "low_risk": None, "swing_score": None}
    assert rd.detect_drivers(row) == ["TECH"]


# ---------- detect_weak_spots ----------


def test_weak_spots_flags_missing_and_low():
    row = {"fundamental": None, "technical": 9.0, "sentiment": 4.0,
           "low_risk": 8.0, "swing_score": 5.0}
    spots = rd.detect_weak_spots(row, mov_pct=0.02)
    joined = "; ".join(spots)
    assert "missing FUND" in joined, joined
    assert "low SENT" in joined, joined
    assert "low SWING" in joined, joined


def test_weak_spots_flags_negative_mov():
    row = {"fundamental": 9.0, "technical": 9.0, "sentiment": 9.0,
           "low_risk": 9.0, "swing_score": 9.0}
    spots = rd.detect_weak_spots(row, mov_pct=-0.05)
    assert any("negative MOV" in s for s in spots), spots


def test_weak_spots_flags_supp_technical_only_basis():
    row = {"fundamental": 9.0, "technical": 9.0, "sentiment": 9.0,
           "low_risk": 9.0, "swing_score": 9.0,
           "ai_score_basis": "supp_technical_only"}
    spots = rd.detect_weak_spots(row, mov_pct=0.05)
    assert any("technical-only" in s for s in spots), spots


def test_weak_spots_clean_when_strong():
    row = {"fundamental": 9.0, "technical": 9.0, "sentiment": 9.0,
           "low_risk": 9.0, "swing_score": 9.0}
    assert rd.detect_weak_spots(row, mov_pct=0.05) == []


# ---------- explain_top + sector_crowding ----------


def test_explain_top_returns_explanations_in_order():
    rows = [
        {"rank": 1, "ticker": "AAA", "company": "Alpha", "sector": "Tech",
         "ai_score": 9.0, "fundamental": 9.0, "technical": 9.0,
         "sentiment": 9.0, "low_risk": 9.0, "swing_score": 9.0,
         "closes": [10.0, 11.0]},
        {"rank": 2, "ticker": "BBB", "company": "Beta", "sector": "Health",
         "ai_score": 8.0, "fundamental": 7.0, "technical": 8.0,
         "sentiment": 7.0, "low_risk": 7.0, "swing_score": 6.0,
         "closes": [10.0, 9.5]},
    ]
    out = rd.explain_top(rows, 5)
    assert len(out) == 2
    assert out[0]["ticker"] == "AAA"
    assert out[0]["mov_pct"] == 0.1
    assert "TECH" in out[0]["primary_drivers"]


def test_sector_crowding_warn_threshold():
    rows = [{"sector": "Technology"}] * 4 + [{"sector": "Health"}] * 3 + [{"sector": "Energy"}] * 3
    info = rd.sector_crowding(rows)
    assert info["status"] == "WARN", info  # 4/10 = 40%, between 30% and 50%
    assert info["top_sector"] == "Technology"


def test_sector_crowding_fail_threshold():
    rows = [{"sector": "Technology"}] * 6 + [{"sector": "Health"}] * 4
    info = rd.sector_crowding(rows)
    assert info["status"] == "FAIL", info
    assert info["top_share"] == 0.6


def test_sector_crowding_ok_when_diverse():
    rows = [{"sector": s} for s in
            ["Tech", "Health", "Energy", "Real Estate",
             "Financial Services", "Industrials",
             "Consumer Cyclical", "Utilities", "Tech", "Health"]]
    info = rd.sector_crowding(rows)
    assert info["status"] == "OK", info  # max share 2/10 = 20%


# ---------- detect_suspicious ----------


def test_suspicious_flags_missing_components():
    explained = [{
        "rank": 1, "ticker": "X", "company": "X Co",
        "ai_score": 9.0, "fundamental": None, "technical": 9.0,
        "sentiment": 9.0, "low_risk": 9.0, "swing_score": 9.0,
        "mov_pct": 0.05,
    }]
    s = rd.detect_suspicious(explained, group_label="main_top10")
    assert len(s) == 1, s
    assert any("missing FUND" in r for r in s[0]["reasons"]), s


def test_suspicious_flags_negative_mov():
    explained = [{
        "rank": 1, "ticker": "X", "company": "X Co",
        "ai_score": 9.0, "fundamental": 9.0, "technical": 9.0,
        "sentiment": 9.0, "low_risk": 9.0, "swing_score": 9.0,
        "mov_pct": -0.04,
    }]
    s = rd.detect_suspicious(explained, group_label="main_top10")
    assert s and any("negative MOV" in r for r in s[0]["reasons"]), s


def test_suspicious_flags_technical_only_basis():
    explained = [{
        "rank": 1, "ticker": "X", "company": "X Co",
        "ai_score": 9.0, "fundamental": 9.0, "technical": 9.0,
        "sentiment": 9.0, "low_risk": 9.0, "swing_score": 9.0,
        "mov_pct": 0.05, "ai_score_basis": "supp_technical_only",
    }]
    s = rd.detect_suspicious(explained, group_label="watchlist_top10")
    assert s and any("technical-only" in r for r in s[0]["reasons"]), s


def test_suspicious_clean_top_returns_empty():
    explained = [{
        "rank": 1, "ticker": "X", "company": "X Co",
        "ai_score": 9.0, "fundamental": 9.0, "technical": 9.0,
        "sentiment": 9.0, "low_risk": 9.0, "swing_score": 9.0,
        "mov_pct": 0.03,
    }]
    assert rd.detect_suspicious(explained, group_label="main_top10") == []


# ---------- alt_weight_score ----------


def test_alt_weight_score_balanced_baseline():
    row = {"fundamental": 8.0, "technical": 8.0, "sentiment": 8.0,
           "low_risk": 8.0, "swing_score": 5.0}
    v = rd.alt_weight_score(row, rd.WEIGHT_SCHEMES["balanced_baseline"])
    assert v == 8.0, v  # swing weight is 0 in balanced_baseline


def test_alt_weight_score_returns_none_when_required_field_missing():
    row = {"fundamental": None, "technical": 9.0, "sentiment": 9.0,
           "low_risk": 9.0, "swing_score": 9.0}
    v = rd.alt_weight_score(row, rd.WEIGHT_SCHEMES["balanced_baseline"])
    assert v is None, v


def test_alt_weight_score_momentum_tilt_emphasizes_tech():
    row_tech = {"fundamental": 5.0, "technical": 9.0, "sentiment": 5.0,
                "low_risk": 5.0, "swing_score": 9.0}
    row_fund = {"fundamental": 9.0, "technical": 5.0, "sentiment": 5.0,
                "low_risk": 9.0, "swing_score": 5.0}
    v_tech = rd.alt_weight_score(row_tech, rd.WEIGHT_SCHEMES["momentum_tilt"])
    v_fund = rd.alt_weight_score(row_fund, rd.WEIGHT_SCHEMES["momentum_tilt"])
    assert v_tech > v_fund, (v_tech, v_fund)


def test_alt_weight_score_quality_tilt_emphasizes_fundamentals():
    row_tech = {"fundamental": 5.0, "technical": 9.0, "sentiment": 5.0,
                "low_risk": 5.0, "swing_score": 9.0}
    row_fund = {"fundamental": 9.0, "technical": 5.0, "sentiment": 5.0,
                "low_risk": 9.0, "swing_score": 5.0}
    v_tech = rd.alt_weight_score(row_tech, rd.WEIGHT_SCHEMES["quality_tilt"])
    v_fund = rd.alt_weight_score(row_fund, rd.WEIGHT_SCHEMES["quality_tilt"])
    assert v_fund > v_tech, (v_tech, v_fund)


# ---------- alt_sensitivity ----------


def _mk(rank, ticker, fund, tech, sent, low_risk, swing, ai=None):
    return {
        "rank": rank, "ticker": ticker, "company": f"{ticker} Co",
        "fundamental": fund, "technical": tech, "sentiment": sent,
        "low_risk": low_risk, "swing_score": swing,
        "ai_score": ai if ai is not None else (fund + tech + sent + low_risk) / 4.0,
        "sector": "Technology", "closes": [10.0, 10.5],
    }


def test_alt_sensitivity_picks_up_movers():
    # Build a tiny universe where rank-1 is fundamentals-heavy and the
    # momentum_tilt scheme should clearly knock it out.
    rows = [
        _mk(1, "QUAL", fund=9.5, tech=4.0, sent=5.0, low_risk=9.0, swing=2.0),
        _mk(2, "MID",  fund=7.0, tech=7.0, sent=7.0, low_risk=7.0, swing=7.0),
        _mk(3, "MOMO", fund=5.0, tech=9.5, sent=8.0, low_risk=5.0, swing=9.5),
    ]
    out = rd.alt_sensitivity(rows, top_n=2, weights_by_scheme=rd.WEIGHT_SCHEMES)
    momo = out["momentum_tilt"]
    # In current top-2: QUAL(rank1) and MID(rank2). Under momentum_tilt
    # MOMO outscores both, so QUAL or MID will fall and MOMO becomes
    # a new entrant. Some rows will appear as exits.
    new_tickers = {x["ticker"] for x in momo["new_entrants_top_n"]}
    assert "MOMO" in new_tickers, momo
    # The fundamentals tilt should keep QUAL on top (it has the strongest
    # fundamentals + low_risk).
    qual = out["quality_tilt"]
    qual_alt_rank = next(
        (d["alt_rank"] for d in qual["risers"] + qual["fallers"] if d["ticker"] == "QUAL"),
        None,
    )
    # QUAL's alt_rank under quality_tilt should still be 1 (no movement
    # = delta 0, which is fine — confirm alt_rank itself).
    if qual_alt_rank is None:
        # If no risers/fallers list QUAL, it means delta=0, also OK.
        pass
    else:
        assert qual_alt_rank == 1, qual_alt_rank


def test_alt_sensitivity_excludes_rows_with_missing_required_field():
    rows = [
        _mk(1, "FULL", fund=9.0, tech=9.0, sent=9.0, low_risk=9.0, swing=9.0),
        # Missing technical: should be excluded under any non-zero-tech scheme.
        {"rank": 2, "ticker": "PART", "company": "Part Co",
         "fundamental": 9.0, "technical": None, "sentiment": 9.0,
         "low_risk": 9.0, "swing_score": 9.0, "ai_score": 9.0,
         "sector": "Technology", "closes": [10.0, 10.5]},
    ]
    out = rd.alt_sensitivity(rows, top_n=5, weights_by_scheme=rd.WEIGHT_SCHEMES)
    for scheme, info in out.items():
        # Every scheme has a non-zero technical weight, so PART must be
        # excluded from rows_with_alt_score.
        assert info["rows_with_alt_score"] == 1, (scheme, info)


# ---------- benchmark_context ----------


def test_benchmark_context_handles_missing():
    ctx = rd.benchmark_context(None)
    assert ctx == {"available": False,
                   "note": "benchmark_review.json not found; skipping forward context."}


def test_benchmark_context_extracts_horizons():
    bench = {
        "as_of_rankings": "2026-05-06",
        "snapshot_summary": {
            "snapshots_total": 4,
            "horizons": {
                "1d": {
                    "completed": 4,
                    "buckets": {
                        "main_top10": {"snapshots": 4, "wins": 1, "losses": 2,
                                        "avg_mean_return": 0.0123},
                    },
                },
            },
        },
    }
    ctx = rd.benchmark_context(bench)
    assert ctx["available"] is True
    assert ctx["snapshots_total"] == 4
    assert ctx["horizons"]["1d"]["buckets"]["main_top10"]["avg_mean_return"] == 0.0123
    assert "Forward-performance" in ctx.get("caveat", "")


# ---------- build_report end-to-end (verdicts) ----------


def _make_clean_main_rows(n: int = 12) -> list:
    rows = []
    sectors = ["Tech", "Health", "Energy", "Real Estate",
               "Financial Services", "Industrials",
               "Consumer Cyclical", "Utilities"]
    for i in range(n):
        rows.append({
            "rank": i + 1, "ticker": f"T{i:02d}", "company": f"T{i:02d} Co",
            "sector": sectors[i % len(sectors)],
            "ai_score": 8.5, "fundamental": 8.5, "technical": 8.5,
            "sentiment": 8.5, "low_risk": 8.5, "swing_score": 7.5,
            "closes": [10.0, 10.3],
        })
    return rows


def test_build_report_overall_ok_for_clean_inputs():
    rankings = {"as_of": "now", "rows": _make_clean_main_rows()}
    watchlist = {"as_of": "now", "rows": _make_clean_main_rows()}
    rep = rd.build_report(rankings, watchlist)
    assert rep["overall"] == "OK", rep["overall"]
    assert rep["suspicious_ranks"] == []
    assert rep["sector_crowding"]["main_top10"]["status"] == "OK"


def test_build_report_warns_on_suspicious_top_row():
    rows = _make_clean_main_rows()
    rows[0]["sentiment"] = 3.0  # weak SENT in the #1 row
    rep = rd.build_report({"as_of": "now", "rows": rows}, {"as_of": "now", "rows": []})
    assert rep["overall"] == "WARN", rep["overall"]
    assert rep["suspicious_ranks"], rep


def test_build_report_warns_on_sector_crowding():
    rows = _make_clean_main_rows()
    for i in range(4):
        rows[i]["sector"] = "Technology"  # 4/12 in top-10 cohort -> 40%
    rep = rd.build_report({"as_of": "now", "rows": rows}, {"as_of": "now", "rows": []})
    assert rep["sector_crowding"]["main_top10"]["status"] in ("WARN", "FAIL")
    assert rep["overall"] in ("WARN", "FAIL")


def test_build_report_fails_when_top_dominated_by_missing_data():
    rows = _make_clean_main_rows()
    # Drop all components from rows 0..6 so missing-data share >= 50%.
    for i in range(7):
        for f in ("fundamental", "technical", "sentiment", "low_risk", "swing_score"):
            rows[i][f] = None
    rep = rd.build_report({"as_of": "now", "rows": rows}, {"as_of": "now", "rows": []})
    assert rep["overall"] == "FAIL", rep["overall"]


def test_build_report_fails_when_no_main_rows():
    rep = rd.build_report({"as_of": "now", "rows": []}, {"as_of": "now", "rows": []})
    assert rep["overall"] == "FAIL", rep["overall"]


def test_build_report_promotes_dq_fail_to_overall_fail():
    rows = _make_clean_main_rows()
    rep = rd.build_report(
        {"as_of": "now", "rows": rows}, {"as_of": "now", "rows": []},
        dq={"overall": "FAIL"},
    )
    assert rep["overall"] == "FAIL", rep["overall"]


def test_build_report_html_renders_without_error():
    rows = _make_clean_main_rows()
    rep = rd.build_report({"as_of": "now", "rows": rows}, {"as_of": "now", "rows": rows})
    html = rd._render_html(rep)
    assert "Ranking Diagnostics" in html
    assert "Top leaders" in html
    assert "Alternate weighting" in html
    assert "Diagnostic only" in html


# ---------- watchlist with technical-only top row ----------


def test_watchlist_supp_technical_only_top_row_flagged():
    wl_rows = [{
        "rank": 1, "ticker": "FOREIGN.X", "company": "Foreign Co",
        "sector": "Technology", "ai_score": 9.5,
        "ai_score_basis": "supp_technical_only",
        "fundamental": None, "technical": 9.5, "sentiment": None,
        "low_risk": None, "swing_score": None, "closes": [10.0, 10.7],
        "data_source": "supplemental_yfinance",
    }]
    rep = rd.build_report(
        {"as_of": "now", "rows": _make_clean_main_rows()},
        {"as_of": "now", "rows": wl_rows},
    )
    flagged = [s for s in rep["suspicious_ranks"] if s["ticker"] == "FOREIGN.X"]
    assert flagged, rep["suspicious_ranks"]
    reasons = "; ".join(flagged[0]["reasons"])
    assert "technical-only" in reasons, reasons


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

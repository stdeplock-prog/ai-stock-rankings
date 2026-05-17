"""Unit tests for activity_adjusted_review.py.

Run: python 02_Code/Python/Reports/test_activity_adjusted_review.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import activity_adjusted_review as aar  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ---------- _liquidity_bump ----------

def test_liquidity_at_pivot_is_zero():
    # 100M $ daily traded -> at the pivot, bump == 0.
    # vol_M * 1_000_000 * close = 100_000_000  ->  e.g. vol_M=1.0, close=100.0
    bump = aar._liquidity_bump(volume_millions=1.0, last_close=100.0)
    if not approx(bump, 0.0, tol=1e-9):
        fail(f"liquidity at pivot expected 0, got {bump}")


def test_liquidity_high_dollar_vol_caps_positive():
    # Way above the pivot -> +LIQUIDITY_MAX_BUMP
    bump = aar._liquidity_bump(volume_millions=100.0, last_close=500.0)  # $50B/day
    if not approx(bump, aar.LIQUIDITY_MAX_BUMP, tol=1e-9):
        fail(f"high dollar vol expected {aar.LIQUIDITY_MAX_BUMP}, got {bump}")


def test_liquidity_low_dollar_vol_caps_negative():
    bump = aar._liquidity_bump(volume_millions=0.01, last_close=5.0)  # $50k/day
    if not approx(bump, -aar.LIQUIDITY_MAX_BUMP, tol=1e-9):
        fail(f"low dollar vol expected {-aar.LIQUIDITY_MAX_BUMP}, got {bump}")


def test_liquidity_zero_inputs_safe():
    if aar._liquidity_bump(volume_millions=0, last_close=10) != 0.0:
        fail("zero volume must yield 0 bump")
    if aar._liquidity_bump(volume_millions=1, last_close=0) != 0.0:
        fail("zero close must yield 0 bump")
    if aar._liquidity_bump(volume_millions=None, last_close=None) != 0.0:
        fail("None inputs must yield 0 bump")


# ---------- _relvol_bump ----------

def test_relvol_at_or_below_one_is_zero():
    if aar._relvol_bump(1.0, 1.0) != 0.0:
        fail("rel-vol == 1 must yield 0 bump")
    if aar._relvol_bump(0.5, 1.0) != 0.0:
        fail("rel-vol < 1 must yield 0 bump")


def test_relvol_full_bonus_at_threshold():
    bump = aar._relvol_bump(aar.RELVOL_FULL_BUMP_AT, 1.0)
    if not approx(bump, aar.RELVOL_MAX_BUMP):
        fail(f"rel-vol at threshold expected {aar.RELVOL_MAX_BUMP}, got {bump}")


def test_relvol_caps_above_threshold():
    bump = aar._relvol_bump(50.0, 1.0)
    if not approx(bump, aar.RELVOL_MAX_BUMP):
        fail(f"rel-vol high expected cap {aar.RELVOL_MAX_BUMP}, got {bump}")


# ---------- _pine_bump ----------

def test_pine_none_returns_zero():
    bump, ovx, norm = aar._pine_bump(None)
    if (bump, ovx, norm) != (0.0, False, 0.0):
        fail(f"pine None expected (0,False,0), got {(bump,ovx,norm)}")


def test_pine_full_score_no_blockers():
    pine_row = {"go_no_go_score_normalized": 1.0, "blockers": []}
    bump, ovx, norm = aar._pine_bump(pine_row)
    if not approx(bump, aar.PINE_GO_MAX_BUMP):
        fail(f"pine full score expected {aar.PINE_GO_MAX_BUMP}, got {bump}")
    if ovx is not False:
        fail("expected overextended False")


def test_pine_overextended_penalty():
    pine_row = {"go_no_go_score_normalized": 0.0, "blockers": ["overextended_bb"]}
    bump, ovx, _ = aar._pine_bump(pine_row)
    if not approx(bump, aar.OVEREXTENDED_PENALTY):
        fail(f"pine overextended expected {aar.OVEREXTENDED_PENALTY}, got {bump}")
    if ovx is not True:
        fail("expected overextended True")


# ---------- compute_adjustments end-to-end ----------

def test_compute_adjustments_basic_shape_and_clamp():
    rankings = {
        "rows": [
            {"ticker": "BIG", "ai_score": 8.0, "volume_millions": 50.0,
             "closes": [100.0], "rank": 1, "sector": "Technology"},
            {"ticker": "SML", "ai_score": 8.0, "volume_millions": 0.1,
             "closes": [10.0], "rank": 2, "sector": "Industrials"},
        ],
    }
    pine_data = {
        "per_ticker": [
            {"ticker": "BIG", "go_no_go_score_normalized": 0.8, "blockers": []},
            {"ticker": "SML", "go_no_go_score_normalized": 0.0,
             "blockers": ["overextended_bb"]},
        ],
    }
    out = aar.compute_adjustments(rankings, pine_data)
    if len(out) != 2:
        fail(f"expected 2 rows got {len(out)}")
    big = next(r for r in out if r["ticker"] == "BIG")
    sml = next(r for r in out if r["ticker"] == "SML")
    if big["activity_rank"] != 1:
        fail("BIG should rank above SML after activity overlay")
    if not big["delta"] > 0:
        fail(f"BIG delta should be positive, got {big['delta']}")
    if not sml["delta"] < 0:
        fail(f"SML delta should be negative, got {sml['delta']}")
    if sml["overextended_bb"] is not True:
        fail("SML overextended flag should be True")
    # Clamp: a single row's multiplier should never exceed bounds.
    for r in out:
        m = r["activity_score"] / r["ai_score"]
        if not (aar.MIN_MULT - 1e-9 <= m <= aar.MAX_MULT + 1e-9):
            fail(f"{r['ticker']} multiplier {m} outside [{aar.MIN_MULT},{aar.MAX_MULT}]")


def test_compute_adjustments_no_pine_safe():
    rankings = {
        "rows": [
            {"ticker": "X", "ai_score": 7.0, "volume_millions": 5.0,
             "closes": [50.0], "rank": 1, "sector": "?"},
        ],
    }
    out = aar.compute_adjustments(rankings, None)
    if len(out) != 1:
        fail("expected 1 row")
    if out[0]["pine_bump"] != 0.0:
        fail("expected pine_bump=0 when no pine data")


def test_verdict_no_movers_is_ok():
    rankings = {"rows": [
        {"ticker": "A", "ai_score": 5.0, "volume_millions": 1.0,
         "closes": [100.0], "rank": 1, "sector": "?"},
    ]}
    out = aar.compute_adjustments(rankings, None)
    verdict, _ = aar._verdict(out)
    if verdict not in ("OK", "INFO"):
        fail(f"unexpected verdict {verdict}")


def test_top_n_comparison_overlap_entrants_drops():
    """Re-ranking that promotes BIG into top-2 and demotes SML out should show
    BIG as entrant, SML as drop."""
    rankings = {
        "rows": [
            {"ticker": "A1", "ai_score": 9.0, "volume_millions": 50.0,
             "closes": [100.0], "rank": 1, "sector": "Tech"},
            {"ticker": "SML", "ai_score": 8.5, "volume_millions": 0.05,
             "closes": [5.0], "rank": 2, "sector": "Other"},
            {"ticker": "BIG", "ai_score": 7.0, "volume_millions": 80.0,
             "closes": [400.0], "rank": 3, "sector": "Tech"},
            {"ticker": "C", "ai_score": 6.0, "volume_millions": 1.0,
             "closes": [50.0], "rank": 4, "sector": "Health"},
        ],
    }
    enriched = aar.compute_adjustments(rankings, None)
    comp = aar.top_n_comparison(enriched, n=2)
    if comp["n"] != 2:
        fail(f"expected n=2 got {comp['n']}")
    if len(comp["current_top"]) != 2 or len(comp["activity_top"]) != 2:
        fail("expected 2 items in each top list")
    activity_set = {r["ticker"] for r in comp["activity_top"]}
    current_set = {r["ticker"] for r in comp["current_top"]}
    # current top should be A1 + SML (by ai_rank 1, 2)
    if current_set != {"A1", "SML"}:
        fail(f"current top expected A1,SML got {current_set}")
    # entrants present only in activity_top
    entrant_tickers = {r["ticker"] for r in comp["entrants"]}
    drop_tickers = {r["ticker"] for r in comp["drops"]}
    if entrant_tickers != activity_set - current_set:
        fail(f"entrants mismatch: {entrant_tickers}")
    if drop_tickers != current_set - activity_set:
        fail(f"drops mismatch: {drop_tickers}")
    if comp["overlap_n"] != len(current_set & activity_set):
        fail("overlap count mismatch")


def test_top_n_comparison_empty_rows_safe():
    comp = aar.top_n_comparison([], n=5)
    if comp["overlap_n"] != 0:
        fail("empty -> overlap 0")
    if comp["entrants"] or comp["drops"]:
        fail("empty -> no entrants/drops")


def test_compute_adjustments_reusable_for_watchlist():
    """compute_adjustments treats its input opaquely so the same function
    re-ranks the watchlist independently of the main board. This keeps the
    watchlist activity overlay scoped to the watchlist universe rather than
    bleeding in main-board ranks."""
    watchlist = {
        "rows": [
            {"ticker": "WL1", "ai_score": 8.0, "volume_millions": 50.0,
             "closes": [200.0], "rank": 1, "sector": "Tech"},
            {"ticker": "WL2", "ai_score": 7.5, "volume_millions": 0.05,
             "closes": [5.0], "rank": 2, "sector": "Other"},
        ],
    }
    out = aar.compute_adjustments(watchlist, None)
    if len(out) != 2:
        fail(f"expected 2 watchlist rows got {len(out)}")
    ranks = sorted(r["activity_rank"] for r in out)
    if ranks != [1, 2]:
        fail(f"activity ranks should be 1..N within input universe, got {ranks}")
    # The rank_delta sign convention: positive = activity overlay promotes
    # the ticker (production rank > activity rank).
    for r in out:
        expected_delta = r["ai_rank"] - r["activity_rank"]
        if r["rank_delta"] != expected_delta:
            fail(f"rank_delta sign wrong for {r['ticker']}: "
                 f"{r['rank_delta']} vs expected {expected_delta}")


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

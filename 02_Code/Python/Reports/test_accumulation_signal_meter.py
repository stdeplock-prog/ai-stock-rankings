"""Unit tests for accumulation_signal_meter.py.

Run: python 02_Code/Python/Reports/test_accumulation_signal_meter.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import accumulation_signal_meter as asm  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def approx(a, b, tol=1e-6):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


# ---------- component math ----------

def test_relvol_below_floor_is_zero():
    if asm.relvol_component(0.5) != 0.0:
        fail("rel-vol below floor must be 0")


def test_relvol_full_at_ceiling():
    if asm.relvol_component(asm.RELVOL_FULL) != 1.0:
        fail("rel-vol at ceiling must be 1.0")


def test_relvol_mid_ramp():
    mid = (asm.RELVOL_FLOOR + asm.RELVOL_FULL) / 2
    v = asm.relvol_component(mid)
    if not approx(v, 0.5, tol=1e-6):
        fail(f"rel-vol mid ramp expected 0.5, got {v}")


def test_relvol_none_safe():
    if asm.relvol_component(None) is not None:
        fail("None input must produce None")


def test_mfi_zero_below_floor():
    if asm.mfi_component(40.0) != 0.0:
        fail("MFI below floor must be 0")


def test_mfi_full_above_ceiling():
    if asm.mfi_component(95.0) != 1.0:
        fail("MFI above ceiling must clamp at 1.0")


def test_rsi_in_zone_full_near_mid():
    mid = (asm.RSI_LOW + asm.RSI_HIGH) / 2
    v = asm.rsi_zone_component(mid)
    if v is None or v < 0.95:
        fail(f"RSI at midpoint should be near 1.0, got {v}")


def test_rsi_out_of_zone_far_is_zero():
    v = asm.rsi_zone_component(20.0)
    if v != 0.0:
        fail(f"far-out RSI should score 0, got {v}")
    v2 = asm.rsi_zone_component(90.0)
    if v2 != 0.0:
        fail(f"far-overbought RSI should score 0, got {v2}")


def test_rsi_just_below_zone_partial():
    just_below = asm.RSI_LOW - 1.0
    v = asm.rsi_zone_component(just_below)
    if v is None or not (0.0 < v <= 0.5):
        fail(f"RSI just below zone should be in (0,0.5], got {v}")


def test_close_location_at_sma20_is_zero():
    v = asm.close_location_component(100.0, 100.0, 110.0)
    if v != 0.0:
        fail(f"close at SMA20 -> 0, got {v}")


def test_close_location_at_bb_upper_is_one():
    v = asm.close_location_component(110.0, 100.0, 110.0)
    if v != 1.0:
        fail(f"close at BB upper -> 1.0, got {v}")


def test_close_location_above_bb_tapers():
    # one full band-width above upper -> 0
    v = asm.close_location_component(120.0, 100.0, 110.0)
    if not approx(v, 0.0, tol=1e-9):
        fail(f"close one band above BB upper -> 0, got {v}")
    v2 = asm.close_location_component(115.0, 100.0, 110.0)
    if v2 is None or not (0.0 < v2 < 1.0):
        fail(f"close half band above BB upper -> in (0,1), got {v2}")


def test_close_location_invalid_inputs_none():
    if asm.close_location_component(None, 100.0, 110.0) is not None:
        fail("missing close -> None")
    if asm.close_location_component(105, 110, 100) is not None:
        fail("bb_upper <= sma20 -> None")


def test_bool_component():
    if asm.bool_component(True) != 1.0:
        fail("True -> 1.0")
    if asm.bool_component(False) != 0.0:
        fail("False -> 0.0")
    if asm.bool_component(None) is not None:
        fail("None -> None")


# ---------- score_components ----------

def test_score_components_renormalizes_missing():
    """All components present at 1.0 -> score 10. One missing should not
    drop the score from 10 (renormalization)."""
    comps_full = {k: 1.0 for k in asm.COMPONENT_WEIGHTS}
    score, n, missing = asm.score_components(comps_full)
    if not approx(score, 10.0):
        fail(f"full 1.0 components -> 10.0 got {score}")
    if missing:
        fail("expected no missing")
    if n != len(asm.COMPONENT_WEIGHTS):
        fail("expected all components present")

    comps_one_missing = dict(comps_full)
    comps_one_missing["bar_strength"] = None
    score2, n2, missing2 = asm.score_components(comps_one_missing)
    if not approx(score2, 10.0):
        fail(f"missing one but rest 1.0 -> still 10.0 (renorm), got {score2}")
    if missing2 != ["bar_strength"]:
        fail(f"missing list mismatch: {missing2}")


def test_score_components_all_missing_yields_zero():
    comps = {k: None for k in asm.COMPONENT_WEIGHTS}
    score, n, missing = asm.score_components(comps)
    if score != 0.0 or n != 0:
        fail(f"all missing -> 0, got {score}, n={n}")
    if len(missing) != len(asm.COMPONENT_WEIGHTS):
        fail("all components should be in missing list")


def test_score_components_partial_score():
    comps = {k: None for k in asm.COMPONENT_WEIGHTS}
    comps["relvol"] = 0.5
    comps["mfi"] = 0.5
    score, n, _ = asm.score_components(comps)
    # weighted avg over present components -> (0.22*0.5 + 0.18*0.5)/(0.22+0.18) = 0.5
    if not approx(score, 5.0, tol=0.01):
        fail(f"partial avg 0.5 -> 5.0 got {score}")
    if n != 2:
        fail(f"expected 2 components present, got {n}")


# ---------- build_rows + verdict ----------

def test_build_rows_skips_not_evaluated():
    pine = {
        "per_ticker": [
            {"ticker": "GOOD", "evaluated": True,
             "metrics": {"rel_vol_20d": 1.5, "mfi14": 80.0,
                         "rsi14": 60.0, "last_close": 110.0,
                         "sma20": 100.0, "bb_upper_20": 110.0},
             "gates": {"bar_strength": True, "near_20d_high": True}},
            {"ticker": "OHLCV_MISSING", "evaluated": False, "metrics": {}, "gates": {}},
        ],
    }
    rows = asm.build_rows(pine, None)
    tickers = [r["ticker"] for r in rows]
    if "OHLCV_MISSING" in tickers:
        fail("evaluated=False ticker should be skipped")
    if "GOOD" not in tickers:
        fail("evaluated=True ticker should be present")
    good = next(r for r in rows if r["ticker"] == "GOOD")
    if not (good["score"] > 9.0):
        fail(f"all-bullish row should score > 9, got {good['score']}")


def test_build_rows_partial_metrics_marks_missing():
    pine = {
        "per_ticker": [
            {"ticker": "PART", "evaluated": True,
             "metrics": {"rel_vol_20d": 1.5},
             "gates": {}},
        ],
    }
    rows = asm.build_rows(pine, None)
    if len(rows) != 1:
        fail("expected 1 row")
    r = rows[0]
    if not r["missing_components"]:
        fail("expected some missing components")
    # rel_vol present -> score should be > 0
    if r["score"] <= 0:
        fail(f"present rel_vol should give positive score, got {r['score']}")


def test_verdict_empty():
    v, note = asm._verdict([])
    if v != "FAIL":
        fail(f"empty rows -> FAIL, got {v}")


def test_verdict_with_strong_row():
    v, note = asm._verdict([{"score": 7.5}])
    if v != "OK":
        fail(f"strong row -> OK, got {v}")


# ---------- overlaps ----------

def test_overlaps_pine_clean_go():
    rows = [{"ticker": "A", "score": 9}, {"ticker": "B", "score": 8},
            {"ticker": "C", "score": 1}]
    pine = {"highlights": {"cleanest_go_main": [{"ticker": "A"}, {"ticker": "Z"}]}}
    out = asm.build_overlaps(rows, pine, None)
    if "A" not in out["pine_clean_go"]["overlap_with_top_accum"]:
        fail("expected A in overlap")
    if "Z" in out["pine_clean_go"]["overlap_with_top_accum"]:
        fail("Z not in top accum")


def test_overlaps_activity_top():
    rows = [{"ticker": "A", "score": 9}, {"ticker": "B", "score": 8}]
    activity = {"rows": [{"ticker": "B"}, {"ticker": "X"}]}
    out = asm.build_overlaps(rows, None, activity)
    overlap = out["activity_top_25"]["overlap_with_top_accum"]
    if "B" not in overlap or "X" in overlap:
        fail(f"unexpected overlap: {overlap}")


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except SystemExit:
            failed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

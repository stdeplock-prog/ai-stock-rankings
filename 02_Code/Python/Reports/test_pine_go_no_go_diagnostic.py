"""Fixture-based tests for pine_go_no_go_diagnostic.py.

Covers:
  * SMA/RSI/BB indicator math against known sequences
  * gate-stack evaluation (trend aligned, RSI in zone, return 20d, rel vol)
  * blockers (overextended BB, low-volatility chop, earnings near)
  * disagreement classification + action recommendations
  * report assembly with synthetic ranking + disagreement queue
  * insufficient-data handling

Run: python 02_Code/Python/Reports/test_pine_go_no_go_diagnostic.py
"""

from __future__ import annotations

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pine_go_no_go_diagnostic as pg  # noqa: E402


# ------------------- helpers -------------------


def _trend_series(start: float, n: int, step: float) -> list[float]:
    return [round(start + step * i, 4) for i in range(n)]


def _flat_series(value: float, n: int) -> list[float]:
    return [float(value)] * n


def _ohlcv_from_closes(closes: list[float], vol: float = 1_000_000.0,
                       ) -> tuple[list[float], list[float], list[float],
                                  list[float], list[float]]:
    opens = closes[:]  # ignore intraday
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    volumes = [vol] * len(closes)
    return opens, highs, lows, closes, volumes


# ------------------- indicator math -------------------


class IndicatorMathTests(unittest.TestCase):

    def test_sma_simple(self):
        self.assertAlmostEqual(pg._sma([1, 2, 3, 4, 5], 5), 3.0)
        self.assertAlmostEqual(pg._sma([10, 20, 30], 2), 25.0)
        self.assertIsNone(pg._sma([1, 2], 5))

    def test_rsi_constant_series(self):
        # No movement -> avg_loss=0 -> RSI snaps to 100 by convention here.
        rsi = pg._rsi([100.0] * 30, n=14)
        # First 14 entries are None, then RSI values.
        self.assertIsNone(rsi[13])
        self.assertEqual(rsi[14], 100.0)

    def test_rsi_pure_uptrend_above_70(self):
        closes = _trend_series(100.0, 30, step=1.0)  # +1/day
        rsi = pg._rsi(closes, n=14)
        # Pure uptrend -> RSI well above 70 (no losses).
        self.assertEqual(rsi[14], 100.0)
        self.assertGreater(rsi[-1], 90.0)

    def test_rsi_in_pine_zone(self):
        # Mild uptrend with occasional pullbacks should land RSI in 55-70.
        closes = []
        base = 100.0
        for i in range(40):
            base += 0.4 if i % 4 != 3 else -0.5
            closes.append(round(base, 4))
        rsi = pg._rsi(closes, n=14)
        self.assertIsNotNone(rsi[-1])
        # Sanity bounds — should be within momentum zone.
        self.assertGreater(rsi[-1], 50.0)
        self.assertLess(rsi[-1], 80.0)

    def test_bb_upper_known(self):
        closes = _flat_series(100.0, 20)
        upper = pg._bb_upper(closes, n=20, k=2.0)
        self.assertAlmostEqual(upper, 100.0)
        # Variance present
        closes = [99.0, 100.0, 101.0] * 7  # length 21 — last 20 used
        upper = pg._bb_upper(closes, n=20, k=2.0)
        self.assertGreater(upper, 100.0)

    def test_mfi_uptrend(self):
        closes = _trend_series(100.0, 30, step=0.5)
        opens, highs, lows, _, volumes = _ohlcv_from_closes(closes)
        mfi = pg._mfi(highs, lows, closes, volumes, n=14)
        # Pure rising typical price -> all positive flow -> MFI=100.
        self.assertEqual(mfi[-1], 100.0)


# ------------------- gate evaluation -------------------


class GateEvalTests(unittest.TestCase):

    def test_strong_uptrend_passes_most_gates(self):
        # Steady but realistic uptrend: ~+0.5%/day for 60 bars
        closes = [100.0]
        for _ in range(59):
            closes.append(closes[-1] * 1.005)
        opens, highs, lows, _, vols = _ohlcv_from_closes(closes, vol=1_000_000)
        # Spike today's volume well above the 20-day SMA so rel_vol passes.
        vols[-1] = vols[-1] * 2.0
        res = pg.evaluate_gates(opens, highs, lows, closes, vols)
        self.assertTrue(res["gates"]["trend_sma_aligned"])
        self.assertTrue(res["gates"]["above_sma20"])
        self.assertTrue(res["gates"]["above_sma50"])
        self.assertTrue(res["gates"]["ma50_rising"])
        self.assertTrue(res["gates"]["return_20d_ok"])
        self.assertTrue(res["gates"]["rel_vol_ok"])
        self.assertTrue(res["gates"]["near_20d_high"])
        # Most gates should pass — score should be >= 0.6
        self.assertGreaterEqual(res["go_no_go_score_normalized"], 0.6)

    def test_flat_series_blocked_by_chop(self):
        closes = _flat_series(100.0, 60)
        # Tiny noise so 10-bar range stays under 2%
        closes = [c + (i % 2) * 0.01 for i, c in enumerate(closes)]
        # Tight highs/lows so 10-bar range really is <2%.
        opens = closes[:]
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        vols = [1_000_000.0] * 60
        res = pg.evaluate_gates(opens, highs, lows, closes, vols)
        # With completely flat closes, most directional gates fail.
        self.assertFalse(res["gates"]["return_20d_ok"])
        self.assertFalse(res["gates"]["trend_sma_aligned"])
        # Low-volatility chop blocker should fire
        self.assertTrue(any("low_volatility_chop" in b for b in res["blockers"]))

    def test_overextended_bb_blocker(self):
        closes = _flat_series(100.0, 25)
        # Force a huge spike on the last bar
        closes[-1] = 110.0
        opens, highs, lows, _, vols = _ohlcv_from_closes(closes)
        res = pg.evaluate_gates(opens, highs, lows, closes, vols)
        self.assertTrue(any("overextended_bb" in b for b in res["blockers"]))

    def test_earnings_blocker(self):
        closes = _trend_series(100.0, 60, step=0.3)
        opens, highs, lows, _, vols = _ohlcv_from_closes(closes)
        res = pg.evaluate_gates(opens, highs, lows, closes, vols, days_to_earnings=10)
        self.assertTrue(any("earnings_near" in b for b in res["blockers"]))

        res2 = pg.evaluate_gates(opens, highs, lows, closes, vols, days_to_earnings=60)
        self.assertFalse(any("earnings_near" in b for b in res2["blockers"]))

    def test_insufficient_data(self):
        closes = _trend_series(100.0, 10, step=0.3)
        opens, highs, lows, _, vols = _ohlcv_from_closes(closes)
        res = pg.evaluate_gates(opens, highs, lows, closes, vols)
        self.assertIsNone(res["go_no_go_score_normalized"])
        self.assertTrue(res["insufficient_data_reasons"])

    def test_return_20d_threshold(self):
        # Build series with exactly +8% over 20 days -> passes
        closes = [100.0] * 21
        closes[-1] = 108.5
        opens, highs, lows, _, vols = _ohlcv_from_closes(closes)
        res = pg.evaluate_gates(opens, highs, lows, closes, vols)
        self.assertTrue(res["gates"]["return_20d_ok"])

        closes2 = [100.0] * 21
        closes2[-1] = 105.0  # +5%
        opens2, highs2, lows2, _, vols2 = _ohlcv_from_closes(closes2)
        res2 = pg.evaluate_gates(opens2, highs2, lows2, closes2, vols2)
        self.assertFalse(res2["gates"]["return_20d_ok"])

    def test_rel_vol_threshold(self):
        closes = _trend_series(100.0, 30, step=0.2)
        opens, highs, lows, _, vols = _ohlcv_from_closes(closes, vol=1_000_000)
        # Bump today's volume to 1.5x average
        vols[-1] = 1_500_000
        res = pg.evaluate_gates(opens, highs, lows, closes, vols)
        self.assertTrue(res["gates"]["rel_vol_ok"])
        # Drop today's volume well below average
        vols[-1] = 500_000
        res2 = pg.evaluate_gates(opens, highs, lows, closes, vols)
        self.assertFalse(res2["gates"]["rel_vol_ok"])

    def test_bar_strength(self):
        closes = _trend_series(100.0, 25, step=0.2)
        opens, highs, lows, _, vols = _ohlcv_from_closes(closes)
        # Force last close strictly above prior bar high
        highs[-2] = 100.0
        closes[-1] = 110.0
        res = pg.evaluate_gates(opens, highs, lows, closes, vols)
        self.assertTrue(res["gates"]["bar_strength"])


# ------------------- disagreement classification -------------------


class DisagreementClassificationTests(unittest.TestCase):

    def test_supports_internal_when_pine_strong(self):
        gate = {
            "go_no_go_score_normalized": 0.8,
            "blockers": [],
        }
        out = pg.classify_disagreement("bullish", gate)
        self.assertEqual(out["classification"], "supports_internal")
        self.assertEqual(out["action"], "keep")

    def test_supports_external_when_pine_weak(self):
        gate = {"go_no_go_score_normalized": 0.2, "blockers": []}
        out = pg.classify_disagreement("bullish", gate)
        self.assertEqual(out["classification"], "supports_external_caution")
        self.assertEqual(out["action"], "downgrade-watchlist-only")

    def test_supports_external_when_blocked(self):
        gate = {"go_no_go_score_normalized": 0.7,
                "blockers": ["low_volatility_chop"]}
        out = pg.classify_disagreement("bullish", gate)
        self.assertEqual(out["classification"], "supports_external_caution")

    def test_mixed_when_midrange(self):
        gate = {"go_no_go_score_normalized": 0.5, "blockers": []}
        out = pg.classify_disagreement("bullish", gate)
        self.assertEqual(out["classification"], "mixed")
        self.assertEqual(out["action"], "review")

    def test_insufficient_data(self):
        out = pg.classify_disagreement("bullish", None)
        self.assertEqual(out["classification"], "insufficient_data")
        out = pg.classify_disagreement("bullish",
                                       {"go_no_go_score_normalized": None})
        self.assertEqual(out["classification"], "insufficient_data")

    def test_neutral_internal_with_strong_pine(self):
        gate = {"go_no_go_score_normalized": 0.8, "blockers": []}
        out = pg.classify_disagreement("neutral", gate)
        # Internal not bullish but Pine strongly bullish -> mixed
        self.assertEqual(out["classification"], "mixed")


# ------------------- report assembly -------------------


class ReportAssemblyTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.ohlcv_dir = os.path.join(self.tmp.name, "ohlcv")
        os.makedirs(self.ohlcv_dir, exist_ok=True)
        # Strong-uptrend ticker A
        closes_a = [100.0]
        for _ in range(79):
            closes_a.append(closes_a[-1] * 1.004)
        self._write_csv("AAA", closes_a, vol=1_500_000.0, vol_spike=2_000_000.0)
        # Downtrending ticker B with tight range -> low score + chop blocker
        closes_b = [100.0]
        for _ in range(59):
            closes_b.append(closes_b[-1] * 0.998)
        # Last 10 bars artificially flat to trigger the chop blocker
        for i in range(10):
            closes_b[-(i + 1)] = closes_b[-10]
        self._write_csv("BBB", closes_b, vol=900_000.0, tight_range=True)
        # Insufficient data ticker C
        self._write_csv("CCC", _trend_series(50.0, 8, 0.1), vol=500_000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_csv(self, ticker: str, closes: list[float], vol: float,
                   vol_spike: float | None = None,
                   tight_range: bool = False) -> None:
        path = os.path.join(self.ohlcv_dir, f"{ticker}_daily.csv")
        with open(path, "w") as f:
            f.write("Date,Open,High,Low,Close,Volume\n")
            for i, c in enumerate(closes):
                v = vol_spike if (vol_spike and i == len(closes) - 1) else vol
                if tight_range:
                    hi, lo = c + 0.05, c - 0.05
                else:
                    hi, lo = c * 1.01, c * 0.99
                f.write(f"2024-01-{i+1:02d},{c:.4f},{hi:.4f},{lo:.4f},"
                        f"{c:.4f},{int(v)}\n")

    def test_full_report_assembly(self):
        rankings = {
            "rows": [
                {"ticker": "AAA", "ai_score": 8.0, "sector": "Tech",
                 "days_to_earnings": 90, "company": "Aaa Corp"},
                {"ticker": "BBB", "ai_score": 7.0, "sector": "Energy",
                 "days_to_earnings": 60, "company": "Bbb Inc"},
                {"ticker": "CCC", "ai_score": 6.5, "sector": "Health",
                 "company": "Ccc Co"},
            ],
        }
        watchlist = {"rows": []}
        disagreement = {
            "queue": [
                {"ticker": "AAA", "internal_ai_direction": "bullish",
                 "internal_ai_score_0to10": 8.0, "sector": "Tech"},
                {"ticker": "BBB", "internal_ai_direction": "bullish",
                 "internal_ai_score_0to10": 7.0, "sector": "Energy"},
            ],
        }
        from pathlib import Path
        report = pg.build_report(rankings, watchlist, None, disagreement,
                                 ohlcv_dir=Path(self.ohlcv_dir))
        # Coverage counts
        self.assertEqual(report["counts"]["candidates"], 3)
        self.assertGreaterEqual(report["counts"]["evaluated"], 2)

        # AAA should land in supports_internal; BBB in external_caution
        s_int = {r["ticker"] for r in report["highlights"]["disagreement_supports_internal"]}
        s_ext = {r["ticker"] for r in report["highlights"]["disagreement_supports_external_caution"]}
        self.assertIn("AAA", s_int)
        self.assertIn("BBB", s_ext)

        # CCC should be flagged with insufficient bars
        ccc = [r for r in report["per_ticker"] if r["ticker"] == "CCC"][0]
        self.assertFalse(ccc["evaluated"])

        # Caveat is present
        self.assertIn("DAILY OHLCV ONLY", report["caveat"])

        # JSON-serializable
        import json
        json.dumps(report, default=str)


# ------------------- HTML rendering smoke -------------------


class HtmlRenderTests(unittest.TestCase):
    def test_render_html_runs(self):
        report = {
            "generated_at": "2026-05-07T15:00:00Z",
            "generated_at_chicago": "2026-05-07 10:00 AM CDT",
            "overall": "OK",
            "caveat": "test",
            "summary": "test summary",
            "thresholds": {"rsi_floor": 55},
            "counts": {
                "candidates": 1, "evaluated": 1, "ohlcv_missing": 0,
                "insufficient_bars": 0, "blocked": 0,
                "go_normalized_ge_07": 1, "go_normalized_lt_04": 0,
            },
            "highlights": {
                "cleanest_go_main": [],
                "weakest_main": [],
                "blocked_main": [],
                "disagreement_supports_internal": [],
                "disagreement_supports_external_caution": [],
                "disagreement_mixed": [],
                "disagreement_insufficient_data": [],
            },
        }
        html = pg._render_html(report)
        self.assertIn("Pine Go/No-Go Diagnostic", html)
        self.assertIn("Caveat:", html)


if __name__ == "__main__":
    unittest.main()

"""Tests for continuity_score.py.

Covers:
  * component formulas (rel strength, close location, volume support, MA
    distance, reversal health, earnings risk, sector strength) behave
    monotonically / sensibly
  * missing-data behaviour: absent components are omitted from the weighted
    average and lower the confidence fraction rather than fabricating 0.5
  * very-low-confidence scores collapse the label to '—'
  * 7-snapshot retention cap
  * dynamic ticker union across retained snapshots
  * badge label derivation thresholds (HIGH/MID/LOW/—)
  * missing-artifact fallback: scoring a board with no OHLCV still produces
    scores from the closes array
  * forward-return accrual in the tracking table (no lookahead)
  * HTML smoke (renders, contains DIAGNOSTIC marker and headers)
  * task stamp adds/updates the row

Run: python 02_Code/Python/Reports/test_continuity_score.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import continuity_score as cs  # noqa: E402


NO_BENCH = {"available": False, "tickers": {}}


def _row(ticker, closes, *, rank=1, sector="Tech", days_to_earnings=40,
         volume_millions=5.0, go_label=None, acc_label=None, ai_score=8.0):
    return {
        "ticker": ticker,
        "company": f"{ticker} Inc",
        "rank": rank,
        "sector": sector,
        "closes": closes,
        "days_to_earnings": days_to_earnings,
        "volume_millions": volume_millions,
        "go_label": go_label,
        "acc_label": acc_label,
        "ai_score": ai_score,
    }


# ---------------- component formula tests ----------------


def test_rel_strength_beats_benchmark_scores_high():
    up = [100, 101, 102, 103, 104, 110]  # +10% over 5 bars
    s_strong, _ = cs.comp_rel_strength(up, 5, 0.0)   # bench flat -> big excess
    s_weak, _ = cs.comp_rel_strength(up, 5, 0.10)    # bench matches -> ~0.5
    assert s_strong is not None and s_weak is not None
    assert s_strong > 0.75, s_strong
    assert 0.4 < s_weak < 0.6, s_weak


def test_rel_strength_missing_when_insufficient_bars():
    s, detail = cs.comp_rel_strength([100, 101], 5, None)
    assert s is None
    assert "insufficient" in detail


def test_rel_strength_10d_falls_back_to_max_lookback_with_min():
    # Exactly 10 closes: a true 10-day lookback is impossible (needs 11 bars),
    # but min_lookback=8 lets it use the 9-bar change instead of going missing.
    closes = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 11.0]
    s, detail = cs.comp_rel_strength(closes, 10, None, min_lookback=8)
    assert s is not None, detail


def test_close_location_prefers_true_range_high_close_scores_high():
    ohlcv = [{"date": "d", "open": 10, "high": 11, "low": 9, "close": 10.9, "volume": 100}]
    s, _ = cs.comp_close_location(ohlcv, [10.9])
    assert s is not None and s > 0.9


def test_close_location_proxy_when_no_ohlcv():
    # Close at the top of the 10-bar range -> high (proxy, shrunk toward 0.5).
    closes = [10, 10.2, 10.1, 10.4, 10.3, 10.6, 10.5, 10.7, 10.8, 11.0]
    s, detail = cs.comp_close_location(None, closes)
    assert s is not None and s > 0.7
    assert "proxy" in detail


def test_volume_support_missing_without_ohlcv():
    s, detail = cs.comp_volume_support(None, 12.5)
    assert s is None
    assert "multi-day" in detail


def test_volume_support_caps_one_day_spike():
    # 20 flat days then a single huge spike in the most recent bar.
    bars = [{"close": 10, "volume": 100} for _ in range(22)]
    bars[-1]["volume"] = 5000  # blow-off spike dominates the 3-day window
    s, detail = cs.comp_volume_support(bars, None)
    assert s is not None
    assert s <= 0.55, (s, detail)
    assert "spike" in detail


def test_ma_distance_overextension_penalised():
    # Way above the MA -> overextended -> should not be top-tier.
    series = [100] * 19 + [140]
    bars = [{"close": c, "volume": 1} for c in series]
    s_over, _ = cs.comp_ma_distance(bars, [])
    # A few % above the MA should score better than 40% above it.
    series2 = [100] * 19 + [104]
    bars2 = [{"close": c, "volume": 1} for c in series2]
    s_sweet, _ = cs.comp_ma_distance(bars2, [])
    assert s_over is not None and s_sweet is not None
    assert s_sweet > s_over, (s_sweet, s_over)


def test_reversal_health_all_up_beats_all_down():
    up = [1, 2, 3, 4, 5, 6]
    down = [6, 5, 4, 3, 2, 1]
    s_up, _ = cs.comp_reversal_health(up)
    s_down, _ = cs.comp_reversal_health(down)
    assert s_up is not None and s_down is not None
    assert s_up > s_down
    assert s_up > 0.7 and s_down < 0.3


def test_earnings_risk_imminent_low_far_high():
    s_imminent, _ = cs.comp_earnings_risk(2)
    s_far, _ = cs.comp_earnings_risk(40)
    s_missing, _ = cs.comp_earnings_risk(None)
    assert s_imminent < 0.2
    assert s_far > 0.8
    assert s_missing is None


def test_sector_strength_outperformer_scores_high():
    medians = {"Tech": 0.0}
    s, _ = cs.comp_sector_strength(0.05, "Tech", medians)
    assert s is not None and s > 0.6
    s_none, _ = cs.comp_sector_strength(None, "Tech", medians)
    assert s_none is None


# ---------------- scoring / confidence tests ----------------


def test_missing_components_lower_confidence_not_fabricated():
    # closes-only row, no OHLCV, no benchmark, no earnings, no sector cohort.
    row = _row("AAA", [10, 10.1, 10.2, 10.3, 10.4, 10.5], days_to_earnings=None)
    scored = cs.score_ticker(row, ohlcv=None, bench=NO_BENCH, sector_medians={})
    assert scored["score"] is not None
    # volume_support and earnings_risk and sector_strength must be absent.
    comps = scored["components"]
    assert comps["volume_support"]["present"] is False
    assert comps["earnings_risk"]["present"] is False
    assert comps["sector_strength"]["present"] is False
    # Confidence is strictly less than 1 because some weight is missing.
    assert 0.0 < scored["confidence"] < 1.0
    # The average must be over present weights only — never includes a guessed
    # 0.5 for the missing ones. Verify by recomputing.
    present = {k: v for k, v in comps.items() if v["present"]}
    pw = sum(v["weight"] for v in present.values())
    expected = sum(v["score"] * v["weight"] for v in present.values()) / pw * 100
    assert abs(scored["score"] - round(expected, 1)) < 0.2


def test_very_low_confidence_collapses_label_to_dash():
    # Flat closes: close_location proxy can't compute (hi==lo) and rel/ma can't
    # either, leaving only reversal_health (weight 0.16) -> confidence < 0.30.
    row = _row("BBB", [10, 10, 10, 10], days_to_earnings=None, sector=None)
    scored = cs.score_ticker(row, ohlcv=None, bench=NO_BENCH, sector_medians={})
    present = [k for k, v in scored["components"].items() if v["present"]]
    assert present == ["reversal_health"], present
    assert scored["confidence"] < 0.30
    assert scored["label"] == "—"


def test_score_board_without_ohlcv_still_scores():
    rankings = {
        "open_date": "2026-06-02",
        "rows": [
            _row("AAA", [10, 10.5, 11, 11.5, 12, 12.5], rank=1),
            _row("BBB", [20, 19, 18, 17, 16, 15], rank=2),
        ],
    }
    # ohlcv_dir points at an empty temp dir -> no CSVs -> OHLCV unavailable.
    with tempfile.TemporaryDirectory() as td:
        scored = cs.score_board(rankings, NO_BENCH, ohlcv_dir=Path(td))
    assert len(scored) == 2
    assert all(s["has_ohlcv"] is False for s in scored)
    by = {s["ticker"]: s for s in scored}
    # The uptrend should out-score the downtrend.
    assert by["AAA"]["score"] > by["BBB"]["score"]


# ---------------- label derivation tests ----------------


def test_derive_label_thresholds():
    assert cs.derive_label(90, 1.0) == "HIGH"
    assert cs.derive_label(65, 1.0) == "HIGH"
    assert cs.derive_label(64.9, 1.0) == "MID"
    assert cs.derive_label(40, 1.0) == "MID"
    assert cs.derive_label(39.9, 1.0) == "LOW"
    assert cs.derive_label(None, 1.0) == "—"
    # Low confidence overrides band.
    assert cs.derive_label(90, 0.2) == "—"


# ---------------- snapshot retention / union tests ----------------


def _snap(date, tickers):
    return {"date": date, "tickers": {t: {"ticker": t, **v} for t, v in tickers.items()}}


def test_upsert_retains_only_max_snapshots():
    snaps = []
    for i in range(1, 12):  # 11 distinct dates
        d = f"2026-06-{i:02d}"
        snaps = cs.upsert_snapshot(snaps, _snap(d, {"AAA": {"score": 50, "label": "MID", "close": 10}}))
    assert len(snaps) == cs.MAX_SNAPSHOTS == 7
    # Kept the most recent 7 dates.
    assert snaps[0]["date"] == "2026-06-05"
    assert snaps[-1]["date"] == "2026-06-11"


def test_upsert_replaces_same_date():
    snaps = [_snap("2026-06-01", {"AAA": {"score": 50, "label": "MID", "close": 10}})]
    snaps = cs.upsert_snapshot(snaps, _snap("2026-06-01", {"AAA": {"score": 80, "label": "HIGH", "close": 12}}))
    assert len(snaps) == 1
    assert snaps[0]["tickers"]["AAA"]["score"] == 80


def test_dynamic_ticker_union_persists_dropped_tickers():
    snaps = [
        _snap("2026-06-01", {"AAA": {"score": 50, "label": "MID", "close": 10},
                             "BBB": {"score": 60, "label": "MID", "close": 20}}),
        _snap("2026-06-02", {"AAA": {"score": 55, "label": "MID", "close": 11}}),  # BBB dropped
    ]
    table = cs.build_tracking_table(snaps)
    tickers = {r["ticker"] for r in table["rows"]}
    assert tickers == {"AAA", "BBB"}
    bbb = next(r for r in table["rows"] if r["ticker"] == "BBB")
    # BBB has no cell on the latest date -> that cell carries no score.
    assert bbb["cells"][-1].get("score") is None


def test_forward_return_accrues_against_earliest_snapshot():
    snaps = [
        _snap("2026-06-01", {"AAA": {"score": 50, "label": "MID", "close": 100}}),
        _snap("2026-06-03", {"AAA": {"score": 70, "label": "HIGH", "close": 110}}),
    ]
    table = cs.build_tracking_table(snaps)
    aaa = next(r for r in table["rows"] if r["ticker"] == "AAA")
    assert aaa["fwd_return_pct"] == 10.0  # 100 -> 110
    assert aaa["fwd_from"] == "2026-06-01"


def test_forward_return_pending_when_single_snapshot():
    snaps = [_snap("2026-06-01", {"AAA": {"score": 50, "label": "MID", "close": 100}})]
    table = cs.build_tracking_table(snaps)
    aaa = table["rows"][0]
    assert aaa["fwd_return_pct"] is None


# ---------------- OHLCV loader tests ----------------


def test_load_ohlcv_missing_file_returns_none():
    with tempfile.TemporaryDirectory() as td:
        assert cs.load_ohlcv("NOPE", ohlcv_dir=Path(td)) is None


def test_load_ohlcv_parses_csv():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ZZZ_daily.csv"
        p.write_text("Date,Open,High,Low,Close,Volume\n"
                     "2026-05-01,10,11,9,10.5,1000\n"
                     "2026-05-02,10.5,12,10,11.8,2000\n", encoding="utf-8")
        bars = cs.load_ohlcv("ZZZ", ohlcv_dir=Path(td))
    assert bars is not None and len(bars) == 2
    assert bars[-1]["close"] == 11.8
    assert bars[-1]["high"] == 12
    assert bars[-1]["volume"] == 2000


# ---------------- summary / report tests ----------------


def test_summary_flags_fade_risk_and_weak_strong():
    # High production rank but a clear downtrend -> LOW continuity -> fade risk.
    scored_main = [
        cs.score_ticker(_row("DOWN", [30, 28, 26, 24, 22, 20], rank=5, days_to_earnings=2),
                        ohlcv=None, bench=NO_BENCH, sector_medians={}),
        cs.score_ticker(_row("UP", [10, 11, 12, 13, 14, 15], rank=3, go_label="GO"),
                        ohlcv=None, bench=NO_BENCH, sector_medians={}),
    ]
    summary = cs.summarize(scored_main, [])
    fade = {s["ticker"] for s in summary["fade_risk_high_rank"]}
    assert "DOWN" in fade


def test_build_report_and_html_smoke(tmp_path=None):
    rankings = {
        "open_date": "2026-06-02",
        "rows": [
            _row("AAA", [10, 10.5, 11, 11.5, 12, 12.5, 12.7, 12.9, 13.1, 13.4], rank=1, go_label="GO"),
            _row("BBB", [20, 19, 18, 17, 16, 15, 14.8, 14.5, 14.2, 14.0], rank=2, days_to_earnings=2),
        ],
    }
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        # Patch module paths so build_report writes nowhere real and reads no
        # benchmark (offline-safe).
        orig = (cs.RANKINGS_FILE, cs.WATCHLIST_FILE, cs.SNAPSHOTS_FILE, cs.OHLCV_DIR)
        try:
            rfile = Path(td) / "rankings.json"
            rfile.write_text(json.dumps(rankings), encoding="utf-8")
            cs.RANKINGS_FILE = rfile
            cs.WATCHLIST_FILE = Path(td) / "watchlist.json"  # missing -> []
            cs.SNAPSHOTS_FILE = Path(td) / "snaps.jsonl"
            cs.OHLCV_DIR = Path(td) / "ohlcv"  # absent
            report = cs.build_report(ohlcv_dir=cs.OHLCV_DIR, fetch_bench=False)
            html = cs._render_html(report)
        finally:
            cs.RANKINGS_FILE, cs.WATCHLIST_FILE, cs.SNAPSHOTS_FILE, cs.OHLCV_DIR = orig
    assert report["summary_line"]
    assert report["badges"].get("AAA")
    assert "DIAGNOSTIC" in html
    assert "Continuity Score" in html
    assert "7-snapshot tracking" in html
    assert "<table" in html


def test_stamp_task_adds_and_updates_row():
    with tempfile.TemporaryDirectory() as td:
        tfile = Path(td) / "tasks.json"
        tfile.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        orig = cs.TASKS_FILE
        try:
            cs.TASKS_FILE = tfile
            cs._stamp_task({"summary_line": "x", "generated_at_chicago": "2026-06-02 03:35 PM CDT"})
            data = json.loads(tfile.read_text())
            assert any(r.get("id") == "continuity-score" for r in data["tasks"])
            # Update path: stamp again, still one row.
            cs._stamp_task({"summary_line": "y", "generated_at_chicago": "2026-06-02 04:00 PM CDT"})
            data = json.loads(tfile.read_text())
            rows = [r for r in data["tasks"] if r.get("id") == "continuity-score"]
            assert len(rows) == 1
            assert rows[0]["summary"] == "y"
        finally:
            cs.TASKS_FILE = orig


def main() -> int:
    tests = [
        test_rel_strength_beats_benchmark_scores_high,
        test_rel_strength_missing_when_insufficient_bars,
        test_rel_strength_10d_falls_back_to_max_lookback_with_min,
        test_close_location_prefers_true_range_high_close_scores_high,
        test_close_location_proxy_when_no_ohlcv,
        test_volume_support_missing_without_ohlcv,
        test_volume_support_caps_one_day_spike,
        test_ma_distance_overextension_penalised,
        test_reversal_health_all_up_beats_all_down,
        test_earnings_risk_imminent_low_far_high,
        test_sector_strength_outperformer_scores_high,
        test_missing_components_lower_confidence_not_fabricated,
        test_very_low_confidence_collapses_label_to_dash,
        test_score_board_without_ohlcv_still_scores,
        test_derive_label_thresholds,
        test_upsert_retains_only_max_snapshots,
        test_upsert_replaces_same_date,
        test_dynamic_ticker_union_persists_dropped_tickers,
        test_forward_return_accrues_against_earliest_snapshot,
        test_forward_return_pending_when_single_snapshot,
        test_load_ohlcv_missing_file_returns_none,
        test_load_ohlcv_parses_csv,
        test_summary_flags_fade_risk_and_weak_strong,
        test_build_report_and_html_smoke,
        test_stamp_task_adds_and_updates_row,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # pragma: no cover
            failed += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

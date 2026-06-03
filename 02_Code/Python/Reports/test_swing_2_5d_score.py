"""Tests for swing_2_5d_score.py.

Covers:
  * positive component formulas behave monotonically / sensibly
  * penalty haircuts (overextension, weak continuity, earnings, low liquidity)
  * missing-data behaviour: absent components are omitted and lower confidence
    (never fabricated to 0.5); very-low-confidence collapses label to '—'
  * badge label thresholds (HIGH/MID/LOW/—)
  * missing-artifact fallback: scoring a board with no OHLCV / no continuity
    artifact still produces scores from the closes array
  * production fields (swing_score/swing_tier, ai_score) are preserved untouched
    on the output rows and never recomputed
  * missing_data_roadmap is present and prioritised
  * build_report smoke (no exception, badges map populated)
  * task stamp adds/updates the row
  * static JS checks: the main + watchlist tables wire the 2-5D badge,
    quick-filters, CSV columns, and tooltip text — and the production swing
    badge/column is left intact.

Run: python 02_Code/Python/Reports/test_swing_2_5d_score.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import swing_2_5d_score as sw  # noqa: E402

REPO_ROOT = Path(HERE).resolve().parents[2]


def _row(ticker, closes, *, rank=1, sector="Tech", technical=8.0,
         go_label="GO", acc_label="MID", volume_millions=5.0, sentiment=6.0,
         days_to_earnings=40, atr_pct=2.0, swing_score=7.0, swing_tier="A",
         ai_score=8.0, rank_delta=None, catalyst_flag=False):
    return {
        "ticker": ticker, "company": f"{ticker} Inc", "rank": rank,
        "sector": sector, "closes": closes, "technical": technical,
        "go_label": go_label, "acc_label": acc_label,
        "volume_millions": volume_millions, "sentiment": sentiment,
        "days_to_earnings": days_to_earnings, "atr_pct": atr_pct,
        "swing_score": swing_score, "swing_tier": swing_tier,
        "ai_score": ai_score, "rank_delta": rank_delta,
        "catalyst_flag": catalyst_flag,
    }


# ---------------- positive component formula tests ----------------


def test_tech_trend_maps_0_10_to_0_1():
    s_hi, _ = sw.comp_tech_trend(9.0)
    s_lo, _ = sw.comp_tech_trend(2.0)
    s_na, d = sw.comp_tech_trend(None)
    assert abs(s_hi - 0.9) < 1e-9
    assert abs(s_lo - 0.2) < 1e-9
    assert s_na is None and "no technical" in d


def test_momentum_thrust_positive_beats_flat():
    thrust = [100, 100, 100, 100, 100, 108]   # sharp 2-day pop at the end
    flat = [100, 100, 100, 100, 100, 100]
    s_thrust, _ = sw.comp_momentum_thrust(thrust, None)
    s_flat, _ = sw.comp_momentum_thrust(flat, None)
    assert s_thrust is not None and s_flat is not None
    assert s_thrust > 0.6, s_thrust
    assert abs(s_flat - 0.5) < 0.06, s_flat


def test_momentum_thrust_missing_when_too_few_closes():
    s, d = sw.comp_momentum_thrust([100, 101], None)
    assert s is None and "insufficient" in d


def test_continuity_prefers_artifact_then_proxy():
    s_art, d_art = sw.comp_continuity({"score": 80, "label": "HIGH"}, [])
    assert s_art is not None and abs(s_art - 0.8) < 1e-9 and "CONT" in d_art
    # No artifact -> proxy from closes up-days.
    s_proxy, d_proxy = sw.comp_continuity(None, [10, 11, 12, 13, 14, 15])
    assert s_proxy is not None and "proxy" in d_proxy and s_proxy > 0.6
    s_none, _ = sw.comp_continuity(None, [10, 11])
    assert s_none is None


def test_accumulation_label_then_meter_fallback():
    assert sw.comp_accumulation("HIGH", None)[0] == 0.85
    assert sw.comp_accumulation("LOW", None)[0] == 0.20
    s_meter, _ = sw.comp_accumulation(None, 7.0)
    assert abs(s_meter - 0.7) < 1e-9
    assert sw.comp_accumulation(None, None)[0] is None


def test_go_gate_ordering():
    assert sw.comp_go_gate("GO")[0] > sw.comp_go_gate("WAIT")[0] > sw.comp_go_gate("WEAK")[0]
    assert sw.comp_go_gate(None)[0] is None


def test_activity_liq_rewards_liquidity_and_promotion():
    s_low, _ = sw.comp_activity_liq(0.5, None)
    s_high, _ = sw.comp_activity_liq(20.0, None)
    s_promo, _ = sw.comp_activity_liq(20.0, 30)
    assert s_high > s_low
    assert s_promo >= s_high
    assert sw.comp_activity_liq(None, None)[0] is None


def test_sentiment_catalyst_nudge():
    base, _ = sw.comp_sentiment_catalyst(6.0, False)
    boosted, _ = sw.comp_sentiment_catalyst(6.0, True)
    assert boosted > base
    assert sw.comp_sentiment_catalyst(None, False)[0] is None
    assert sw.comp_sentiment_catalyst(None, True)[0] is not None  # catalyst alone


# ---------------- penalty haircut tests ----------------


def test_overextension_penalises_parabolic():
    parabolic = [100, 100, 100, 100, 100, 100, 100, 100, 100, 140]  # +40% over MA
    mult, detail = sw.pen_overextension(parabolic, None, atr_pct=2.0)
    assert mult < 1.0 and mult >= 0.80 and "overext" in detail
    calm = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101]
    mult2, _ = sw.pen_overextension(calm, None, atr_pct=2.0)
    assert mult2 == 1.0


def test_weak_continuity_haircut():
    assert sw.pen_weak_continuity({"label": "LOW"})[0] == 0.85
    assert sw.pen_weak_continuity({"label": "HIGH"})[0] == 1.0
    assert sw.pen_weak_continuity(None)[0] == 1.0


def test_earnings_penalty_inside_window_only():
    assert sw.pen_earnings(1)[0] == 0.70
    assert sw.pen_earnings(4)[0] == 0.82
    assert sw.pen_earnings(20)[0] == 1.0
    assert sw.pen_earnings(None)[0] == 1.0  # unknown -> neutral


def test_low_liquidity_penalty():
    assert sw.pen_low_liquidity(0.4)[0] == 0.85
    assert sw.pen_low_liquidity(5.0)[0] == 1.0
    assert sw.pen_low_liquidity(None)[0] == 1.0


def test_penalty_lowers_final_below_positive():
    # Strong positive setup but earnings tomorrow -> score < positive_score.
    row = _row("AAA", [100, 101, 102, 103, 104, 107], days_to_earnings=1)
    res = sw.score_ticker(row, ohlcv=None, cont_entry={"score": 80, "label": "HIGH"})
    assert res["positive_score"] is not None
    assert res["score"] < res["positive_score"]
    assert "earnings" in res["penalties"]


# ---------------- label / confidence tests ----------------


def test_derive_label_thresholds():
    assert sw.derive_label(80.0, 1.0) == "HIGH"
    assert sw.derive_label(55.0, 1.0) == "MID"
    assert sw.derive_label(30.0, 1.0) == "LOW"
    assert sw.derive_label(None, 1.0) == "—"
    # Very low confidence collapses to dash even with a numeric score.
    assert sw.derive_label(80.0, 0.10) == "—"


def test_missing_components_lower_confidence_not_fabricated():
    # Only closes present (no technical/go/acc/volume/sentiment).
    bare = {"ticker": "B", "closes": [10, 10, 10, 11, 12, 13]}
    res = sw.score_ticker(bare, ohlcv=None, cont_entry=None)
    # momentum + continuity-proxy present; the rest omitted -> confidence < 1.
    assert 0.0 < res["confidence"] < 1.0
    present = [k for k, v in res["components"].items() if v["present"]]
    assert "tech_trend" not in present
    assert "momentum_thrust" in present


def test_very_low_confidence_collapses_label_to_dash():
    # A single non-closes component -> tiny confidence -> '—'.
    only_tech = {"ticker": "C", "technical": 9.0, "closes": []}
    res = sw.score_ticker(only_tech, ohlcv=None, cont_entry=None)
    assert res["confidence"] < sw.MIN_CONFIDENCE
    assert res["label"] == "—"


# ---------------- board / fallback tests ----------------


def test_score_board_without_ohlcv_or_continuity_still_scores():
    rankings = {"rows": [
        _row("AAA", [100, 101, 102, 103, 104, 106]),
        _row("BBB", [50, 49, 48, 47, 46, 45], go_label="WEAK", acc_label="LOW"),
    ]}
    with tempfile.TemporaryDirectory() as td:
        scored = sw.score_board(rankings, continuity=None, accum=None, ohlcv_dir=Path(td))
    assert len(scored) == 2
    by = {s["ticker"]: s for s in scored}
    assert by["AAA"]["score"] is not None
    assert by["BBB"]["score"] is not None
    # The rising/GO name should out-score the falling/WEAK name.
    assert by["AAA"]["score"] > by["BBB"]["score"]


def test_production_fields_preserved_untouched():
    row = _row("AAA", [100, 101, 102, 103, 104, 106], swing_score=9.3,
               swing_tier="A", ai_score=9.1)
    res = sw.score_ticker(row, ohlcv=None, cont_entry=None)
    # The diagnostic must echo production fields exactly, never recompute them.
    assert res["swing_score"] == 9.3
    assert res["swing_tier"] == "A"
    assert res["ai_score"] == 9.1
    # The diagnostic score lives in its own key and is distinct from swing_score.
    assert "score" in res and res["score"] != res["swing_score"]


def test_missing_data_roadmap_present_and_prioritised():
    rm = sw.missing_data_roadmap()
    assert "priorities" in rm and isinstance(rm["priorities"], list)
    sigs = {p["signal"] for p in rm["priorities"]}
    assert any("VWAP" in s for s in sigs)
    assert any("put/call" in s for s in sigs)
    assert any("sentiment" in s.lower() for s in sigs)
    # Every entry names a source (free-source prioritisation requirement).
    assert all(p.get("source") for p in rm["priorities"])


def test_build_report_smoke(monkeypatch=None):
    # Point module file paths at a temp dir with a minimal rankings.json.
    import importlib
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "reports").mkdir(parents=True, exist_ok=True)
        rankings = {"open_date": "2026-06-03", "rows": [
            _row("AAA", [100, 101, 102, 103, 104, 106]),
            _row("BBB", [50, 50, 50, 50, 50, 50], go_label="WAIT"),
        ]}
        (tdp / "rankings.json").write_text(json.dumps(rankings), encoding="utf-8")
        # Redirect module globals.
        orig = (sw.RANKINGS_FILE, sw.WATCHLIST_FILE, sw.CONTINUITY_FILE,
                sw.ACCUM_FILE, sw.OHLCV_DIR, sw.JSON_OUTPUT, sw.TASKS_FILE)
        sw.RANKINGS_FILE = tdp / "rankings.json"
        sw.WATCHLIST_FILE = tdp / "watchlist_rankings.json"
        sw.CONTINUITY_FILE = tdp / "reports" / "continuity_score.json"
        sw.ACCUM_FILE = tdp / "reports" / "accumulation_signal_meter.json"
        sw.OHLCV_DIR = tdp / "ohlcv"
        sw.JSON_OUTPUT = tdp / "reports" / "swing_2_5d_score.json"
        sw.TASKS_FILE = tdp / "tasks.json"
        try:
            report = sw.build_report(ohlcv_dir=tdp / "ohlcv")
            assert report["diagnostic_only"] is True
            assert report["horizon"] == "2-5 trading days"
            assert "differs_from_continuity" in report
            assert report["badges"].get("AAA") is not None
            assert "missing_data_roadmap" in report
            assert abs(sum(sw.COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9
        finally:
            (sw.RANKINGS_FILE, sw.WATCHLIST_FILE, sw.CONTINUITY_FILE,
             sw.ACCUM_FILE, sw.OHLCV_DIR, sw.JSON_OUTPUT, sw.TASKS_FILE) = orig


def test_stamp_task_adds_and_updates_row():
    with tempfile.TemporaryDirectory() as td:
        tf = Path(td) / "tasks.json"
        tf.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        orig = sw.TASKS_FILE
        sw.TASKS_FILE = tf
        try:
            sw._stamp_task({"generated_at_chicago": "x", "summary_line": "s"})
            data = json.loads(tf.read_text())
            ids = [t["id"] for t in data["tasks"]]
            assert "swing-2-5d-score" in ids
            # Idempotent update, not duplicate.
            sw._stamp_task({"generated_at_chicago": "y", "summary_line": "s2"})
            data2 = json.loads(tf.read_text())
            rows = [t for t in data2["tasks"] if t["id"] == "swing-2-5d-score"]
            assert len(rows) == 1 and rows[0]["last_run"] == "y"
        finally:
            sw.TASKS_FILE = orig


# ---------------- static JS / HTML wiring checks ----------------


def _read(p):
    return (REPO_ROOT / p).read_text(encoding="utf-8")


def test_index_html_wires_swing_2_5d_badge():
    html = _read("index.html")
    # Data source registered.
    assert "swing_2_5d_score.json" in html
    # New diagnostic column header labelled 2-5D with a diagnostic-only tooltip.
    assert ">2-5D<" in html
    assert "deriveSwing25Badge" in html or "swing25ByTicker" in html
    # Quick filters present.
    assert 'data-diag="swing25-high"' in html
    assert 'data-diag="swing25-low"' in html
    # CSV export columns added.
    assert "Swing25" in html
    # Tooltip makes the diagnostic-only / does-not-affect-rank promise.
    assert "does not affect production rank" in html.lower() or \
           "does not affect production" in html.lower()


def test_watchlist_html_wires_swing_2_5d_badge():
    html = _read("watchlist.html")
    assert "swing_2_5d_score.json" in html
    assert ">2-5D<" in html
    assert 'data-diag="swing25-high"' in html
    assert 'data-diag="swing25-low"' in html
    assert "Swing25" in html


def test_production_swing_badge_and_column_left_intact():
    # The existing production SWING column + swing_score wiring must remain.
    for f in ("index.html", "watchlist.html"):
        html = _read(f)
        assert 'data-k="swing_score"' in html, f
        assert "swingBadgeHTML" in html, f  # production badge untouched
        assert ">SWING<" in html, f         # production header label intact


def test_diagnostics_page_links_swing_2_5d_artifact():
    html = _read("diagnostics.html")
    assert "swing_2_5d_score.json" in html
    assert "swing2_5dScoreLink" in html or "swing25ScoreLink" in html


def test_workflow_runs_swing_2_5d_and_stages_artifact():
    wf = _read(".github/workflows/update-rankings.yml")
    assert "swing_2_5d_score.py" in wf
    assert "data/reports/swing_2_5d_score.json" in wf


# ---------------- runner ----------------


def main() -> int:
    tests = [
        test_tech_trend_maps_0_10_to_0_1,
        test_momentum_thrust_positive_beats_flat,
        test_momentum_thrust_missing_when_too_few_closes,
        test_continuity_prefers_artifact_then_proxy,
        test_accumulation_label_then_meter_fallback,
        test_go_gate_ordering,
        test_activity_liq_rewards_liquidity_and_promotion,
        test_sentiment_catalyst_nudge,
        test_overextension_penalises_parabolic,
        test_weak_continuity_haircut,
        test_earnings_penalty_inside_window_only,
        test_low_liquidity_penalty,
        test_penalty_lowers_final_below_positive,
        test_derive_label_thresholds,
        test_missing_components_lower_confidence_not_fabricated,
        test_very_low_confidence_collapses_label_to_dash,
        test_score_board_without_ohlcv_or_continuity_still_scores,
        test_production_fields_preserved_untouched,
        test_missing_data_roadmap_present_and_prioritised,
        test_build_report_smoke,
        test_stamp_task_adds_and_updates_row,
        test_index_html_wires_swing_2_5d_badge,
        test_watchlist_html_wires_swing_2_5d_badge,
        test_production_swing_badge_and_column_left_intact,
        test_diagnostics_page_links_swing_2_5d_artifact,
        test_workflow_runs_swing_2_5d_and_stages_artifact,
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

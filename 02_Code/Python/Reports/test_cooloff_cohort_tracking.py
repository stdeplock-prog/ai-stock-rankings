"""Fixture-based tests for cooloff_cohort_tracking.py.

Run: python 02_Code/Python/Reports/test_cooloff_cohort_tracking.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cooloff_cohort_tracking as cc  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _pine_entry(ticker, score, blockers=None, evaluated=True, sector="Tech",
                ai=7.0):
    return {
        "ticker": ticker, "evaluated": evaluated, "sector": sector,
        "ai_score": ai, "swing_score": ai - 0.5,
        "go_no_go_score_normalized": score,
        "blockers": blockers or [],
        "sources": ["main"],
    }


def _ranking_row(ticker, last_close, sector="Tech"):
    return {
        "ticker": ticker, "sector": sector,
        "closes": [last_close - 1.0, last_close],
    }


# ---------- classify_cohorts ----------


def test_classify_cohorts_partitions_correctly():
    pine = {"per_ticker": [
        _pine_entry("CLEAN1", 0.9),
        _pine_entry("CLEAN2", 0.7),
        _pine_entry("OVEREXT", 0.6, blockers=["overextended_bb (>1.5% above BB upper)"]),
        _pine_entry("BLOCK_LOWVOL", 0.65, blockers=["low_volatility_chop (10d range <2%)"]),
        _pine_entry("WEAK", 0.3),
        _pine_entry("MID", 0.55),
        _pine_entry("UNEVAL", 0.0, evaluated=False),
        _pine_entry("NOSCORE", None),
    ]}
    out = cc.classify_cohorts(pine)
    cg = {m["ticker"] for m in out["clean_go"]}
    oe = {m["ticker"] for m in out["overextended_bb"]}
    weak = {m["ticker"] for m in out["weak_go"]}
    assert cg == {"CLEAN1", "CLEAN2"}, cg
    assert oe == {"OVEREXT"}, oe
    assert weak == {"WEAK"}, weak
    # Names with non-overextended blockers stay out of all three positive
    # cohorts (clean_go excluded by blocker, overextended_bb excluded by
    # blocker text mismatch, weak_go excluded by score).
    assert "BLOCK_LOWVOL" not in cg | oe | weak


def test_classify_cohorts_handles_empty_input():
    assert cc.classify_cohorts(None) == {
        "clean_go": [], "overextended_bb": [], "weak_go": []}
    assert cc.classify_cohorts({"per_ticker": []}) == {
        "clean_go": [], "overextended_bb": [], "weak_go": []}


# ---------- snapshot persistence ----------


def test_snapshots_roundtrip(tmp_path):
    p = tmp_path / "snaps.jsonl"
    cc.save_snapshots([
        {"as_of_date": "2026-05-01", "cohorts": {"clean_go": {"size": 1, "members": []}}},
    ], path=p)
    out = cc.load_snapshots(p)
    assert len(out) == 1
    assert out[0]["as_of_date"] == "2026-05-01"
    # Tolerates a corrupt line in the middle
    p.write_text(p.read_text() + "{not json}\n", encoding="utf-8")
    out2 = cc.load_snapshots(p)
    assert len(out2) == 1


def test_prune_snapshots_drops_old_records():
    today = date(2026, 5, 1)
    recs = [
        {"as_of_date": "2025-12-01"},  # >90d old
        {"as_of_date": "2026-04-15"},  # within retention
        {"as_of_date": "2026-04-30"},
    ]
    kept = cc.prune_snapshots(recs, today=today, retention_days=90)
    dates = sorted(r["as_of_date"] for r in kept)
    assert "2025-12-01" not in dates
    assert "2026-04-15" in dates
    assert "2026-04-30" in dates


def test_prune_snapshots_caps_max_rows():
    today = date(2026, 5, 1)
    recs = [{"as_of_date": (date(2026, 1, 1)).isoformat()} for _ in range(0)]
    # Build a small set then enforce a cap of 2.
    recs = [{"as_of_date": "2026-04-25"},
            {"as_of_date": "2026-04-26"},
            {"as_of_date": "2026-04-27"},
            {"as_of_date": "2026-04-28"}]
    kept = cc.prune_snapshots(recs, today=today, retention_days=90, max_rows=2)
    dates = sorted(r["as_of_date"] for r in kept)
    assert dates == ["2026-04-27", "2026-04-28"]


# ---------- pending horizons (no lookahead) ----------


def test_horizons_pending_on_day_zero(tmp_path):
    pine = {"per_ticker": [
        _pine_entry("CLEAN1", 0.9),
        _pine_entry("OVEREXT", 0.5, blockers=["overextended_bb (>1.5%)"]),
    ]}
    rankings = {"open_date": "2026-05-01", "rows": [
        _ranking_row("CLEAN1", 100.0), _ranking_row("OVEREXT", 50.0)]}
    snaps_path = tmp_path / "snaps.jsonl"
    report, snaps = cc.build_report(
        pine_report=pine, rankings=rankings, watchlist={"rows": []},
        today=date(2026, 5, 1), snapshots_path=snaps_path)

    assert len(snaps) == 1
    fwd = snaps[0]["forward"]
    for h in cc.FORWARD_HORIZONS_TRADING_DAYS:
        slot = fwd[f"{h}d"]
        assert slot["status"] == "pending", slot
    # No completed observations anywhere yet
    for h in cc.FORWARD_HORIZONS_TRADING_DAYS:
        cohorts = (report["horizon_comparison"][f"{h}d"]).get("cohorts") or {}
        for c in cohorts.values():
            assert c.get("n_observations") in (0, None), c


def test_returns_no_lookahead_resolves_after_horizon(tmp_path):
    snaps_path = tmp_path / "snaps.jsonl"
    pine = {"per_ticker": [
        _pine_entry("CLEAN1", 0.9), _pine_entry("CLEAN2", 0.8),
        _pine_entry("OVEREXT", 0.5, blockers=["overextended_bb"]),
    ]}
    rankings_d1 = {"open_date": "2026-05-01", "rows": [
        _ranking_row("CLEAN1", 100.0),
        _ranking_row("CLEAN2", 200.0),
        _ranking_row("OVEREXT", 50.0),
    ]}
    cc.build_report(
        pine_report=pine, rankings=rankings_d1, watchlist={"rows": []},
        today=date(2026, 5, 1), snapshots_path=snaps_path)

    # 5 trading days later (Fri 5/1 -> Fri 5/8): clean_go +2%, overextended -1%.
    rankings_d2 = {"open_date": "2026-05-08", "rows": [
        _ranking_row("CLEAN1", 102.0),
        _ranking_row("CLEAN2", 204.0),
        _ranking_row("OVEREXT", 49.5),
    ]}
    report, snaps = cc.build_report(
        pine_report=pine, rankings=rankings_d2, watchlist={"rows": []},
        today=date(2026, 5, 8), snapshots_path=snaps_path)

    # The 2026-05-01 snapshot should now have 1d/3d/5d completed; 10d pending.
    orig = next(s for s in snaps if s["as_of_date"] == "2026-05-01")
    fwd = orig["forward"]
    assert fwd["1d"]["status"] == "completed"
    assert fwd["3d"]["status"] == "completed"
    assert fwd["5d"]["status"] == "completed"
    assert fwd["10d"]["status"] == "pending"

    # 5d returns should reflect the prices we set
    five_clean = fwd["5d"]["cohorts"]["clean_go"]
    assert five_clean["evaluated"] == 2
    assert abs(five_clean["mean_return"] - 0.02) < 1e-6
    five_oe = fwd["5d"]["cohorts"]["overextended_bb"]
    assert five_oe["evaluated"] == 1
    assert abs(five_oe["mean_return"] - (-0.01)) < 1e-6

    # The cross-snapshot aggregation should pool only completed observations.
    five_summary = report["horizon_comparison"]["5d"]
    assert five_summary["cohorts"]["clean_go"]["n_observations"] == 2
    assert five_summary["cohorts"]["overextended_bb"]["n_observations"] == 1


def test_existing_completed_horizon_not_overwritten(tmp_path):
    """If a horizon was already resolved on an earlier run, a later run
    must NOT recompute it using today's prices — that would drift away
    from the true horizon close."""
    snaps_path = tmp_path / "snaps.jsonl"
    pine = {"per_ticker": [_pine_entry("T1", 0.9)]}
    rankings_d1 = {"open_date": "2026-05-01",
                   "rows": [_ranking_row("T1", 100.0)]}
    cc.build_report(pine_report=pine, rankings=rankings_d1,
                    watchlist={"rows": []}, today=date(2026, 5, 1),
                    snapshots_path=snaps_path)

    # Day +5 (Fri -> Fri): horizon resolves at 110.
    rankings_d2 = {"open_date": "2026-05-08",
                   "rows": [_ranking_row("T1", 110.0)]}
    cc.build_report(pine_report=pine, rankings=rankings_d2,
                    watchlist={"rows": []}, today=date(2026, 5, 8),
                    snapshots_path=snaps_path)

    # Day +6 (Mon 5/11): if we ran again with a different price for T1,
    # the previously-completed 5d slot should keep its 110-based return.
    rankings_d3 = {"open_date": "2026-05-11",
                   "rows": [_ranking_row("T1", 200.0)]}
    _, snaps = cc.build_report(pine_report=pine, rankings=rankings_d3,
                               watchlist={"rows": []}, today=date(2026, 5, 11),
                               snapshots_path=snaps_path)

    orig = next(s for s in snaps if s["as_of_date"] == "2026-05-01")
    five = orig["forward"]["5d"]
    assert five["status"] == "completed"
    cg = five["cohorts"]["clean_go"]
    # Should still be (110-100)/100 = 0.10, NOT (200-100)/100 = 1.0
    assert abs(cg["mean_return"] - 0.10) < 1e-6, cg


# ---------- decision logic ----------


def test_decision_keep_advisory_when_n_below_threshold():
    comparison = {"5d": {"cohorts": {
        "clean_go": {"n_observations": 5, "mean_return": 0.05},
        "overextended_bb": {"n_observations": 5, "mean_return": -0.01},
    }}}
    d = cc.decide_recommendation(comparison, min_obs=30)
    assert d["recommendation"] == "keep_advisory"
    assert d["ready"] is False


def test_decision_recommends_change_when_gap_large_and_n_sufficient():
    comparison = {"5d": {"cohorts": {
        "clean_go": {"n_observations": 50, "mean_return": 0.04},
        "overextended_bb": {"n_observations": 35, "mean_return": -0.01},
    }}}
    d = cc.decide_recommendation(comparison, min_obs=30)
    assert d["recommendation"] == "consider_scoring_change"
    assert d["ready"] is True


def test_decision_keep_advisory_when_gap_small():
    comparison = {"5d": {"cohorts": {
        "clean_go": {"n_observations": 50, "mean_return": 0.011},
        "overextended_bb": {"n_observations": 35, "mean_return": 0.008},
    }}}
    d = cc.decide_recommendation(comparison, min_obs=30)
    assert d["recommendation"] == "keep_advisory"
    assert d["ready"] is True  # we have enough data, just not enough gap


# ---------- top-level: same-day re-run replaces ----------


def test_same_day_rerun_replaces_record(tmp_path):
    snaps_path = tmp_path / "snaps.jsonl"
    pine_v1 = {"per_ticker": [_pine_entry("A", 0.9)]}
    pine_v2 = {"per_ticker": [_pine_entry("B", 0.9)]}
    rankings = {"open_date": "2026-05-01",
                "rows": [_ranking_row("A", 100.0), _ranking_row("B", 50.0)]}
    cc.build_report(pine_report=pine_v1, rankings=rankings,
                    watchlist={"rows": []}, today=date(2026, 5, 1),
                    snapshots_path=snaps_path)
    _, snaps = cc.build_report(
        pine_report=pine_v2, rankings=rankings, watchlist={"rows": []},
        today=date(2026, 5, 1), snapshots_path=snaps_path)
    same = [s for s in snaps if s["as_of_date"] == "2026-05-01"]
    assert len(same) == 1
    members = [m["ticker"] for m in same[0]["cohorts"]["clean_go"]["members"]]
    assert members == ["B"]


# ---------- HTML safety ----------


def test_render_html_produces_string(tmp_path):
    snaps_path = tmp_path / "snaps.jsonl"
    pine = {"per_ticker": [
        _pine_entry("CLEAN1", 0.9),
        _pine_entry("OVEREXT", 0.5, blockers=["overextended_bb"]),
    ]}
    rankings = {"open_date": "2026-05-01", "rows": [
        _ranking_row("CLEAN1", 100.0), _ranking_row("OVEREXT", 50.0)]}
    report, _ = cc.build_report(
        pine_report=pine, rankings=rankings, watchlist={"rows": []},
        today=date(2026, 5, 1), snapshots_path=snaps_path)
    html = cc._render_html(report)
    assert "Cool-off" in html
    assert "Recommendation" in html
    assert "clean_go" in html
    assert "overextended_bb" in html


# ---------- runner ----------


def main():
    tmp_root = tempfile.mkdtemp(prefix="cooloff_test_")
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            if "tmp_path" in fn.__code__.co_varnames:
                sub = Path(tmp_root) / name
                sub.mkdir(parents=True, exist_ok=True)
                fn(sub)
            else:
                fn()
            print(f"PASS: {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR: {name}: {type(e).__name__}: {e}")
    if failed:
        _fail(f"{failed} test(s) failed")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

"""Fixture-based tests for benchmark_review.py.

Run: python 02_Code/Python/Reports/test_benchmark_review.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import benchmark_review as br  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# ---- return helpers ----


def test_returns_from_closes_basic():
    closes = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 11.0]
    out = br.returns_from_closes(closes)
    assert out["available"] is True
    # last vs first: 11/10 - 1 = 0.10
    assert abs(out["return_window"] - 0.10) < 1e-3, out
    # last vs prev: 11/10.8 - 1 ~ 0.01852 (rounded to 4dp)
    assert out["return_1d"] is not None and abs(out["return_1d"] - (11.0 / 10.8 - 1)) < 1e-3
    # 5d: last vs nums[-6] = 10.4 (5 bars back): 11/10.4 - 1
    assert out["return_5d"] is not None and abs(out["return_5d"] - (11.0 / 10.4 - 1)) < 1e-3


def test_returns_from_closes_unavailable():
    assert br.returns_from_closes([])["available"] is False
    assert br.returns_from_closes(None)["available"] is False
    assert br.returns_from_closes([10.0])["available"] is False
    # zero first close -> unsafe div, returns unavailable
    assert br.returns_from_closes([0.0, 1.0])["available"] is False


def test_returns_from_closes_with_missing_entries():
    # NaN-like entries (stringified or None) are dropped; remaining list still usable
    closes = [None, "x", 10.0, 11.0]
    out = br.returns_from_closes(closes)
    assert out["available"] is True
    assert abs(out["return_window"] - 0.10) < 1e-9


# ---- sector concentration ----


def test_sector_concentration_flag_above_40_pct():
    rows = [{"sector": "Technology"} for _ in range(11)] + \
           [{"sector": "Healthcare"} for _ in range(8)] + \
           [{"sector": "Energy"} for _ in range(6)]
    sd = br.sector_distribution(rows)
    # 11 / 25 = 0.44
    assert sd["top_sector"] == "Technology"
    assert sd["concentrated"] is True
    assert abs(sd["top_pct"] - 11 / 25) < 1e-9


def test_sector_concentration_under_threshold_not_flagged():
    rows = [{"sector": s} for s in ["A", "B", "C", "D"] * 5]
    sd = br.sector_distribution(rows)
    assert sd["concentrated"] is False
    # missing/em-dash sectors don't count toward distribution
    rows2 = [{"sector": "—"}] * 3 + [{"sector": "Tech"}] * 2
    sd2 = br.sector_distribution(rows2)
    assert sd2["missing_sector"] == 3
    assert sd2["total_classified"] == 2


# ---- bucket metrics ----


def _row(ticker, closes, sector="Tech", ai=7.0):
    return {"ticker": ticker, "closes": closes, "sector": sector,
            "ai_score": ai, "fundamental": ai, "technical": ai, "swing_score": ai}


def test_bucket_metrics_aggregates_returns():
    rows = [
        _row("A", [10, 10.5, 11.0]),
        _row("B", [20, 19, 21]),
        _row("C", [5, 5.0, 5.0]),  # zero return
    ]
    b = br.bucket_metrics("test", rows)
    assert b["available"] is True
    assert b["size"] == 3
    assert b["return_window"]["count"] == 3
    # mean of (.10, .05, 0) = 0.05
    assert abs(b["return_window"]["mean"] - 0.05) < 1e-6
    assert b["return_window"]["pct_positive"] is not None and \
           b["return_window"]["pct_positive"] == round(2 / 3, 4)


def test_bucket_metrics_handles_missing_closes():
    rows = [_row("A", []), _row("B", [10, 11])]
    b = br.bucket_metrics("test", rows)
    assert b["missing_closes"] == 1
    assert b["return_window"]["count"] == 1


def test_bucket_metrics_empty_returns_unavailable():
    b = br.bucket_metrics("empty", [])
    assert b["available"] is False
    assert b["size"] == 0


# ---- snapshot scaffold (no lookahead) ----


def _bench_review_with_tmp_snapshots(tmp_path: Path):
    """Patch SNAPSHOTS_FILE for the duration of the test."""
    br.SNAPSHOTS_FILE = tmp_path / "snapshots.jsonl"


def test_snapshot_append_and_pending(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)
    rankings = {
        "open_date": "2026-05-01",
        "rows": [_row(f"T{i}", [10, 10 + i * 0.1, 10.0 + i * 0.1])
                 for i in range(15)],
    }
    watchlist = {"open_date": "2026-05-01", "rows": []}
    today = date(2026, 5, 1)
    report, snapshots = br.build_report(
        rankings, watchlist, fetch_benchmarks=False, today=today)

    assert len(snapshots) == 1
    rec = snapshots[0]
    assert rec["as_of_date"] == "2026-05-01"
    main10 = rec["buckets"]["main_top10"]
    assert main10["size"] == 10
    # All horizons should be pending on day 0
    fwd = rec["forward"]
    for h in br.FORWARD_HORIZONS_TRADING_DAYS:
        assert fwd[f"{h}d"]["status"] == "pending", fwd[f"{h}d"]


def test_snapshot_evaluation_after_horizon_no_lookahead(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)

    # Day 1: capture snapshot at 2026-05-01 with refs of 100.
    rankings_d1 = {
        "open_date": "2026-05-01",
        "rows": [_row(f"T{i}", [100.0] * 5) for i in range(10)],
    }
    watchlist = {"open_date": "2026-05-01", "rows": []}
    report1, _snap1 = br.build_report(
        rankings_d1, watchlist, fetch_benchmarks=False, today=date(2026, 5, 1))
    assert report1["snapshots_kept"] == 1

    # Day 8 calendar (= 5 trading days later: 5/1 Fri through 5/8 Fri):
    # horizons 1d, 3d, 5d should be completed; 10d and 20d still pending.
    new_prices = [101.0] * 10  # +1%
    rankings_d2 = {
        "open_date": "2026-05-08",
        "rows": [_row(f"T{i}", [100.0, 101.0]) for i in range(10)],
    }
    # The new run will append a fresh snapshot for 2026-05-08; the prior
    # snapshot for 2026-05-01 should now have its 1/3/5d slots completed.
    report2, snap2 = br.build_report(
        rankings_d2, watchlist, fetch_benchmarks=False, today=date(2026, 5, 8))

    # Find the original 2026-05-01 record after re-evaluation
    orig = next(s for s in snap2 if s["as_of_date"] == "2026-05-01")
    fwd = orig["forward"]
    assert fwd["1d"]["status"] == "completed"
    assert fwd["3d"]["status"] == "completed"
    assert fwd["5d"]["status"] == "completed"
    assert fwd["10d"]["status"] == "pending"
    assert fwd["20d"]["status"] == "pending"

    # Returns should reflect 1.0% across the bucket, not lookahead-cheated.
    main10 = fwd["5d"]["buckets"]["main_top10"]
    assert main10["evaluated"] == 10
    assert main10["mean_return"] is not None
    assert abs(main10["mean_return"] - 0.01) < 1e-6


def test_snapshot_replaces_same_day_record(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)
    rankings = {"open_date": "2026-05-01",
                "rows": [_row(f"T{i}", [10, 11]) for i in range(5)]}
    watchlist = {"open_date": "2026-05-01", "rows": []}

    br.build_report(rankings, watchlist, fetch_benchmarks=False, today=date(2026, 5, 1))
    # Re-run same day with different ranking — should not double-write
    rankings2 = {"open_date": "2026-05-01",
                 "rows": [_row(f"X{i}", [20, 22]) for i in range(5)]}
    report, snaps = br.build_report(
        rankings2, watchlist, fetch_benchmarks=False, today=date(2026, 5, 1))

    same_day = [s for s in snaps if s["as_of_date"] == "2026-05-01"]
    assert len(same_day) == 1, same_day
    members = same_day[0]["buckets"]["main_top10"]["members"]
    assert members[0]["ticker"] == "X0"


def test_snapshot_pruned_to_retention(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)
    today = date(2026, 5, 1)
    old = today - timedelta(days=120)  # outside 90d retention
    recent = today - timedelta(days=10)

    # Seed a snapshots file directly
    seed = [
        {"as_of_date": old.isoformat(), "buckets": {}, "captured_at": "x"},
        {"as_of_date": recent.isoformat(), "buckets": {}, "captured_at": "y"},
    ]
    br.save_snapshots(seed)

    rankings = {"open_date": today.isoformat(),
                "rows": [_row(f"T{i}", [10, 11]) for i in range(3)]}
    watchlist = {"open_date": today.isoformat(), "rows": []}
    report, snaps = br.build_report(
        rankings, watchlist, fetch_benchmarks=False, today=today)

    dates = sorted(s["as_of_date"] for s in snaps)
    assert old.isoformat() not in dates, dates
    assert recent.isoformat() in dates, dates


def test_supp_bucket_emitted_when_supp_rows_present(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)
    main_rows = [_row(f"M{i}", [10, 11]) for i in range(20)]
    wl_rows = [{**_row(f"W{i}", [10, 11]), "data_source": "main_pipeline"}
               for i in range(5)] + \
              [{**_row(f"S{i}", [10, 11.5]), "data_source": "supplemental_yfinance"}
               for i in range(15)]
    rankings = {"open_date": "2026-05-01", "rows": main_rows}
    watchlist = {"open_date": "2026-05-01", "rows": wl_rows}

    report, _snaps = br.build_report(
        rankings, watchlist, fetch_benchmarks=False, today=date(2026, 5, 1))
    assert "supp_top10" in report["buckets"]
    assert "supp_top25" in report["buckets"]
    # Supp bucket has only 15 rows, so top25 size is 15
    assert report["buckets"]["supp_top25"]["size"] == 15


def test_concentration_finding_emitted(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)
    rows = [_row(f"T{i}", [10, 11], sector="Technology") for i in range(20)] + \
           [_row(f"X{i}", [10, 11], sector="Energy") for i in range(5)]
    rankings = {"open_date": "2026-05-01", "rows": rows}
    watchlist = {"open_date": "2026-05-01", "rows": []}
    report, _ = br.build_report(rankings, watchlist, fetch_benchmarks=False,
                                today=date(2026, 5, 1))
    findings = [f for f in report["findings"] if "main_top25" in f["name"]]
    assert findings and findings[0]["status"] == "WARN", findings


def test_market_context_override(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)
    fake = {
        "available": True,
        "tickers": {
            "SPY": {"last": 500.0, "return_21d": 0.02, "return_1d": 0.001,
                    "above_50dma": True, "above_200dma": True},
        },
    }
    rankings = {"open_date": "2026-05-01",
                "rows": [_row(f"T{i}", [10, 11]) for i in range(25)]}
    watchlist = {"open_date": "2026-05-01", "rows": []}
    report, _ = br.build_report(
        rankings, watchlist, fetch_benchmarks=False,
        market_context_override=fake, today=date(2026, 5, 1))
    assert report["market_context"]["available"] is True
    bc = report["benchmark_compare"]
    assert bc["available"] is True
    assert bc["spy_return_21d"] == 0.02


def test_render_html_produces_safe_string(tmp_path):
    _bench_review_with_tmp_snapshots(tmp_path)
    rankings = {"open_date": "2026-05-01",
                "rows": [_row(f"T{i}", [10, 11]) for i in range(10)]}
    watchlist = {"open_date": "2026-05-01", "rows": []}
    report, _ = br.build_report(rankings, watchlist, fetch_benchmarks=False,
                                today=date(2026, 5, 1))
    html = br._render_html(report)
    assert "Benchmark Review" in html
    assert "Forward-performance scaffold" in html
    assert "Internal model validation" in html


def test_trading_days_between_skips_weekends():
    # Fri 2026-05-01 -> Mon 2026-05-04 = 1 trading day
    assert br._trading_days_between(date(2026, 5, 1), date(2026, 5, 4)) == 1
    # Fri -> Fri (one week) = 5 trading days
    assert br._trading_days_between(date(2026, 5, 1), date(2026, 5, 8)) == 5
    # Same day = 0
    assert br._trading_days_between(date(2026, 5, 1), date(2026, 5, 1)) == 0


def test_snapshots_roundtrip(tmp_path):
    p = tmp_path / "s.jsonl"
    br.save_snapshots(
        [{"as_of_date": "2026-05-01", "buckets": {"main_top10": {"members": [], "size": 0}}}],
        path=p,
    )
    out = br.load_snapshots(p)
    assert len(out) == 1 and out[0]["as_of_date"] == "2026-05-01"

    # Tolerates a corrupt line in the middle
    p.write_text(p.read_text() + "{not json}\n" + json.dumps(
        {"as_of_date": "2026-05-02", "buckets": {}}) + "\n", encoding="utf-8")
    out2 = br.load_snapshots(p)
    assert len(out2) == 2


# ---- runner ----


def main():
    tmp_root = tempfile.mkdtemp(prefix="bench_review_test_")
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            # Tests that take a tmp_path get a unique subdir
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

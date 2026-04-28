"""Regression / simulation tests for export_to_json baseline policy.

Verifies:
  1. First run of a new day uses the EXISTING daily_open snapshot (which holds
     the prior trading day's ranking) as the comparison baseline, then
     overwrites it with today's full-universe ranking.
  2. Subsequent runs of the SAME day compare against today's open snapshot
     (intra-day movement). When ranks haven't moved, change = 0 (expected).
  3. Tickers absent from the baseline are treated as having entered from
     just outside the top 100, producing a positive change rather than 0.

Run: python 02_Code/Python/Scoring_Engine/test_export_to_json.py
"""
import os
import sys
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPORT_SCRIPT = REPO_ROOT / "02_Code" / "Python" / "Scoring_Engine" / "export_to_json.py"


def _ranking_csv(rows):
    header = (
        "Rank,Ticker,Name,Sector,Industry,Index,AI_Score,Technical,Fundamental,"
        "Sentiment,Risk,RSI,MACD_Hist,Above_SMA50,Above_SMA200,Golden_Cross,"
        "Short_Interest,Insider_Buying,MarketCap"
    )
    lines = [header]
    for rank, ticker in rows:
        lines.append(
            f"{rank},{ticker},{ticker} Inc,Tech,Software,SP500,"
            f"8.5,8.0,8.0,8.0,8.0,60.0,0.1,1,1,0,,False,1000000000"
        )
    return "\n".join(lines) + "\n"


def _setup_fake_repo(tmp, current_rows, baseline_rows=None, baseline_date=""):
    """Create a temporary repo layout that export_to_json.py can run against."""
    code_dir = tmp / "02_Code" / "Python" / "Scoring_Engine"
    out_dir = tmp / "data" / "processed" / "scoring_outputs"
    raw_dir = tmp / "data" / "raw" / "ohlcv_daily"
    code_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    raw_dir.mkdir(parents=True)

    # Copy the real script into the fake repo so its relative path math works.
    shutil.copy(EXPORT_SCRIPT, code_dir / "export_to_json.py")

    (out_dir / "rankings.csv").write_text(_ranking_csv(current_rows))

    if baseline_rows is not None:
        (out_dir / "rankings_daily_open.csv").write_text(_ranking_csv(baseline_rows))
    if baseline_date:
        (out_dir / "rankings_daily_open_date.txt").write_text(baseline_date)


def _run(tmp):
    proc = subprocess.run(
        [sys.executable, str(tmp / "02_Code" / "Python" / "Scoring_Engine" / "export_to_json.py")],
        capture_output=True,
        text=True,
        cwd=str(tmp),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"export_to_json failed:\n{proc.stdout}\n{proc.stderr}")
    out = json.loads((tmp / "data" / "rankings.json").read_text())
    return out, proc.stdout


def _changes_by_ticker(out):
    return {r["ticker"]: r["change"] for r in out["rows"]}


def test_first_run_compares_vs_prior_baseline():
    """First run of a new calendar day uses the existing snapshot as baseline."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Yesterday: AAPL #1, MSFT #2, GOOG #3
        baseline = [(1, "AAPL"), (2, "MSFT"), (3, "GOOG"), (4, "AMZN")]
        # Today: MSFT moves to #1, AAPL drops to #3
        current = [(1, "MSFT"), (2, "GOOG"), (3, "AAPL"), (4, "AMZN")]
        _setup_fake_repo(tmp, current, baseline, baseline_date="1999-01-01")

        out, log = _run(tmp)
        assert out["is_open_run"] is True, f"expected first run, got {out['is_open_run']}"
        changes = _changes_by_ticker(out)
        # MSFT was 2, now 1 -> +1; AAPL was 1, now 3 -> -2; GOOG was 3, now 2 -> +1
        assert changes["MSFT"] == 1, f"MSFT: {changes['MSFT']}"
        assert changes["AAPL"] == -2, f"AAPL: {changes['AAPL']}"
        assert changes["GOOG"] == 1, f"GOOG: {changes['GOOG']}"
        assert changes["AMZN"] == 0, f"AMZN: {changes['AMZN']}"
        # Today's baseline should now equal current rankings
        date_file = tmp / "data" / "processed" / "scoring_outputs" / "rankings_daily_open_date.txt"
        snap_file = tmp / "data" / "processed" / "scoring_outputs" / "rankings_daily_open.csv"
        assert date_file.read_text().strip() == out["open_date"]
        snap_first_lines = snap_file.read_text().splitlines()[1:5]
        snap_tickers = [line.split(",")[1] for line in snap_first_lines]
        assert snap_tickers == ["MSFT", "GOOG", "AAPL", "AMZN"], snap_tickers
        print("PASS: first_run_compares_vs_prior_baseline")


def test_subsequent_run_compares_vs_todays_open():
    """Within the same day, baseline is today's open snapshot — not yesterday."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Today's "open" snapshot was already saved this morning
        todays_open = [(1, "MSFT"), (2, "GOOG"), (3, "AAPL"), (4, "AMZN")]
        # By midday: GOOG climbed to #1, MSFT dropped to #2
        current = [(1, "GOOG"), (2, "MSFT"), (3, "AAPL"), (4, "AMZN")]

        # Use today's date so the script sees this as a SUBSEQUENT run.
        from datetime import datetime, timezone, timedelta
        utc_now = datetime.now(timezone.utc)
        offset = timedelta(hours=-5) if 3 <= utc_now.month <= 10 else timedelta(hours=-6)
        today = (utc_now + offset).strftime("%Y-%m-%d")

        _setup_fake_repo(tmp, current, todays_open, baseline_date=today)
        out, _ = _run(tmp)
        assert out["is_open_run"] is False, f"expected subsequent run, got {out['is_open_run']}"
        changes = _changes_by_ticker(out)
        # GOOG 2->1 = +1; MSFT 1->2 = -1; AAPL 3->3 = 0
        assert changes["GOOG"] == 1, f"GOOG: {changes['GOOG']}"
        assert changes["MSFT"] == -1, f"MSFT: {changes['MSFT']}"
        assert changes["AAPL"] == 0, f"AAPL: {changes['AAPL']}"
        print("PASS: subsequent_run_compares_vs_todays_open")


def test_subsequent_run_static_data_yields_zero():
    """When score inputs haven't changed, subsequent runs naturally produce zeros.

    This documents the live-site observation: same OHLCV in -> same ranks out
    -> change = 0 for everything. That is correct behavior, not a bug.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rows = [(1, "AAPL"), (2, "MSFT"), (3, "GOOG")]
        from datetime import datetime, timezone, timedelta
        utc_now = datetime.now(timezone.utc)
        offset = timedelta(hours=-5) if 3 <= utc_now.month <= 10 else timedelta(hours=-6)
        today = (utc_now + offset).strftime("%Y-%m-%d")

        _setup_fake_repo(tmp, rows, rows, baseline_date=today)
        out, _ = _run(tmp)
        changes = _changes_by_ticker(out)
        assert all(c == 0 for c in changes.values()), changes
        print("PASS: subsequent_run_static_data_yields_zero")


def test_missing_baseline_no_prior_snapshot():
    """First run with NO prior snapshot: change = 0 for all (graceful fallback)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        current = [(1, "AAPL"), (2, "MSFT"), (3, "GOOG")]
        _setup_fake_repo(tmp, current)  # no baseline, no date file
        out, _ = _run(tmp)
        assert out["is_open_run"] is True
        changes = _changes_by_ticker(out)
        assert all(c == 0 for c in changes.values()), changes
        print("PASS: missing_baseline_no_prior_snapshot")


def test_ticker_re_enters_top_100_gets_positive_change():
    """A ticker absent from the baseline (was outside top 100) but present
    in today's top 100 should show a positive change, not zero."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Baseline only has AAPL,MSFT,GOOG — NVDA is absent (was outside top N)
        baseline = [(1, "AAPL"), (2, "MSFT"), (3, "GOOG")]
        # Today NVDA shows up at rank 5
        current = [(1, "AAPL"), (2, "MSFT"), (3, "GOOG"), (4, "AMZN"), (5, "NVDA")]
        _setup_fake_repo(tmp, current, baseline, baseline_date="1999-01-01")
        out, _ = _run(tmp)
        changes = _changes_by_ticker(out)
        # NVDA was missing -> sentinel rank 101 -> change = 101 - 5 = 96
        assert changes["NVDA"] > 0, f"NVDA change should be positive, got {changes['NVDA']}"
        # AMZN also missing from baseline -> sentinel
        assert changes["AMZN"] > 0, f"AMZN change should be positive, got {changes['AMZN']}"
        # AAPL/MSFT/GOOG didn't move
        assert changes["AAPL"] == 0
        assert changes["MSFT"] == 0
        print("PASS: ticker_re_enters_top_100_gets_positive_change")


def main():
    test_first_run_compares_vs_prior_baseline()
    test_subsequent_run_compares_vs_todays_open()
    test_subsequent_run_static_data_yields_zero()
    test_missing_baseline_no_prior_snapshot()
    test_ticker_re_enters_top_100_gets_positive_change()
    print("\nAll baseline-policy regression tests passed.")


if __name__ == "__main__":
    main()

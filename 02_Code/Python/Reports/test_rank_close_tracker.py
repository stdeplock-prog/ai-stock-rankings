"""Tests for rank_close_tracker.py.

Covers:
  * upsert appends new dates and replaces same-day snapshots
  * retention caps the rolling list at MAX_SNAPSHOTS
  * dynamic ticker union (rows persist after a ticker drops out)
  * missing ticker/date cells render with em-dash
  * close-change falls back to prior-snapshot day-over-day when the
    intraday closes-array signal isn't available
  * stamp_task adds/updates the tasks.json row
  * HTML smoke (renders without error and contains expected headers)

Run: python 02_Code/Python/Reports/test_rank_close_tracker.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rank_close_tracker as rct  # noqa: E402


def _rankings(date: str, rows: list[dict]) -> dict:
    return {
        "as_of": f"{date} 03:35 PM CDT",
        "open_date": date,
        "is_open_run": False,
        "universe": "test",
        "rows": rows,
    }


def _row(rank: int, ticker: str, closes: list[float], ai_score: float = 8.0) -> dict:
    return {
        "rank": rank,
        "ticker": ticker,
        "ai_score": ai_score,
        "closes": closes,
    }


# ---------- snapshot build ----------


def test_build_today_snapshot_extracts_close_and_intraday_change():
    rk = _rankings("2026-05-27", [
        _row(1, "AAA", [100.0, 110.0]),
        _row(2, "BBB", [50.0]),
        _row(3, "CCC", []),
    ])
    snap = rct.build_today_snapshot(rk)
    assert snap is not None
    assert snap["date"] == "2026-05-27"
    aaa = snap["tickers"]["AAA"]
    assert aaa["close"] == 110.0
    assert aaa["prior_close"] == 100.0
    assert aaa["intraday_change_abs"] == 10.0
    assert abs(aaa["intraday_change_pct"] - 10.0) < 1e-9
    # Only one close in the array -> no intraday change
    bbb = snap["tickers"]["BBB"]
    assert bbb["close"] == 50.0
    assert bbb["intraday_change_pct"] is None
    # Empty closes -> close is None
    ccc = snap["tickers"]["CCC"]
    assert ccc["close"] is None


def test_build_today_snapshot_returns_none_for_empty_rankings():
    assert rct.build_today_snapshot(None) is None
    assert rct.build_today_snapshot({"rows": []}) is None
    assert rct.build_today_snapshot({"rows": "nope"}) is None


# ---------- upsert + retention ----------


def test_upsert_appends_new_date_and_replaces_same_day():
    snaps: list[dict] = []
    a = {"date": "2026-05-26", "tickers": {"AAA": {"ticker": "AAA", "rank": 1, "close": 10.0}}}
    snaps = rct.upsert_snapshot(snaps, a)
    assert [s["date"] for s in snaps] == ["2026-05-26"]

    b = {"date": "2026-05-27", "tickers": {"AAA": {"ticker": "AAA", "rank": 2, "close": 12.0}}}
    snaps = rct.upsert_snapshot(snaps, b)
    assert [s["date"] for s in snaps] == ["2026-05-26", "2026-05-27"]

    # Re-running on the same trading day overwrites that day's snapshot
    b2 = {"date": "2026-05-27", "tickers": {"AAA": {"ticker": "AAA", "rank": 3, "close": 12.5}}}
    snaps = rct.upsert_snapshot(snaps, b2)
    assert len(snaps) == 2
    latest = [s for s in snaps if s["date"] == "2026-05-27"][0]
    assert latest["tickers"]["AAA"]["rank"] == 3
    assert latest["tickers"]["AAA"]["close"] == 12.5


def test_retention_keeps_only_most_recent():
    snaps: list[dict] = []
    for i in range(35):
        d = f"2026-04-{i+1:02d}" if i < 30 else f"2026-05-{i-29:02d}"
        snaps = rct.upsert_snapshot(snaps, {"date": d, "tickers": {}}, max_keep=10)
    assert len(snaps) == 10
    # Latest must be present, earliest must have been trimmed
    dates = [s["date"] for s in snaps]
    assert dates == sorted(dates)
    assert "2026-04-01" not in dates


# ---------- dynamic universe + missing cells ----------


def test_dynamic_ticker_union_persists_dropped_tickers():
    snaps: list[dict] = []
    snaps = rct.upsert_snapshot(snaps, rct.build_today_snapshot(
        _rankings("2026-05-26", [_row(1, "AAA", [10.0, 11.0]), _row(2, "BBB", [20.0, 21.0])])
    ))
    snaps = rct.upsert_snapshot(snaps, rct.build_today_snapshot(
        _rankings("2026-05-27", [_row(1, "BBB", [21.0, 22.0]), _row(2, "CCC", [30.0, 31.0])])
    ))
    table = rct.build_table(snaps)
    tickers = [r["ticker"] for r in table["rows"]]
    # Union: AAA (dropped today), BBB (top today), CCC (new today)
    assert set(tickers) == {"AAA", "BBB", "CCC"}
    # Sort: ranked-today first by rank (BBB=1, CCC=2), then unranked (AAA)
    assert tickers == ["BBB", "CCC", "AAA"]
    # AAA's cell for 2026-05-27 must be present but empty (no rank/close)
    aaa = next(r for r in table["rows"] if r["ticker"] == "AAA")
    today_cell = next(c for c in aaa["cells"] if c["date"] == "2026-05-27")
    assert today_cell.get("rank") is None
    assert today_cell.get("close") is None


def test_missing_cells_render_em_dash():
    snaps = [
        rct.build_today_snapshot(_rankings("2026-05-26", [_row(1, "AAA", [10.0, 11.0])])),
        rct.build_today_snapshot(_rankings("2026-05-27", [_row(1, "BBB", [20.0, 21.0])])),
    ]
    table = rct.build_table(snaps)
    # AAA on 2026-05-27 has no rank -> rendered as em-dash
    aaa = next(r for r in table["rows"] if r["ticker"] == "AAA")
    today_cell = next(c for c in aaa["cells"] if c["date"] == "2026-05-27")
    assert rct._fmt_rank(today_cell.get("rank")) == "—"
    txt, _ = rct._fmt_close_change(today_cell)
    assert txt == "—"


# ---------- close-change fallback ----------


def test_close_change_falls_back_to_prior_snapshot_dod():
    # Ticker appears only with a single-element closes array on day 2 ->
    # intraday change is None, but day-over-day vs day 1 should fill it in.
    snaps = [
        rct.build_today_snapshot(_rankings("2026-05-26", [_row(1, "AAA", [100.0])])),
        rct.build_today_snapshot(_rankings("2026-05-27", [_row(1, "AAA", [110.0])])),
    ]
    table = rct.build_table(snaps)
    aaa = next(r for r in table["rows"] if r["ticker"] == "AAA")
    today_cell = next(c for c in aaa["cells"] if c["date"] == "2026-05-27")
    assert today_cell["intraday_change_pct"] is None
    assert today_cell["dod_change_pct"] is not None
    assert abs(today_cell["dod_change_pct"] - 10.0) < 1e-9
    assert today_cell["dod_compared_to"] == "2026-05-26"
    # Formatted cell carries the (vs ...) suffix and the pos class
    txt, css = rct._fmt_close_change(today_cell)
    assert css == "pos"
    assert "vs 2026-05-26" in txt


# ---------- summary ----------


def test_summary_reports_entrants_and_exits():
    snaps = [
        rct.build_today_snapshot(_rankings("2026-05-26", [
            _row(1, "AAA", [10.0, 11.0]),
            _row(2, "BBB", [20.0, 21.0]),
        ])),
        rct.build_today_snapshot(_rankings("2026-05-27", [
            _row(1, "BBB", [21.0, 22.0]),
            _row(2, "CCC", [30.0, 31.0]),
        ])),
    ]
    table = rct.build_table(snaps)
    summary = rct.summarize(table, snaps)
    assert summary["entrants_vs_prior"] == ["CCC"]
    assert summary["exits_vs_prior"] == ["AAA"]
    assert summary["snapshot_count"] == 2
    assert summary["ranked_today_count"] == 2
    assert summary["ticker_universe_count"] == 3


# ---------- JSONL round-trip ----------


def test_jsonl_roundtrip_preserves_snapshots(tmp_path: Path = None):
    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "snaps.jsonl"
        snaps = [
            rct.build_today_snapshot(_rankings("2026-05-26", [_row(1, "AAA", [10.0, 11.0])])),
            rct.build_today_snapshot(_rankings("2026-05-27", [_row(1, "AAA", [11.0, 12.0])])),
        ]
        rct._write_snapshots(path, snaps)
        loaded = rct._load_snapshots(path)
        assert [s["date"] for s in loaded] == ["2026-05-26", "2026-05-27"]
        assert loaded[1]["tickers"]["AAA"]["close"] == 12.0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- task stamping ----------


def test_stamp_task_adds_row_when_missing(tmp_path: Path = None):
    tmp = Path(tempfile.mkdtemp())
    try:
        tasks_path = tmp / "tasks.json"
        tasks_path.write_text(json.dumps({"tasks": [
            {"id": "other-task", "name": "Other", "status": "OK"}
        ]}), encoding="utf-8")
        orig = rct.TASKS_FILE
        try:
            rct.TASKS_FILE = tasks_path
            rct._stamp_task({
                "summary_line": "test summary",
                "generated_at_chicago": "2026-05-27 03:35 PM CDT",
            })
        finally:
            rct.TASKS_FILE = orig
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
        row = next(r for r in data["tasks"] if r["id"] == "rank-close-tracker")
        assert row["summary"] == "test summary"
        assert row["report_url"] == "./reports/rank-close-tracker.html"
        assert row["last_run"] == "2026-05-27 03:35 PM CDT"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_stamp_task_updates_existing_row(tmp_path: Path = None):
    tmp = Path(tempfile.mkdtemp())
    try:
        tasks_path = tmp / "tasks.json"
        tasks_path.write_text(json.dumps({"tasks": [
            {"id": "rank-close-tracker", "name": "Old", "status": "WARN", "summary": "old"}
        ]}), encoding="utf-8")
        orig = rct.TASKS_FILE
        try:
            rct.TASKS_FILE = tasks_path
            rct._stamp_task({
                "summary_line": "new summary",
                "generated_at_chicago": "2026-05-27 04:00 PM CDT",
            })
        finally:
            rct.TASKS_FILE = orig
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
        rows = [r for r in data["tasks"] if r["id"] == "rank-close-tracker"]
        assert len(rows) == 1
        assert rows[0]["summary"] == "new summary"
        assert rows[0]["status"] == "OK"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- HTML smoke ----------


def test_render_html_contains_expected_headers_and_classes():
    snaps = [
        rct.build_today_snapshot(_rankings("2026-05-26", [
            _row(1, "AAA", [100.0, 110.0]),
            _row(2, "BBB", [50.0, 49.0]),
        ])),
    ]
    table = rct.build_table(snaps)
    report = {
        "generated_at_chicago": "2026-05-27 03:35 PM CDT",
        "table": table,
        "summary": rct.summarize(table, snaps),
    }
    html = rct._render_html(report)
    assert "<title>Rank vs Close Tracker</title>" in html
    assert "AI Ranking - 05/26/2026" in html
    assert "Close &amp; Change - 05/26/2026" in html
    assert "AAA" in html and "BBB" in html
    assert "class='pos'" in html  # AAA: 100 -> 110 (+10%)
    assert "class='neg'" in html  # BBB: 50 -> 49 (-2%)


def test_render_html_handles_empty_snapshots():
    table = rct.build_table([])
    report = {
        "generated_at_chicago": "—",
        "table": table,
        "summary": rct.summarize(table, []),
    }
    html = rct._render_html(report)
    assert "No snapshots yet" in html


def test_fmt_close_change_signs_correctly():
    cell_pos = {"close": 100.0, "intraday_change_abs": 2.5, "intraday_change_pct": 2.5}
    txt, css = rct._fmt_close_change(cell_pos)
    assert css == "pos"
    assert "+2.50%" in txt
    assert "+$2.50" in txt
    cell_neg = {"close": 100.0, "intraday_change_abs": -1.5, "intraday_change_pct": -1.5}
    txt, css = rct._fmt_close_change(cell_neg)
    assert css == "neg"
    assert "-1.50%" in txt
    assert "-$1.50" in txt


# ---------- runner ----------


def main() -> int:
    tests = [
        test_build_today_snapshot_extracts_close_and_intraday_change,
        test_build_today_snapshot_returns_none_for_empty_rankings,
        test_upsert_appends_new_date_and_replaces_same_day,
        test_retention_keeps_only_most_recent,
        test_dynamic_ticker_union_persists_dropped_tickers,
        test_missing_cells_render_em_dash,
        test_close_change_falls_back_to_prior_snapshot_dod,
        test_summary_reports_entrants_and_exits,
        test_jsonl_roundtrip_preserves_snapshots,
        test_stamp_task_adds_row_when_missing,
        test_stamp_task_updates_existing_row,
        test_render_html_contains_expected_headers_and_classes,
        test_render_html_handles_empty_snapshots,
        test_fmt_close_change_signs_correctly,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # pragma: no cover — surface unexpected errors
            failed += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

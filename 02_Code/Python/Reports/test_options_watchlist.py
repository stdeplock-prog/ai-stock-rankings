"""Tests for options_watchlist.py — rolling research watchlist generator.

Covers:
  - candidate selection filters and composite ranking
  - overextended_bb exclusion
  - Pine blocker exclusion
  - earnings-within-7d caution + score penalty
  - external-confirm boost
  - HTML/JSON smoke
  - tasks.json stamping (no longer pointing at stale April markdown)

Run: python 02_Code/Python/Reports/test_options_watchlist.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import options_watchlist as ow  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _row(ticker, **kw):
    base = {
        "ticker": ticker,
        "company": f"{ticker} Co",
        "sector": "Technology",
        "ai_score": 9.0,
        "swing_score": 7.0,
        "volume_millions": 5.0,
        "go_label": "GO",
        "acc_label": "HIGH",
        "days_to_earnings": None,
        "next_earnings": None,
    }
    base.update(kw)
    return base


def test_filters_require_go():
    e = ow.evaluate_candidate(_row("AAA", go_label="NO-GO"), {}, set(), set())
    if e["include"]:
        fail("NO-GO should be rejected")


def test_filters_require_acc_high_or_mid():
    e = ow.evaluate_candidate(_row("AAA", acc_label="LOW"), {}, set(), set())
    if e["include"]:
        fail("acc_label=LOW should be rejected")
    e2 = ow.evaluate_candidate(_row("AAA", acc_label="MID"), {}, set(), set())
    if not e2["include"]:
        fail("acc_label=MID should be accepted")


def test_filters_require_ai_score_floor():
    e = ow.evaluate_candidate(_row("AAA", ai_score=7.99), {}, set(), set())
    if e["include"]:
        fail("ai_score<8 should be rejected")


def test_filters_require_volume_floor():
    e = ow.evaluate_candidate(_row("AAA", volume_millions=0.3), {}, set(), set())
    if e["include"]:
        fail("volume<0.5M should be rejected")


def test_overextended_excluded():
    e = ow.evaluate_candidate(_row("AAA"), {}, {"AAA"}, set())
    if e["include"]:
        fail("overextended_bb ticker should be rejected")


def test_pine_blocker_excluded():
    pine_idx = {"AAA": {"go_normalized": 0.9, "blocker_count": 1, "blockers": ["earnings_block"]}}
    e = ow.evaluate_candidate(_row("AAA"), pine_idx, set(), set())
    if e["include"]:
        fail("Pine blocker should reject candidate")


def test_pine_clean_go_boost():
    pine_idx_clean = {"AAA": {"go_normalized": 0.8, "blocker_count": 0, "blockers": []}}
    pine_idx_weak = {"AAA": {"go_normalized": 0.4, "blocker_count": 0, "blockers": []}}
    e_clean = ow.evaluate_candidate(_row("AAA"), pine_idx_clean, set(), set())
    e_weak = ow.evaluate_candidate(_row("AAA"), pine_idx_weak, set(), set())
    if not e_clean["pine_clean_go"]:
        fail("clean_go flag missing")
    if e_clean["composite"] <= e_weak["composite"]:
        fail(f"Pine clean-go should boost composite: clean={e_clean['composite']} weak={e_weak['composite']}")


def test_external_confirm_boost():
    e_plain = ow.evaluate_candidate(_row("AAA"), {}, set(), set())
    e_conf = ow.evaluate_candidate(_row("AAA"), {}, set(), {"AAA"})
    if not e_conf["external_confirms"]:
        fail("external_confirms flag missing")
    if e_conf["composite"] - e_plain["composite"] < 0.49:
        fail(f"external confirm should add ~0.5: delta={e_conf['composite']-e_plain['composite']}")


def test_earnings_caution_penalty_and_flag():
    e = ow.evaluate_candidate(_row("AAA", days_to_earnings=3, next_earnings="2026-06-01"), {}, set(), set())
    if not e["include"]:
        fail("near-earnings should still be included with caution")
    if not any("earnings" in c for c in e["cautions"]):
        fail("near-earnings caution missing")
    e_no = ow.evaluate_candidate(_row("AAA", days_to_earnings=60), {}, set(), set())
    if e["composite"] >= e_no["composite"]:
        fail("near-earnings should reduce composite vs far-earnings")


def test_select_candidates_top_n_and_ordering():
    rows = [_row(f"T{i:02d}", ai_score=8.0 + 0.1 * i) for i in range(20)]
    picks, rejected = ow.select_candidates(rows, {}, set(), set(), top_n=5)
    if len(picks) != 5:
        fail(f"expected top_n=5, got {len(picks)}")
    scores = [p["composite"] for p in picks]
    if scores != sorted(scores, reverse=True):
        fail(f"candidates not sorted descending: {scores}")
    if picks[0]["ticker"] != "T19":
        fail(f"top should be T19, got {picks[0]['ticker']}")


def test_build_universe_dedupes_and_prefers_watchlist_enrichment():
    main = [_row("AAA", ai_score=9.0), _row("BBB", ai_score=8.5)]
    wl = [_row("AAA", swing_score=8.5), _row("CCC", ai_score=8.2)]
    uni = ow.build_universe(main, wl)
    by_t = {r["ticker"]: r for r in uni}
    if set(by_t.keys()) != {"AAA", "BBB", "CCC"}:
        fail(f"universe tickers wrong: {set(by_t.keys())}")
    if by_t["AAA"]["_source"] != "both":
        fail("AAA should be tagged source=both")
    if by_t["AAA"]["swing_score"] != 8.5:
        fail("watchlist enrichment (swing_score) should win on dup")


def test_external_confirms_only_keep_action():
    pine = {
        "disagreement_supports_internal": [
            {"ticker": "AAA", "action": "keep"},
            {"ticker": "BBB", "action": "review"},
        ]
    }
    confirms = ow._build_external_confirms(pine)
    if confirms != {"AAA"}:
        fail(f"only 'keep' action should confirm; got {confirms}")


def test_build_report_smoke_with_fixture_data(tmpdir):
    rankings = {
        "as_of": "2026-05-27 10:25 PM CDT",
        "rows": [_row("META", ai_score=9.15, swing_score=6.9),
                 _row("BAD", go_label="NO-GO", ai_score=9.0)],
    }
    watchlist = {
        "as_of": "2026-05-27 10:26 PM CDT",
        "rows": [_row("NVDA", ai_score=8.8), _row("META", swing_score=7.0)],
    }
    pine = {
        "per_ticker": [
            {"ticker": "META", "go_no_go_score_normalized": 0.9, "blocker_count": 0, "blockers": []},
            {"ticker": "NVDA", "go_no_go_score_normalized": 0.4, "blocker_count": 0, "blockers": []},
        ],
        "disagreement_supports_internal": [{"ticker": "META", "action": "keep"}],
    }
    cooloff = {"current_cohort_members": {"overextended_bb": []}}
    tasks_json = {"tasks": [
        {"id": "options-earnings-watchlist", "name": "Options & Earnings Watchlist",
         "status": "OK", "summary": "stale April content", "last_run": "x",
         "next_run": "x", "schedule": "x", "report_url": "./reports/options-watchlist.html"}
    ]}

    base = Path(tmpdir)
    (base / "data" / "reports").mkdir(parents=True)
    (base / "reports").mkdir(parents=True)
    (base / "data" / "rankings.json").write_text(json.dumps(rankings))
    (base / "data" / "watchlist_rankings.json").write_text(json.dumps(watchlist))
    (base / "data" / "reports" / "pine_go_no_go_diagnostic.json").write_text(json.dumps(pine))
    (base / "data" / "reports" / "cooloff_cohort_tracking.json").write_text(json.dumps(cooloff))
    (base / "data" / "tasks.json").write_text(json.dumps(tasks_json))

    saved = {
        "RANKINGS_FILE": ow.RANKINGS_FILE,
        "WATCHLIST_FILE": ow.WATCHLIST_FILE,
        "PINE_FILE": ow.PINE_FILE,
        "COOLOFF_FILE": ow.COOLOFF_FILE,
        "EXTERNAL_FILE": ow.EXTERNAL_FILE,
        "JSON_OUT": ow.JSON_OUT,
        "HTML_OUT": ow.HTML_OUT,
        "TASKS_FILE": ow.TASKS_FILE,
    }
    try:
        ow.RANKINGS_FILE = base / "data" / "rankings.json"
        ow.WATCHLIST_FILE = base / "data" / "watchlist_rankings.json"
        ow.PINE_FILE = base / "data" / "reports" / "pine_go_no_go_diagnostic.json"
        ow.COOLOFF_FILE = base / "data" / "reports" / "cooloff_cohort_tracking.json"
        ow.EXTERNAL_FILE = base / "data" / "reports" / "external_benchmark_review.json"
        ow.JSON_OUT = base / "data" / "reports" / "options_watchlist.json"
        ow.HTML_OUT = base / "reports" / "options-watchlist.html"
        ow.TASKS_FILE = base / "data" / "tasks.json"

        rc = ow.render()
        if rc != 0:
            fail(f"render() returned {rc}")

        if not ow.JSON_OUT.exists():
            fail("JSON output not created")
        if not ow.HTML_OUT.exists():
            fail("HTML output not created")

        report = json.loads(ow.JSON_OUT.read_text())
        tickers = [c["ticker"] for c in report["candidates"]]
        if "META" not in tickers:
            fail(f"META should be a candidate, got {tickers}")
        if "BAD" in tickers:
            fail("NO-GO ticker should not be a candidate")

        html = ow.HTML_OUT.read_text()
        if "Options &amp; Earnings Watchlist" not in html:
            fail("HTML missing title")
        if "highest-conviction-options-calls-2026-04-28" in html:
            fail("HTML still references stale April markdown source")
        if "META" not in html:
            fail("HTML missing top candidate")

        tasks = json.loads((base / "data" / "tasks.json").read_text())
        row = next(r for r in tasks["tasks"] if r["id"] == "options-earnings-watchlist")
        if "April 28, 2026" in row.get("summary", ""):
            fail(f"tasks.json summary still references stale April source: {row['summary']}")
        if "candidates" not in row.get("summary", ""):
            fail(f"tasks.json summary should reflect rolling output, got: {row['summary']}")
        if row.get("report_url") != "./reports/options-watchlist.html":
            fail(f"tasks.json report_url wrong: {row.get('report_url')}")
    finally:
        for k, v in saved.items():
            setattr(ow, k, v)


def test_repo_no_longer_renders_static_april_markdown():
    """Guards against regression: the rolling generator should not import
    the static April markdown as its content source."""
    src = (Path(HERE) / "options_watchlist.py").read_text()
    if "highest-conviction-options-calls-2026-04-28.md" in src:
        fail("options_watchlist.py still references the stale April markdown")
    if "import markdown" in src:
        fail("options_watchlist.py still imports `markdown` (static-md renderer leftover)")


def main():
    test_filters_require_go()
    test_filters_require_acc_high_or_mid()
    test_filters_require_ai_score_floor()
    test_filters_require_volume_floor()
    test_overextended_excluded()
    test_pine_blocker_excluded()
    test_pine_clean_go_boost()
    test_external_confirm_boost()
    test_earnings_caution_penalty_and_flag()
    test_select_candidates_top_n_and_ordering()
    test_build_universe_dedupes_and_prefers_watchlist_enrichment()
    test_external_confirms_only_keep_action()
    with tempfile.TemporaryDirectory() as d:
        test_build_report_smoke_with_fixture_data(d)
    test_repo_no_longer_renders_static_april_markdown()
    print("OK: all options_watchlist tests passed")


if __name__ == "__main__":
    main()

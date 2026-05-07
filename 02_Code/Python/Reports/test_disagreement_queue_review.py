"""Fixture-based tests for disagreement_queue_review.py.

Run: python 02_Code/Python/Reports/test_disagreement_queue_review.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import disagreement_queue_review as dqr  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# ---------- helpers ----------


def _queue_entry(ticker, source="fidelity", severity="severe", gap=-3.0,
                 n_sources=2, ai=7.5, direction="bullish",
                 extra_signals=None):
    sources = [source]
    signals = [{
        "source": source, "external_value": 1.0, "external_label": "Bearish",
        "internal_value_1to5": 4.0, "primary_internal_field": "ai_score",
        "gap": gap, "severity": severity, "direction_agrees": False,
    }]
    for s in extra_signals or []:
        signals.append(s)
        if s.get("source") and s["source"] not in sources:
            sources.append(s["source"])
    return {
        "ticker": ticker, "sector": "Tech",
        "internal_ai_score_0to10": ai,
        "internal_ai_direction": direction,
        "headline_source": source,
        "headline_severity": severity,
        "headline_gap": gap,
        "reason": f"{source} bearish vs internal",
        "confidence_n_sources": n_sources,
        "sources_flagging": sources,
        "external_signals": signals,
        "marketbeat_target": None,
        "reviewed": False,
        "notes": "",
    }


def _pine_entry(ticker, score=0.8, classification="supports_internal",
                blockers=None):
    return {
        "ticker": ticker, "evaluated": True, "sector": "Tech",
        "ai_score": 7.0, "go_no_go_score_normalized": score,
        "blockers": list(blockers or []),
        "disagreement": {
            "classification": classification,
            "action": "—",
            "rationale": "test",
        },
    }


# ---------- key + suggestion logic ----------


def test_make_key_is_stable_and_normalized():
    assert dqr.make_key("ECG", "FIDELITY") == "ECG|fidelity"
    assert dqr.make_key("  bwa ", "Marketbeat") == "BWA|marketbeat"
    assert dqr.make_key("", "fidelity") == ""


def test_suggest_supports_internal_severe_returns_needs_more_data():
    q = _queue_entry("AAA", severity="severe")
    p = _pine_entry("AAA", classification="supports_internal", score=0.9)
    decision, _ = dqr.suggest_decision(queue_entry=q, pine_entry=p,
                                       cooloff_blockers=None)
    assert decision == "needs_more_data", decision


def test_suggest_supports_internal_strong_returns_keep():
    q = _queue_entry("AAA", severity="strong", gap=-2.0)
    p = _pine_entry("AAA", classification="supports_internal", score=0.9)
    decision, _ = dqr.suggest_decision(queue_entry=q, pine_entry=p,
                                       cooloff_blockers=None)
    assert decision == "keep", decision


def test_suggest_supports_external_caution_severe_returns_watchlist():
    q = _queue_entry("BBB", severity="severe", n_sources=2,
                     extra_signals=[{
                         "source": "zacks", "external_value": 2,
                         "internal_value_1to5": 4.0,
                         "primary_internal_field": "ai_score",
                         "gap": -2.0, "severity": "strong",
                         "direction_agrees": False,
                     }])
    p = _pine_entry("BBB", classification="supports_external_caution",
                    score=0.7,
                    blockers=["overextended_bb (>1.5% above BB upper)"])
    decision, _ = dqr.suggest_decision(queue_entry=q, pine_entry=p,
                                       cooloff_blockers=["overextended_bb"])
    assert decision == "watchlist_only", decision


def test_suggest_weak_pine_multi_bearish_returns_watchlist():
    q = _queue_entry("CCC", severity="strong", n_sources=3,
                     extra_signals=[
                         {"source": "zacks", "gap": -2.0,
                          "severity": "strong", "direction_agrees": False},
                         {"source": "marketbeat", "gap": -1.5,
                          "severity": "strong", "direction_agrees": False},
                     ])
    p = _pine_entry("CCC", classification="other", score=0.2)
    decision, _ = dqr.suggest_decision(queue_entry=q, pine_entry=p,
                                       cooloff_blockers=None)
    assert decision == "watchlist_only", decision


def test_suggest_no_pine_returns_needs_more_data():
    q = _queue_entry("DDD", severity="moderate")
    decision, _ = dqr.suggest_decision(queue_entry=q, pine_entry=None,
                                       cooloff_blockers=None)
    assert decision == "needs_more_data", decision


# ---------- merge_state ----------


def test_merge_state_new_item_marks_first_seen_today():
    queue = [_queue_entry("AAA")]
    state = dqr.merge_state(prior={}, queue=queue, today_iso="2026-05-07")
    key = dqr.make_key("AAA", "fidelity")
    assert key in state
    e = state[key]
    assert e["first_seen"] == "2026-05-07"
    assert e["last_seen"] == "2026-05-07"
    assert e["current"] is True
    assert e["reviewed"] is False
    assert e["decision"] == ""


def test_merge_state_preserves_prior_decision_and_notes():
    key = dqr.make_key("AAA", "fidelity")
    prior = {key: {
        "key": key, "ticker": "AAA",
        "reviewed": True, "decision": "watchlist_only",
        "notes": "Sector weakness, hold off",
        "reviewed_at": "2026-05-06T15:00:00Z",
        "follow_up_date": "2026-05-13",
        "first_seen": "2026-05-01", "last_seen": "2026-05-06",
        "current": True,
    }}
    queue = [_queue_entry("AAA")]
    state = dqr.merge_state(prior=prior, queue=queue, today_iso="2026-05-07")
    e = state[key]
    assert e["reviewed"] is True
    assert e["decision"] == "watchlist_only"
    assert e["notes"] == "Sector weakness, hold off"
    assert e["reviewed_at"] == "2026-05-06T15:00:00Z"
    assert e["follow_up_date"] == "2026-05-13"
    assert e["first_seen"] == "2026-05-01"  # untouched
    assert e["last_seen"] == "2026-05-07"   # bumped
    assert e["current"] is True


def test_merge_state_disappeared_item_marked_not_current():
    key = dqr.make_key("AAA", "fidelity")
    prior = {key: {
        "key": key, "ticker": "AAA",
        "reviewed": True, "decision": "ignore",
        "notes": "noise",
        "first_seen": "2026-04-30", "last_seen": "2026-05-06",
        "current": True,
    }}
    queue = []  # AAA gone today
    state = dqr.merge_state(prior=prior, queue=queue, today_iso="2026-05-07")
    e = state[key]
    assert e["current"] is False
    assert e["decision"] == "ignore"        # preserved
    assert e["notes"] == "noise"            # preserved
    assert e["last_seen"] == "2026-05-06"   # untouched (no fresh sighting)
    assert e["first_seen"] == "2026-04-30"


def test_merge_state_reappearance_keeps_history_and_refreshes():
    key = dqr.make_key("AAA", "fidelity")
    prior = {key: {
        "key": key, "ticker": "AAA",
        "reviewed": True, "decision": "needs_more_data",
        "notes": "watch earnings",
        "first_seen": "2026-04-15", "last_seen": "2026-04-30",
        "current": False,
    }}
    queue = [_queue_entry("AAA")]
    state = dqr.merge_state(prior=prior, queue=queue, today_iso="2026-05-07")
    e = state[key]
    assert e["current"] is True
    assert e["decision"] == "needs_more_data"
    assert e["notes"] == "watch earnings"
    assert e["first_seen"] == "2026-04-15"
    assert e["last_seen"] == "2026-05-07"


def test_merge_state_invalid_decision_normalized_to_blank():
    key = dqr.make_key("AAA", "fidelity")
    prior = {key: {
        "key": key, "ticker": "AAA",
        "reviewed": True, "decision": "bogus_value",
        "first_seen": "2026-05-01",
    }}
    queue = [_queue_entry("AAA")]
    state = dqr.merge_state(prior=prior, queue=queue, today_iso="2026-05-07")
    assert state[key]["decision"] == ""


# ---------- build_report end-to-end ----------


def test_build_report_summary_counts_and_sorting():
    queue = [
        _queue_entry("ALPHA", severity="severe", n_sources=3),
        _queue_entry("BETA", severity="strong", n_sources=2),
        _queue_entry("GAMMA", severity="moderate", n_sources=1),
        _queue_entry("DELTA", severity="severe", n_sources=1),
    ]
    pine = {"per_ticker": [
        _pine_entry("ALPHA", classification="supports_internal", score=0.9),
        _pine_entry("BETA", classification="supports_external_caution",
                    score=0.6, blockers=["overextended_bb"]),
        _pine_entry("DELTA", classification="other", score=0.3),
    ]}
    cool = {"current_cohort_members": {"overextended_bb": ["BETA"]}}

    # one prior reviewed entry that's still in queue
    key_alpha = dqr.make_key("ALPHA", "fidelity")
    prior = {key_alpha: {
        "key": key_alpha, "ticker": "ALPHA",
        "reviewed": True, "decision": "keep",
        "notes": "earnings beat", "first_seen": "2026-05-01",
        "last_seen": "2026-05-06", "current": True,
    }}

    report, state = dqr.build_report(
        queue_payload={"queue": queue},
        external_payload={"metrics_by_source": {"fidelity": {}, "zacks": {}}},
        pine_payload=pine,
        cooloff_payload=cool,
        prior_state=prior,
        today_iso="2026-05-07",
    )

    s = report["summary_counts"]
    assert s["total"] == 4
    assert s["severe"] == 2
    assert s["strong"] == 1
    assert s["reviewed"] == 1, s
    assert s["unresolved"] == 3
    assert s["decisions"]["keep"] == 1

    rows = report["rows"]
    # first row should be a severe one with most sources flagging
    assert rows[0]["ticker"] == "ALPHA", [r["ticker"] for r in rows]
    # moderate falls to the bottom
    assert rows[-1]["ticker"] == "GAMMA", [r["ticker"] for r in rows]

    # state: every queue ticker present, all current
    assert {dqr.make_key(t, "fidelity") for t in
            ("ALPHA", "BETA", "GAMMA", "DELTA")} <= set(state.keys())

    # BETA gets cool-off blocker pulled in
    beta_row = next(r for r in rows if r["ticker"] == "BETA")
    assert beta_row["cooloff_blockers"] == ["overextended_bb"]
    assert beta_row["pine_classification"] == "supports_external_caution"

    # ALPHA carries prior decision
    alpha_row = next(r for r in rows if r["ticker"] == "ALPHA")
    assert alpha_row["review"]["decision"] == "keep"
    assert alpha_row["review"]["reviewed"] is True


def test_build_report_handles_missing_inputs_gracefully():
    report, state = dqr.build_report(
        queue_payload=None, external_payload=None,
        pine_payload=None, cooloff_payload=None,
        prior_state={}, today_iso="2026-05-07")
    assert report["summary_counts"]["total"] == 0
    assert report["overall"] == "OK"
    assert state == {}


def test_state_disk_payload_roundtrip(tmp_path):
    queue = [_queue_entry("ALPHA")]
    report, state = dqr.build_report(
        queue_payload={"queue": queue}, external_payload=None,
        pine_payload=None, cooloff_payload=None,
        prior_state={}, today_iso="2026-05-07")
    payload = dqr.state_to_disk_payload(state, report["generated_at"])
    path = tmp_path / "state.json"
    dqr.save_state(payload, path=path)
    loaded = dqr.load_state(path)
    key = dqr.make_key("ALPHA", "fidelity")
    assert key in loaded
    assert loaded[key]["ticker"] == "ALPHA"
    assert loaded[key]["first_seen"] == "2026-05-07"


# ---------- HTML smoke ----------


def test_render_html_produces_expected_strings():
    queue = [
        _queue_entry("ALPHA", severity="severe"),
        _queue_entry("BETA", severity="strong"),
    ]
    pine = {"per_ticker": [
        _pine_entry("ALPHA", classification="supports_internal"),
    ]}
    report, _ = dqr.build_report(
        queue_payload={"queue": queue}, external_payload=None,
        pine_payload=pine, cooloff_payload=None,
        prior_state={}, today_iso="2026-05-07")
    html = dqr._render_html(report)
    assert "Disagreement Queue Review" in html
    assert "ALPHA" in html and "BETA" in html
    assert "Unresolved severe queue" in html
    assert "Reviewed decisions" in html
    assert "review suggestions" in html.lower()


# ---------- runner ----------


def main():
    tmp_root = tempfile.mkdtemp(prefix="dqr_test_")
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

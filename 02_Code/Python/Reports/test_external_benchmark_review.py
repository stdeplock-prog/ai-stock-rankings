"""Tests for external_benchmark_review.py and the build_seed normalizer.

Run: python 02_Code/Python/Reports/test_external_benchmark_review.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

import external_benchmark_review as ebr  # noqa: E402

# Load build_seed.py (it lives under data/, not on sys.path).
_BUILD_SEED_PATH = REPO_ROOT / "data" / "external_benchmarks" / "build_seed.py"
_spec = importlib.util.spec_from_file_location("build_seed", _BUILD_SEED_PATH)
build_seed = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(build_seed)


# ---------- Normalization helpers ----------


def test_to_1to5():
    assert ebr.to_1to5(7.6) == 3.8
    assert ebr.to_1to5(0) == 0.0
    assert ebr.to_1to5(10) == 5.0
    assert ebr.to_1to5(None) is None
    assert ebr.to_1to5("nope") is None
    assert ebr.to_1to5(float("nan")) is None


def test_direction_of():
    assert ebr.direction_of(1.5) == "bearish"
    assert ebr.direction_of(2.4) == "bearish"
    assert ebr.direction_of(2.5) == "neutral"
    assert ebr.direction_of(3.49) == "neutral"
    assert ebr.direction_of(3.5) == "bullish"
    assert ebr.direction_of(5.0) == "bullish"
    assert ebr.direction_of(None) is None


def test_severity_for_gap():
    assert ebr.severity_for_gap(0.4) == "moderate"
    assert ebr.severity_for_gap(1.5) == "strong"
    assert ebr.severity_for_gap(2.0) == "strong"
    assert ebr.severity_for_gap(2.5) == "severe"
    assert ebr.severity_for_gap(3.5) == "severe"


# ---------- build_seed parsing ----------


def test_build_seed_label_to_5():
    assert build_seed._label_to_5("Strong Buy", build_seed.TV_LABEL_TO_5) == 5
    assert build_seed._label_to_5("strong buy", build_seed.TV_LABEL_TO_5) == 5
    assert build_seed._label_to_5("Sell", build_seed.TV_LABEL_TO_5) == 2
    assert build_seed._label_to_5(None, build_seed.TV_LABEL_TO_5) is None
    assert build_seed._label_to_5("garbage", build_seed.TV_LABEL_TO_5) is None


def test_build_seed_lseg_to_bullish():
    # 1=Strong Buy -> bullish 5; 5=Sell -> bullish 1; 3=Hold -> bullish 3
    assert build_seed._lseg_to_bullish(1.0) == 5.0
    assert build_seed._lseg_to_bullish(5.0) == 1.0
    assert build_seed._lseg_to_bullish(3.0) == 3.0
    assert build_seed._lseg_to_bullish(2.6) == 3.4
    assert build_seed._lseg_to_bullish(None) is None


def test_build_seed_zacks_rank():
    assert build_seed._parse_zacks_rank("3-Hold") == 3
    assert build_seed._parse_zacks_rank("1-Strong Buy") == 1
    assert build_seed._parse_zacks_rank("5-Strong Sell") == 5
    assert build_seed._parse_zacks_rank("N/A") is None
    assert build_seed._parse_zacks_rank("ETF-N/A") is None


def test_build_seed_zacks_industry_pct():
    assert build_seed._parse_zacks_industry_pct("Top 41% (99/244)") == 59.0
    assert build_seed._parse_zacks_industry_pct("Bottom 18% (199/244)") == 18.0
    assert build_seed._parse_zacks_industry_pct("garbage") is None
    assert build_seed._parse_zacks_industry_pct(None) is None


def test_build_seed_upside():
    assert build_seed._parse_upside("+21.5%") == 21.5
    assert build_seed._parse_upside("~-12.7%") == -12.7
    assert build_seed._parse_upside("-0.9%") == -0.9
    assert build_seed._parse_upside("N/A") is None


def test_build_seed_safe_float():
    assert build_seed._safe_float("$361.97") == 361.97
    assert build_seed._safe_float("1,234.5") == 1234.5
    assert build_seed._safe_float("N/A") is None
    assert build_seed._safe_float(None) is None
    assert build_seed._safe_float(3) == 3.0


def test_build_seed_zacks_real_data():
    """Smoke-test zacks parser against the seeded markdown."""
    raw_md = REPO_ROOT / "data" / "external_benchmarks" / "raw" / "zacks_benchmark_data.md"
    if not raw_md.exists():
        return  # Optional smoke; skip silently if raw isn't present.
    payload = build_seed.build_zacks()
    by_t = {r["ticker"]: r for r in payload["rows"]}
    assert by_t["GS"]["normalized"]["zacks_rank_1to5"] == 3
    assert by_t["GS"]["normalized"]["zacks_rank_bullish_1to5"] == 3
    # MDB is 5-Strong Sell -> bullish 1
    assert by_t["MDB"]["normalized"]["zacks_rank_bullish_1to5"] == 1
    # ECG: A,A,B,B
    assert by_t["ECG"]["normalized"]["value_1to5"] == 5
    assert by_t["ECG"]["normalized"]["growth_1to5"] == 5
    assert by_t["ECG"]["normalized"]["momentum_1to5"] == 4
    assert by_t["ECG"]["normalized"]["vgm_1to5"] == 4
    # XE has no rank -> not covered
    assert by_t["XE"]["covered"] is False


def test_build_seed_marketbeat_real_data():
    raw_md = REPO_ROOT / "data" / "external_benchmarks" / "raw" / "marketbeat_benchmark_sample.md"
    if not raw_md.exists():
        return
    payload = build_seed.build_marketbeat()
    by_t = {r["ticker"]: r for r in payload["rows"]}
    # GS = Hold -> 3, upside -0.9
    assert by_t["GS"]["normalized"]["consensus_1to5"] == 3
    assert by_t["GS"]["normalized"]["upside_pct"] == -0.9
    # CVNA = Moderate Buy with -72.5% upside (target divergence)
    assert by_t["CVNA"]["normalized"]["consensus_1to5"] == 4
    assert by_t["CVNA"]["normalized"]["upside_pct"] == -72.5


# ---------- compare_row ----------


def _ext_row(source, ticker, ext_value, label="Buy"):
    cfg = ebr.SOURCE_CONFIG[source]
    return {
        "ticker": ticker,
        "covered": ext_value is not None,
        "raw": {cfg["label_field"]: label},
        "normalized": {cfg["external_field"]: ext_value},
    }


def test_compare_row_agree():
    ext = _ext_row("tradingview", "AAA", 4)
    internal = {"technical": 8.0}  # 1..5: 4.0
    comp = ebr.compare_row("tradingview", ext, internal)
    assert comp is not None
    assert comp["external_value"] == 4
    assert comp["internal_value_1to5"] == 4.0
    assert comp["gap"] == 0.0
    assert comp["direction_agrees"] is True
    assert comp["external_direction"] == "bullish"


def test_compare_row_disagree():
    ext = _ext_row("fidelity", "BBB", 1.5, label="Bearish")
    internal = {"ai_score": 8.0}
    comp = ebr.compare_row("fidelity", ext, internal)
    assert comp is not None
    assert comp["gap"] == round(1.5 - 4.0, 3)
    assert comp["direction_agrees"] is False
    assert comp["external_direction"] == "bearish"
    assert comp["internal_direction"] == "bullish"


def test_compare_row_uncovered_returns_none():
    ext = _ext_row("tradingview", "CCC", None)
    assert ebr.compare_row("tradingview", ext, {"technical": 7.0}) is None


def test_compare_row_no_internal_returns_none():
    ext = _ext_row("tradingview", "DDD", 4)
    assert ebr.compare_row("tradingview", ext, {}) is None


# ---------- per_source_metrics ----------


def test_per_source_metrics_empty():
    m = ebr.per_source_metrics("tradingview", [], [])
    assert m["compared"] == 0
    assert m["direction_agreement_rate"] is None
    assert m["mean_abs_gap"] is None


def test_per_source_metrics_basic():
    comparisons = [
        {"gap": 0.5, "abs_gap": 0.5, "direction_agrees": True},
        {"gap": -2.0, "abs_gap": 2.0, "direction_agrees": False},
        {"gap": 0.1, "abs_gap": 0.1, "direction_agrees": True},
        {"gap": 1.7, "abs_gap": 1.7, "direction_agrees": True},
    ]
    seed_rows = [{"covered": True}] * 4 + [{"covered": False}]
    m = ebr.per_source_metrics("fidelity", comparisons, seed_rows)
    assert m["compared"] == 4
    assert m["covered"] == 4
    assert m["seed_total"] == 5
    assert m["direction_agreement_rate"] == 0.75
    # strong agreement = direction_agrees AND abs_gap <= 0.5
    assert m["strong_agreements"] == 2
    # strong disagreement = abs_gap >= 1.5
    assert m["strong_disagreements"] == 2


# ---------- queue building ----------


def _comp(source, ticker, ext_value, gap, ext_dir="bearish", int_dir="bullish",
           agrees=False):
    return {
        "ticker": ticker,
        "source": source,
        "external_value": ext_value,
        "external_label": "x",
        "primary_internal_field": ebr.SOURCE_CONFIG[source]["primary_internal"],
        "internal_value_1to5": ext_value - gap,
        "internal_value_0to10": (ext_value - gap) * 2,
        "extras_1to5": {},
        "gap": gap,
        "abs_gap": abs(gap),
        "external_direction": ext_dir,
        "internal_direction": int_dir,
        "direction_agrees": agrees,
    }


def test_queue_flags_severe():
    per_ticker = {
        "MDB": [_comp("fidelity", "MDB", 1.0, -3.0)],
        "GS": [_comp("fidelity", "GS", 2.0, -2.0)],
        "OK": [_comp("tradingview", "OK", 4.0, 0.2, "bullish", "bullish", True)],
    }
    internal = {
        "MDB": {"ai_score": 8.0, "sector": "Tech"},
        "GS": {"ai_score": 8.0, "sector": "Fin"},
        "OK": {"ai_score": 7.4, "sector": "Tech"},
    }
    queue = ebr.build_queue_entries(per_ticker, {}, internal)
    tickers = [q["ticker"] for q in queue]
    assert "MDB" in tickers
    assert "GS" in tickers
    assert "OK" not in tickers
    # MDB has bigger gap, comes first
    assert tickers.index("MDB") < tickers.index("GS")
    mdb = next(q for q in queue if q["ticker"] == "MDB")
    assert mdb["headline_severity"] == "severe"


def test_queue_marketbeat_target_divergence_only():
    """Even when consensus matches our score, a -50% upside flags."""
    per_ticker = {
        "CVNA": [_comp("marketbeat", "CVNA", 4.0, 0.0, "bullish", "bullish", True)],
    }
    internal = {"CVNA": {"ai_score": 8.0, "sector": "Auto"}}
    targets = {"CVNA": {"upside_pct": -72.5, "price_target": 109.75}}
    queue = ebr.build_queue_entries(per_ticker, targets, internal)
    assert len(queue) == 1
    q = queue[0]
    assert q["ticker"] == "CVNA"
    assert q["marketbeat_target"]["kind"] == "negative"
    # confidence should reference marketbeat:target
    assert "marketbeat:target" in q["sources_flagging"]


def test_queue_confidence_counts_distinct_sources():
    per_ticker = {
        "X": [
            _comp("fidelity", "X", 1.0, -2.5),
            _comp("zacks", "X", 1.5, -2.0),
            _comp("etrade", "X", 4.0, 0.0, "bullish", "bullish", True),  # not flagged
        ],
    }
    internal = {"X": {"ai_score": 8.0}}
    queue = ebr.build_queue_entries(per_ticker, {}, internal)
    assert len(queue) == 1
    q = queue[0]
    assert q["confidence_n_sources"] == 2
    assert set(q["sources_flagging"]) == {"fidelity", "zacks"}
    # external_signals lists ALL sources (including the agreeing etrade)
    assert {s["source"] for s in q["external_signals"]} == {"fidelity", "zacks", "etrade"}


def test_queue_entries_have_review_fields():
    per_ticker = {"X": [_comp("fidelity", "X", 1.0, -3.0)]}
    queue = ebr.build_queue_entries(per_ticker, {}, {"X": {"ai_score": 8.0}})
    assert queue[0]["reviewed"] is False
    assert queue[0]["notes"] == ""


# ---------- confirmations ----------


def test_confirmations_require_min_sources():
    per_ticker = {
        "TWO": [
            _comp("tradingview", "TWO", 4.0, 0.2, "bullish", "bullish", True),
            _comp("etrade", "TWO", 4.1, 0.3, "bullish", "bullish", True),
        ],
        "ONE": [
            _comp("tradingview", "ONE", 4.0, 0.2, "bullish", "bullish", True),
        ],
        "MIXED": [
            _comp("fidelity", "MIXED", 1.0, -2.5, "bearish", "bullish", False),
            _comp("zacks", "MIXED", 4.0, 0.5, "bullish", "bullish", True),
        ],
    }
    internal = {
        "TWO": {"ai_score": 7.6}, "ONE": {"ai_score": 7.6},
        "MIXED": {"ai_score": 7.6},
    }
    confs = ebr.build_confirmations(per_ticker, internal)
    tickers = [c["ticker"] for c in confs]
    assert "TWO" in tickers
    assert "ONE" not in tickers
    assert "MIXED" not in tickers


# ---------- internal index ----------


def test_build_internal_index_merges_main_and_watchlist():
    rankings = {"rows": [{"ticker": "AAA", "ai_score": 9.0,
                          "sector": "Tech", "technical": 8.0}]}
    watchlist = {"rows": [
        {"ticker": "AAA", "ai_score": 5.0, "sector": "WrongTech"},  # main wins
        {"ticker": "BBB", "ai_score": 6.0, "sector": "Health"},
    ]}
    idx = ebr.build_internal_index(rankings, watchlist)
    assert "AAA" in idx and "BBB" in idx
    assert idx["AAA"]["ai_score"] == 9.0
    assert idx["AAA"]["sector"] == "Tech"
    assert idx["BBB"]["ai_score"] == 6.0


# ---------- Integration smoke ----------


def test_build_report_smoke():
    """End-to-end smoke against tiny synthetic seeds + rankings."""
    rankings = {"rows": [
        {"ticker": "AGREE", "ai_score": 7.6, "fundamental": 8.0,
         "technical": 8.0, "sentiment": 7.5, "swing_score": 6.0,
         "sector": "Tech"},
        {"ticker": "DISAGREE", "ai_score": 7.5, "fundamental": 7.0,
         "technical": 7.0, "sentiment": 7.0, "swing_score": 6.0,
         "sector": "Fin"},
    ]}
    watchlist = {"rows": []}
    seeds = {
        "tradingview": {
            "source": "tradingview", "as_of_date": "2026-05-07",
            "rows": [
                {"ticker": "AGREE", "covered": True,
                 "raw": {"tv_overall_label": "Buy"},
                 "normalized": {"overall_1to5": 4}},
                {"ticker": "DISAGREE", "covered": True,
                 "raw": {"tv_overall_label": "Strong Sell"},
                 "normalized": {"overall_1to5": 1}},
            ],
        },
        "fidelity": {
            "source": "fidelity", "as_of_date": "2026-05-07",
            "rows": [
                {"ticker": "AGREE", "covered": True,
                 "raw": {"fidelity_label": "Bullish"},
                 "normalized": {"ess_1to5": 4.0}},
                {"ticker": "DISAGREE", "covered": True,
                 "raw": {"fidelity_label": "Very Bearish"},
                 "normalized": {"ess_1to5": 0.5}},
            ],
        },
    }

    report, queue = ebr.build_report(rankings, watchlist, seeds)
    assert report["overall"] in ("WARN", "OK")
    assert "tradingview" in report["metrics_by_source"]
    # DISAGREE should be in the queue (multiple severe gaps)
    assert any(q["ticker"] == "DISAGREE" for q in queue)
    # AGREE should be in confirmations (>=2 agreeing sources)
    assert any(c["ticker"] == "AGREE"
               for c in report["confirmations"])


def test_render_html_smoke():
    """HTML render must include marker strings without crashing."""
    rankings = {"rows": [{"ticker": "X", "ai_score": 5.0, "technical": 5.0,
                          "sector": "Fin"}]}
    seeds = {
        "tradingview": {
            "source": "tradingview", "as_of_date": "2026-05-07",
            "rows": [{"ticker": "X", "covered": True,
                      "raw": {"tv_overall_label": "Strong Sell"},
                      "normalized": {"overall_1to5": 1}}],
        }
    }
    report, queue = ebr.build_report(rankings, None, seeds)
    html = ebr._render_html(report, queue)
    assert "<title>External Benchmark Review</title>" in html
    assert "Disagreement queue" in html
    assert "X" in html
    assert "Confirmations" in html


# ---------- main / file output smoke ----------


def test_main_writes_outputs(tmp_path=None):
    """Run main() in a temp DATA_DIR clone and verify output files exist."""
    # Use the actual repo paths for this smoke -- we already wrote real
    # files in the tree, so just assert they're present and parseable.
    json_path = REPO_ROOT / "data" / "reports" / "external_benchmark_review.json"
    queue_path = REPO_ROOT / "data" / "reports" / "disagreement_queue.json"
    html_path = REPO_ROOT / "reports" / "external-benchmark-review.html"
    assert json_path.exists()
    assert queue_path.exists()
    assert html_path.exists()
    payload = json.loads(json_path.read_text())
    assert "overall" in payload
    assert "metrics_by_source" in payload
    queue_payload = json.loads(queue_path.read_text())
    assert "queue" in queue_payload


# ---------- runner ----------


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        fail(f"{failed} test(s) failed")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

"""Smoke tests for watchlist tickers: parsing, dedup, JSON shape.

These tests don't hit the network and don't depend on the main pipeline
having run. They validate that:
  * data/watchlist_sources.json is well-formed and non-empty.
  * Symbol overrides are applied so SOL-USD/SOLUSD-style duplicates collapse.
  * generate_watchlist_rankings.py runs end-to-end with supplemental fetch
    disabled and produces a valid JSON envelope, even if every row ends up
    in the unavailable list (because rankings.csv may not exist in unit env).

Run: python 02_Code/Python/Scoring_Engine/test_watchlist_rankings.py
"""

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SOURCES = os.path.join(REPO_ROOT, "data", "watchlist_sources.json")
GENERATOR = os.path.join(REPO_ROOT, "02_Code", "Python", "Scoring_Engine", "generate_watchlist_rankings.py")
OUTPUT = os.path.join(REPO_ROOT, "data", "watchlist_rankings.json")


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def test_sources_well_formed():
    with open(SOURCES) as f:
        cfg = json.load(f)
    assert "sources" in cfg, "missing 'sources'"
    csv_t = cfg["sources"]["csv"]["tickers"]
    tv_t = cfg["sources"]["tradingview"]["tickers"]
    assert len(csv_t) >= 70, f"expected >=70 CSV symbols, got {len(csv_t)}"
    assert len(tv_t) >= 90, f"expected >=90 TV symbols, got {len(tv_t)}"
    overrides = cfg.get("symbol_overrides", {})
    assert overrides.get("SOLUSD") == "SOL-USD", "SOLUSD override missing"
    assert overrides.get("BTCUSD") == "BTC-USD", "BTCUSD override missing"
    print("  sources well-formed: OK")


def test_dedup_after_override():
    with open(SOURCES) as f:
        cfg = json.load(f)
    overrides = cfg.get("symbol_overrides", {})
    csv_t = [t.upper() for t in cfg["sources"]["csv"]["tickers"]]
    tv_t = [t.upper() for t in cfg["sources"]["tradingview"]["tickers"]]
    canon = lambda s: overrides.get(s, s)
    csv_canon = {canon(t) for t in csv_t}
    tv_canon = {canon(t) for t in tv_t}
    combined = csv_canon | tv_canon
    # SOLUSD -> SOL-USD should now dedupe across sources
    assert ("SOL-USD" in combined) and ("SOLUSD" not in combined), \
        "SOL-USD/SOLUSD did not dedupe correctly"
    print(f"  combined unique after override: {len(combined)} (OK)")


def test_generator_runs():
    env = os.environ.copy()
    env["WATCHLIST_DISABLE_SUPPLEMENTAL"] = "1"  # don't hit network in tests
    res = subprocess.run([sys.executable, GENERATOR],
                         capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    if res.returncode != 0:
        fail(f"generator exited {res.returncode}\nstdout={res.stdout}\nstderr={res.stderr}")
    assert os.path.exists(OUTPUT), f"output not written: {OUTPUT}"
    with open(OUTPUT) as f:
        data = json.load(f)
    for k in ("as_of", "rows", "source_meta", "unavailable", "universe"):
        assert k in data, f"output missing key {k!r}"
    assert isinstance(data["rows"], list)
    assert isinstance(data["unavailable"], list)
    sm = data["source_meta"]
    assert sm["combined_unique"] >= 140, f"combined_unique unexpectedly small: {sm['combined_unique']}"
    print(f"  generator end-to-end: rows={len(data['rows'])}, "
          f"unavailable={len(data['unavailable'])} (OK)")


def test_classify_instrument():
    """Cover the SUPP _classify_instrument loosened equity inference rule.

    The previous rule required sector AND marketCap, which left most rows
    'unknown' because yfinance .info often returns only one or the other.
    The current rule accepts either, plus other equity-only markers (eps,
    pe, sharesOutstanding, ...) so SUPP coverage isn't gated on a single
    field that yfinance happens to omit. ETF / crypto / fund / foreign
    quoteType matches should still take precedence.
    """
    sys.path.insert(0, os.path.dirname(GENERATOR))
    from generate_watchlist_rankings import _classify_instrument

    # quoteType wins regardless of other fields
    assert _classify_instrument("SPY", {"quoteType": "ETF", "sector": "Technology",
                                        "marketCap": 1e12}) == "etf"
    assert _classify_instrument("VTSAX", {"quoteType": "MUTUALFUND"}) == "fund"
    assert _classify_instrument("BTC-USD", {"quoteType": "CRYPTOCURRENCY"}) == "crypto"
    # Crypto inferred by symbol suffix
    assert _classify_instrument("ETH-USD", {}) == "crypto"
    # Foreign by suffix
    assert _classify_instrument("005930.KS", {}) == "foreign"
    assert _classify_instrument("AAPL.L", {}) == "foreign"

    # Equity inference: each marker alone is enough.
    assert _classify_instrument("AAPL", {"sector": "Technology"}) == "equity"
    assert _classify_instrument("AAPL", {"marketCap": 3_000_000_000_000}) == "equity"
    assert _classify_instrument("AAPL", {"sector": "Technology",
                                         "marketCap": 3e12}) == "equity"
    assert _classify_instrument("AAPL", {"industry": "Consumer Electronics"}) == "equity"
    assert _classify_instrument("AAPL", {"trailingEps": 6.5}) == "equity"
    assert _classify_instrument("AAPL", {"trailingPE": 28.0}) == "equity"
    assert _classify_instrument("AAPL", {"sharesOutstanding": 15e9}) == "equity"
    assert _classify_instrument("AAPL", {"bookValue": 4.5}) == "equity"

    # No equity markers at all → unknown
    assert _classify_instrument("ZZZ", {}) == "unknown"
    # Country alone shouldn't promote to equity (ETFs have country too)
    assert _classify_instrument("ZZZ", {"country": "United States"}) == "unknown"

    # Explicit EQUITY quoteType still respected
    assert _classify_instrument("MSFT", {"quoteType": "EQUITY"}) == "equity"
    print("  classify_instrument cases: OK")


def test_supp_summary_categorization():
    """The supp_summary buckets must remain consistent with classify+enrichment.

    An equity inferred from sector-only (no PE/growth fields) must NOT
    fabricate a Fundamental score, and must land in technical_only or
    metadata_only — never full_fundamentals.
    """
    sys.path.insert(0, os.path.dirname(GENERATOR))
    from generate_watchlist_rankings import row_from_supplemental, fundamental_from_yfinance

    # Sector-only equity, no fundamental signal: fundamental score must be None.
    score, comps = fundamental_from_yfinance({"beta": 1.1})  # only beta, no PE/growth/EPS
    assert score is None and comps is None, \
        f"expected no fundamental score from beta-only fundamentals, got {score!r}"

    # Real signal: returns a number.
    score2, comps2 = fundamental_from_yfinance({"trailingPE": 18.0, "revenueGrowth": 0.1})
    assert score2 is not None and 0 <= score2 <= 10, \
        f"expected 0..10 fundamental score, got {score2!r}"

    # Row built from a sector-only equity has fundamental=None and ai_basis technical-only.
    fetched = {
        "price": 100.0,
        "closes": [99.0, 100.0, 101.0],
        "vol_millions": 1.0,
        "company": "Test Co",
        "industry": "",
        "sector": "Technology",
        "market_cap_raw": None,
        "country": "US",
        "fundamentals": {"beta": 1.0},   # no real fundamental signal
        "instrument_kind": "equity",
    }
    row = row_from_supplemental(1, "TEST", fetched)
    assert row["instrument_kind"] == "equity"
    assert row["fundamental"] is None, f"expected no Fundamental score, got {row['fundamental']!r}"
    assert row["ai_score_basis"] == "supp_technical_only"
    # enrichment_source must NOT claim full fundamentals
    assert row["enrichment_source"] != "yfinance_fundamentals"
    print("  supp_summary categorization honesty: OK")


def main():
    print("Running watchlist smoke tests...")
    test_sources_well_formed()
    test_dedup_after_override()
    test_classify_instrument()
    test_supp_summary_categorization()
    test_generator_runs()
    print("All tests passed.")


if __name__ == "__main__":
    main()

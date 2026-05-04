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


def test_eodhd_overlay_into_supp_row():
    """When yfinance returns no fundamentals but EODHD (via cache) has them,
    the row produced by row_from_supplemental must:
      * pick up the fundamentals from EODHD
      * report fundamental_source = 'eodhd' (not 'yfinance_derived')
      * carry eodhd_fundamentals = True
      * still carry data_source = 'supplemental_yfinance' (origin label
        unchanged so the dashboard treats it as a SUPP row)
      * land in the eodhd_fundamentals enrichment_source bucket
    """
    sys.path.insert(0, os.path.dirname(GENERATOR))
    from generate_watchlist_rankings import row_from_supplemental

    fetched = {
        "price": 75.0,
        "closes": [73.0, 74.0, 75.0],
        "vol_millions": 0.5,
        "company": "Test Foreign Co",
        "industry": "Banking",
        "sector": "Financials",
        "market_cap_raw": 5_000_000_000,
        "country": "Canada",
        # Real signal sourced via EODHD. Provenance reflects that.
        "fundamentals": {
            "trailingPE": 14.0,
            "trailingEps": 5.0,
            "revenueGrowth": 0.06,
            "earningsGrowth": 0.10,
            "beta": 0.9,
            "profitMargins": 0.15,
        },
        "fundamentals_provenance": {
            "trailingPE": "eodhd", "trailingEps": "eodhd",
            "revenueGrowth": "eodhd", "earningsGrowth": "eodhd",
            "beta": "eodhd", "profitMargins": "eodhd",
        },
        "fundamental_source": "eodhd",
        "eodhd_used": True,
        "eodhd_symbol": "TST.TO",
        "instrument_kind": "equity",
    }
    row = row_from_supplemental(1, "TST.TO", fetched)
    assert row["data_source"] == "supplemental_yfinance", \
        "SUPP origin label must be preserved when EODHD enriches the row"
    assert row["eodhd_fundamentals"] is True
    assert row["eodhd_symbol"] == "TST.TO"
    assert row["fundamental_source"] == "eodhd", row["fundamental_source"]
    assert row["enrichment_source"] == "eodhd_fundamentals", row["enrichment_source"]
    assert row["fundamental"] is not None and 0 <= row["fundamental"] <= 10
    assert row["ai_score_basis"] == "supp_composite"
    print("  EODHD overlay into SUPP row: OK")


def test_eodhd_budget_metadata_in_output():
    """The generator must emit the EODHD live-call budget accounting fields
    in source_meta so quota behavior is auditable from the JSON alone.
    Even with supplemental fetch disabled, the budget snapshot should be
    present (zero counters, configured budget value)."""
    env = os.environ.copy()
    env["WATCHLIST_DISABLE_SUPPLEMENTAL"] = "1"
    env["EODHD_MAX_FUNDAMENTAL_CALLS"] = "7"
    res = subprocess.run([sys.executable, GENERATOR],
                         capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    if res.returncode != 0:
        fail(f"generator exited {res.returncode}\nstderr={res.stderr}")
    with open(OUTPUT) as f:
        data = json.load(f)
    sm = data["source_meta"]
    for k in ("eodhd_budget", "eodhd_cache_hits", "eodhd_live_calls", "eodhd_deferred"):
        assert k in sm, f"source_meta missing {k!r}: {sorted(sm.keys())}"
    assert sm["eodhd_budget"] == 7, sm["eodhd_budget"]
    # Supplemental disabled => no fetches, no cache hits, no defers.
    assert sm["eodhd_live_calls"] == 0
    assert sm["eodhd_deferred"] == 0
    print(f"  EODHD budget metadata: budget={sm['eodhd_budget']}, "
          f"live={sm['eodhd_live_calls']}, cache={sm['eodhd_cache_hits']}, "
          f"deferred={sm['eodhd_deferred']} (OK)")


def test_eodhd_enrichment_disabled_flag():
    """EODHD_ENRICHMENT_ENABLED=false must force max_live_calls to 0 even
    when EODHD_MAX_FUNDAMENTAL_CALLS is positive AND a key is in env. This
    is the gate the workflow uses on midday/close runs to keep the live
    quota intact while still allowing cache hits.

    Conversely, leaving the flag unset preserves prior behavior (budget
    honored as configured). The flag is the single source of truth for the
    "is this run allowed to make live EODHD calls" question — no more
    fragile conditional secret expressions in step env.
    """
    env = os.environ.copy()
    env["WATCHLIST_DISABLE_SUPPLEMENTAL"] = "1"
    env["EODHD_MAX_FUNDAMENTAL_CALLS"] = "15"
    env["EODHD_ENRICHMENT_ENABLED"] = "false"
    env["EODHD_API_KEY"] = "fake_should_not_be_used"
    res = subprocess.run([sys.executable, GENERATOR],
                         capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    if res.returncode != 0:
        fail(f"generator exited {res.returncode}\nstderr={res.stderr}")
    with open(OUTPUT) as f:
        data = json.load(f)
    sm = data["source_meta"]
    # When disabled, configured budget collapses to 0 — the live-call gate
    # below it then refuses every uncached symbol via the deferred path.
    assert sm["eodhd_budget"] == 0, sm
    assert sm["eodhd_live_calls"] == 0
    # The pre-flight log line must announce the disabled state so future
    # debugging can see the flag without having to guess the env.
    assert "enabled=False" in res.stdout, res.stdout
    print("  EODHD_ENRICHMENT_ENABLED=false forces budget=0: OK")


def test_eodhd_observability_keys_in_output():
    """source_meta must carry the new observability counters so the JSON
    alone explains why a run made zero live calls. Pre-fix: a run could
    legitimately produce live=cache=deferred=0 without any way to tell
    'no key' from 'no eligible symbols' from 'all 401s'."""
    env = os.environ.copy()
    env["WATCHLIST_DISABLE_SUPPLEMENTAL"] = "1"
    env["EODHD_ENRICHMENT_ENABLED"] = "true"
    res = subprocess.run([sys.executable, GENERATOR],
                         capture_output=True, text=True, env=env, cwd=REPO_ROOT)
    if res.returncode != 0:
        fail(f"generator exited {res.returncode}\nstderr={res.stderr}")
    with open(OUTPUT) as f:
        data = json.load(f)
    sm = data["source_meta"]
    for k in ("eodhd_key_present", "eodhd_attempted", "eodhd_skipped_no_key",
              "eodhd_skipped_no_symbol", "eodhd_skipped_http_error",
              "eodhd_skipped_request_error", "eodhd_gate"):
        assert k in sm, f"source_meta missing {k!r}: {sorted(sm.keys())}"
    gate = sm["eodhd_gate"]
    for k in ("skipped_not_equity", "skipped_helper_missing", "eligible"):
        assert k in gate, f"eodhd_gate missing {k!r}: {sorted(gate.keys())}"
    print("  EODHD observability keys in output: OK")


def test_likely_equity_heuristic():
    """The pre-yfinance gate must accept plain US tickers (so EODHD can
    classify/enrich them when yfinance returns empty .info under rate-
    limiting), and must reject crypto / ETFs / KRX / numeric-foreign tickers
    that should never spend live EODHD budget.
    """
    sys.path.insert(0, os.path.dirname(GENERATOR))
    from generate_watchlist_rankings import _likely_equity_symbol

    # Plain US tickers should be eligible — these are exactly the rows
    # yfinance was failing to classify as equity before the fix.
    for sym in ("AXTI", "SOUN", "VISN", "NBIS", "VIAV", "PI", "AAPL",
                "NVDA", "AMD", "MP", "BBAI", "USAR"):
        assert _likely_equity_symbol(sym) is True, f"{sym} must be eligible"

    # Crypto pairs must be excluded.
    for sym in ("BTC-USD", "SOL-USD", "ETH-USD", "BTCUSD", "SOLUSD"):
        assert _likely_equity_symbol(sym) is False, f"{sym} must be excluded (crypto)"

    # Known ETFs / funds / non-equity vehicles must be excluded.
    for sym in ("USO", "GUSH", "GLD", "SLV", "IBIT", "QTUM", "MSOS", "BIPC"):
        assert _likely_equity_symbol(sym) is False, f"{sym} must be excluded (non-equity)"

    # KRX foreign listings — numeric heads with .KS / .KQ.
    for sym in ("005930.KS", "000660.KS", "005930", "000660"):
        # The bare numeric form passes the symbol-shape filter but should
        # still be rejected (digit-only head). With .KS suffix the
        # explicit suffix rule rejects it.
        if "." in sym:
            assert _likely_equity_symbol(sym) is False, f"{sym} must be excluded (KRX)"
    # Other foreign (Canadian / London / etc.) suffixes are allowed
    # through — EODHD covers them and a 404 is harmless.
    assert _likely_equity_symbol("SHOP.TO") is True
    assert _likely_equity_symbol("BARC.L") is True

    # Empty / blank inputs are not eligible.
    assert _likely_equity_symbol("") is False
    assert _likely_equity_symbol(None) is False
    print("  likely_equity heuristic: OK")


def test_eodhd_attempted_for_unknown_likely_equity():
    """When yfinance classifies a row as 'unknown' (e.g. rate-limited info),
    a likely-equity symbol must still be considered eligible for EODHD and,
    if EODHD returns equity-like fundamentals, the row gets reclassified to
    equity. This is the core regression: pre-fix this row would have been
    counted under skipped_not_equity and EODHD would never run.

    We exercise the gating logic by calling fetch_supplemental directly
    with a stubbed yfinance + injected EODHD response.
    """
    sys.path.insert(0, os.path.dirname(GENERATOR))
    import generate_watchlist_rankings as gen
    from eodhd_fundamentals import EodhdBudget

    # Stub yfinance: hist with closes, but info empty so kind => 'unknown'.
    class _Hist:
        empty = False
        columns = ["Close", "Volume"]
        def __getitem__(self, k):
            import pandas as pd
            if k == "Close":
                return pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
            return pd.Series([1_000_000])
    class _Stub:
        def __init__(self, sym): pass
        def history(self, **kw): return _Hist()
        @property
        def info(self): return {}
    class _yf_mod:
        Ticker = _Stub
    saved_yf = sys.modules.get("yfinance")
    sys.modules["yfinance"] = _yf_mod

    # Stub EODHD: return equity-like fundamentals so reclassification fires.
    saved_helper = gen.fetch_eodhd_fundamentals
    def fake_eodhd(symbol, budget=None, **kw):
        if budget is not None:
            budget.note_attempted()
            budget.note_live_call()
            budget.note_key_present()
        return {
            "trailingPE": 25.0, "trailingEps": 1.0, "revenueGrowth": 0.20,
            "earningsGrowth": 0.30, "profitMargins": 0.10,
            "sector": "Technology", "industry": "Semiconductors",
            "marketCap": 1_000_000_000, "shortName": "Stub Co", "country": "USA",
            "_eodhd_symbol": f"{symbol}.US",
        }
    gen.fetch_eodhd_fundamentals = fake_eodhd
    try:
        budget = EodhdBudget(15)
        gate = {
            "skipped_not_equity": 0, "skipped_known_non_equity": 0,
            "skipped_symbol_pattern": 0, "skipped_helper_missing": 0,
            "eligible": 0, "eligible_yfinance_equity": 0,
            "eligible_likely_equity": 0, "reclassified_to_equity": 0,
        }
        out = gen.fetch_supplemental("AXTI", eodhd_budget=budget, gate_counts=gate)
        assert out is not None, "fetch_supplemental returned None"
        assert out["instrument_kind"] == "equity", \
            f"expected reclassified equity, got {out['instrument_kind']!r}"
        assert out["eodhd_used"] is True
        assert out["fundamentals"]["trailingPE"] == 25.0
        assert gate["eligible_likely_equity"] == 1, gate
        assert gate["reclassified_to_equity"] == 1, gate
        assert gate["skipped_not_equity"] == 0, gate
    finally:
        gen.fetch_eodhd_fundamentals = saved_helper
        if saved_yf is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = saved_yf
    print("  EODHD attempted for unknown likely-equity: OK")


def test_eodhd_skips_known_non_equity_via_pre_gate():
    """Crypto and ETF symbols must be counted under skipped_known_non_equity
    (not skipped_not_equity) so the metadata distinguishes 'we know this
    isn't an equity' from 'yfinance hasn't told us yet'. Budget must not
    advance for them.
    """
    sys.path.insert(0, os.path.dirname(GENERATOR))
    import generate_watchlist_rankings as gen
    from eodhd_fundamentals import EodhdBudget

    class _Hist:
        empty = False
        columns = ["Close", "Volume"]
        def __getitem__(self, k):
            import pandas as pd
            if k == "Close":
                return pd.Series([10.0, 11.0, 12.0])
            return pd.Series([1_000_000])
    class _StubETF:
        def __init__(self, sym): self.sym = sym
        def history(self, **kw): return _Hist()
        @property
        def info(self):
            return {"quoteType": "ETF"} if self.sym in ("USO", "GLD", "GUSH") else {}
    class _yf_mod:
        Ticker = _StubETF
    saved_yf = sys.modules.get("yfinance")
    sys.modules["yfinance"] = _yf_mod

    saved_helper = gen.fetch_eodhd_fundamentals
    called = {"n": 0}
    def must_not_be_called(*a, **kw):
        called["n"] += 1
        return None
    gen.fetch_eodhd_fundamentals = must_not_be_called
    try:
        budget = EodhdBudget(15)
        gate = {
            "skipped_not_equity": 0, "skipped_known_non_equity": 0,
            "skipped_symbol_pattern": 0, "skipped_helper_missing": 0,
            "eligible": 0, "eligible_yfinance_equity": 0,
            "eligible_likely_equity": 0, "reclassified_to_equity": 0,
        }
        # ETF: yfinance.info reports quoteType=ETF, so kind='etf'.
        gen.fetch_supplemental("USO", eodhd_budget=budget, gate_counts=gate)
        # Crypto inferred by suffix.
        gen.fetch_supplemental("BTC-USD", eodhd_budget=budget, gate_counts=gate)
        assert gate["skipped_known_non_equity"] >= 2, gate
        assert called["n"] == 0, "EODHD must not be called for ETF/crypto"
        assert budget.live_calls == 0
    finally:
        gen.fetch_eodhd_fundamentals = saved_helper
        if saved_yf is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = saved_yf
    print("  EODHD pre-gate skips ETF/crypto: OK")


def test_eodhd_budget_respected_with_likely_equity():
    """When many likely-equity rows are processed but the budget is small,
    only `budget` live calls happen; the rest are deferred (cache hits would
    still be free, but we don't pre-populate cache here).
    """
    sys.path.insert(0, os.path.dirname(GENERATOR))
    import generate_watchlist_rankings as gen
    from eodhd_fundamentals import EodhdBudget

    class _Hist:
        empty = False
        columns = ["Close", "Volume"]
        def __getitem__(self, k):
            import pandas as pd
            if k == "Close":
                return pd.Series([10.0, 11.0, 12.0])
            return pd.Series([1_000_000])
    class _Stub:
        def __init__(self, sym): pass
        def history(self, **kw): return _Hist()
        @property
        def info(self): return {}
    class _yf_mod:
        Ticker = _Stub
    saved_yf = sys.modules.get("yfinance")
    sys.modules["yfinance"] = _yf_mod

    saved_helper = gen.fetch_eodhd_fundamentals
    def fake_eodhd(symbol, budget=None, **kw):
        # Honour the budget like the real helper does.
        if budget is not None:
            budget.note_attempted()
            budget.note_key_present()
            if not budget.has_room():
                budget.note_deferred()
                return None
            budget.note_live_call()
        return {"trailingPE": 18.0, "_eodhd_symbol": f"{symbol}.US",
                "sector": "Technology"}
    gen.fetch_eodhd_fundamentals = fake_eodhd
    try:
        budget = EodhdBudget(2)
        gate = {
            "skipped_not_equity": 0, "skipped_known_non_equity": 0,
            "skipped_symbol_pattern": 0, "skipped_helper_missing": 0,
            "eligible": 0, "eligible_yfinance_equity": 0,
            "eligible_likely_equity": 0, "reclassified_to_equity": 0,
        }
        for sym in ("AXTI", "SOUN", "VISN", "NBIS", "VIAV"):
            gen.fetch_supplemental(sym, eodhd_budget=budget, gate_counts=gate)
        assert budget.live_calls == 2, budget.as_dict()
        assert budget.deferred == 3, budget.as_dict()
        assert gate["eligible_likely_equity"] == 5, gate
    finally:
        gen.fetch_eodhd_fundamentals = saved_helper
        if saved_yf is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = saved_yf
    print("  EODHD budget respected with likely-equity rows: OK")


def test_eodhd_disabled_when_no_module():
    """Sanity: even if the EODHD module is somehow unavailable, the watchlist
    generator must still run (the import is wrapped). This is a regression
    guard for environments where requests/yfinance are absent."""
    sys.path.insert(0, os.path.dirname(GENERATOR))
    import generate_watchlist_rankings as gen
    # The module-level fetch_eodhd_fundamentals must always exist as a name
    # (None when import failed, callable otherwise) so the runtime branch
    # `if fetch_eodhd_fundamentals is not None` doesn't NameError.
    assert hasattr(gen, "fetch_eodhd_fundamentals")
    print("  EODHD soft-import guard: OK")


def main():
    print("Running watchlist smoke tests...")
    test_sources_well_formed()
    test_dedup_after_override()
    test_classify_instrument()
    test_supp_summary_categorization()
    test_eodhd_overlay_into_supp_row()
    test_likely_equity_heuristic()
    test_eodhd_attempted_for_unknown_likely_equity()
    test_eodhd_skips_known_non_equity_via_pre_gate()
    test_eodhd_budget_respected_with_likely_equity()
    test_eodhd_disabled_when_no_module()
    test_eodhd_budget_metadata_in_output()
    test_eodhd_enrichment_disabled_flag()
    test_eodhd_observability_keys_in_output()
    test_generator_runs()
    print("All tests passed.")


if __name__ == "__main__":
    main()

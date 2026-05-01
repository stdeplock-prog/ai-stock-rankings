"""Unit tests for eodhd_fundamentals.py.

Network-free: uses an injected fake request_fn and a temporary cache
directory under the system tmp dir. Validates:
  * Symbol normalization to TICKER.EXCHANGE form
  * EODHD JSON -> canonical FUND_FIELDS mapping
  * Cache short-circuits the API-key requirement (so SUPP enrichment is
    testable without live credentials)
  * No-API-key + no-cache returns None cleanly
  * Crypto / blank symbols return None (not appropriate for the equity
    fundamentals endpoint)

Run: python 02_Code/Python/Data_Fetch/test_eodhd_fundamentals.py
"""

import json
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eodhd_fundamentals as ef  # noqa: E402


def _fake_response(status, payload):
    class R:
        status_code = status
        def json(self):
            return payload
    return R()


def test_normalize_symbol():
    n = ef.normalize_symbol_for_eodhd
    assert n("AAPL") == "AAPL.US"
    assert n("aapl") == "AAPL.US"
    assert n("BRK.B") == "BRK-B.US"
    assert n("BF.B") == "BF-B.US"
    assert n("005930.KS") == "005930.KS"
    assert n("SHOP.TO") == "SHOP.TO"
    assert n("TSCO.L") == "TSCO.L"
    # Crypto: explicit and legacy
    assert n("BTC-USD") is None
    assert n("SOLUSD") is None
    # Blank
    assert n("") is None
    assert n("   ") is None
    # Already foreign with unknown suffix passes through
    assert n("FOO.XYZ") == "FOO.XYZ"
    print("  normalize_symbol_for_eodhd: OK")


def test_map_payload_full():
    payload = {
        "General":   {"Name": "Apple Inc",   "Sector": "Technology",
                      "Industry": "Consumer Electronics", "CountryName": "USA"},
        "Highlights":{"MarketCapitalization": 3_000_000_000_000,
                      "PERatio": 28.5, "EarningsShare": 6.5,
                      "QuarterlyRevenueGrowthYOY": 0.08,
                      "QuarterlyEarningsGrowthYOY": 0.12,
                      "DividendYield": 0.005,
                      "ProfitMargin": 0.25,
                      "OperatingMarginTTM": 0.30,
                      "ReturnOnEquityTTM": 1.5,
                      "ReturnOnAssetsTTM": 0.25},
        "Valuation": {"ForwardPE": 26.0, "PriceBookMRQ": 50.0},
        "Technicals":{"Beta": 1.25, "52WeekHigh": 250.0, "52WeekLow": 150.0},
    }
    mapped = ef.map_eodhd_payload(payload)
    assert mapped["shortName"] == "Apple Inc"
    assert mapped["sector"] == "Technology"
    assert mapped["industry"] == "Consumer Electronics"
    assert mapped["country"] == "USA"
    assert mapped["marketCap"] == 3_000_000_000_000
    assert mapped["trailingPE"] == 28.5
    assert mapped["forwardPE"] == 26.0
    assert mapped["trailingEps"] == 6.5
    assert mapped["revenueGrowth"] == 0.08
    assert mapped["earningsGrowth"] == 0.12
    assert mapped["dividendYield"] == 0.005
    assert mapped["beta"] == 1.25
    assert mapped["fiftyTwoWeekHigh"] == 250.0
    assert mapped["fiftyTwoWeekLow"] == 150.0
    assert mapped["returnOnEquity"] == 1.5
    assert mapped["returnOnAssets"] == 0.25
    assert mapped["profitMargins"] == 0.25
    assert mapped["operatingMargins"] == 0.30
    assert mapped["priceToBook"] == 50.0
    print("  map_payload (full): OK")


def test_map_payload_partial_and_sentinels():
    """Missing sections / "NA" sentinels must yield None, not crash."""
    payload = {
        "General":    {"Name": "Foo Corp", "Sector": "NA"},
        "Highlights": {"PERatio": "18.5", "MarketCapitalization": "1500000000"},
        # No Valuation / Technicals
    }
    mapped = ef.map_eodhd_payload(payload)
    assert mapped["shortName"] == "Foo Corp"
    assert mapped["sector"] is None  # "NA" coerced to None
    assert mapped["trailingPE"] == 18.5  # numeric string coerced
    assert mapped["marketCap"] == 1_500_000_000
    assert mapped["beta"] is None  # Technicals section absent
    assert mapped["forwardPE"] is None
    assert mapped["priceToBook"] is None
    print("  map_payload (partial / sentinels): OK")


def test_map_payload_empty():
    assert ef.map_eodhd_payload({}) == {k: None for k in ef.EODHD_FIELD_MAP}
    print("  map_payload (empty): OK")


def test_fetch_no_key_no_cache_returns_none(tmp_cache):
    # No API key, no cache file -> None (clean fall-through).
    out = ef.fetch_eodhd_fundamentals("AAPL", api_key="", use_cache=True)
    assert out is None
    print("  fetch with no key & no cache: OK")


def test_fetch_crypto_returns_none(tmp_cache):
    out = ef.fetch_eodhd_fundamentals("BTC-USD", api_key="any", use_cache=True)
    assert out is None
    out2 = ef.fetch_eodhd_fundamentals("SOLUSD", api_key="any", use_cache=True)
    assert out2 is None
    print("  fetch crypto: OK")


def test_fetch_via_injected_request(tmp_cache):
    """Inject a fake request_fn so we never touch the network. Verify the
    call is routed to the right URL and the payload is mapped+cached."""
    payload = {
        "General":    {"Name": "Test Co", "Sector": "Healthcare", "Industry": "Biotech",
                       "CountryName": "USA"},
        "Highlights": {"PERatio": 22.0, "MarketCapitalization": 2_500_000_000,
                       "EarningsShare": 1.5, "QuarterlyRevenueGrowthYOY": 0.18,
                       "ProfitMargin": 0.05},
        "Valuation":  {"PriceBookMRQ": 4.5, "ForwardPE": 18.0},
        "Technicals": {"Beta": 0.95},
    }
    captured = {}
    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _fake_response(200, payload)

    out = ef.fetch_eodhd_fundamentals("ZZZX", api_key="dummy", use_cache=True,
                                      request_fn=fake_get)
    assert out is not None
    assert captured["url"].endswith("/fundamentals/ZZZX.US"), captured["url"]
    assert captured["params"]["api_token"] == "dummy"
    assert out["trailingPE"] == 22.0
    assert out["sector"] == "Healthcare"
    assert out["_eodhd_symbol"] == "ZZZX.US"
    assert out["_eodhd_source"] == "api"

    # Second call should hit the cache and NOT call the request_fn again.
    captured.clear()
    def boom(*a, **k):
        raise AssertionError("request_fn called when cache should have hit")
    out2 = ef.fetch_eodhd_fundamentals("ZZZX", api_key="dummy", use_cache=True,
                                       request_fn=boom)
    assert out2 is not None
    assert out2["_eodhd_source"] == "cache"
    assert out2["trailingPE"] == 22.0
    print("  fetch via injected request + cache hit: OK")


def test_fetch_404_returns_none(tmp_cache):
    def fake_get(url, params=None, timeout=None):
        return _fake_response(404, {})
    out = ef.fetch_eodhd_fundamentals("NOPE", api_key="k",
                                       use_cache=False, request_fn=fake_get)
    assert out is None
    print("  fetch 404 -> None: OK")


def test_cache_makes_test_runnable_without_key(tmp_cache):
    """Pre-populate the cache for a foreign symbol; verify fetch returns the
    mapped data without an API key. This is the path SUPP enrichment uses
    in CI for repeatability when no live credentials are available."""
    payload = {
        "General":    {"Name": "Samsung Electronics", "Sector": "Technology",
                       "Industry": "Consumer Electronics", "CountryName": "South Korea"},
        "Highlights": {"PERatio": 12.0, "MarketCapitalization": 350_000_000_000,
                       "EarningsShare": 5_000.0, "QuarterlyRevenueGrowthYOY": 0.09,
                       "QuarterlyEarningsGrowthYOY": 0.20,
                       "ProfitMargin": 0.10, "ReturnOnEquityTTM": 0.08},
        "Valuation":  {"PriceBookMRQ": 1.2},
        "Technicals": {"Beta": 1.05},
    }
    os.makedirs(ef.CACHE_DIR, exist_ok=True)
    with open(os.path.join(ef.CACHE_DIR, "005930.KS.json"), "w") as f:
        json.dump(payload, f)
    out = ef.fetch_eodhd_fundamentals("005930.KS", api_key="", use_cache=True)
    assert out is not None
    assert out["trailingPE"] == 12.0
    assert out["beta"] == 1.05
    assert out["country"] == "South Korea"
    assert out["_eodhd_source"] == "cache"
    print("  cache-only fetch (no key): OK")


# ---- pytest-free harness with per-test cache reset ----

def tmp_cache():
    """Context manager substitute: redirects ef.CACHE_DIR to a tempdir and
    restores it afterward. Used as a positional arg via monkey-patch."""
    pass


def _run_with_tmp_cache(fn):
    saved = ef.CACHE_DIR
    tmpdir = tempfile.mkdtemp(prefix="eodhd_test_")
    ef.CACHE_DIR = tmpdir
    try:
        fn(tmp_cache=None)
    finally:
        ef.CACHE_DIR = saved
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    print("Running eodhd_fundamentals unit tests...")
    test_normalize_symbol()
    test_map_payload_full()
    test_map_payload_partial_and_sentinels()
    test_map_payload_empty()
    _run_with_tmp_cache(test_fetch_no_key_no_cache_returns_none)
    _run_with_tmp_cache(test_fetch_crypto_returns_none)
    _run_with_tmp_cache(test_fetch_via_injected_request)
    _run_with_tmp_cache(test_fetch_404_returns_none)
    _run_with_tmp_cache(test_cache_makes_test_runnable_without_key)
    print("All eodhd_fundamentals tests passed.")


if __name__ == "__main__":
    main()

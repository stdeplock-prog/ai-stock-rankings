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


def main():
    print("Running watchlist smoke tests...")
    test_sources_well_formed()
    test_dedup_after_override()
    test_generator_runs()
    print("All tests passed.")


if __name__ == "__main__":
    main()

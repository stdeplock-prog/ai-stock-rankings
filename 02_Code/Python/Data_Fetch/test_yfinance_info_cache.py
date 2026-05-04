"""Fixture-based tests for yfinance_info_cache.py.

Validates fresh-cache short-circuit, cache-fallback on rate-limit/empty,
and that empty payloads don't evict good cache entries. No network.

Run: python 02_Code/Python/Data_Fetch/test_yfinance_info_cache.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yfinance_info_cache as yc  # noqa: E402


def _swap_cache_dir():
    tmp = tempfile.TemporaryDirectory()
    yc.CACHE_DIR = Path(tmp.name) / "yfinance_info"
    return tmp


def _good_info():
    return {
        "shortName": "Acme Corp",
        "sector": "Technology",
        "marketCap": 1_000_000_000,
        "trailingPE": 18.4,
        "industry": "Software",
        "regularMarketPrice": 100.0,
    }


def test_save_and_load_roundtrip():
    tmp = _swap_cache_dir()
    try:
        yc.save_entry("ABC", _good_info(), run_url="https://example/runs/1")
        entry = yc.load_entry("ABC")
        assert entry is not None and entry["info"]["shortName"] == "Acme Corp"
        assert entry["cache_age_days"] is not None and entry["cache_age_days"] >= 0
    finally:
        tmp.cleanup()


def test_save_skips_empty_info():
    tmp = _swap_cache_dir()
    try:
        # Pre-seed a good entry.
        yc.save_entry("ABC", _good_info())
        # Try to save an empty payload — should not overwrite.
        yc.save_entry("ABC", {})
        entry = yc.load_entry("ABC")
        assert entry["info"]["shortName"] == "Acme Corp"
    finally:
        tmp.cleanup()


def test_get_info_fresh_cache_short_circuits_network():
    tmp = _swap_cache_dir()
    try:
        yc.save_entry("ABC", _good_info())
        calls = {"n": 0}
        def fetcher(_sym):
            calls["n"] += 1
            return {"shortName": "Should not be called"}
        counters = {}
        res = yc.get_info("ABC", fetcher=fetcher, counters=counters)
        assert calls["n"] == 0, "fresh cache should skip live fetch"
        assert res["source"] == "yfinance_fresh_cache"
        assert counters["cache_hit_fresh"] == 1
    finally:
        tmp.cleanup()


def test_get_info_falls_back_to_cache_on_empty_live():
    tmp = _swap_cache_dir()
    try:
        # Save a good entry, then artificially age it past freshness.
        yc.save_entry("ABC", _good_info())
        p = yc.cache_path("ABC")
        data = json.loads(p.read_text())
        old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["fetched_at_utc"] = old
        p.write_text(json.dumps(data))
        # Live returns empty (rate-limited).
        counters = {}
        res = yc.get_info("ABC", fetcher=lambda s: {}, counters=counters)
        assert res["source"] == "yfinance_cache_fallback", res
        assert res["info"]["shortName"] == "Acme Corp"
        assert counters["fallback_to_cache"] == 1
        assert counters["network_call"] == 1
        assert counters["rate_limit_or_empty"] == 1
    finally:
        tmp.cleanup()


def test_get_info_marks_stale_when_old_fallback():
    tmp = _swap_cache_dir()
    try:
        yc.save_entry("ABC", _good_info())
        p = yc.cache_path("ABC")
        data = json.loads(p.read_text())
        old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["fetched_at_utc"] = old
        p.write_text(json.dumps(data))
        res = yc.get_info("ABC", fetcher=lambda s: {})
        assert res["metadata_stale"] is True, res
    finally:
        tmp.cleanup()


def test_get_info_returns_empty_when_no_cache_and_live_empty():
    tmp = _swap_cache_dir()
    try:
        res = yc.get_info("UNKNOWN", fetcher=lambda s: {})
        assert res["source"] == "yfinance_empty"
        assert res["info"] == {}
    finally:
        tmp.cleanup()


def test_get_info_live_fetch_caches_for_next_call():
    tmp = _swap_cache_dir()
    try:
        calls = {"n": 0}
        def fetcher(_):
            calls["n"] += 1
            return _good_info()
        # First call: live + cache.
        r1 = yc.get_info("ABC", fetcher=fetcher)
        assert r1["source"] == "yfinance"
        # Second call: should hit fresh cache, no live call.
        r2 = yc.get_info("ABC", fetcher=fetcher)
        assert calls["n"] == 1
        assert r2["source"] == "yfinance_fresh_cache"
    finally:
        tmp.cleanup()


def test_safe_filename_strips_unsafe_chars():
    assert yc._safe_filename("BRK.B") == "BRK.B.json"
    assert yc._safe_filename("BTC-USD") == "BTC-USD.json"
    # weird chars become _
    assert yc._safe_filename("FOO/BAR") == "FOO_BAR.json"


def test_is_useful_info():
    assert yc._is_useful_info({"sector": "Tech"}) is True
    assert yc._is_useful_info({"marketCap": 1}) is True
    assert yc._is_useful_info({}) is False
    assert yc._is_useful_info(None) is False
    assert yc._is_useful_info({"shortName": ""}) is False


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
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
        print(f"FAIL: {failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

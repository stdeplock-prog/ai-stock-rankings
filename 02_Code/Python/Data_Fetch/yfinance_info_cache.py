"""yfinance_info_cache.py

Persistent cache for yfinance `.info` payloads used by the watchlist SUPP
path. Yahoo's metadata endpoint is rate-limited and intermittently returns
empty payloads, which causes downstream classification to drop SUPP rows
back to `unknown` (and ultimately strips fundamentals). On 2026-05-04 a
single rate-limit episode took SUPP from 64 full-fundamental rows to 0.

Design:
  * Cache directory: data/cache/yfinance_info/<SAFE_SYMBOL>.json
  * Each entry is a JSON object with:
      symbol, fetched_at_utc, info, cache_age_days (computed at read time),
      provenance ("yfinance" on the run that wrote it), source_run_url
  * Fresh entries (under FRESH_AGE_DAYS) short-circuit the network call.
  * Older entries serve as a *fallback* on rate-limit or empty-info: the
    caller still calls yfinance first, but if yfinance returns empty,
    the helper returns the cached payload with a `_cache_fallback=True`
    flag and `cache_age_days` populated so consumers can mark the row
    stale rather than silently filling fields.
  * Symbol normalization: uppercase, replace os-unsafe chars with '_'.

This module is stdlib-only at import time (no yfinance dependency); the
caller passes in fetched info dicts. That way unit tests can exercise it
without yfinance installed.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = REPO_ROOT / "data" / "cache" / "yfinance_info"

# How long an entry is considered fresh enough to skip the live call.
# yfinance fundamentals don't change intraday; one calendar day is plenty
# and matches the morning-only enrichment pattern the workflow uses.
FRESH_AGE_DAYS = 1.0
# How long a cached entry can serve as a rate-limit fallback. Beyond this
# we still serve it but mark `metadata_stale=True` so the dashboard can
# label the row honestly. Drop entirely after FALLBACK_MAX_AGE_DAYS so
# the cache file doesn't grow unbounded.
STALE_AGE_DAYS = 7.0
FALLBACK_MAX_AGE_DAYS = 60.0


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(symbol: str) -> str:
    s = symbol.strip().upper()
    s = _SAFE_RE.sub("_", s)
    return s + ".json"


def cache_path(symbol: str) -> Path:
    return CACHE_DIR / _safe_filename(symbol)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Allow trailing Z.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _age_days(fetched_at: datetime | None) -> float | None:
    if fetched_at is None:
        return None
    delta = _now_utc() - fetched_at
    return round(delta.total_seconds() / 86400.0, 2)


def load_entry(symbol: str) -> dict | None:
    """Return the raw cached entry if present and parseable, else None.
    The returned dict has a `cache_age_days` field added at read time."""
    p = cache_path(symbol)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = _parse_iso(data.get("fetched_at_utc"))
    age = _age_days(fetched_at)
    data["cache_age_days"] = age
    data["fetched_at_dt"] = fetched_at  # internal, not persisted
    return data


def is_fresh(entry: dict | None, fresh_days: float = FRESH_AGE_DAYS) -> bool:
    """True iff entry exists and its age is below the freshness threshold."""
    if not entry:
        return False
    age = entry.get("cache_age_days")
    if age is None:
        return False
    return age <= fresh_days


def is_usable_fallback(entry: dict | None,
                      max_age_days: float = FALLBACK_MAX_AGE_DAYS) -> bool:
    """True iff entry can serve as a fallback when yfinance returns empty."""
    if not entry:
        return False
    age = entry.get("cache_age_days")
    if age is None:
        return False
    return age <= max_age_days


def _is_useful_info(info: dict | None) -> bool:
    """Heuristic for "yfinance returned something usable" — good enough to
    cache. We require at least price/sector/marketCap/quoteType signal so
    a totally empty .info doesn't displace a useful older cache entry."""
    if not isinstance(info, dict) or not info:
        return False
    keys = (
        "regularMarketPrice", "currentPrice", "previousClose",
        "marketCap", "sector", "quoteType", "shortName", "longName",
        "trailingPE", "industry",
    )
    return any(info.get(k) not in (None, "", 0) for k in keys)


def save_entry(symbol: str, info: dict, *, run_url: str | None = None) -> None:
    """Persist info to disk. No-op for empty / clearly-broken payloads to
    avoid evicting a previously-good entry with a rate-limited blank one."""
    if not _is_useful_info(info):
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol.strip().upper(),
        "fetched_at_utc": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "info": info,
        "provenance": "yfinance",
    }
    if run_url:
        payload["source_run_url"] = run_url
    p = cache_path(symbol)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, p)


def get_info(symbol: str, *, fetcher,
             fresh_days: float = FRESH_AGE_DAYS,
             stale_days: float = STALE_AGE_DAYS,
             counters: dict | None = None,
             run_url: str | None = None) -> dict:
    """High-level helper for callers: returns a dict
        {info, source, cache_age_days, metadata_stale}
    where `source` is one of:
      * yfinance_fresh_cache       — fresh cache hit, network skipped
      * yfinance                   — live fetch succeeded
      * yfinance_cache_fallback    — live fetch returned empty/blank, served cache
      * yfinance_empty             — live + cache both empty
    `fetcher` is a callable taking the symbol and returning the live yfinance
    info dict (or {} on error). Decoupled so unit tests can pass a stub.

    counters (optional dict) is incremented in place to expose hit/miss/
    fallback metrics in source_meta.
    """
    counters = counters if counters is not None else {}
    counters.setdefault("cache_hit_fresh", 0)
    counters.setdefault("cache_miss", 0)
    counters.setdefault("network_call", 0)
    counters.setdefault("fallback_to_cache", 0)
    counters.setdefault("rate_limit_or_empty", 0)
    counters.setdefault("network_success", 0)

    entry = load_entry(symbol)

    # Fresh-cache short-circuit: don't even call yfinance.
    if is_fresh(entry, fresh_days=fresh_days):
        counters["cache_hit_fresh"] += 1
        return {
            "info": entry["info"],
            "source": "yfinance_fresh_cache",
            "cache_age_days": entry.get("cache_age_days"),
            "metadata_stale": False,
        }

    counters["cache_miss"] += 1
    counters["network_call"] += 1
    try:
        live = fetcher(symbol) or {}
    except Exception:
        live = {}

    if _is_useful_info(live):
        counters["network_success"] += 1
        save_entry(symbol, live, run_url=run_url)
        return {
            "info": live,
            "source": "yfinance",
            "cache_age_days": 0,
            "metadata_stale": False,
        }

    # Live failed/empty: try cache as fallback.
    counters["rate_limit_or_empty"] += 1
    if is_usable_fallback(entry):
        counters["fallback_to_cache"] += 1
        age = entry.get("cache_age_days") or 0.0
        return {
            "info": entry["info"],
            "source": "yfinance_cache_fallback",
            "cache_age_days": age,
            "metadata_stale": age > stale_days,
        }

    return {
        "info": {},
        "source": "yfinance_empty",
        "cache_age_days": None,
        "metadata_stale": False,
    }

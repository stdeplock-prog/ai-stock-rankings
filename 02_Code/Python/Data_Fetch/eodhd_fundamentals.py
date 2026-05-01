"""eodhd_fundamentals.py

Optional EODHD fundamentals fetcher used by the watchlist generator to enrich
SUPP rows that the main yfinance pipeline does not cover (foreign listings,
small caps, OTC, etc.). Stays inactive when EODHD_API_KEY is unset, so the
default code path (yfinance) is unchanged for callers without an API key.

Key responsibilities:
  * Symbol normalization to EODHD's `TICKER.EXCHANGE` form. US listings get
    `.US`; tickers that already carry an exchange suffix (e.g. `005930.KS`,
    `SHOP.TO`) are passed through unchanged. Crypto and ETFs are returned as
    None — those instruments are not what this module is for, and a None
    return tells the caller to fall through to yfinance / metadata-only.
  * Field mapping from EODHD's nested fundamentals JSON onto the same field
    names the main pipeline already persists (FUND_FIELDS in fetch_ohlcv.py),
    so downstream code (fundamental_from_yfinance, etc.) does not need to
    know which provider produced the values.
  * Cache outputs under data/cache/eodhd_fundamentals/<symbol>.json so reruns
    are cheap and stable, and so unit tests can be exercised against a
    pre-populated cache without hitting the network.

This module never fabricates fields. If EODHD returns a section but a field
is missing, the mapped output value is None.
"""

import json
import os
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CACHE_DIR = os.path.join(REPO_ROOT, "data", "cache", "eodhd_fundamentals")

EODHD_BASE = "https://eodhd.com/api"

# Foreign-suffix exchanges EODHD recognises directly. If a watchlist symbol
# already carries one of these suffixes we leave it alone. The list is
# intentionally narrow; an unknown suffix is treated as foreign-already and
# passed through unchanged rather than guessed at.
KNOWN_EXCHANGE_SUFFIXES = {
    "US",   # NYSE/NASDAQ/AMEX (default for plain US tickers)
    "TO", "V",          # Canada (Toronto, Venture)
    "L",                # London
    "DE", "F", "BE", "MU", "HM", "HA", "DU", "STU", "XETRA",  # Germany
    "PA",               # Paris
    "MI",               # Milan
    "MC",               # Madrid
    "AS",               # Amsterdam
    "BR",               # Brussels
    "LS",               # Lisbon
    "IR",               # Ireland
    "SW", "VX",         # Switzerland
    "ST",               # Stockholm
    "CO",               # Copenhagen
    "OL",               # Oslo
    "HE",               # Helsinki
    "WAR",              # Warsaw
    "VI",               # Vienna
    "AT",               # Athens
    "IS",               # Istanbul
    "JO",               # Johannesburg
    "TA",               # Tel Aviv
    "HK",               # Hong Kong
    "SS", "SZ",         # Shanghai, Shenzhen
    "T",                # Tokyo
    "KS", "KQ",         # Korea (KOSPI, KOSDAQ)
    "TW", "TWO",        # Taiwan
    "BO", "NSE",        # India
    "AX",               # Australia
    "NZ",               # New Zealand
    "SA",               # Brazil
    "MX",               # Mexico
    "BA",               # Buenos Aires
    "SN",               # Santiago
    "BK",               # Bangkok
    "JK",               # Jakarta
    "KL",               # Kuala Lumpur
    "SI",               # Singapore
    "ME",               # Moscow
}

# Mapping from EODHD fundamentals JSON paths to our canonical field names.
# Paths are dotted into the nested JSON; each entry yields the same field
# name the yfinance pipeline already produces, so consumers (e.g.
# fundamental_from_yfinance) work unchanged.
EODHD_FIELD_MAP = {
    "shortName":       ("General", "Name"),
    "sector":          ("General", "Sector"),
    "industry":        ("General", "Industry"),
    "country":         ("General", "CountryName"),
    "marketCap":       ("Highlights", "MarketCapitalization"),
    "trailingPE":      ("Highlights", "PERatio"),
    "forwardPE":       ("Valuation", "ForwardPE"),
    "trailingEps":     ("Highlights", "EarningsShare"),
    "revenueGrowth":   ("Highlights", "QuarterlyRevenueGrowthYOY"),
    "earningsGrowth":  ("Highlights", "QuarterlyEarningsGrowthYOY"),
    "dividendYield":   ("Highlights", "DividendYield"),
    "beta":            ("Technicals", "Beta"),
    "fiftyTwoWeekHigh": ("Technicals", "52WeekHigh"),
    "fiftyTwoWeekLow":  ("Technicals", "52WeekLow"),
    "returnOnEquity":  ("Highlights", "ReturnOnEquityTTM"),
    "returnOnAssets":  ("Highlights", "ReturnOnAssetsTTM"),
    "profitMargins":   ("Highlights", "ProfitMargin"),
    "operatingMargins": ("Highlights", "OperatingMarginTTM"),
    "priceToBook":     ("Valuation", "PriceBookMRQ"),
}


def normalize_symbol_for_eodhd(sym: str) -> Optional[str]:
    """Return the EODHD `TICKER.EXCHANGE` form, or None if the symbol is
    something EODHD's equity fundamentals endpoint should not be asked about
    (crypto, mutual funds, blank input).

    Plain US tickers get `.US`. Symbols already carrying a known foreign
    suffix are passed through. `BRK.B` style class tickers are rewritten to
    EODHD's `BRK-B.US` convention so callers do not have to remember it.
    Crypto pairs (`*-USD` or `*USD` with a plausible base) are excluded —
    the watchlist generator already classifies those as crypto and the
    fundamentals endpoint would either error or return a useless payload.
    """
    if not sym:
        return None
    s = sym.strip().upper()
    if not s:
        return None

    # Crypto: explicit BTC-USD style and the legacy SOLUSD-style spellings.
    if s.endswith("-USD"):
        return None
    if s.endswith("USD") and len(s) > 3 and s[:-3].isalpha() and len(s[:-3]) <= 5:
        return None

    # Already carries a known exchange suffix — pass through unchanged.
    # Check known-suffix membership BEFORE the class-share rewrite so that
    # legitimate single-letter foreign codes (e.g. .L for London) aren't
    # misinterpreted as a US class share.
    if "." in s:
        head, tail = s.rsplit(".", 1)
        if tail in KNOWN_EXCHANGE_SUFFIXES:
            return s
        # Class-share dot (BRK.B, BF.B): rewrite to EODHD US convention.
        # Only applies when tail is a single letter AND not a known foreign
        # exchange code (handled above).
        if len(tail) == 1 and tail.isalpha():
            return f"{head}-{tail}.US"
        # Unknown suffix: leave alone; EODHD may or may not know it. Caller
        # will treat a 404 as "no fundamentals" and fall through.
        return s

    # Plain US ticker.
    return f"{s}.US"


def _dig(d: dict, path: tuple):
    """Walk a nested dict by tuple path, returning None on any miss or
    non-dict intermediate. Treats empty strings and explicit "NA" sentinels
    as None so callers can rely on real values being present when set."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    if isinstance(cur, str):
        s = cur.strip()
        if s in ("", "NA", "N/A", "None"):
            return None
        # Numeric strings — try to coerce, otherwise return as-is for text fields.
        try:
            if "." in s or "e" in s.lower():
                return float(s)
            return int(s)
        except ValueError:
            return cur
    return cur


def map_eodhd_payload(payload: dict) -> dict:
    """Project the raw EODHD fundamentals JSON onto our canonical field set.
    Missing fields stay None so the resulting dict is safe to feed into the
    same downstream code that consumes yfinance .info dicts.
    """
    out = {}
    for canonical, path in EODHD_FIELD_MAP.items():
        out[canonical] = _dig(payload or {}, path)
    return out


def _cache_path(symbol_eodhd: str) -> str:
    safe = symbol_eodhd.replace("/", "_")
    return os.path.join(CACHE_DIR, f"{safe}.json")


def _load_cache(symbol_eodhd: str) -> Optional[dict]:
    path = _cache_path(symbol_eodhd)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(symbol_eodhd: str, payload: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol_eodhd)
    try:
        with open(path, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"  EODHD cache write failed for {symbol_eodhd}: {e}")


def fetch_eodhd_fundamentals(symbol: str,
                             api_key: Optional[str] = None,
                             use_cache: bool = True,
                             request_fn=None) -> Optional[dict]:
    """Fetch EODHD fundamentals for a watchlist symbol and return the canonical
    field dict, or None when:
      * the symbol is not appropriate for the equity fundamentals endpoint
        (crypto, blank, etc.)
      * no API key is available and there is no cached payload to fall back to
      * the network call fails or returns a non-200 response

    Args:
      symbol: raw watchlist symbol (e.g. "AAPL", "005930.KS").
      api_key: EODHD API token; defaults to EODHD_API_KEY env var.
      use_cache: read/write data/cache/eodhd_fundamentals/<sym>.json.
      request_fn: optional injection point for tests; should mimic
        requests.get (returns an object with .status_code and .json()).

    Returns:
      dict mapped via map_eodhd_payload(), augmented with `_eodhd_symbol`
      so callers can record what was actually queried, or None.
    """
    eodhd_sym = normalize_symbol_for_eodhd(symbol)
    if not eodhd_sym:
        return None

    # Cache hit short-circuits both API-key and network checks. This is what
    # makes tests deterministic without live credentials.
    if use_cache:
        cached = _load_cache(eodhd_sym)
        if cached is not None:
            mapped = map_eodhd_payload(cached)
            mapped["_eodhd_symbol"] = eodhd_sym
            mapped["_eodhd_source"] = "cache"
            return mapped

    key = api_key if api_key is not None else os.environ.get("EODHD_API_KEY", "")
    if not key:
        return None

    if request_fn is None:
        try:
            import requests
            request_fn = requests.get
        except Exception:
            return None

    url = f"{EODHD_BASE}/fundamentals/{eodhd_sym}"
    try:
        resp = request_fn(url, params={"api_token": key, "fmt": "json"}, timeout=30)
    except Exception as e:
        print(f"  EODHD fundamentals request failed for {eodhd_sym}: {e}")
        return None

    status = getattr(resp, "status_code", None)
    if status != 200:
        # 404 and 401 both legitimately produce None — let the caller fall
        # through to yfinance / metadata-only without flagging as failure.
        return None

    try:
        payload = resp.json()
    except Exception as e:
        print(f"  EODHD fundamentals JSON parse failed for {eodhd_sym}: {e}")
        return None
    if not isinstance(payload, dict) or not payload:
        return None

    if use_cache:
        _save_cache(eodhd_sym, payload)

    mapped = map_eodhd_payload(payload)
    mapped["_eodhd_symbol"] = eodhd_sym
    mapped["_eodhd_source"] = "api"
    return mapped

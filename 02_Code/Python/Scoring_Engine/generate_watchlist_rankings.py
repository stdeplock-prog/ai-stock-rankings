"""generate_watchlist_rankings.py

Generates data/watchlist_rankings.json for the secondary dashboard page
(watchlist.html). The ticker universe is the union of:

  * Symbols in the user-provided CSV (committed at data/watchlist_sources.json)
  * Symbols extracted from a TradingView watchlist (also committed there)

Approach:
  1. Read data/watchlist_sources.json. Apply symbol_overrides (e.g. SOLUSD ->
     SOL-USD, 005930 -> 005930.KS) so yfinance / pipeline lookups succeed.
  2. For tickers already scored by the main pipeline (present in
     data/processed/scoring_outputs/rankings.csv), reuse their full row plus
     the matching swing fields and OHLCV closes - identical to what the main
     dashboard renders. Re-rank within the watchlist by AI_Score.
  3. For tickers NOT in main rankings (foreign / crypto / OTC / ETFs the
     pipeline doesn't ingest), attempt a lightweight yfinance fetch and
     compute a reduced subset of fields (price, volume, 10-day closes,
     market cap, industry/sector). These rows get a synthetic AI score from
     the basic technicals so they sort sensibly alongside scored rows.
  4. Tickers we cannot resolve at all are recorded under `unavailable` in
     the output JSON rather than silently dropped.

Output JSON shape mirrors data/rankings.json so the existing dashboard
JS (in watchlist.html, copied/forked from index.html) can render it.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Import the EODHD fundamentals helper from the sibling Data_Fetch package.
# It is intentionally side-effect free at import time (no network calls,
# no env reads), so this works in the unit-test environment too.
sys.path.insert(0, os.path.join(REPO_ROOT, "02_Code", "Python", "Data_Fetch"))
try:
    from eodhd_fundamentals import fetch_eodhd_fundamentals, EodhdBudget  # noqa: E402
except Exception:
    fetch_eodhd_fundamentals = None  # type: ignore
    EodhdBudget = None  # type: ignore
try:
    import yfinance_info_cache as yf_cache  # noqa: E402
except Exception:
    yf_cache = None  # type: ignore

SOURCES_FILE   = os.path.join(REPO_ROOT, "data", "watchlist_sources.json")
RANKINGS_CSV   = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "rankings.csv")
SWING_CSV      = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "swing_rankings.csv")
OHLCV_DIR      = os.path.join(REPO_ROOT, "data", "raw", "ohlcv_daily")
OUTPUT_FILE    = os.path.join(REPO_ROOT, "data", "watchlist_rankings.json")

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)


def get_central_now():
    utc_now = datetime.now(timezone.utc)
    offset = timedelta(hours=-5) if 3 <= utc_now.month <= 10 else timedelta(hours=-6)
    return utc_now + offset


def get_central_time_str(dt):
    label = "CDT" if 3 <= dt.month <= 10 else "CST"
    return dt.strftime("%Y-%m-%d %I:%M %p") + " " + label


def safe_str(val, default=""):
    if val is None:
        return default
    s = str(val).strip()
    if s.lower() in ("nan", "none", "n/a", ""):
        return default
    return s


def fmt_market_cap(val):
    try:
        if val is None or pd.isna(val):
            return ""
        n = float(val)
    except Exception:
        return ""
    if n <= 0:
        return ""
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= scale:
            v = n / scale
            return f"{v:.2f}{unit}" if v < 10 else f"{v:.1f}{unit}"
    return f"{n:.0f}"


def load_sources():
    with open(SOURCES_FILE, "r") as f:
        cfg = json.load(f)
    csv_tickers = [t.strip().upper() for t in cfg["sources"]["csv"]["tickers"] if t.strip()]
    tv_tickers  = [t.strip().upper() for t in cfg["sources"]["tradingview"]["tickers"] if t.strip()]
    overrides_raw = cfg.get("symbol_overrides", {}) or {}
    overrides = {k.upper(): v for k, v in overrides_raw.items()}
    return csv_tickers, tv_tickers, overrides


def normalize_symbol(sym, overrides):
    s = sym.strip().upper()
    if s in overrides:
        return overrides[s]
    return s


def source_label(sym, csv_set, tv_set):
    in_csv = sym in csv_set
    in_tv = sym in tv_set
    if in_csv and in_tv:
        return "both"
    if in_csv:
        return "csv"
    if in_tv:
        return "tradingview"
    return "unknown"


def load_swing_lookup():
    if not os.path.exists(SWING_CSV):
        return {}
    try:
        sdf = pd.read_csv(SWING_CSV)
    except Exception as e:
        print(f"Warning: could not read swing rankings: {e}")
        return {}
    keep = [c for c in [
        "Ticker", "SwingScore", "Swing_Rank", "Swing_Tier",
        "ATR_Pct", "Vol_Bucket", "Catalyst_Flag",
        "days_to_earnings", "next_earnings_date",
        "Ext_Rating_Score", "num_analysts", "Ext_Up_Downside_Pct",
    ] if c in sdf.columns]
    sdf = sdf[keep].copy()
    sdf["Ticker"] = sdf["Ticker"].astype(str).str.strip().str.upper()
    return {r["Ticker"]: r for _, r in sdf.iterrows()}


def load_main_rankings_lookup():
    if not os.path.exists(RANKINGS_CSV):
        print(f"Warning: main rankings.csv missing at {RANKINGS_CSV}; watchlist scored rows will be empty.")
        return {}
    df = pd.read_csv(RANKINGS_CSV)
    df["Ticker"] = df["Ticker"].astype(str).str.strip().str.upper()
    return {r["Ticker"]: r for _, r in df.iterrows()}


def closes_and_volume(ticker_for_ohlcv):
    """Return (closes_list, vol_millions) from cached OHLCV CSV, or ([], 0)."""
    closes = []
    vol_millions = 0
    path = os.path.join(OHLCV_DIR, f"{ticker_for_ohlcv}_daily.csv")
    if not os.path.exists(path):
        return closes, vol_millions
    try:
        ohlcv = pd.read_csv(path, index_col=0)
        if isinstance(ohlcv.columns, pd.MultiIndex):
            ohlcv.columns = [c[0] for c in ohlcv.columns]
        ohlcv.columns = [c.title() if isinstance(c, str) else c for c in ohlcv.columns]
        if "Volume" in ohlcv.columns and len(ohlcv) > 0:
            try:
                vol_millions = round(float(ohlcv["Volume"].iloc[-1]) / 1_000_000, 1)
            except Exception:
                vol_millions = 0
        if "Close" in ohlcv.columns:
            raw_closes = ohlcv["Close"].dropna().tail(10).tolist()
            closes = [round(float(c), 2) for c in raw_closes]
    except Exception as e:
        print(f"  Warning: could not read OHLCV for {ticker_for_ohlcv}: {e}")
    return closes, vol_millions


def swing_fields_from_lookup(swing_lookup, ticker):
    """Mirror of export_to_json.py swing field block. Returns dict."""
    srow = swing_lookup.get(ticker)

    def f(col, cast=float, nd=2):
        if srow is None or col not in srow.index:
            return None
        v = srow[col]
        try:
            if pd.isna(v):
                return None
            if cast is float:
                return round(float(v), nd)
            if cast is int:
                return int(float(v))
            if cast is bool:
                if isinstance(v, str):
                    return v.strip().lower() == "true"
                return bool(v)
            return str(v).strip()
        except Exception:
            return None

    raw_swing = f("SwingScore", float, 2)
    return {
        "swing_score":      round(raw_swing / 10.0, 1) if raw_swing is not None else None,
        "swing_rank":       f("Swing_Rank", int),
        "swing_tier":       f("Swing_Tier", str),
        "atr_pct":          f("ATR_Pct", float, 2),
        "vol_bucket":       f("Vol_Bucket", str),
        "catalyst_flag":    f("Catalyst_Flag", bool),
        "days_to_earnings": f("days_to_earnings", int),
        "next_earnings":    f("next_earnings_date", str),
        "ext_rating":       f("Ext_Rating_Score", float, 2),
        "num_analysts":     f("num_analysts", int),
        "upside_pct":       f("Ext_Up_Downside_Pct", float, 1),
    }


def row_from_main(rank, raw_sym, ticker, row, swing_lookup):
    """Build a JSON row for a ticker present in main rankings.csv."""
    closes, vol_millions = closes_and_volume(ticker)
    industry_val = safe_str(row.get("Industry", ""))
    if not industry_val:
        industry_val = safe_str(row.get("Sector", ""))
    si_raw = row.get("Short_Interest", None)
    short_interest = None
    if si_raw is not None and str(si_raw).lower() not in ("nan", "none", ""):
        try:
            short_interest = round(float(si_raw), 1)
        except Exception:
            short_interest = None
    ib_raw = row.get("Insider_Buying", False)
    try:
        insider_buying = bool(ib_raw) if str(ib_raw).lower() not in ("nan", "none", "") else False
    except Exception:
        insider_buying = False
    market_cap_display = fmt_market_cap(row.get("MarketCap", None))
    swing = swing_fields_from_lookup(swing_lookup, ticker)
    out = {
        "rank":            rank,
        "ticker":          raw_sym,
        "company":         safe_str(row.get("Name", "")) or raw_sym,
        "country":         "US",
        "market_cap":      market_cap_display,
        "ai_score":        round(float(row["AI_Score"]), 1) if "AI_Score" in row.index else None,
        "change":          0,
        "fundamental":     round(float(row.get("Fundamental", 5.0)), 1) if "Fundamental" in row.index else None,
        "technical":       round(float(row.get("Technical",   5.0)), 1) if "Technical"   in row.index else None,
        "sentiment":       round(float(row.get("Sentiment",   5.0)), 1) if "Sentiment"   in row.index else None,
        "low_risk":        round(float(row.get("Risk",        5.0)), 1) if "Risk"        in row.index else None,
        "volume_millions": vol_millions,
        "closes":          closes,
        "industry":        industry_val,
        "sector":          safe_str(row.get("Sector", "")),
        "short_interest":  short_interest,
        "insider_buying":  insider_buying,
        "data_source":     "main_pipeline",
    }
    out.update(swing)
    return out


# Symbols known to be non-equity instruments where EODHD's equity fundamentals
# endpoint either errors or returns useless payloads. Tickers in this set are
# explicitly skipped before the live gate so we don't waste budget on them. Add
# new ones as we observe them — the cost of a missing symbol here is one
# wasted live call (caught by the helper's None return), so the list is
# intentionally conservative.
KNOWN_NON_EQUITY_SYMBOLS = {
    # Crypto pairs (also caught by suffix rule, listed for clarity).
    "BTC-USD", "ETH-USD", "SOL-USD", "BTCUSD", "SOLUSD", "ETHUSD",
    # Commodity / sector / thematic ETFs that frequently appear in watchlists.
    "USO", "GUSH", "GLD", "SLV", "IBIT", "QTUM", "MSOS",
    # Class-share class ETFs / vehicles that are not regular equities.
    "BIPC",  # Brookfield Infrastructure Partners (LP class)
}


def _likely_equity_symbol(sym):
    """Return True iff the symbol is a plausible candidate for EODHD's equity
    fundamentals endpoint *before* we know yfinance's classification.

    The watchlist generator previously waited for yfinance to classify a
    ticker as 'equity' before attempting EODHD enrichment. Under yfinance
    rate-limiting that classification frequently lands as 'unknown' even for
    real US equities (sector/marketCap come back empty), which silently
    locked out EODHD enrichment for the very rows it was added to cover.

    This heuristic is the pre-yfinance gate. It is intentionally conservative
    — it filters out cases where calling EODHD is *guaranteed* to be wrong
    (crypto pairs, KRX, known non-equity symbols) but otherwise lets EODHD
    answer the question. EODHD's response (or its absence) is then the
    authoritative signal: if it returns equity-like fundamentals we
    reclassify the row as equity; if it returns nothing we fall through.
    """
    if not sym:
        return False
    s = sym.strip().upper()
    if not s:
        return False
    # Crypto pair patterns — *-USD or trailing USD with alpha base.
    if s.endswith("-USD"):
        return False
    if s.endswith("USD") and len(s) > 3 and s[:-3].isalpha() and len(s[:-3]) <= 5:
        return False
    # Known non-equity symbols (ETFs, funds, etc.) we explicitly do not
    # want to spend EODHD live calls on.
    if s in KNOWN_NON_EQUITY_SYMBOLS:
        return False
    # Foreign listings: only allow through if the suffix is one EODHD will
    # actually classify as equity. KRX (.KS / .KQ) returns useless ETF/fund
    # payloads for the bare-number Korean tickers in this watchlist, so
    # leave those out and let the existing yfinance path handle them.
    if "." in s:
        head, tail = s.rsplit(".", 1)
        if tail in ("KS", "KQ"):
            return False
        # Pure-numeric heads (e.g. 005930.KS, 000660.KS) are KRX-style
        # foreign listings — even with a different suffix, treat as non-
        # equity for EODHD purposes.
        if head.isdigit():
            return False
        # Other foreign suffixes are allowed through; EODHD covers most.
        # A 404 is harmless (None return, no quota cost beyond the call).
        return True
    # Plain US-style ticker. Allow alphabetic (and digit-bearing) tickers
    # of reasonable length. Reject anything with unusual characters.
    if not all(c.isalnum() or c in ("-",) for c in s):
        return False
    if len(s) > 6:
        return False
    return True


def _classify_instrument(symbol, info):
    """Identify instrument kind for SUPP rows so the dashboard can be honest
    about what fundamentals are or are not available.

    Returns one of: 'crypto', 'etf', 'fund', 'foreign', 'otc', 'equity', 'unknown'.
    Fundamentals fields (PE / margins / etc.) only meaningfully apply to
    'equity' rows; the others legitimately stay null.
    """
    sym = (symbol or "").upper()
    if sym.endswith("-USD") or sym.endswith("USD") and any(c.isalpha() for c in sym[:-3]):
        return "crypto"
    quote_type = (info.get("quoteType") or "").upper()
    if quote_type in ("CRYPTOCURRENCY", "CRYPTO"):
        return "crypto"
    if quote_type == "ETF":
        return "etf"
    if quote_type in ("MUTUALFUND", "FUND"):
        return "fund"
    # Foreign listings end with a country suffix (e.g. .KS, .HK, .TO, .L).
    if "." in sym and sym.split(".")[-1].isalpha() and len(sym.split(".")[-1]) <= 3:
        return "foreign"
    market = (info.get("market") or "").lower()
    if "otc" in market:
        return "otc"
    if quote_type == "EQUITY":
        return "equity"
    # yfinance .info often omits quoteType for tickers it returns price data for.
    # Infer equity when the row carries individual-stock signals. Strict
    # sector AND marketCap gating left most rows as 'unknown' even when
    # yfinance returned fields that only apply to equities (e.g. industry,
    # sharesOutstanding, an EPS reading). Loosen to: sector OR marketCap,
    # OR another reliable equity-only marker. We still avoid mis-tagging
    # ETFs/funds because those quoteTypes were caught above; the residual
    # risk is an ETF whose .info lacks quoteType but exposes a sector —
    # acceptable since we only treat as equity, downstream fundamentals
    # gating still requires real PE/growth/EPS to produce a Fundamental
    # score (see fundamental_from_yfinance).
    if info.get("sector") or info.get("marketCap"):
        return "equity"
    equity_only_markers = (
        info.get("industry"),
        info.get("sharesOutstanding"),
        info.get("trailingEps"),
        info.get("epsTrailingTwelveMonths"),
        info.get("trailingPE"),
        info.get("forwardPE"),
        info.get("bookValue"),
        info.get("priceToBook"),
    )
    if any(m for m in equity_only_markers):
        return "equity"
    return "unknown"


def fetch_supplemental(ticker_for_yf, eodhd_budget=None, gate_counts=None,
                        cache_counters=None, run_url=None):
    """Fetch fields via yfinance for a ticker outside the main pipeline.

    Mirrors the FUND_FIELDS set from 02_Code/Python/Data_Fetch/fetch_ohlcv.py
    so SUPP rows expose the same fundamentals as the main pipeline whenever
    the source has them. yfinance is the main pipeline's fundamentals source
    today (see fetch_ohlcv.py FUND_FIELDS), so reusing it keeps the data path
    consistent rather than introducing a parallel patchwork.

    Returns dict with price/closes plus a `fundamentals` sub-dict and an
    `instrument_kind` classification, or None on failure.

    cache_counters: optional dict for the watchlist source_meta to track
    how many SUPP rows hit the persistent yfinance .info cache vs. went
    live vs. fell back after a rate-limit. The cache survives runs so
    SUPP rows stay populated across yfinance rate-limit episodes.
    """
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(ticker_for_yf)
        hist = t.history(period="60d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        if "Close" in hist.columns:
            raw_closes = hist["Close"].dropna().tail(10).tolist()
            closes = [round(float(c), 2) for c in raw_closes]
        else:
            closes = []
        if not closes:
            return None
        last_close = closes[-1]
        try:
            vol_millions = round(float(hist["Volume"].dropna().iloc[-1]) / 1_000_000, 1)
        except Exception:
            vol_millions = 0

        # .info via persistent cache. yfinance.info is the rate-limit hot
        # spot; on the May 4 2026 incident we saw ~64 SUPP fundamentals
        # collapse to 0 in a single run because every .info came back
        # empty. The cache layer (yfinance_info_cache) returns a fresh
        # entry without calling the network when one is available, and
        # falls back to a recent cached payload when yfinance comes back
        # blank. Either way, a single bad day doesn't wipe the watchlist.
        info = {}
        info_source = "yfinance"
        cache_age_days = None
        metadata_stale = False
        if yf_cache is not None:
            def _live(_sym):
                try:
                    return t.info or {}
                except Exception:
                    return {}
            res = yf_cache.get_info(ticker_for_yf, fetcher=_live,
                                     counters=cache_counters,
                                     run_url=run_url)
            info = res.get("info") or {}
            info_source = res.get("source") or "yfinance"
            cache_age_days = res.get("cache_age_days")
            metadata_stale = bool(res.get("metadata_stale"))
        else:
            try:
                info = t.info or {}
            except Exception:
                info = {}

        # Same fundamental field set the main pipeline persists, so dashboard
        # rows look the same regardless of origin. Missing fields stay None.
        fund_keys = [
            "trailingPE", "forwardPE",
            "trailingEps", "epsTrailingTwelveMonths",
            "revenueGrowth", "earningsGrowth",
            "dividendYield", "beta",
            "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
            "returnOnEquity", "returnOnAssets",
            "debtToEquity", "currentRatio",
            "grossMargins", "operatingMargins", "profitMargins",
            "freeCashflow", "priceToBook",
        ]
        fundamentals = {k: info.get(k) for k in fund_keys}
        kind = _classify_instrument(ticker_for_yf, info)

        # Track which provider populated each fundamental field. Default to
        # yfinance for whatever yfinance gave us; EODHD enrichment below
        # overlays missing fields and stamps them as eodhd.
        provenance = {
            k: ("yfinance" if v is not None else None) for k, v in fundamentals.items()
        }
        fundamental_source = "yfinance" if any(v is not None for v in fundamentals.values()) else None

        # EODHD enrichment for equities the watchlist cares about: foreign
        # listings, small-caps, OTC names that yfinance .info often returns
        # empty for. The pre-fix flow gated EODHD on yfinance having already
        # classified the row as 'equity'. That broke under yfinance rate-
        # limiting: rows came back with empty .info (kind='unknown'), so
        # EODHD never got a chance to classify or enrich them — defeating
        # the whole reason EODHD was wired in.
        #
        # The new flow lets EODHD enrich any *likely-equity* symbol whose
        # source-side metadata (suffix, override mapping, known non-equity
        # list) doesn't disqualify it, regardless of yfinance's verdict.
        # When EODHD returns equity-like fundamentals for a row we marked
        # 'unknown', we reclassify the row to 'equity' so the downstream
        # composite scoring kicks in. The SUPP origin label is preserved.
        eodhd_used = False
        eodhd_symbol_used = None
        eodhd_deferred = False
        likely_equity = _likely_equity_symbol(ticker_for_yf)
        eodhd_eligible = (kind == "equity") or (kind == "unknown" and likely_equity)

        if not eodhd_eligible:
            if gate_counts is not None:
                if kind in ("crypto", "etf", "fund"):
                    gate_counts["skipped_known_non_equity"] += 1
                elif kind == "foreign":
                    # Foreign listings whose suffix _likely_equity_symbol
                    # excluded (KRX, numeric heads). The yfinance flow
                    # still serves them via metadata-only.
                    gate_counts["skipped_known_non_equity"] += 1
                elif kind == "unknown" and not likely_equity:
                    gate_counts["skipped_symbol_pattern"] += 1
                else:
                    gate_counts["skipped_not_equity"] += 1
        elif fetch_eodhd_fundamentals is None:
            if gate_counts is not None:
                gate_counts["skipped_helper_missing"] += 1
        else:
            if gate_counts is not None:
                gate_counts["eligible"] += 1
                if kind == "equity":
                    gate_counts["eligible_yfinance_equity"] += 1
                else:
                    gate_counts["eligible_likely_equity"] += 1
            deferred_before = eodhd_budget.deferred if eodhd_budget is not None else 0
            try:
                eodhd_data = fetch_eodhd_fundamentals(ticker_for_yf, budget=eodhd_budget)
            except Exception as e:
                print(f"  EODHD enrichment failed for {ticker_for_yf}: {e}")
                eodhd_data = None
            if eodhd_data is None and eodhd_budget is not None \
               and eodhd_budget.deferred > deferred_before:
                eodhd_deferred = True
            if eodhd_data:
                eodhd_used = True
                eodhd_symbol_used = eodhd_data.get("_eodhd_symbol")
                for k in fundamentals:
                    if fundamentals.get(k) is None:
                        v = eodhd_data.get(k)
                        if v is not None:
                            fundamentals[k] = v
                            provenance[k] = "eodhd"
                if not safe_str(info.get("sector", "")) and eodhd_data.get("sector"):
                    info["sector"] = eodhd_data["sector"]
                if not safe_str(info.get("industry", "")) and eodhd_data.get("industry"):
                    info["industry"] = eodhd_data["industry"]
                if info.get("marketCap") in (None, 0) and eodhd_data.get("marketCap"):
                    info["marketCap"] = eodhd_data["marketCap"]
                if not safe_str(info.get("shortName") or info.get("longName") or "") and eodhd_data.get("shortName"):
                    info["shortName"] = eodhd_data["shortName"]
                if not safe_str(info.get("country", "")) and eodhd_data.get("country"):
                    info["country"] = eodhd_data["country"]
                yf_count = sum(1 for k, src in provenance.items() if src == "yfinance")
                eo_count = sum(1 for k, src in provenance.items() if src == "eodhd")
                if eo_count and not yf_count:
                    fundamental_source = "eodhd"
                elif eo_count and yf_count:
                    fundamental_source = "yfinance+eodhd"
                # If the row was previously kind='unknown' and EODHD came
                # back with equity-like signal (any of PE / EPS / growth /
                # margins / sector), reclassify to equity so the SUPP
                # downstream composite scoring path applies. We only
                # *promote* to equity — we never downgrade an existing
                # equity classification. ETFs/funds/crypto are gated out
                # above so this only fires on ambiguous 'unknown' rows.
                if kind == "unknown":
                    equity_signal = any(
                        eodhd_data.get(k) is not None for k in (
                            "trailingPE", "trailingEps", "revenueGrowth",
                            "earningsGrowth", "profitMargins", "sector",
                            "marketCap", "industry",
                        )
                    )
                    if equity_signal:
                        kind = "equity"
                        if gate_counts is not None:
                            gate_counts["reclassified_to_equity"] += 1

        return {
            "price":           last_close,
            "closes":          closes,
            "vol_millions":    vol_millions,
            "company":         safe_str(info.get("shortName") or info.get("longName") or ""),
            "industry":        safe_str(info.get("industry", "")),
            "sector":          safe_str(info.get("sector", "")),
            "market_cap_raw":  info.get("marketCap", None),
            "country":         safe_str(info.get("country", "")) or "—",
            "fundamentals":    fundamentals,
            "fundamentals_provenance": provenance,
            "fundamental_source":      fundamental_source,
            "eodhd_used":      eodhd_used,
            "eodhd_symbol":    eodhd_symbol_used,
            "eodhd_deferred":  eodhd_deferred,
            "instrument_kind": kind,
            # yfinance .info cache provenance — exposes when a row was
            # served from the persistent cache so audits can distinguish
            # genuinely missing data from a yfinance rate-limit episode.
            "info_source":     info_source,
            "cache_age_days":  cache_age_days,
            "metadata_stale":  metadata_stale,
        }
    except Exception as e:
        print(f"  yfinance fetch failed for {ticker_for_yf}: {e}")
        return None


def synthetic_score_from_closes(closes):
    """Cheap technical-only AI score for supplemental tickers, on a 0-10 scale.
    Uses last vs 10-day mean as a momentum proxy, clamped to [0,10]."""
    if not closes or len(closes) < 2:
        return 5.0
    last = closes[-1]
    avg = sum(closes) / len(closes)
    if avg <= 0:
        return 5.0
    pct = (last / avg) - 1.0
    score = 5.0 + pct * 25.0
    return round(max(0.0, min(10.0, score)), 1)


def _get(d, key):
    """Safe getter that treats None / NaN / empty as missing."""
    if d is None:
        return None
    v = d.get(key)
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def fundamental_from_yfinance(fundamentals):
    """Ports the Fundamental composite from score_tickers.py so SUPP equity
    rows expose the same 0-10 score the main pipeline computes from the same
    yfinance fields. Identical formula and weights — keep in sync if the
    main scoring formula changes.

    Returns a tuple (fundamental_0_to_10, components_dict) where
    components_dict surfaces the sub-scores actually used so the page can
    explain how the value was derived. Returns (None, None) when there is
    not enough fundamental signal to produce a meaningful score (no PE, no
    growth, no margins) — better to render '—' than fabricate a 5.0.
    """
    if not fundamentals:
        return None, None

    pe        = _get(fundamentals, "trailingPE")
    eps_t     = _get(fundamentals, "trailingEps") or _get(fundamentals, "epsTrailingTwelveMonths")
    eps_g     = _get(fundamentals, "earningsGrowth")
    rev_g     = _get(fundamentals, "revenueGrowth")
    beta      = _get(fundamentals, "beta")

    # Require at least one of PE / growth / earnings momentum signal — pure
    # margin/beta isn't enough to fake a fundamental score honestly.
    has_signal = any(v is not None for v in (pe, eps_t, eps_g, rev_g))
    if not has_signal:
        return None, None

    # PE score (same formula as score_tickers.py).
    if pe is None or pe <= 0:
        pe_score = 50
    elif 10 <= pe <= 20:
        pe_score = 100
    elif pe < 10:
        pe_score = max(0, 50 + (pe - 10) * 5)
    else:
        pe_score = max(0, 100 - (pe - 20) * 2)

    # Earnings momentum: blend of growth + forward-vs-trailing beat. SUPP
    # path doesn't have forwardEps, so we approximate beat_score via the
    # field if present, otherwise neutral 50.
    eps_forward = _get(fundamentals, "forwardPE")  # placeholder; not the same as forwardEps
    beat_score = 50  # neutral when we don't have forward EPS
    growth_score = 50 if eps_g is None else min(100, max(0, 50 + float(eps_g) * 150))
    earnings_momentum_score = growth_score * 0.5 + beat_score * 0.5

    revenue_score = 50 if rev_g is None else min(100, max(0, 50 + float(rev_g) * 200))
    beta_score    = 50 if beta is None  else min(100, max(0, 100 - abs(float(beta) - 1) * 50))

    fundamental_100 = (
        pe_score                * 0.20 +
        earnings_momentum_score * 0.35 +
        revenue_score           * 0.30 +
        beta_score              * 0.15
    )

    rev_g_f = float(rev_g) if rev_g is not None else 0.0
    if rev_g_f >= 0.10:    multiplier = 1.00
    elif rev_g_f >= 0.05:  multiplier = 0.95
    elif rev_g_f >= 0.02:  multiplier = 0.88
    elif rev_g_f >= 0.0:   multiplier = 0.80
    else:                  multiplier = 0.70
    fundamental_100 = fundamental_100 * multiplier

    return round(fundamental_100 / 10.0, 2), {
        "pe_score":      round(pe_score, 1),
        "growth_score":  round(growth_score, 1),
        "revenue_score": round(revenue_score, 1),
        "beta_score":    round(beta_score, 1),
        "growth_quality_multiplier": multiplier,
    }


def sentiment_and_risk_from_yfinance(closes, fundamentals):
    """SUPP-side approximation of the Sentiment and Risk subscores.

    The main pipeline derives Sentiment from RSI_14 (a true technical
    indicator), which we don't compute on SUPP rows; using the 10-day
    momentum proxy here would be misleading because it would just echo
    the technical score. We therefore leave Sentiment None when no honest
    estimate is available.

    Risk uses beta (same as main pipeline), with the RSI-based modifier
    omitted; if beta is missing we leave Risk None as well.
    """
    sentiment = None  # no honest signal source on the SUPP side
    risk = None
    beta = _get(fundamentals or {}, "beta")
    if beta is not None:
        try:
            b = float(beta)
            risk = round(max(0.0, min(10.0, 10 - abs(b - 1) * 5)), 2)
        except Exception:
            risk = None
    return sentiment, risk


def row_from_supplemental(rank, raw_sym, fetched):
    technical_score = synthetic_score_from_closes(fetched["closes"])
    kind = fetched.get("instrument_kind") or "unknown"
    fundamentals = fetched.get("fundamentals") or {}
    # Fundamentals are only meaningful for individual equities. Crypto/ETFs/
    # funds get the technical-only treatment with the kind surfaced so the
    # dashboard / consumer can label rows honestly instead of inventing zeros.
    enriched = kind == "equity" and any(v is not None for v in fundamentals.values())

    # Compute fundamentals composite for SUPP equities using the SAME formula
    # the main pipeline applies in score_tickers.py — fed by the SAME yfinance
    # fields the main pipeline reads. For non-equity rows or rows with no
    # fundamental signal, leave the score null rather than fabricating one.
    fundamental_score = None
    fundamental_components = None
    sentiment_score = None
    risk_score = None
    if kind == "equity" and enriched:
        fundamental_score, fundamental_components = fundamental_from_yfinance(fundamentals)
        sentiment_score, risk_score = sentiment_and_risk_from_yfinance(fetched["closes"], fundamentals)

    # Composite AI_Score for SUPP equities mirrors the main pipeline weights
    # (30% Tech / 45% Fund / 10% Sentiment / 15% Risk). When components are
    # missing we fall back to the technical-only momentum proxy so the row
    # still sorts. This keeps the computation transparent: see ai_score_basis.
    if fundamental_score is not None:
        # Use neutrals for whichever subscores aren't available so the
        # weighted sum still maps onto the same 0-10 axis.
        sent_use = sentiment_score if sentiment_score is not None else 5.0
        risk_use = risk_score      if risk_score      is not None else 5.0
        ai_score = (
            technical_score   * 0.30 +
            fundamental_score * 0.45 +
            sent_use          * 0.10 +
            risk_use          * 0.15
        )
        ai_score = round(max(0.0, min(10.0, ai_score)), 1)
        ai_basis = "supp_composite"
    else:
        ai_score = technical_score
        ai_basis = "supp_technical_only"

    market_cap_display = fmt_market_cap(fetched["market_cap_raw"])
    sector = fetched["sector"] or ("Crypto" if kind == "crypto" else
                                   "ETF"    if kind == "etf"    else "—")
    industry = fetched["industry"] or sector

    enrichment_source = "yfinance_info" if enriched else (
        "yfinance_price_only" if kind in ("crypto", "etf", "fund") else "yfinance_partial"
    )
    # Distinguish equities that yielded a real Fundamental composite from
    # those that only had thin metadata (sector/marketCap, no PE/growth).
    # The watchlist summary uses this to count "full SUPP enrichment".
    if fundamental_score is not None:
        enrichment_source = "yfinance_fundamentals"
    # If EODHD contributed any of the canonical fields, label the
    # enrichment source accordingly. This is what supp_summary uses to
    # report how many SUPP rows were fully enriched and via which provider.
    fetched_fund_source = fetched.get("fundamental_source")
    if fundamental_score is not None and fetched_fund_source == "eodhd":
        enrichment_source = "eodhd_fundamentals"
    elif fundamental_score is not None and fetched_fund_source == "yfinance+eodhd":
        enrichment_source = "yfinance+eodhd_fundamentals"

    return {
        "rank":             rank,
        "ticker":           raw_sym,
        "company":          fetched["company"] or raw_sym,
        "country":          fetched["country"] or "—",
        "market_cap":       market_cap_display,
        "ai_score":         ai_score,
        "ai_score_basis":   ai_basis,
        "change":           0,
        "fundamental":      fundamental_score,
        "technical":        technical_score,
        "sentiment":        sentiment_score,
        "low_risk":         risk_score,
        "volume_millions":  fetched["vol_millions"],
        "closes":           fetched["closes"],
        "industry":         industry,
        "sector":           sector,
        "short_interest":   None,
        "insider_buying":   False,
        "swing_score":      None,
        "swing_rank":       None,
        "swing_tier":       None,
        "atr_pct":          None,
        "vol_bucket":       None,
        "catalyst_flag":    None,
        "days_to_earnings": None,
        "next_earnings":    None,
        "ext_rating":       None,
        "num_analysts":     None,
        "upside_pct":       None,
        # Detailed fundamentals from yfinance .info (same field set as
        # 02_Code/Python/Data_Fetch/fetch_ohlcv.py FUND_FIELDS). Useful for
        # downstream consumers; the current dashboard does not render them.
        "fundamentals":     fundamentals if enriched else {},
        "fundamental_components": fundamental_components,
        # Concrete provider for the fundamental composite, derived from
        # field-level provenance. Values: yfinance, eodhd, yfinance+eodhd,
        # or None (no composite produced).
        "fundamental_source":     (
            fetched.get("fundamental_source") if fundamental_score is not None else None
        ) or ("yfinance_derived" if fundamental_score is not None else None),
        "fundamentals_provenance": fetched.get("fundamentals_provenance"),
        "eodhd_fundamentals":     bool(fetched.get("eodhd_used")),
        "eodhd_symbol":           fetched.get("eodhd_symbol"),
        "eodhd_deferred":         bool(fetched.get("eodhd_deferred")),
        "instrument_kind":  kind,
        "enrichment_source": enrichment_source,
        "data_source":      "supplemental_yfinance",
        # yfinance .info cache provenance for this row. info_source is
        # one of: yfinance / yfinance_fresh_cache / yfinance_cache_fallback /
        # yfinance_empty. metadata_stale=True means the cache served data
        # older than the freshness threshold (caller may surface "stale"
        # badge on the dashboard). cache_age_days is None for live fetches.
        "info_source":      fetched.get("info_source"),
        "cache_age_days":   fetched.get("cache_age_days"),
        "metadata_stale":   bool(fetched.get("metadata_stale")),
    }


def main():
    csv_tickers, tv_tickers, overrides = load_sources()

    # Pre-canonicalize input symbols against their override targets so that
    # e.g. SOLUSD (TradingView form) and SOL-USD (yfinance form) collapse to a
    # single watchlist entry instead of producing two separate rows.
    def canon(sym):
        s = sym.upper()
        return overrides.get(s, s)

    csv_set = {canon(t) for t in csv_tickers}
    tv_set  = {canon(t) for t in tv_tickers}

    # Combined unique input symbols, preserving canonical (override-applied) form.
    combined = sorted(csv_set | tv_set)
    print(f"Watchlist: {len(combined)} unique input symbols "
          f"(csv={len(csv_set)}, tv={len(tv_set)}, both={len(csv_set & tv_set)})")

    main_lookup  = load_main_rankings_lookup()
    swing_lookup = load_swing_lookup()

    rows = []
    unavailable = []
    source_counts = {"main_pipeline": 0, "supplemental_yfinance": 0, "unavailable": 0}
    src_label_counts = {"csv": 0, "tradingview": 0, "both": 0}

    # Allow opting out of the network fetch (CI flag).
    allow_supplemental = os.environ.get("WATCHLIST_DISABLE_SUPPLEMENTAL", "").lower() not in ("1", "true", "yes")

    # Per-run live-call budget for EODHD fundamentals. The free EODHD plan
    # is tight on daily fundamentals calls; without a guard a manual or
    # morning workflow run could blow through the quota by querying every
    # uncached SUPP equity. Cache hits are always allowed and never count
    # against the budget. Default chosen conservatively for the free plan
    # (a few dozen total calls/day covers fundamentals + catalysts + buffer).
    try:
        max_live_calls = int(os.environ.get("EODHD_MAX_FUNDAMENTAL_CALLS", "15"))
    except ValueError:
        max_live_calls = 15

    # Boolean gate from the workflow. Defaults to "true" so local invocations
    # (and CI environments without the new variable) keep prior behavior:
    # respect the API-key presence and the call budget. Setting this to a
    # falsey value forces the budget to zero so no live calls are made,
    # regardless of whether the key is present. Cache hits remain free.
    enabled_raw = os.environ.get("EODHD_ENRICHMENT_ENABLED", "true").strip().lower()
    eodhd_enabled = enabled_raw in ("1", "true", "yes", "on")
    if not eodhd_enabled:
        max_live_calls = 0

    eodhd_budget = EodhdBudget(max_live_calls) if EodhdBudget is not None else None

    # Pre-flight visibility on the EODHD enrichment gate. Logs the boolean
    # without ever leaking the secret value. The env-derived flag is
    # forced onto the budget so that the persisted JSON answers
    # "was the secret available to this run?" even when the helper is
    # never invoked (no eligible rows / yfinance rate-limited / etc.) —
    # without it, eodhd_key_present would stay False on a budget that
    # never got touched, indistinguishable from a missing secret.
    eodhd_key_in_env = bool(os.environ.get("EODHD_API_KEY", ""))
    if eodhd_budget is not None and eodhd_key_in_env:
        eodhd_budget.note_key_present()
    print(f"EODHD enrichment: enabled={eodhd_enabled} "
          f"key_present={eodhd_key_in_env} "
          f"max_live_calls={max_live_calls} "
          f"helper_loaded={fetch_eodhd_fundamentals is not None}")
    # Track per-row gating reasons that don't reach the helper at all so the
    # final summary explains every supp row's outcome.
    eodhd_gate_counts = {
        "skipped_not_equity": 0,
        "skipped_known_non_equity": 0,
        "skipped_symbol_pattern": 0,
        "skipped_helper_missing": 0,
        "eligible": 0,
        "eligible_yfinance_equity": 0,
        "eligible_likely_equity": 0,
        "reclassified_to_equity": 0,
    }

    # yfinance .info cache hit/miss/fallback counters. Surfaced under
    # source_meta.yfinance_info_cache so the data-quality audit can
    # distinguish "genuinely missing data" from "yfinance was rate-limited
    # but cache covered us" — the latter should not WARN on supp coverage.
    yf_cache_counters: dict[str, int] = {}
    run_url = os.environ.get("GITHUB_RUN_URL", "") or None

    # Build rows. We rank later by AI_Score, then assign 1..N.
    pending = []  # list of dicts before ranking
    for raw_sym in combined:
        normalized = normalize_symbol(raw_sym, overrides)
        # 1) main pipeline: try both raw and normalized; main pipeline tickers
        #    are usually US (no override needed) so raw is correct in most cases.
        main_key = None
        if raw_sym in main_lookup:
            main_key = raw_sym
        elif normalized in main_lookup:
            main_key = normalized
        if main_key:
            row = row_from_main(0, raw_sym, main_key, main_lookup[main_key], swing_lookup)
            pending.append(row)
            source_counts["main_pipeline"] += 1
            continue

        # 2) supplemental yfinance fetch for non-pipeline tickers
        if allow_supplemental:
            fetched = fetch_supplemental(normalized, eodhd_budget=eodhd_budget,
                                          gate_counts=eodhd_gate_counts,
                                          cache_counters=yf_cache_counters,
                                          run_url=run_url)
            if fetched:
                pending.append(row_from_supplemental(0, raw_sym, fetched))
                source_counts["supplemental_yfinance"] += 1
                continue

        # 3) Could not resolve.
        unavailable.append({
            "input": raw_sym,
            "tried": normalized if normalized != raw_sym else None,
            "source": source_label(raw_sym, csv_set, tv_set),
            "reason": "not in main rankings and supplemental fetch failed or disabled"
        })
        source_counts["unavailable"] += 1

    # Sort by AI score desc, with None last
    def sort_key(r):
        s = r.get("ai_score")
        return (-(s if s is not None else -1), r["ticker"])

    pending.sort(key=sort_key)
    for i, r in enumerate(pending, 1):
        r["rank"] = i
        rows.append(r)
        lbl = source_label(r["ticker"], csv_set, tv_set)
        if lbl in src_label_counts:
            src_label_counts[lbl] += 1
        r["source"] = lbl

    # SUPP enrichment breakdown so the dashboard / consumers can see what
    # fraction of supplemental rows came back with full fundamentals vs.
    # technical-only / unavailable.
    supp_kind_counts = {}
    supp_enrich_counts = {}
    supp_info_source_counts: dict[str, int] = {}
    supp_metadata_stale = 0
    # Higher-level summary aimed at the watchlist UI (option #4).
    supp_summary = {
        "total":           0,
        "full_fundamentals": 0,   # fundamentals composite produced (any provider)
        "eodhd_enriched":    0,   # at least one fundamental field came from EODHD
        "metadata_only":     0,   # enrichment_source == yfinance_info (sector/mc but no PE/growth)
        "price_only":        0,   # crypto/etf/fund with price but no fundamentals
        "technical_only":    0,   # equity with neither fundamentals nor sector/mc
    }
    for r in rows:
        if r.get("data_source") != "supplemental_yfinance":
            continue
        supp_summary["total"] += 1
        k = r.get("instrument_kind") or "unknown"
        supp_kind_counts[k] = supp_kind_counts.get(k, 0) + 1
        es = r.get("enrichment_source") or "unknown"
        supp_enrich_counts[es] = supp_enrich_counts.get(es, 0) + 1
        if es in ("yfinance_fundamentals", "eodhd_fundamentals", "yfinance+eodhd_fundamentals"):
            supp_summary["full_fundamentals"] += 1
        elif es == "yfinance_info":
            supp_summary["metadata_only"] += 1
        elif es == "yfinance_price_only":
            supp_summary["price_only"] += 1
        else:
            supp_summary["technical_only"] += 1
        if r.get("eodhd_fundamentals"):
            supp_summary["eodhd_enriched"] += 1
        info_src = r.get("info_source") or "unknown"
        supp_info_source_counts[info_src] = supp_info_source_counts.get(info_src, 0) + 1
        if r.get("metadata_stale"):
            supp_metadata_stale += 1
    supp_summary["unavailable"] = len(unavailable)

    central_now = get_central_now()
    today_str = central_now.strftime("%Y-%m-%d")
    as_of_str = get_central_time_str(central_now)

    output = {
        "as_of":      as_of_str,
        "open_date":  today_str,
        "is_open_run": False,
        "universe":   "Watchlist (CSV + TradingView 203377841)",
        "source_meta": {
            "csv_count":          len(csv_set),
            "tradingview_count":  len(tv_set),
            "combined_unique":    len(combined),
            "in_both":            len(csv_set & tv_set),
            "scored":             len(rows),
            "unavailable_count":  len(unavailable),
            "by_data_source":     source_counts,
            "by_source_label":    src_label_counts,
            "supp_by_kind":       supp_kind_counts,
            "supp_by_enrichment": supp_enrich_counts,
            "supp_summary":       supp_summary,
            # EODHD live-call quota accounting for the run. Cache hits are
            # always served (free); live_calls is capped at eodhd_budget;
            # eodhd_deferred counts uncached symbols skipped after budget
            # exhaustion. Use this to audit whether a given run touched the
            # free-plan quota ceiling.
            **(eodhd_budget.as_dict() if eodhd_budget is not None else {}),
            # Gating breakdown for the EODHD enrichment path. Together with
            # the budget snapshot above, this fully accounts for every supp
            # equity decision and lets future debugging answer "why was the
            # call skipped" without re-running the pipeline.
            "eodhd_gate":          eodhd_gate_counts,
            # yfinance .info persistent cache accounting. Use this to
            # distinguish "genuinely missing fundamentals" from "yfinance
            # rate-limited and we served cached metadata" — the latter
            # should not raise an alert on SUPP coverage drops.
            "yfinance_info_cache": yf_cache_counters,
            "supp_info_sources":   supp_info_source_counts,
            "supp_metadata_stale": supp_metadata_stale,
        },
        "unavailable": unavailable,
        "rows":        rows,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {OUTPUT_FILE}")
    print(f"  scored rows:        {len(rows)}")
    print(f"  unavailable:        {len(unavailable)}")
    print(f"  data source counts: {source_counts}")
    if unavailable:
        print(f"  unavailable inputs: {[u['input'] for u in unavailable]}")
    if eodhd_budget is not None:
        print(f"  EODHD budget:       {eodhd_budget.as_dict()}")
        print(f"  EODHD gate:         {eodhd_gate_counts}")


if __name__ == "__main__":
    main()

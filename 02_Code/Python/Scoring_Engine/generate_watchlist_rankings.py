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
    # Infer equity when the row carries individual-stock signals (sector +
    # marketCap, neither of which apply to crypto/ETF/forex). Conservative:
    # both must be present to avoid mis-tagging an ETF whose info has only
    # sector. Falls back to 'unknown' otherwise.
    if info.get("sector") and info.get("marketCap"):
        return "equity"
    return "unknown"


def fetch_supplemental(ticker_for_yf):
    """Fetch fields via yfinance for a ticker outside the main pipeline.

    Mirrors the FUND_FIELDS set from 02_Code/Python/Data_Fetch/fetch_ohlcv.py
    so SUPP rows expose the same fundamentals as the main pipeline whenever
    the source has them. yfinance is the main pipeline's fundamentals source
    today (see fetch_ohlcv.py FUND_FIELDS), so reusing it keeps the data path
    consistent rather than introducing a parallel patchwork.

    Returns dict with price/closes plus a `fundamentals` sub-dict and an
    `instrument_kind` classification, or None on failure.
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
        info = {}
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
            "instrument_kind": kind,
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


def row_from_supplemental(rank, raw_sym, fetched):
    score = synthetic_score_from_closes(fetched["closes"])
    kind = fetched.get("instrument_kind") or "unknown"
    fundamentals = fetched.get("fundamentals") or {}
    # Fundamentals are only meaningful for individual equities. Crypto/ETFs/
    # funds get the technical-only treatment with the kind surfaced so the
    # dashboard / consumer can label rows honestly instead of inventing zeros.
    enriched = kind == "equity" and any(v is not None for v in fundamentals.values())

    market_cap_display = fmt_market_cap(fetched["market_cap_raw"])
    sector = fetched["sector"] or ("Crypto" if kind == "crypto" else
                                   "ETF"    if kind == "etf"    else "—")
    industry = fetched["industry"] or sector

    return {
        "rank":             rank,
        "ticker":           raw_sym,
        "company":          fetched["company"] or raw_sym,
        "country":          fetched["country"] or "—",
        "market_cap":       market_cap_display,
        "ai_score":         score,
        "change":           0,
        "fundamental":      None,    # composite score from main pipeline; not derivable here
        "technical":        score,
        "sentiment":        None,
        "low_risk":         None,
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
        "instrument_kind":  kind,
        "enrichment_source": "yfinance_info" if enriched else (
            "yfinance_price_only" if kind in ("crypto", "etf", "fund") else "yfinance_partial"
        ),
        "data_source":      "supplemental_yfinance",
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
            fetched = fetch_supplemental(normalized)
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
    for r in rows:
        if r.get("data_source") != "supplemental_yfinance":
            continue
        k = r.get("instrument_kind") or "unknown"
        supp_kind_counts[k] = supp_kind_counts.get(k, 0) + 1
        es = r.get("enrichment_source") or "unknown"
        supp_enrich_counts[es] = supp_enrich_counts.get(es, 0) + 1

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


if __name__ == "__main__":
    main()

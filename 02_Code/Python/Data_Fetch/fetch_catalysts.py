# fetch_catalysts.py  v1.0
# Fetches earnings calendar + analyst ratings + price targets for all tickers
# in the master universe, and writes to data/processed/catalysts.csv.
#
# Provider is switchable via env var DATA_PROVIDER ("yfinance" default, or "eodhd").
# EODHD requires env var EODHD_API_KEY.
#
# Output columns:
#   Ticker, next_earnings_date, days_to_earnings,
#   last_earnings_surprise_pct,
#   analyst_rating_mean (1=Strong Buy ... 5=Strong Sell),
#   num_analysts,
#   price_target_mean, price_target_upside_pct,
#   news_sent_score_30d

import os
import time
import pandas as pd
from datetime import datetime, timezone

PROVIDER = os.environ.get("DATA_PROVIDER", "yfinance").lower()
EODHD_KEY = os.environ.get("EODHD_API_KEY", "")

REPO_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
UNIVERSE_FILE = os.path.join(REPO_ROOT, "data", "reference", "master_universe.csv")
OUTPUT_FILE   = os.path.join(REPO_ROOT, "data", "processed", "catalysts.csv")
os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)


def _safe_days_to(date_val):
    if date_val is None or pd.isna(date_val):
        return None
    try:
        d = pd.to_datetime(date_val)
        if d.tzinfo is None:
            d = d.tz_localize("UTC")
        now = datetime.now(timezone.utc)
        return (d - now).days
    except Exception:
        return None


def fetch_yfinance(ticker):
    import yfinance as yf
    row = {"Ticker": ticker}
    try:
        t = yf.Ticker(ticker)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}

        # Next earnings
        next_earn = None
        try:
            ed = t.get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                future = ed[ed.index > pd.Timestamp.now(tz=ed.index.tz)]
                if not future.empty:
                    next_earn = future.index.min()
                # Last surprise
                past = ed[ed.index <= pd.Timestamp.now(tz=ed.index.tz)]
                if not past.empty and "Surprise(%)" in past.columns:
                    row["last_earnings_surprise_pct"] = float(past.iloc[0]["Surprise(%)"])
        except Exception:
            pass

        row["next_earnings_date"] = str(next_earn.date()) if next_earn is not None else None
        row["days_to_earnings"]   = _safe_days_to(next_earn)

        # Analyst ratings
        # yfinance recommendationMean: 1=Strong Buy ... 5=Strong Sell
        row["analyst_rating_mean"] = info.get("recommendationMean")
        row["num_analysts"]        = info.get("numberOfAnalystOpinions")
        row["price_target_mean"]   = info.get("targetMeanPrice")

        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        if row["price_target_mean"] and current_price:
            row["price_target_upside_pct"] = (row["price_target_mean"] - current_price) / current_price * 100
        else:
            row["price_target_upside_pct"] = None

        # No native news sentiment in yfinance - leave blank (scorer defaults to neutral)
        row["news_sent_score_30d"] = None

    except Exception as e:
        print(f"    yfinance error for {ticker}: {e}")
    return row


def fetch_eodhd(ticker):
    import requests
    row = {"Ticker": ticker}
    if not EODHD_KEY:
        print(f"    EODHD_API_KEY not set; skipping EODHD for {ticker}")
        return row
    base = "https://eodhd.com/api"
    # Fundamentals (includes AnalystRatings, earnings history)
    try:
        r = requests.get(f"{base}/fundamentals/{ticker}.US",
                         params={"api_token": EODHD_KEY}, timeout=30)
        if r.status_code == 200:
            data = r.json()
            ar = (data.get("AnalystRatings") or {})
            row["analyst_rating_mean"]  = ar.get("Rating")
            row["num_analysts"]         = (ar.get("StrongBuy", 0) + ar.get("Buy", 0) +
                                           ar.get("Hold", 0) + ar.get("Sell", 0) +
                                           ar.get("StrongSell", 0)) or None
            row["price_target_mean"]    = ar.get("TargetPrice")
            hp = (data.get("Highlights") or {})
            cur = hp.get("MarketCapitalization") and hp.get("SharesOutstanding") and (
                hp["MarketCapitalization"] / hp["SharesOutstanding"])
            if row["price_target_mean"] and cur:
                row["price_target_upside_pct"] = (row["price_target_mean"] - cur) / cur * 100
    except Exception as e:
        print(f"    EODHD fundamentals error {ticker}: {e}")

    # Earnings calendar (next upcoming)
    try:
        r = requests.get(f"{base}/calendar/earnings",
                         params={"api_token": EODHD_KEY, "symbols": f"{ticker}.US", "fmt": "json"},
                         timeout=30)
        if r.status_code == 200:
            data = r.json().get("earnings", [])
            upcoming = [e for e in data
                        if pd.to_datetime(e.get("report_date")) > pd.Timestamp.now()]
            if upcoming:
                nxt = sorted(upcoming, key=lambda e: e["report_date"])[0]
                row["next_earnings_date"] = nxt["report_date"]
                row["days_to_earnings"]   = _safe_days_to(nxt["report_date"])
    except Exception as e:
        print(f"    EODHD earnings error {ticker}: {e}")

    # News sentiment (30 day average)
    try:
        r = requests.get(f"{base}/sentiments",
                         params={"api_token": EODHD_KEY, "s": f"{ticker}.US"},
                         timeout=30)
        if r.status_code == 200:
            data = r.json().get(f"{ticker}.US", [])
            recent = data[-30:] if data else []
            if recent:
                avg = sum(d.get("normalized", 0) for d in recent) / len(recent)
                # normalize from [-1, 1] to [0, 100]
                row["news_sent_score_30d"] = (avg + 1) * 50
    except Exception as e:
        print(f"    EODHD sentiment error {ticker}: {e}")

    return row


def main():
    universe = pd.read_csv(UNIVERSE_FILE)
    tickers = universe["Ticker"].tolist()
    print(f"Fetching catalyst data for {len(tickers)} tickers (provider={PROVIDER})...")

    fetch_fn = fetch_eodhd if PROVIDER == "eodhd" else fetch_yfinance
    rows = []
    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i}/{len(tickers)}] {ticker}")
        rows.append(fetch_fn(ticker))
        if i % 10 == 0:
            time.sleep(1)

    df = pd.DataFrame(rows)
    df.to_csv(OUTPUT_FILE, index=False)
    print(f"\nSaved {len(df)} rows to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

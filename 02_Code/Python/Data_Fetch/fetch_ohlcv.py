# fetch_ohlcv.py  v2
# Fetches daily OHLCV data AND fundamental data for all tickers.
# OHLCV  -> 01_Data/Raw/OHLCV_Daily/<TICKER>_daily.csv
# Fundamentals -> 01_Data/Processed/fundamentals.csv  (one row per ticker)

import yfinance as yf
import pandas as pd
import os
import time
from datetime import datetime

# --- CONFIG ---
UNIVERSE_FILE = "G:/My Drive/AI-Stock-Rankings/01_Data/Reference/master_universe.csv"
OHLCV_DIR     = "G:/My Drive/AI-Stock-Rankings/01_Data/Raw/OHLCV_Daily"
FUND_FILE     = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/fundamentals.csv"
START_DATE    = "2023-01-01"
END_DATE      = datetime.today().strftime("%Y-%m-%d")

os.makedirs(OHLCV_DIR, exist_ok=True)
os.makedirs(os.path.dirname(FUND_FILE), exist_ok=True)

# --- LOAD UNIVERSE ---
universe = pd.read_csv(UNIVERSE_FILE)
tickers  = universe["Ticker"].tolist()
print(f"Fetching OHLCV + fundamentals for {len(tickers)} tickers...")
print(f"Date range: {START_DATE} to {END_DATE}\n")

# --- FUNDAMENTAL FIELDS TO COLLECT ---
FUND_FIELDS = [
    "shortName", "sector", "industry",
    "marketCap",
    "trailingPE", "forwardPE",
    "trailingEps", "epsTrailingTwelveMonths",
    "revenueGrowth", "earningsGrowth",
    "dividendYield",
    "beta",
    "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
    "returnOnEquity", "returnOnAssets",
    "debtToEquity",
    "currentRatio",
    "grossMargins", "operatingMargins", "profitMargins",
    "freeCashflow",
    "priceToBook",
    "country",
]

# --- FETCH LOOP ---
ohlcv_success, ohlcv_failed = [], []
fund_rows = []

for i, ticker in enumerate(tickers, 1):
    try:
        t = yf.Ticker(ticker)

        # -- OHLCV --
        df = t.history(start=START_DATE, end=END_DATE, auto_adjust=True)
        if df.empty:
            print(f"  [{i}/{len(tickers)}] WARNING: No OHLCV for {ticker}")
            ohlcv_failed.append(ticker)
        else:
            filepath = os.path.join(OHLCV_DIR, f"{ticker}_daily.csv")
            df.to_csv(filepath)
            ohlcv_success.append(ticker)
            print(f"  [{i}/{len(tickers)}] OHLCV OK: {ticker} ({len(df)} rows)")

        # -- FUNDAMENTALS --
        info = t.info
        row = {"Ticker": ticker}
        for field in FUND_FIELDS:
            row[field] = info.get(field, None)
        fund_rows.append(row)

    except Exception as e:
        print(f"  [{i}/{len(tickers)}] ERROR: {ticker} - {e}")
        ohlcv_failed.append(ticker)
        fund_rows.append({"Ticker": ticker})

    # Be polite to the API - small pause every 10 tickers
    if i % 10 == 0:
        time.sleep(1)

# --- SAVE FUNDAMENTALS ---
fund_df = pd.DataFrame(fund_rows)
fund_df.to_csv(FUND_FILE, index=False)
print(f"\nFundamentals saved: {FUND_FILE} ({len(fund_df)} rows)")

# --- SUMMARY ---
print(f"\n{'='*50}")
print(f"OHLCV:        {len(ohlcv_success)} succeeded, {len(ohlcv_failed)} failed")
print(f"Fundamentals: {len(fund_df)} rows saved")
if ohlcv_failed:
    print(f"Failed OHLCV tickers: {ohlcv_failed}")

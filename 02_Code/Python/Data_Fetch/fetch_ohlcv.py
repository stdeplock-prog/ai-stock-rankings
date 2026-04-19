# fetch_ohlcv.py
# Fetches daily OHLCV data for all tickers in master_universe.csv
# Saves each ticker as a CSV to 01_Data/Raw/OHLCV_Daily/

import yfinance as yf
import pandas as pd
import os
from datetime import datetime

# --- CONFIG ---
UNIVERSE_FILE = "G:/My Drive/AI-Stock-Rankings/01_Data/Reference/master_universe.csv"
OUTPUT_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Raw/OHLCV_Daily"
START_DATE = "2023-01-01"
END_DATE = datetime.today().strftime("%Y-%m-%d")

# --- SETUP ---
os.makedirs(OUTPUT_DIR, exist_ok=True)
universe = pd.read_csv(UNIVERSE_FILE)
tickers = universe["Ticker"].tolist()
print(f"Fetching data for {len(tickers)} tickers...")
print(f"Date range: {START_DATE} to {END_DATE}\n")

# --- FETCH & SAVE ---
success, failed = [], []

for i, ticker in enumerate(tickers, 1):
    try:
        df = yf.download(ticker, start=START_DATE, end=END_DATE, 
                        auto_adjust=True, progress=False)
        if df.empty:
            print(f"  [{i}/{len(tickers)}] WARNING: No data for {ticker}")
            failed.append(ticker)
        else:
            filepath = os.path.join(OUTPUT_DIR, f"{ticker}_daily.csv")
            df.to_csv(filepath)
            print(f"  [{i}/{len(tickers)}] Saved: {ticker} ({len(df)} rows)")
            success.append(ticker)
    except Exception as e:
        print(f"  [{i}/{len(tickers)}] ERROR: {ticker} — {e}")
        failed.append(ticker)

# --- SUMMARY ---
print(f"\n{'='*50}")
print(f"Complete! {len(success)} succeeded, {len(failed)} failed")
if failed:
    print(f"Failed tickers: {failed}")
# fetch_ohlcv.py
# Fetches daily OHLCV data for a list of tickers using yfinance
# Saves each ticker as a CSV to 01_Data/Raw/OHLCV_Daily/

import yfinance as yf
import os
from datetime import datetime

# --- CONFIG ---
TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]
START_DATE = "2023-01-01"
END_DATE = datetime.today().strftime("%Y-%m-%d")
OUTPUT_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Raw/OHLCV_Daily"

# --- CREATE OUTPUT FOLDER IF NEEDED ---
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- FETCH & SAVE ---
for ticker in TICKERS:
    print(f"Fetching {ticker}...")
    df = yf.download(ticker, start=START_DATE, end=END_DATE, auto_adjust=True)
    
    if df.empty:
        print(f"  WARNING: No data returned for {ticker}")
        continue
    
    filepath = os.path.join(OUTPUT_DIR, f"{ticker}_daily.csv")
    df.to_csv(filepath)
    print(f"  Saved: {filepath}")

print("\nAll done!")
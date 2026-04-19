# calc_indicators.py
# Calculates technical indicators for all tickers in OHLCV_Daily
# Saves results to 01_Data/Processed/Technical_Indicators/

import pandas as pd
import numpy as np
import os
from datetime import datetime

# --- CONFIG ---
INPUT_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Raw/OHLCV_Daily"
OUTPUT_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Technical_Indicators"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- INDICATOR FUNCTIONS ---
def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram

# --- PROCESS EACH TICKER ---
files = [f for f in os.listdir(INPUT_DIR) if f.endswith("_daily.csv")]
print(f"Processing {len(files)} tickers...\n")

success, failed = [], []

for i, filename in enumerate(sorted(files), 1):
    ticker = filename.replace("_daily.csv", "")
    try:
        # Load data
        df = pd.read_csv(os.path.join(INPUT_DIR, filename), 
                        header=[0,1], index_col=0, parse_dates=True)
        
        # Flatten multi-level columns if needed
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]
        
        # Ensure we have Close and Volume
        df = df.rename(columns=str.title)
        close = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        # Calculate indicators
        df["RSI_14"] = calc_rsi(close)
        df["MACD"], df["MACD_Signal"], df["MACD_Hist"] = calc_macd(close)
        df["SMA_50"] = close.rolling(50).mean()
        df["SMA_200"] = close.rolling(200).mean()
        df["EMA_20"] = close.ewm(span=20, adjust=False).mean()
        df["Vol_SMA_20"] = volume.rolling(20).mean()
        df["Above_SMA50"] = (close > df["SMA_50"]).astype(int)
        df["Above_SMA200"] = (close > df["SMA_200"]).astype(int)
        df["Golden_Cross"] = ((df["SMA_50"] > df["SMA_200"]) & 
                             (df["SMA_50"].shift(1) <= df["SMA_200"].shift(1))).astype(int)

        # Save
        out_path = os.path.join(OUTPUT_DIR, f"{ticker}_indicators.csv")
        df.to_csv(out_path)
        print(f"  [{i}/{len(files)}] {ticker} ✓")
        success.append(ticker)

    except Exception as e:
        print(f"  [{i}/{len(files)}] {ticker} ERROR: {e}")
        failed.append(ticker)

# --- SUMMARY ---
print(f"\n{'='*50}")
print(f"Done! {len(success)} succeeded, {len(failed)} failed")
if failed:
    print(f"Failed: {failed}")
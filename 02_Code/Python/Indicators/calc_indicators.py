# calc_indicators.py  v2.0
# Calculates technical indicators for all tickers in ohlcv_daily
# Saves results to data/processed/technical_indicators/
# Paths are relative to repo root for GitHub Actions compatibility
#
# v2.0 additions (swing-trading oriented):
#   ATR_14, ATR_Pct, ADX_14, Hist_Vol_20,
#   BB_Upper_20, BB_Lower_20, BB_PctB,
#   Stoch_K_14, Stoch_D_3,
#   ADV_20 (20-day avg dollar volume),
#   Dist_From_SMA50_Pct, Dist_From_SMA200_Pct

import pandas as pd
import numpy as np
import os
from datetime import datetime

# --- CONFIG ---
REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
INPUT_DIR  = os.path.join(REPO_ROOT, "data", "raw", "ohlcv_daily")
OUTPUT_DIR = os.path.join(REPO_ROOT, "data", "processed", "technical_indicators")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- INDICATOR FUNCTIONS ---
def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.where(delta > 0, 0).rolling(period).mean()
    loss  = -delta.where(delta < 0, 0).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def calc_macd(series, fast=12, slow=26, signal=9):
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd        = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram   = macd - signal_line
    return macd, signal_line, histogram

def calc_atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return atr

def calc_adx(high, low, close, period=14):
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm   = pd.Series(plus_dm, index=high.index)
    minus_dm  = pd.Series(minus_dm, index=high.index)
    atr = calc_atr(high, low, close, period=period)
    plus_di  = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean()  / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx

def calc_bollinger(series, period=20, num_std=2):
    ma    = series.rolling(period).mean()
    sd    = series.rolling(period).std()
    upper = ma + num_std * sd
    lower = ma - num_std * sd
    pct_b = (series - lower) / (upper - lower).replace(0, np.nan)
    return upper, lower, pct_b

def calc_stochastic(high, low, close, k_period=14, d_period=3):
    lowest_low   = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d

def calc_hist_vol(series, period=20):
    log_ret = np.log(series / series.shift(1))
    return log_ret.rolling(period).std() * np.sqrt(252)

# --- PROCESS EACH TICKER ---
files = [f for f in os.listdir(INPUT_DIR) if f.endswith("_daily.csv")]
print(f"Processing {len(files)} tickers...\n")

success, failed = [], []

for i, filename in enumerate(sorted(files), 1):
    ticker = filename.replace("_daily.csv", "")
    try:
        df = pd.read_csv(os.path.join(INPUT_DIR, filename),
                         index_col=0, parse_dates=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]
        df = df.rename(columns=str.title)
        close  = df["Close"].squeeze()
        high   = df["High"].squeeze()  if "High"  in df.columns else close
        low    = df["Low"].squeeze()   if "Low"   in df.columns else close
        volume = df["Volume"].squeeze()

        df["RSI_14"] = calc_rsi(close)
        df["MACD"], df["MACD_Signal"], df["MACD_Hist"] = calc_macd(close)
        df["SMA_50"]     = close.rolling(50).mean()
        df["SMA_200"]    = close.rolling(200).mean()
        df["EMA_20"]     = close.ewm(span=20, adjust=False).mean()
        df["Vol_SMA_20"] = volume.rolling(20).mean()
        df["Above_SMA50"]  = (close > df["SMA_50"]).astype(int)
        df["Above_SMA200"] = (close > df["SMA_200"]).astype(int)
        df["Golden_Cross"] = ((df["SMA_50"] > df["SMA_200"]) &
                              (df["SMA_50"].shift(1) <= df["SMA_200"].shift(1))).astype(int)

        # --- NEW swing indicators ---
        df["ATR_14"]       = calc_atr(high, low, close, period=14)
        df["ATR_Pct"]      = df["ATR_14"] / close
        df["ADX_14"]       = calc_adx(high, low, close, period=14)
        df["Hist_Vol_20"]  = calc_hist_vol(close, period=20)
        bb_u, bb_l, bb_pb = calc_bollinger(close, period=20, num_std=2)
        df["BB_Upper_20"]  = bb_u
        df["BB_Lower_20"]  = bb_l
        df["BB_PctB"]      = bb_pb
        k, d = calc_stochastic(high, low, close, k_period=14, d_period=3)
        df["Stoch_K_14"]   = k
        df["Stoch_D_3"]    = d
        df["ADV_20"]       = (volume * close).rolling(20).mean()
        df["Dist_From_SMA50_Pct"]  = (close - df["SMA_50"])  / df["SMA_50"]
        df["Dist_From_SMA200_Pct"] = (close - df["SMA_200"]) / df["SMA_200"]

        out_path = os.path.join(OUTPUT_DIR, f"{ticker}_indicators.csv")
        df.to_csv(out_path)
        print(f"  [{i}/{len(files)}] {ticker} OK")
        success.append(ticker)
    except Exception as e:
        print(f"  [{i}/{len(files)}] {ticker} ERROR: {e}")
        failed.append(ticker)

print(f"\n{'='*50}")
print(f"Done! {len(success)} succeeded, {len(failed)} failed")
if failed:
    print(f"Failed: {failed}")

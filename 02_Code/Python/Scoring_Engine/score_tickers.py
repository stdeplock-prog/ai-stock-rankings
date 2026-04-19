# score_tickers.py
# Scores and ranks all 517 tickers based on technical indicators
# Saves ranked output to 01_Data/Processed/Scoring_Outputs/

import pandas as pd
import numpy as np
import os

# --- CONFIG ---
INDICATORS_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Technical_Indicators"
UNIVERSE_FILE = "G:/My Drive/AI-Stock-Rankings/01_Data/Reference/master_universe.csv"
OUTPUT_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Scoring_Outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- LOAD UNIVERSE ---
universe = pd.read_csv(UNIVERSE_FILE)
print(f"Scoring {len(universe)} tickers...\n")

results = []

for _, row in universe.iterrows():
    ticker = row["Ticker"]
    filepath = os.path.join(INDICATORS_DIR, f"{ticker}_indicators.csv")

    if not os.path.exists(filepath):
        continue

    try:
        df = pd.read_csv(filepath, index_col=0, parse_dates=True)
        if len(df) < 200:
            continue

        latest = df.iloc[-1]
        close = df["Close"].squeeze() if "Close" in df.columns else df.iloc[:, 0]

        # --- SCORING COMPONENTS (0-100 each) ---

        # 1. RSI Score — favor 50-70 range (momentum without overbought)
        rsi = latest.get("RSI_14", 50)
        if 50 <= rsi <= 70:
            rsi_score = 100
        elif rsi < 50:
            rsi_score = max(0, rsi * 2)
        else:
            rsi_score = max(0, 100 - (rsi - 70) * 3)

        # 2. MACD Score — favor positive histogram
        macd_hist = latest.get("MACD_Hist", 0)
        macd_score = 100 if macd_hist > 0 else 0

        # 3. Trend Score — above SMA50 and SMA200
        above_50 = latest.get("Above_SMA50", 0)
        above_200 = latest.get("Above_SMA200", 0)
        trend_score = (above_50 * 50) + (above_200 * 50)

        # 4. Golden Cross bonus
        golden = latest.get("Golden_Cross", 0)
        golden_score = 100 if golden == 1 else 0

        # 5. Volume Score — recent volume vs average
        vol = latest.get("Volume", 0)
        vol_avg = latest.get("Vol_SMA_20", 1)
        vol_ratio = vol / vol_avg if vol_avg > 0 else 1
        vol_score = min(100, vol_ratio * 50)

        # --- WEIGHTED TOTAL SCORE ---
        total_score = (
            rsi_score    * 0.25 +
            macd_score   * 0.25 +
            trend_score  * 0.30 +
            golden_score * 0.10 +
            vol_score    * 0.10
        )

        results.append({
            "Ticker": ticker,
            "Name": row["Name"],
            "Sector": row["Sector"],
            "Index": row["Index"],
            "Score": round(total_score, 2),
            "RSI": round(rsi, 2),
            "MACD_Hist": round(macd_hist, 4),
            "Above_SMA50": int(above_50),
            "Above_SMA200": int(above_200),
            "Golden_Cross": int(golden),
        })

    except Exception as e:
        print(f"  ERROR {ticker}: {e}")

# --- RANK & SAVE ---
scores_df = pd.DataFrame(results)
scores_df = scores_df.sort_values("Score", ascending=False).reset_index(drop=True)
scores_df.index += 1  # Start ranking at 1
scores_df.index.name = "Rank"

output_file = os.path.join(OUTPUT_DIR, "rankings.csv")
scores_df.to_csv(output_file)

print(f"{'='*50}")
print(f"Scored {len(scores_df)} tickers\n")
print("TOP 20 STOCKS:")
print(scores_df.head(20).to_string())
print(f"\nSaved to: {output_file}")
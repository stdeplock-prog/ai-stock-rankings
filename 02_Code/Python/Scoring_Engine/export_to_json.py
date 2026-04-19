# export_to_json.py
# Converts rankings.csv to rankings.json for the dashboard
# Maps our scoring output to the exact format the dashboard expects

import pandas as pd
import json
import os
from datetime import datetime

# --- CONFIG ---
RANKINGS_CSV = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Scoring_Outputs/rankings.csv"
OHLCV_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Raw/OHLCV_Daily"
OUTPUT_FILE = "G:/My Drive/AI-Stock-Rankings/data/rankings.json"

# --- LOAD RANKINGS ---
df = pd.read_csv(RANKINGS_CSV)
df = df.head(100)  # Top 100 for dashboard

rows = []
for _, row in df.iterrows():
    ticker = row["Ticker"]
    
    # Get latest volume from OHLCV data
    vol_millions = 0
    try:
        ohlcv = pd.read_csv(
            os.path.join(OHLCV_DIR, f"{ticker}_daily.csv"),
            header=[0,1], index_col=0
        )
        if isinstance(ohlcv.columns, pd.MultiIndex):
            ohlcv.columns = [col[0] for col in ohlcv.columns]
        ohlcv.columns = [c.title() for c in ohlcv.columns]
        vol_millions = round(ohlcv["Volume"].iloc[-1] / 1_000_000, 1)
    except:
        pass

    # Normalize score from 0-100 to 0-10 scale
    ai_score = round(row["Score"] / 10, 1)
    technical = round(row["Score"] / 10, 1)
    
    # RSI-based sentiment (50-70 = good)
    rsi = row.get("RSI", 50)
    sentiment = round(min(10, max(0, (rsi - 30) / 5)), 1)
    
    # Trend-based fundamental proxy
    above_50 = row.get("Above_SMA50", 0)
    above_200 = row.get("Above_SMA200", 0)
    fundamental = round(5 + (above_50 * 2) + (above_200 * 3), 1)
    
    # Risk score (inverse of volatility proxy)
    low_risk = round(10 - min(10, abs(rsi - 50) / 5), 1)

    rows.append({
        "rank": int(row["Rank"]) if "Rank" in row else _+1,
        "ticker": ticker,
        "company": str(row["Name"]),
        "country": "US",
        "ai_score": ai_score,
        "change": 0.0,
        "fundamental": fundamental,
        "technical": technical,
        "sentiment": sentiment,
        "low_risk": low_risk,
        "volume_millions": vol_millions,
        "industry": str(row["Sector"]),
        "sector": str(row["Sector"])
    })

# --- BUILD JSON ---
output = {
    "as_of": datetime.today().strftime("%Y-%m-%d"),
    "universe": "SP500 + NDX100 (517 tickers)",
    "rows": rows
}

with open(OUTPUT_FILE, "w") as f:
    json.dump(output, f, indent=2)

print(f"Exported {len(rows)} tickers to {OUTPUT_FILE}")
print(f"As of: {output['as_of']}")
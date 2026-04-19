# export_to_json.py  v2
# Converts rankings.csv to rankings.json for dashboard
# NEW: tracks rank changes day-over-day (CHG column)

import pandas as pd
import json
import os
from datetime import datetime

# --- CONFIG ---
RANKINGS_CSV  = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Scoring_Outputs/rankings.csv"
OHLCV_DIR     = "G:/My Drive/AI-Stock-Rankings/01_Data/Raw/OHLCV_Daily"
OUTPUT_FILE   = "G:/My Drive/AI-Stock-Rankings/data/rankings.json"
HISTORY_FILE  = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Scoring_Outputs/rankings_history.csv"

# --- LOAD CURRENT RANKINGS ---
df = pd.read_csv(RANKINGS_CSV)
df = df.head(100)  # Top 100 for dashboard

# --- LOAD PREVIOUS DAY RANKINGS (if exists) ---
prev_df = None
if os.path.exists(HISTORY_FILE):
    try:
        prev_df = pd.read_csv(HISTORY_FILE)
    except:
        prev_df = None

# --- BUILD ROWS WITH CHG ---
rows = []
for _, row in df.iterrows():
    ticker = row["Ticker"]
    curr_rank = int(row["Rank"]) if "Rank" in row else (_ + 1)

    # Calculate CHG (positive = moved UP in rank, negative = moved DOWN)
    change = 0
    if prev_df is not None and ticker in prev_df["Ticker"].values:
        prev_rank = int(prev_df[prev_df["Ticker"] == ticker]["Rank"].iloc[0])
        change = prev_rank - curr_rank  # rank 10 -> 5 = +5 (moved up)

    # Get latest volume from OHLCV
    vol_millions = 0
    try:
        ohlcv = pd.read_csv(
            os.path.join(OHLCV_DIR, f"{ticker}_daily.csv"),
            header=[0,1] if isinstance(pd.read_csv(os.path.join(OHLCV_DIR, f"{ticker}_daily.csv"), nrows=0).columns, pd.MultiIndex) else 0,
            index_col=0
        )
        if isinstance(ohlcv.columns, pd.MultiIndex):
            ohlcv.columns = [col[0] for col in ohlcv.columns]
        ohlcv.columns = [c.title() if isinstance(c, str) else c for c in ohlcv.columns]
        vol_millions = round(ohlcv["Volume"].iloc[-1] / 1_000_000, 1) if "Volume" in ohlcv.columns else 0
    except:
        pass

    rows.append({
        "rank":            curr_rank,
        "ticker":          ticker,
        "company":         str(row["Name"]),
        "country":         "US",
        "ai_score":        round(row["AI_Score"], 1) if "AI_Score" in row else round(row.get("Score", 0) / 10, 1),
        "change":          change,
        "fundamental":     round(row.get("Fundamental", 5.0), 1),
        "technical":       round(row.get("Technical", 5.0), 1),
        "sentiment":       round(row.get("Sentiment", 5.0), 1),
        "low_risk":        round(row.get("Risk", 5.0), 1),
        "volume_millions": vol_millions,
        "industry":        str(row["Sector"]),
        "sector":          str(row["Sector"]),
    })

# --- BUILD JSON OUTPUT ---
output = {
    "as_of":    datetime.today().strftime("%Y-%m-%d"),
    "universe": "SP500 + NDX100 (517 tickers)",
    "rows":     rows
}

with open(OUTPUT_FILE, "w") as f:
    json.dump(output, f, indent=2)

print(f"Exported {len(rows)} tickers to {OUTPUT_FILE}")
print(f"As of: {output['as_of']}")

# --- SAVE CURRENT RANKINGS AS HISTORY FOR NEXT RUN ---
df.to_csv(HISTORY_FILE, index=False)
print(f"Saved history to: {HISTORY_FILE}")

# export_to_json.py  v3
# Converts rankings.csv -> data/rankings.json for the live dashboard
# Includes: nan sanitization, day-over-day rank change (CHG), industry fallback
# Paths are relative to repo root for GitHub Actions compatibility
import pandas as pd
import json
import os
from datetime import datetime, timezone, timedelta

# --- CONFIG ---
REPO_ROOT     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RANKINGS_CSV  = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "rankings.csv")
OHLCV_DIR     = os.path.join(REPO_ROOT, "data", "raw", "ohlcv_daily")
OUTPUT_FILE   = os.path.join(REPO_ROOT, "data", "rankings.json")
HISTORY_FILE  = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "rankings_history.csv")

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

# --- TIMEZONE: Convert UTC runner time to US Central (CDT = UTC-5, CST = UTC-6) ---
def get_central_time_str():
    utc_now = datetime.now(timezone.utc)
    # Determine CDT (UTC-5) vs CST (UTC-6) using simple month check
    # CDT: 2nd Sun Mar - 1st Sun Nov; CST: otherwise
    month = utc_now.month
    if 3 <= month <= 10:
        offset = timedelta(hours=-5)
        label = "CDT"
    else:
        offset = timedelta(hours=-6)
        label = "CST"
    central_now = utc_now + offset
    return central_now.strftime("%Y-%m-%d %I:%M %p") + " " + label

# --- HELPER: safely convert a field to string, replacing nan/None ---
def safe_str(val, default=""):
    if val is None:
        return default
    s = str(val).strip()
    if s.lower() in ("nan", "none", "n/a", ""):
        return default
    return s

# --- LOAD CURRENT RANKINGS ---
df = pd.read_csv(RANKINGS_CSV)
df = df.head(100)  # Top 100 for dashboard

# --- LOAD PREVIOUS RANKINGS (for CHG column) ---
prev_df = None
if os.path.exists(HISTORY_FILE):
    try:
        prev_df = pd.read_csv(HISTORY_FILE)
    except:
        prev_df = None

# --- BUILD ROWS ---
rows = []
for i, (_, row) in enumerate(df.iterrows(), 1):
    ticker    = row["Ticker"]
    curr_rank = int(row["Rank"]) if "Rank" in row.index else i

    # Day-over-day rank change (positive = moved UP)
    change = 0
    if prev_df is not None and ticker in prev_df["Ticker"].values:
        prev_rank = int(prev_df[prev_df["Ticker"] == ticker]["Rank"].iloc[0])
        change = prev_rank - curr_rank

    # Latest volume from OHLCV
    vol_millions = 0
    try:
        ohlcv_path = os.path.join(OHLCV_DIR, f"{ticker}_daily.csv")
        ohlcv = pd.read_csv(ohlcv_path, index_col=0)
        if isinstance(ohlcv.columns, pd.MultiIndex):
            ohlcv.columns = [col[0] for col in ohlcv.columns]
        ohlcv.columns = [c.title() if isinstance(c, str) else c for c in ohlcv.columns]
        vol_millions = round(float(ohlcv["Volume"].iloc[-1]) / 1_000_000, 1) if "Volume" in ohlcv.columns else 0
    except:
        pass

    # Industry: prefer "Industry" column, fall back to Sector
    industry_val = safe_str(row.get("Industry", ""))
    if not industry_val:
        industry_val = safe_str(row.get("Sector", ""))

    rows.append({
        "rank":           curr_rank,
        "ticker":         ticker,
        "company":        safe_str(row["Name"]),
        "country":        "US",
        "ai_score":       round(float(row["AI_Score"]), 1) if "AI_Score" in row.index else round(float(row.get("Score", 0)) / 10, 1),
        "change":         change,
        "fundamental":    round(float(row.get("Fundamental", 5.0)), 1),
        "technical":      round(float(row.get("Technical", 5.0)), 1),
        "sentiment":      round(float(row.get("Sentiment", 5.0)), 1),
        "low_risk":       round(float(row.get("Risk", 5.0)), 1),
        "volume_millions": vol_millions,
        "industry":       industry_val,
        "sector":         safe_str(row.get("Sector", "")),
    })

# Save history snapshot for next run's CHG calculation
df.to_csv(HISTORY_FILE, index=False)

# --- BUILD JSON OUTPUT ---
as_of_str = get_central_time_str()
output = {
    "as_of":    as_of_str,
    "universe": "SP500 + NDX100 (517 tickers)",
    "rows":     rows
}

with open(OUTPUT_FILE, "w") as f:
    json.dump(output, f, indent=2)

print(f"Exported {len(rows)} tickers to {OUTPUT_FILE}")
print(f"As of: {output['as_of']}")

# export_to_json.py  v5
# Converts rankings.csv -> data/rankings.json for the live dashboard
# Change logic: "vs Open" = first run of today vs current run
#   - On first run of day: saves rankings_daily_open.csv, change = 0
#   - On subsequent runs: compares against rankings_daily_open.csv
#   - daily_open resets each calendar day (CDT/CST)
# v5: adds short_interest and insider_buying fields from score_tickers v3
import pandas as pd
import json
import os
from datetime import datetime, timezone, timedelta

# --- CONFIG ---
REPO_ROOT     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RANKINGS_CSV  = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "rankings.csv")
OHLCV_DIR     = os.path.join(REPO_ROOT, "data", "raw", "ohlcv_daily")
OUTPUT_FILE   = os.path.join(REPO_ROOT, "data", "rankings.json")

# Daily open snapshot — first run of each calendar day
DAILY_OPEN_FILE = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "rankings_daily_open.csv")
DAILY_OPEN_DATE = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "rankings_daily_open_date.txt")

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

# --- TIMEZONE: Convert UTC runner time to US Central ---
def get_central_now():
    utc_now = datetime.now(timezone.utc)
    offset  = timedelta(hours=-5) if 3 <= utc_now.month <= 10 else timedelta(hours=-6)
    return utc_now + offset

def get_central_time_str(dt):
    label = "CDT" if 3 <= dt.month <= 10 else "CST"
    return dt.strftime("%Y-%m-%d %I:%M %p") + " " + label

# --- HELPER: safely convert a field to string ---
def safe_str(val, default=""):
    if val is None:
        return default
    s = str(val).strip()
    if s.lower() in ("nan", "none", "n/a", ""):
        return default
    return s

# --- LOAD CURRENT RANKINGS ---
df = pd.read_csv(RANKINGS_CSV)
df = df.head(100)

# --- DETERMINE IF THIS IS THE FIRST RUN OF TODAY ---
central_now = get_central_now()
today_str   = central_now.strftime("%Y-%m-%d")

# Read what date the last daily open snapshot was saved
last_open_date = ""
if os.path.exists(DAILY_OPEN_DATE):
    with open(DAILY_OPEN_DATE, "r") as f:
        last_open_date = f.read().strip()

is_first_run_today = (last_open_date != today_str)

if is_first_run_today:
    # Save this run as today's open baseline
    df.to_csv(DAILY_OPEN_FILE, index=False)
    with open(DAILY_OPEN_DATE, "w") as f:
        f.write(today_str)
    print(f"First run of {today_str} — saved daily open snapshot.")
    open_df = df.copy()
else:
    print(f"Subsequent run — comparing vs open snapshot from {last_open_date}.")
    try:
        open_df = pd.read_csv(DAILY_OPEN_FILE)
    except Exception as e:
        print(f"Warning: could not load daily open file ({e}), change = 0 for all.")
        open_df = df.copy()

# Build open rank lookup: ticker -> rank at open
open_rank_map = {}
for _, row in open_df.iterrows():
    t = row["Ticker"]
    r = int(row["Rank"]) if "Rank" in row.index else 0
    open_rank_map[t] = r

# --- BUILD ROWS ---
rows = []
for i, (_, row) in enumerate(df.iterrows(), 1):
    ticker    = row["Ticker"]
    curr_rank = int(row["Rank"]) if "Rank" in row.index else i

    # vs-open change: positive = moved UP in rank (lower number = better)
    open_rank = open_rank_map.get(ticker, curr_rank)
    change    = open_rank - curr_rank  # e.g. was 10, now 7 -> +3 (moved up)

    # Latest volume from OHLCV
    vol_millions = 0
    try:
        ohlcv_path = os.path.join(OHLCV_DIR, f"{ticker}_daily.csv")
        ohlcv      = pd.read_csv(ohlcv_path, index_col=0)
        if isinstance(ohlcv.columns, pd.MultiIndex):
            ohlcv.columns = [col[0] for col in ohlcv.columns]
        ohlcv.columns = [c.title() if isinstance(c, str) else c for c in ohlcv.columns]
        vol_millions  = round(float(ohlcv["Volume"].iloc[-1]) / 1_000_000, 1) if "Volume" in ohlcv.columns else 0
    except:
        pass

    # Industry: prefer "Industry", fall back to Sector
    industry_val = safe_str(row.get("Industry", ""))
    if not industry_val:
        industry_val = safe_str(row.get("Sector", ""))

    # Short interest flag (% of float, or None)
    si_raw = row.get("Short_Interest", None)
    if si_raw is not None and str(si_raw).lower() not in ("nan", "none", ""):
        try:
            short_interest = round(float(si_raw), 1)
        except Exception:
            short_interest = None
    else:
        short_interest = None

    # Insider buying flag (True/False)
    ib_raw = row.get("Insider_Buying", False)
    try:
        insider_buying = bool(ib_raw) if str(ib_raw).lower() not in ("nan", "none", "") else False
    except Exception:
        insider_buying = False

    rows.append({
        "rank":            curr_rank,
        "ticker":          ticker,
        "company":         safe_str(row["Name"]),
        "country":         "US",
        "ai_score":        round(float(row["AI_Score"]), 1)        if "AI_Score"    in row.index else round(float(row.get("Score", 0)) / 10, 1),
        "change":          change,
        "fundamental":     round(float(row.get("Fundamental", 5.0)), 1),
        "technical":       round(float(row.get("Technical",   5.0)), 1),
        "sentiment":       round(float(row.get("Sentiment",   5.0)), 1),
        "low_risk":        round(float(row.get("Risk",         5.0)), 1),
        "volume_millions": vol_millions,
        "industry":        industry_val,
        "sector":          safe_str(row.get("Sector", "")),
        "short_interest":  short_interest,   # % of float short, or null
        "insider_buying":  insider_buying,   # true if net insider buys > sells
    })

# --- BUILD JSON OUTPUT ---
as_of_str = get_central_time_str(central_now)
output = {
    "as_of":      as_of_str,
    "open_date":  today_str,
    "is_open_run": is_first_run_today,
    "universe":   "SP500 + NDX100 (517 tickers)",
    "rows":       rows
}

with open(OUTPUT_FILE, "w") as f:
    json.dump(output, f, indent=2)

print(f"Exported {len(rows)} tickers to {OUTPUT_FILE}")
print(f"As of: {as_of_str}")
print(f"Open run: {is_first_run_today} | Open date: {today_str}")

# build_universe.py
# Builds the master ticker universe from S&P 500 + Nasdaq 100 + Russell 1000
# Removes duplicates and saves to data/reference/
# Paths are relative to repo root for GitHub Actions compatibility

import pandas as pd
import os
import requests
from io import StringIO

# --- CONFIG ---
# Use path relative to repo root (works on GitHub Actions + local if run from repo root)
REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUTPUT_DIR  = os.path.join(REPO_ROOT, "data", "reference")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "master_universe.csv")

os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def read_wiki_table(url, table_index=0):
    response = requests.get(url, headers=HEADERS)
    return pd.read_html(StringIO(response.text))[table_index]

# --- PULL S&P 500 ---
print("Fetching S&P 500 list...")
sp500 = read_wiki_table("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
sp500 = sp500[["Symbol", "Security", "GICS Sector"]].copy()
sp500.columns = ["Ticker", "Name", "Sector"]
sp500["Index"] = "SP500"
sp500["Ticker"] = sp500["Ticker"].str.replace(".", "-", regex=False)
print(f"  S&P 500: {len(sp500)} tickers")

# --- PULL NASDAQ 100 ---
print("Fetching Nasdaq 100 list...")
ndx = read_wiki_table("https://en.wikipedia.org/wiki/Nasdaq-100", table_index=4)
ndx = ndx.iloc[:, :2].copy()  # Select first 2 columns (Ticker, Company)
ndx.columns = ["Ticker", "Name"]
ndx["Ticker"] = ndx["Ticker"].astype(str)  # Convert to string for .str operations
ndx["Sector"] = "N/A"
ndx["Index"] = "NDX100"
ndx["Ticker"] = ndx["Ticker"].str.replace(".", "-", regex=False)
print(f"  Nasdaq 100: {len(ndx)} tickers")

# --- PULL RUSSELL 1000 ---
print("Fetching Russell 1000 list...")
try:
    r1k = read_wiki_table("https://en.wikipedia.org/wiki/Russell_1000_Index", table_index=2)
except Exception:
    try:
        r1k = read_wiki_table("https://en.wikipedia.org/wiki/Russell_1000_Index", table_index=1)
    except Exception:
        r1k = read_wiki_table("https://en.wikipedia.org/wiki/Russell_1000_Index", table_index=0)
r1k = r1k.iloc[:, :2].copy()  # Select first 2 columns (Ticker, Company)
r1k.columns = ["Ticker", "Name"]
r1k["Ticker"] = r1k["Ticker"].astype(str)  # Convert to string for .str operations
r1k["Sector"] = "N/A"
r1k["Index"] = "Russell1000"
r1k["Ticker"] = r1k["Ticker"].str.replace(".", "-", regex=False)
print(f"  Russell 1000: {len(r1k)} tickers")

# --- COMBINE & DEDUPLICATE ---
# Keep S&P 500 label when a ticker appears in multiple indices (first = SP500)
combined = pd.concat([sp500, ndx, r1k], ignore_index=True)
combined = combined.drop_duplicates(subset="Ticker", keep="first")
combined = combined.sort_values("Ticker").reset_index(drop=True)
print(f"\n  Combined unique tickers: {len(combined)}")

# --- SAVE ---
combined.to_csv(OUTPUT_FILE, index=False)
print(f"\nSaved to: {OUTPUT_FILE}")
print("\nSample:")
print(combined.head(10).to_string())

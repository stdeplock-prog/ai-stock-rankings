# build_universe.py
# Builds the master ticker universe from S&P 500 + Nasdaq 100 + Russell 1000
# Removes duplicates and saves to data/reference/
# Paths are relative to repo root for GitHub Actions compatibility

import os
import re
import sys
from io import StringIO

import pandas as pd
import requests

# --- CONFIG ---
REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUTPUT_DIR  = os.path.join(REPO_ROOT, "data", "reference")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "master_universe.csv")
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# --- Ticker validation ---
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")

def is_valid_ticker(value):
    if value is None:
        return False
    s = str(value).strip().upper()
    return bool(TICKER_RE.match(s))

def read_wiki_tables(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return pd.read_html(StringIO(response.text))

def read_wiki_table(url, table_index=0):
    return read_wiki_tables(url)[table_index]

def pick_ticker_table(tables, min_rows=50, ticker_col_hints=("Ticker", "Symbol")):
    """Return (df, ticker_col) for the first table that looks like a ticker list.

    A table qualifies when:
      - it has >= min_rows rows
      - one of its columns matches a ticker_col_hint (case-insensitive, substring ok)
      - >= 80%% of values in that column pass is_valid_ticker
    """
    best = None
    for df in tables:
        # Flatten multi-index columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = [" ".join([str(c) for c in col if str(c) != "nan"]).strip() for col in df.columns.values]
        for col in df.columns:
            col_name = str(col)
            if any(h.lower() in col_name.lower() for h in ticker_col_hints):
                series = df[col].astype(str).str.strip()
                if len(series) < min_rows:
                    continue
                valid_frac = series.apply(is_valid_ticker).mean()
                if valid_frac >= 0.80:
                    return df, col
                if best is None or valid_frac > best[2]:
                    best = (df, col, valid_frac)
    if best is not None and best[2] >= 0.50:
        return best[0], best[1]
    return None, None

def load_cached_universe():
    if os.path.exists(OUTPUT_FILE):
        try:
            cached = pd.read_csv(OUTPUT_FILE)
            if "Ticker" in cached.columns and len(cached) >= 400:
                return cached
        except Exception:
            pass
    return None

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
ndx_tables = read_wiki_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
ndx_df, ndx_col = pick_ticker_table(ndx_tables, min_rows=50)
if ndx_df is None:
    print("  WARN: Nasdaq 100 table not recognized; skipping.")
    ndx = pd.DataFrame(columns=["Ticker", "Name", "Sector", "Index"])
else:
    other_cols = [c for c in ndx_df.columns if c != ndx_col]
    name_col = other_cols[0] if other_cols else ndx_col
    ndx = pd.DataFrame({"Ticker": ndx_df[ndx_col].astype(str).str.strip(),
                        "Name":   ndx_df[name_col].astype(str).str.strip()})
    ndx["Sector"] = "N/A"
    ndx["Index"]  = "NDX100"
    ndx["Ticker"] = ndx["Ticker"].str.replace(".", "-", regex=False)
    ndx = ndx[ndx["Ticker"].apply(is_valid_ticker)]
print(f"  Nasdaq 100: {len(ndx)} tickers")

# --- PULL RUSSELL 1000 ---
# Primary source: iShares IWB holdings CSV (authoritative, daily-updated, stable schema).
# Secondary source: Wikipedia (existing scrape).
# Tertiary source: cached master_universe.csv (handled later in SAFETY NET).
IWB_CSV_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "?fileType=csv&fileName=IWB_holdings&dataType=fund"
)

def fetch_russell1000_iwb():
    """Fetch Russell 1000 constituents from iShares IWB holdings CSV.
    Returns a DataFrame[Ticker,Name,Sector,Index] or None on any failure.
    The CSV has ~9 metadata rows above the real header that starts with 'Ticker,'.
    """
    try:
        resp = requests.get(IWB_CSV_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        text = resp.text
        # Find the real header line (the one that actually starts with 'Ticker,')
        lines = text.splitlines()
        header_idx = None
        for i, line in enumerate(lines):
            if line.lstrip().lower().startswith("ticker,"):
                header_idx = i
                break
        if header_idx is None:
            print("  WARN: IWB CSV header row not found.")
            return None
        csv_body = "\n".join(lines[header_idx:])
        df = pd.read_csv(StringIO(csv_body))
        if "Ticker" not in df.columns:
            print("  WARN: IWB CSV missing Ticker column.")
            return None
        # Keep only equity holdings; drop cash/FX/derivative rows.
        if "Asset Class" in df.columns:
            df = df[df["Asset Class"].astype(str).str.strip().str.lower() == "equity"]
        out = pd.DataFrame({
            "Ticker": df["Ticker"].astype(str).str.strip(),
            "Name":   df.get("Name", pd.Series([""] * len(df))).astype(str).str.strip(),
            "Sector": df.get("Sector", pd.Series(["N/A"] * len(df))).astype(str).str.strip(),
        })
        out["Index"] = "Russell1000"
        out["Ticker"] = out["Ticker"].str.replace(".", "-", regex=False)
        out = out[out["Ticker"].apply(is_valid_ticker)]
        out = out.drop_duplicates(subset="Ticker", keep="first")
        if len(out) < 500:
            print(f"  WARN: IWB returned only {len(out)} tickers; treating as invalid.")
            return None
        return out.reset_index(drop=True)
    except Exception as e:
        print(f"  WARN: IWB fetch failed: {e}")
        return None

print("Fetching Russell 1000 list (IWB primary)...")
r1k = fetch_russell1000_iwb()
if r1k is not None:
    print(f"  Russell 1000 (IWB): {len(r1k)} tickers")
else:
    print("  Falling back to Wikipedia Russell 1000 scrape...")
    try:
        r1k_tables = read_wiki_tables("https://en.wikipedia.org/wiki/Russell_1000_Index")
        r1k_df, r1k_col = pick_ticker_table(r1k_tables, min_rows=500)
    except Exception as e:
        print(f"  WARN: Russell 1000 Wikipedia fetch failed: {e}")
        r1k_df, r1k_col = None, None
    if r1k_df is None:
        print("  WARN: Russell 1000 Wikipedia table not recognized (layout may have changed).")
        r1k = pd.DataFrame(columns=["Ticker", "Name", "Sector", "Index"])
    else:
        other_cols = [c for c in r1k_df.columns if c != r1k_col]
        name_col = other_cols[0] if other_cols else r1k_col
        r1k = pd.DataFrame({
            "Ticker": r1k_df[r1k_col].astype(str).str.strip(),
            "Name":   r1k_df[name_col].astype(str).str.strip(),
        })
        r1k["Sector"] = "N/A"
        r1k["Index"]  = "Russell1000"
        r1k["Ticker"] = r1k["Ticker"].str.replace(".", "-", regex=False)
        r1k = r1k[r1k["Ticker"].apply(is_valid_ticker)]
    print(f"  Russell 1000 (Wikipedia): {len(r1k)} tickers")

# --- COMBINE & DEDUPLICATE ---
combined = pd.concat([sp500, ndx, r1k], ignore_index=True)
combined = combined[combined["Ticker"].apply(is_valid_ticker)]
combined = combined.drop_duplicates(subset="Ticker", keep="first")
combined = combined.sort_values("Ticker").reset_index(drop=True)
print(f"\n  Combined unique tickers: {len(combined)}")

# --- SAFETY NET: fall back to last-known-good if result is suspiciously small ---
MIN_EXPECTED = 400
if len(combined) < MIN_EXPECTED:
    cached = load_cached_universe()
    if cached is not None:
        print(f"  WARN: scraped universe has {len(combined)} tickers (< {MIN_EXPECTED}).")
        print(f"  Falling back to last-known-good cached universe ({len(cached)} tickers).")
        combined = cached
    else:
        print(f"  ERROR: scraped universe has only {len(combined)} tickers and no cache available.", file=sys.stderr)
        sys.exit(1)

# --- SAVE ---
combined.to_csv(OUTPUT_FILE, index=False)
print(f"\nSaved to: {OUTPUT_FILE}")
print("\nSample:")
print(combined.head(10).to_string())

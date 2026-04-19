# score_tickers.py  v2
# Scores all 517 tickers using TECHNICAL + FUNDAMENTAL data
# Output: rankings.csv with AI score (weighted avg of all subscores)

import pandas as pd
import numpy as np
import os

# --- CONFIG ---
INDICATORS_DIR = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Technical_Indicators"
FUNDAMENTALS   = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/fundamentals.csv"
UNIVERSE_FILE  = "G:/My Drive/AI-Stock-Rankings/01_Data/Reference/master_universe.csv"
OUTPUT_DIR     = "G:/My Drive/AI-Stock-Rankings/01_Data/Processed/Scoring_Outputs"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- LOAD DATA ---
universe = pd.read_csv(UNIVERSE_FILE)
fund_df  = pd.read_csv(FUNDAMENTALS)
print(f"Scoring {len(universe)} tickers...\n")

results = []

for _, row in universe.iterrows():
    ticker = row["Ticker"]
    ind_file = os.path.join(INDICATORS_DIR, f"{ticker}_indicators.csv")
    if not os.path.exists(ind_file):
        continue

    try:
        # -- TECHNICAL INDICATORS --
        df = pd.read_csv(ind_file, index_col=0, parse_dates=True)
        if len(df) < 200:
            continue
        latest = df.iloc[-1]
        close  = df["Close"].squeeze() if "Close" in df.columns else df.iloc[:, 0]

        # RSI (50-70 is ideal)
        rsi = latest.get("RSI_14", 50)
        if 50 <= rsi <= 70:
            rsi_score = 100
        elif rsi < 50:
            rsi_score = max(0, rsi * 2)
        else:
            rsi_score = max(0, 100 - (rsi - 70) * 3)

        # MACD Histogram
        macd_hist = latest.get("MACD_Hist", 0)
        macd_score = 100 if macd_hist > 0 else 0

        # Trend (above SMAs)
        above_50  = latest.get("Above_SMA50", 0)
        above_200 = latest.get("Above_SMA200", 0)
        trend_score = (above_50 * 50) + (above_200 * 50)

        # Golden Cross bonus
        golden = latest.get("Golden_Cross", 0)
        golden_score = 100 if golden == 1 else 0

        # Volume spike
        vol     = latest.get("Volume", 0)
        vol_avg = latest.get("Vol_SMA_20", 1)
        vol_ratio = vol / vol_avg if vol_avg > 0 else 1
        vol_score = min(100, vol_ratio * 50)

        # WEIGHTED TECHNICAL SCORE
        technical = (
            rsi_score    * 0.25 +
            macd_score   * 0.25 +
            trend_score  * 0.30 +
            golden_score * 0.10 +
            vol_score    * 0.10
        )

        # -- FUNDAMENTAL DATA --
        fund_row = fund_df[fund_df["Ticker"] == ticker]
        if fund_row.empty:
            # No fundamentals -> use default neutral 50
            fundamental = 50.0
            pe_score = earnings_score = revenue_score = beta_score = 50.0
        else:
            fund = fund_row.iloc[0]

            # P/E Score (10-20 ideal)
            pe = fund.get("trailingPE")
            if pd.isna(pe) or pe <= 0:
                pe_score = 50
            elif 10 <= pe <= 20:
                pe_score = 100
            elif pe < 10:
                pe_score = max(0, 50 + (pe - 10) * 5)  # penalize too low
            else:
                pe_score = max(0, 100 - (pe - 20) * 2)  # penalize high PE

            # Earnings Growth (positive is good)
            eps_growth = fund.get("earningsGrowth")
            if pd.isna(eps_growth):
                earnings_score = 50
            else:
                earnings_score = min(100, max(0, 50 + eps_growth * 200))

            # Revenue Growth
            rev_growth = fund.get("revenueGrowth")
            if pd.isna(rev_growth):
                revenue_score = 50
            else:
                revenue_score = min(100, max(0, 50 + rev_growth * 200))

            # Beta (low beta = low risk, high score)
            beta = fund.get("beta")
            if pd.isna(beta):
                beta_score = 50
            else:
                beta_score = min(100, max(0, 100 - abs(beta - 1) * 50))

            # WEIGHTED FUNDAMENTAL SCORE
            fundamental = (
                pe_score       * 0.30 +
                earnings_score * 0.30 +
                revenue_score  * 0.25 +
                beta_score     * 0.15
            )

        # -- SENTIMENT (RSI-based proxy) --
        sentiment = min(10, max(0, (rsi - 30) / 4))

        # -- RISK (inverse of beta + RSI volatility) --
        beta_val = fund_row.iloc[0].get("beta") if not fund_row.empty else 1.0
        if pd.isna(beta_val):
            beta_val = 1.0
        risk = min(10, max(0, 10 - abs(beta_val - 1) * 5 - abs(rsi - 50) / 10))

        # -- AI SCORE (weighted avg: 40% technical, 40% fundamental, 10% sentiment, 10% risk) --
        ai_score = (
            (technical / 10)   * 0.40 +
            (fundamental / 10) * 0.40 +
            sentiment          * 0.10 +
            risk               * 0.10
        )

        results.append({
            "Ticker":       ticker,
            "Name":         row["Name"],
            "Sector":       row["Sector"],
            "Index":        row["Index"],
            "AI_Score":     round(ai_score, 2),
            "Technical":    round(technical / 10, 2),
            "Fundamental":  round(fundamental / 10, 2),
            "Sentiment":    round(sentiment, 2),
            "Risk":         round(risk, 2),
            "RSI":          round(rsi, 2),
            "MACD_Hist":    round(macd_hist, 4),
            "Above_SMA50":  int(above_50),
            "Above_SMA200": int(above_200),
            "Golden_Cross": int(golden),
        })

    except Exception as e:
        print(f"  ERROR {ticker}: {e}")

# --- RANK & SAVE ---
scores_df = pd.DataFrame(results)
scores_df = scores_df.sort_values("AI_Score", ascending=False).reset_index(drop=True)
scores_df.index += 1
scores_df.index.name = "Rank"

output_file = os.path.join(OUTPUT_DIR, "rankings.csv")
scores_df.to_csv(output_file)

print(f"{'='*50}")
print(f"Scored {len(scores_df)} tickers\n")
print("TOP 20 STOCKS:")
print(scores_df.head(20).to_string())
print(f"\nSaved to: {output_file}")

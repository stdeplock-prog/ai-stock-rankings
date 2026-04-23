# score_tickers.py  v3
# Scores all tickers using TECHNICAL + FUNDAMENTAL data
# Changes v3:
#   - Revised weights: 35% Technical, 35% Fundamental, 15% Sentiment, 15% Risk
#   - Earnings momentum replaces raw earningsGrowth in fundamental
#   - Short interest flag added (flag only, not scored)
#   - Insider buying flag added (flag only, not scored)
# Output: data/processed/scoring_outputs/rankings.csv
# Paths are relative to repo root for GitHub Actions compatibility

import pandas as pd
import numpy as np
import os

# --- CONFIG ---
REPO_ROOT       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
INDICATORS_DIR  = os.path.join(REPO_ROOT, "data", "processed", "technical_indicators")
FUNDAMENTALS    = os.path.join(REPO_ROOT, "data", "processed", "fundamentals.csv")
UNIVERSE_FILE   = os.path.join(REPO_ROOT, "data", "reference", "master_universe.csv")
OUTPUT_DIR      = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- LOAD DATA ---
universe = pd.read_csv(UNIVERSE_FILE)
fund_df  = pd.read_csv(FUNDAMENTALS)
print(f"Scoring {len(universe)} tickers...\n")

results = []

for _, row in universe.iterrows():
    ticker    = row["Ticker"]
    ind_file  = os.path.join(INDICATORS_DIR, f"{ticker}_indicators.csv")
    if not os.path.exists(ind_file):
        continue

    try:
        df      = pd.read_csv(ind_file, index_col=0, parse_dates=True)
        if len(df) < 200:
            continue
        latest  = df.iloc[-1]
        close   = df["Close"].squeeze() if "Close" in df.columns else df.iloc[:, 0]

        # ── TECHNICAL ──────────────────────────────────────────────────────────
        rsi = latest.get("RSI_14", 50)
        if 50 <= rsi <= 70:
            rsi_score = 100
        elif rsi < 50:
            rsi_score = max(0, rsi * 2)
        else:
            rsi_score = max(0, 100 - (rsi - 70) * 3)

        macd_hist  = latest.get("MACD_Hist", 0)
        macd_score = 100 if macd_hist > 0 else 0

        above_50   = latest.get("Above_SMA50",  0)
        above_200  = latest.get("Above_SMA200", 0)
        trend_score = (above_50 * 50) + (above_200 * 50)

        golden       = latest.get("Golden_Cross", 0)
        golden_score = 100 if golden == 1 else 0

        vol       = latest.get("Volume",      0)
        vol_avg   = latest.get("Vol_SMA_20",  1)
        vol_ratio = vol / vol_avg if vol_avg > 0 else 1
        vol_score = min(100, vol_ratio * 50)

        # Momentum: price vs 52-week high (uses close series)
        high_52w = close.tail(252).max() if len(close) >= 252 else close.max()
        cur_close = close.iloc[-1]
        momentum_score = min(100, max(0, (cur_close / high_52w) * 100)) if high_52w > 0 else 50

        technical = (
            rsi_score      * 0.20 +
            macd_score     * 0.20 +
            trend_score    * 0.25 +
            golden_score   * 0.10 +
            vol_score      * 0.10 +
            momentum_score * 0.15
        )

        # ── FUNDAMENTAL ────────────────────────────────────────────────────────
        fund_row = fund_df[fund_df["Ticker"] == ticker]
        if fund_row.empty:
            fundamental    = 50.0
            pe_score       = earnings_momentum_score = revenue_score = beta_score = 50.0
            short_interest = None
            insider_flag   = False
        else:
            fund = fund_row.iloc[0]

            pe = fund.get("trailingPE")
            if pd.isna(pe) or pe <= 0:
                pe_score = 50
            elif 10 <= pe <= 20:
                pe_score = 100
            elif pe < 10:
                pe_score = max(0, 50 + (pe - 10) * 5)
            else:
                pe_score = max(0, 100 - (pe - 20) * 2)

            # Earnings momentum: reward consistent beats (earningsGrowth + epsTrailingVsForward)
            eps_growth   = fund.get("earningsGrowth")
            eps_trailing = fund.get("trailingEps",  None)
            eps_forward  = fund.get("forwardEps",   None)
            beat_score   = 50  # neutral default
            if not pd.isna(eps_trailing) and not pd.isna(eps_forward) and eps_forward is not None and eps_trailing is not None:
                try:
                    eps_trailing = float(eps_trailing)
                    eps_forward  = float(eps_forward)
                    if eps_trailing > 0:
                        # forward > trailing = analysts expect growth = positive momentum
                        fwd_ratio = eps_forward / eps_trailing
                        beat_score = min(100, max(0, 50 + (fwd_ratio - 1) * 200))
                    elif eps_forward > 0:
                        beat_score = 75  # recovering from negative
                except Exception:
                    pass

            if pd.isna(eps_growth):
                growth_score = 50
            else:
                growth_score = min(100, max(0, 50 + eps_growth * 150))

            # Earnings momentum = blend of growth rate + forward vs trailing
            earnings_momentum_score = growth_score * 0.5 + beat_score * 0.5

            rev_growth    = fund.get("revenueGrowth")
            revenue_score = 50 if pd.isna(rev_growth) else min(100, max(0, 50 + rev_growth * 200))

            beta = fund.get("beta")
            beta_score = 50 if pd.isna(beta) else min(100, max(0, 100 - abs(beta - 1) * 50))

            # Short interest flag (flag only — not scored)
            short_pct = fund.get("shortPercentOfFloat", None)
            if not pd.isna(short_pct) if short_pct is not None else False:
                try:
                    short_interest = round(float(short_pct) * 100, 1)  # as %
                except Exception:
                    short_interest = None
            else:
                short_interest = None

            # Insider buying flag: net insider buys > 0 in last 6 months
            insider_buys  = fund.get("insiderPurchases",  0)
            insider_sells = fund.get("insiderSales",      0)
            try:
                insider_flag = (float(insider_buys) > float(insider_sells))
            except Exception:
                insider_flag = False

            fundamental = (
                pe_score                * 0.25 +
                earnings_momentum_score * 0.30 +
                revenue_score           * 0.25 +
                beta_score              * 0.20
            )

        # ── SENTIMENT (RSI-derived proxy — to be replaced with real sentiment data) ──
        sentiment = min(10, max(0, (rsi - 30) / 4))

        # ── RISK ───────────────────────────────────────────────────────────────
        beta_val = fund_row.iloc[0].get("beta") if not fund_row.empty else 1.0
        if pd.isna(beta_val):
            beta_val = 1.0
        # Penalise high short interest as additional risk
        short_risk_penalty = 0
        if short_interest is not None:
            if short_interest > 20:
                short_risk_penalty = 2.0
            elif short_interest > 10:
                short_risk_penalty = 1.0
        risk = min(10, max(0, 10 - abs(beta_val - 1) * 5 - abs(rsi - 50) / 10 - short_risk_penalty))

        # ── AI COMPOSITE SCORE (v3 weights) ────────────────────────────────────
        ai_score = (
            (technical   / 10) * 0.35 +
            (fundamental / 10) * 0.35 +
            sentiment          * 0.15 +
            risk               * 0.15
        )

        # ── INDUSTRY ───────────────────────────────────────────────────────────
        industry_val = ""
        if not fund_row.empty:
            raw_ind = fund_row.iloc[0].get("industry", "")
            if raw_ind and str(raw_ind).lower() not in ("nan", "none", "n/a", ""):
                industry_val = str(raw_ind)
        if not industry_val:
            raw_sec = row.get("Sector", "")
            if raw_sec and str(raw_sec).lower() not in ("nan", "none", "n/a", ""):
                industry_val = str(raw_sec)

        results.append({
            "Ticker":           ticker,
            "Name":             row["Name"],
            "Sector":           row["Sector"],
            "Industry":         industry_val,
            "Index":            row["Index"],
            "AI_Score":         round(ai_score, 2),
            "Technical":        round(technical  / 10, 2),
            "Fundamental":      round(fundamental / 10, 2),
            "Sentiment":        round(sentiment, 2),
            "Risk":             round(risk, 2),
            "RSI":              round(rsi, 2),
            "MACD_Hist":        round(macd_hist, 4),
            "Above_SMA50":      int(above_50),
            "Above_SMA200":     int(above_200),
            "Golden_Cross":     int(golden),
            "Short_Interest":   short_interest,   # % of float short, or None
            "Insider_Buying":   insider_flag,      # True if net insider buys > sells
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

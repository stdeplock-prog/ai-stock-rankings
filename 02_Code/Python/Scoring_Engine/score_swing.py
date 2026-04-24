# score_swing.py  v1.1  (swing-trader calibration)
# Generates a SWING-TRADING oriented score alongside the existing AI composite.
# Reads:
#   data/processed/technical_indicators/{ticker}_indicators.csv  (from calc_indicators.py v2.0)
#   data/processed/catalysts.csv                                  (from fetch_catalysts.py)
#   data/processed/scoring_outputs/rankings.csv                   (existing AI scores)
# Writes:
#   data/processed/scoring_outputs/swing_rankings.csv

import os
import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
IND_DIR   = os.path.join(REPO_ROOT, "data", "processed", "technical_indicators")
CATALYSTS = os.path.join(REPO_ROOT, "data", "processed", "catalysts.csv")
RANKINGS  = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "rankings.csv")
UNIVERSE  = os.path.join(REPO_ROOT, "data", "reference", "master_universe.csv")
OUT_FILE  = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs", "swing_rankings.csv")
os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)


def load_latest_indicators(ticker):
    path = os.path.join(IND_DIR, f"{ticker}_indicators.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    if df.empty:
        return None
    return df.iloc[-1]


def map_yfinance_rating_to_5scale(rec_mean):
    # yfinance: 1=Strong Buy ... 5=Sell. Our target: 5=Strong Buy ... 1=Strong Sell.
    if rec_mean is None or pd.isna(rec_mean):
        return None
    try:
        return max(1.0, min(5.0, 6.0 - float(rec_mean)))
    except Exception:
        return None


def bucket_atr(atr_pct):
    if atr_pct is None or pd.isna(atr_pct):
        return "Unknown"
    if atr_pct < 0.015: return "Low"
    if atr_pct < 0.035: return "Med"
    return "High"


def catalyst_bonus(days):
    if days is None or pd.isna(days):
        return 50.0
    d = float(days)
    if 10 <= d <= 60: return 100.0
    if 0 <= d < 3:  return 20.0
    if 3 <= d < 10:  return 55.0
    if 60 < d <= 90: return 60.0
    return 50.0

def main():
    universe = pd.read_csv(UNIVERSE)
    tickers  = universe["Ticker"].tolist()
    catalysts_df = pd.read_csv(CATALYSTS) if os.path.exists(CATALYSTS) else pd.DataFrame(columns=["Ticker"])
    rankings_df  = pd.read_csv(RANKINGS)  if os.path.exists(RANKINGS)  else pd.DataFrame(columns=["Ticker"])

    out_rows = []
    for ticker in tickers:
        latest = load_latest_indicators(ticker)
        if latest is None:
            continue
        row = {"Ticker": ticker}

        rsi       = latest.get("RSI_14", 50) or 50
        macd_hist = latest.get("MACD_Hist", 0) or 0
        above_50  = latest.get("Above_SMA50", 0) or 0
        above_200 = latest.get("Above_SMA200", 0) or 0
        adx       = latest.get("ADX_14", 0) or 0
        pctb      = latest.get("BB_PctB", 0.5)
        stoch_k   = latest.get("Stoch_K_14", 50) or 50

        if 50 <= rsi <= 70:   rsi_s = 100
        elif rsi < 50:        rsi_s = max(0, rsi * 2)
        else:                 rsi_s = max(0, 100 - (rsi - 70) * 3)
        macd_s  = 100 if macd_hist > 0 else 0
        trend_s = (int(above_50) * 50) + (int(above_200) * 50)
        adx_s   = min(100, max(0, (adx - 15) * 5))
        try:
            bb_s = 100 - abs(float(pctb) - 0.5) * 200
        except Exception:
            bb_s = 50
        stoch_s = 100 if 20 <= stoch_k <= 80 else 40

            tech = (rsi_s * 0.20 + macd_s * 0.20 + trend_s * 0.30 + adx_s * 0.20 + bb_s * 0.05 + stoch_s * 0.05)

        dist_50 = latest.get("Dist_From_SMA50_Pct", 0) or 0
        if dist_50 >= 0:
            momentum = max(0, 100 - dist_50 * 400)
        else:
            momentum = max(0, 60 + dist_50 * 200)

        cat = catalysts_df[catalysts_df["Ticker"] == ticker]
        if not cat.empty:
            c = cat.iloc[0]
            row["days_to_earnings"]    = c.get("days_to_earnings")
            row["next_earnings_date"]  = c.get("next_earnings_date")
            row["Ext_Rating_Score"]    = map_yfinance_rating_to_5scale(c.get("analyst_rating_mean"))
            row["num_analysts"]        = c.get("num_analysts")
            row["price_target_mean"]   = c.get("price_target_mean")
            row["Ext_Up_Downside_Pct"] = c.get("price_target_upside_pct")
            sent_from_news = c.get("news_sent_score_30d")
        else:
            row.update({"days_to_earnings": None, "next_earnings_date": None,
                        "Ext_Rating_Score": None, "Ext_Up_Downside_Pct": None})
            sent_from_news = None

        cat_bonus = catalyst_bonus(row["days_to_earnings"])
        row["Catalyst_Flag"] = bool(row["days_to_earnings"] is not None and
                                    10 <= (row["days_to_earnings"] or -1) <= 60)

        if sent_from_news is not None and not pd.isna(sent_from_news):
            sent = float(sent_from_news)
        else:
            ai_row = rankings_df[rankings_df["Ticker"] == ticker]
            sent = float(ai_row.iloc[0].get("Sent", 5)) * 10 if not ai_row.empty else 50.0
            sent = min(100, max(0, sent))

        atr_pct = latest.get("ATR_Pct", 0.02) or 0.02
        atr_norm = 100 if 0.015 <= atr_pct <= 0.040 else (60 + (atr_pct/0.015)*30 if atr_pct < 0.015 else max(0, 100 - (atr_pct - 0.040) * 2000) if atr_pct <= 0.080 else 0)
        vol_drag = atr_norm  # v1.1: now a vol-fit score (higher = better-fit for swing)

        swing = (0.35 * tech + 0.25 * momentum + 0.15 * sent + 0.15 * cat_bonus + 0.10 * vol_drag)

        row["Tech"] = round(tech, 2)
        row["Sent"] = round(sent, 2)
        row["Momentum"] = round(momentum, 2)
        row["Cat_Bonus"] = round(cat_bonus, 2)
        row["Vol_Drag"] = round(vol_drag, 2)
        row["ATR_Pct"] = round(atr_pct, 4)
        row["Vol_Bucket"] = bucket_atr(atr_pct)
        row["SwingScore"] = round(swing, 2)
        out_rows.append(row)

    df = pd.DataFrame(out_rows).sort_values("SwingScore", ascending=False).reset_index(drop=True)
    df["Swing_Rank"] = df.index + 1
    def tier(s):
        if s >= 70: return "A"
        if s >= 55: return "B"
        if s >= 40: return "C"
        return "D"
    df["Swing_Tier"] = df["SwingScore"].apply(tier)
    df.to_csv(OUT_FILE, index=False)
    print(f"Wrote {len(df)} rows to {OUT_FILE}")
    print(df.head(15).to_string(index=False))


if __name__ == "__main__":
    main()

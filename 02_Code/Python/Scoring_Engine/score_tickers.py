# score_tickers.py  v4.1
# Scores all tickers using TECHNICAL + FUNDAMENTAL data
# Changes v3.1:
#  - Revised weights: 30% Technical, 45% Fundamental, 10% Sentiment, 15% Risk#   - Earnings momentum replaces raw earningsGrowth in fundamental
#   - Short interest flag added (flag only, not scored)
#   - Insider buying flag added (flag only, not scored)
#   - Growth quality multiplier: penalises low/negative revenue growth
# Changes v4.1:
#   - Sentiment now blends RSI (>=50% weight) with analyst rating + price-target
#     upside from data/processed/catalysts.csv when available. Missing external
#     signals shift weight back to RSI (no penalty for missing data), so output
#     is identical to the legacy RSI-only formula when catalysts.csv is absent.
#   - rsi_sentiment + sentiment_source persisted as audit fields.
# Output: data/processed/scoring_outputs/rankings.csv
import pandas as pd
import numpy as np
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from industry_sector_map import resolve_sector
from sentiment_components import (
    rsi_sentiment as _rsi_sent,
    analyst_sentiment as _analyst_sent,
    upside_sentiment as _upside_sent,
    news_sentiment as _news_sent,
    blended_sentiment as _blend_sent,
)

REPO_ROOT       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
INDICATORS_DIR  = os.path.join(REPO_ROOT, "data", "processed", "technical_indicators")
FUNDAMENTALS    = os.path.join(REPO_ROOT, "data", "processed", "fundamentals.csv")
CATALYSTS_FILE  = os.path.join(REPO_ROOT, "data", "processed", "catalysts.csv")
UNIVERSE_FILE   = os.path.join(REPO_ROOT, "data", "reference", "master_universe.csv")
OUTPUT_DIR      = os.path.join(REPO_ROOT, "data", "processed", "scoring_outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

universe = pd.read_csv(UNIVERSE_FILE)
fund_df  = pd.read_csv(FUNDAMENTALS)
# catalysts.csv is produced by fetch_catalysts.py. In the workflow it runs
# *after* score_tickers, so on the very first run of a clean tree the file
# may be absent — the sentiment helpers fall back to RSI-only in that case.
if os.path.exists(CATALYSTS_FILE):
    catalysts_df = pd.read_csv(CATALYSTS_FILE).set_index("Ticker", drop=False)
else:
    catalysts_df = pd.DataFrame(columns=["Ticker"]).set_index("Ticker", drop=False)
print(f"Scoring {len(universe)} tickers...\n")

results = []

for _, row in universe.iterrows():
    ticker   = row["Ticker"]
    ind_file = os.path.join(INDICATORS_DIR, f"{ticker}_indicators.csv")
    if not os.path.exists(ind_file):
        continue
    try:
        df     = pd.read_csv(ind_file, index_col=0, parse_dates=True)
        if len(df) < 200:
            continue
        latest = df.iloc[-1]
        close  = df["Close"].squeeze() if "Close" in df.columns else df.iloc[:, 0]

        # TECHNICAL
        rsi = latest.get("RSI_14", 50)
        if 50 <= rsi <= 70:   rsi_score = 100
        elif rsi < 50:        rsi_score = max(0, rsi * 2)
        else:                 rsi_score = max(0, 100 - (rsi - 70) * 3)

        macd_hist  = latest.get("MACD_Hist", 0)
        macd_score = 100 if macd_hist > 0 else 0

        above_50    = latest.get("Above_SMA50",  0)
        above_200   = latest.get("Above_SMA200", 0)
        trend_score = (above_50 * 50) + (above_200 * 50)

        golden       = latest.get("Golden_Cross", 0)
        golden_score = 100 if golden == 1 else 0

        vol       = latest.get("Volume",     0)
        vol_avg   = latest.get("Vol_SMA_20", 1)
        vol_ratio = vol / vol_avg if vol_avg > 0 else 1
        vol_score = min(100, vol_ratio * 50)

        high_52w       = close.tail(252).max() if len(close) >= 252 else close.max()
        cur_close      = close.iloc[-1]
        momentum_score = min(100, max(0, (cur_close / high_52w) * 100)) if high_52w > 0 else 50

        technical = (
            rsi_score      * 0.20 +
            macd_score     * 0.15 +
            trend_score    * 0.20 +
            golden_score   * 0.10 +
            vol_score      * 0.10 +
            momentum_score * 0.25
        )

        # FUNDAMENTAL
        fund_row = fund_df[fund_df["Ticker"] == ticker]
        if fund_row.empty:
            fundamental    = 50.0
            short_interest = None
            insider_flag   = False
        else:
            fund = fund_row.iloc[0]

            pe = fund.get("trailingPE")
            if pd.isna(pe) or pe <= 0:  pe_score = 50
            elif 10 <= pe <= 20:        pe_score = 100
            elif pe < 10:               pe_score = max(0, 50 + (pe - 10) * 5)
            else:                       pe_score = max(0, 100 - (pe - 20) * 2)

            eps_growth   = fund.get("earningsGrowth")
            eps_trailing = fund.get("trailingEps", None)
            eps_forward  = fund.get("forwardEps",  None)
            beat_score   = 50
            try:
                if eps_trailing is not None and eps_forward is not None and not pd.isna(eps_trailing) and not pd.isna(eps_forward):
                    et = float(eps_trailing); ef = float(eps_forward)
                    if et > 0:    beat_score = min(100, max(0, 50 + (ef / et - 1) * 200))
                    elif ef > 0:  beat_score = 75
            except Exception:
                pass

            growth_score            = 50 if pd.isna(eps_growth) else min(100, max(0, 50 + eps_growth * 150))
            earnings_momentum_score = growth_score * 0.5 + beat_score * 0.5

            rev_growth    = fund.get("revenueGrowth")
            revenue_score = 50 if pd.isna(rev_growth) else min(100, max(0, 50 + rev_growth * 200))

            beta       = fund.get("beta")
            beta_score = 50 if pd.isna(beta) else min(100, max(0, 100 - abs(beta - 1) * 50))

            fundamental = (
                pe_score                * 0.20 +
                earnings_momentum_score * 0.35 +
                revenue_score           * 0.30 +
                beta_score              * 0.15
            )

            # Growth quality multiplier: penalise low/negative revenue growth
            rev_g = float(rev_growth) if not pd.isna(rev_growth) else 0.0
            if rev_g >= 0.10:    growth_quality_multiplier = 1.00
            elif rev_g >= 0.05:  growth_quality_multiplier = 0.95
            elif rev_g >= 0.02:  growth_quality_multiplier = 0.88
            elif rev_g >= 0.0:   growth_quality_multiplier = 0.80
            else:                growth_quality_multiplier = 0.70
            fundamental = fundamental * growth_quality_multiplier

            # Short interest flag
            short_pct = fund.get("shortPercentOfFloat", None)
            try:
                short_interest = round(float(short_pct) * 100, 1) if short_pct is not None and not pd.isna(short_pct) else None
            except Exception:
                short_interest = None

            # Insider buying flag
            try:
                insider_flag = float(fund.get("insiderPurchases", 0)) > float(fund.get("insiderSales", 0))
            except Exception:
                insider_flag = False

        # SENTIMENT
        # Legacy RSI-only sentiment kept as audit field. Final Sentiment is a
        # blend of RSI (>=50% weight) with analyst rating + price-target
        # upside drawn from catalysts.csv. Missing external signals shift
        # weight back to RSI rather than penalising the ticker, so the
        # output collapses to the legacy formula when catalysts data is
        # absent or sparse for a given ticker.
        rsi_sent_val = _rsi_sent(rsi)
        cat_rating  = None
        cat_upside  = None
        cat_news    = None
        cat_nanalys = None
        if ticker in catalysts_df.index:
            cat_row     = catalysts_df.loc[ticker]
            cat_rating  = cat_row.get("analyst_rating_mean")
            cat_upside  = cat_row.get("price_target_upside_pct")
            cat_news    = cat_row.get("news_sent_score_30d")
            cat_nanalys = cat_row.get("num_analysts")
        analyst_sent_val = _analyst_sent(cat_rating, cat_nanalys)
        upside_sent_val  = _upside_sent(cat_upside,  cat_nanalys)
        news_sent_val    = _news_sent(cat_news)
        sentiment, sentiment_source = _blend_sent(
            rsi_sent_val, analyst_sent_val, upside_sent_val, news_sent_val
        )

        # RISK
        beta_val = fund_row.iloc[0].get("beta") if not fund_row.empty else 1.0
        if pd.isna(beta_val): beta_val = 1.0
        short_risk_penalty = 0
        if short_interest is not None:
            if short_interest > 20:   short_risk_penalty = 2.0
            elif short_interest > 10: short_risk_penalty = 1.0
        risk = min(10, max(0, 10 - abs(beta_val - 1) * 5 - abs(rsi - 50) / 10 - short_risk_penalty))

        # AI COMPOSITE SCORE (v3.1 weights)
        ai_score = (
            (technical   / 10) * 0.30 +
            (fundamental / 10) * 0.45 +
            sentiment          * 0.10 +
            risk               * 0.15
        )

        # INDUSTRY
        industry_val = ""
        if not fund_row.empty:
            raw_ind = fund_row.iloc[0].get("industry", "")
            if raw_ind and str(raw_ind).lower() not in ("nan", "none", "n/a", ""):
                industry_val = str(raw_ind)
        if not industry_val:
            raw_sec = row.get("Sector", "")
            if raw_sec and str(raw_sec).lower() not in ("nan", "none", "n/a", ""):
                industry_val = str(raw_sec)

        # SECTOR: prefer yfinance sector, fall back to universe Sector, then a
        # deterministic industry->sector mapping. Many NDX100 / Russell1000
        # rows have universe Sector="N/A", so without this fallback rankings
        # rows show a populated industry but blank sector.
        yf_sector = fund_row.iloc[0].get("sector") if not fund_row.empty else None
        sector_val = resolve_sector(
            yf_sector=yf_sector,
            universe_sector=row.get("Sector"),
            industry=industry_val,
        )

        # MARKET CAP (raw number; display formatting happens in export_to_json)
        market_cap_raw = None
        if not fund_row.empty:
            mc = fund_row.iloc[0].get("marketCap", None)
            try:
                if mc is not None and not pd.isna(mc):
                    market_cap_raw = float(mc)
            except Exception:
                market_cap_raw = None

        results.append({
            "Ticker":         ticker,
            "Name":           row["Name"],
            "Sector":         sector_val,
            "Industry":       industry_val,
            "Index":          row["Index"],
            "AI_Score":       round(ai_score, 2),
            "Technical":      round(technical  / 10, 2),
            "Fundamental":    round(fundamental / 10, 2),
            "Sentiment":      round(sentiment, 2),
            "RSI_Sentiment":  round(rsi_sent_val, 2),
            "Analyst_Sentiment": round(analyst_sent_val, 2) if analyst_sent_val is not None else None,
            "Upside_Sentiment":  round(upside_sent_val, 2) if upside_sent_val is not None else None,
            "News_Sentiment":    round(news_sent_val, 2) if news_sent_val is not None else None,
            "Sentiment_Source":  sentiment_source,
            "Risk":           round(risk, 2),
            "RSI":            round(rsi, 2),
            "MACD_Hist":      round(macd_hist, 4),
            "Above_SMA50":    int(above_50),
            "Above_SMA200":   int(above_200),
            "Golden_Cross":   int(golden),
            "Short_Interest": short_interest,
            "Insider_Buying": insider_flag,
            "MarketCap":      market_cap_raw,
        })
    except Exception as e:
        print(f"  ERROR {ticker}: {e}")

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

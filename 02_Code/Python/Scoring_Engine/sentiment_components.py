# sentiment_components.py  v1.0
# Builds the Sentiment subscore (0-10) used by score_tickers.py.
#
# History: Sentiment was originally derived purely from RSI_14 — really a
# second technical signal in disguise. This module keeps the RSI-based
# component (so existing weighting/audit trail is preserved) and supplements
# it with analyst rating and price-target upside drawn from
# data/processed/catalysts.csv (fetched by fetch_catalysts.py).
#
# Conservative defaults:
#   - RSI keeps at least 50% of the weight.
#   - Missing external signals are treated as "no information" — weight is
#     shifted to RSI rather than penalising the ticker with a neutral 5.
#   - If both external signals are missing, output is identical to the old
#     RSI-only formula (full backwards-compatible fallback).
#
# Component breakdown (each 0-10):
#   rsi_sentiment      : (RSI - 30) / 4, clipped to [0, 10]
#   analyst_sentiment  : from analyst_rating_mean (yfinance 1=SB..5=SS)
#                        mapped linearly so 1.0 -> 10, 3.0 -> 5, 5.0 -> 0.
#                        Requires num_analysts >= MIN_ANALYSTS (default 3).
#   upside_sentiment   : from price_target_upside_pct, piecewise linear:
#                        <= -20%  -> 0,  0% -> 5,  >= +30% -> 10.
#                        Requires num_analysts >= MIN_ANALYSTS.
#   news_sentiment     : from news_sent_score_30d (already 0-100), scaled /10.
#                        Currently only populated by the EODHD provider.

from __future__ import annotations

from typing import Optional
import math

MIN_ANALYSTS = 3        # below this, analyst signals are treated as absent
RSI_MIN_WEIGHT = 0.50   # RSI always retains at least this share of the blend


def _is_missing(v) -> bool:
    if v is None:
        return True
    try:
        if isinstance(v, float) and math.isnan(v):
            return True
    except Exception:
        pass
    try:
        # pandas NaN / NA without importing pandas
        if v != v:  # NaN is the only value that != itself
            return True
    except Exception:
        pass
    return False


def rsi_sentiment(rsi) -> float:
    """Old formula: 30 -> 0, 50 -> 5, 70 -> 10, clipped."""
    if _is_missing(rsi):
        return 5.0
    try:
        return min(10.0, max(0.0, (float(rsi) - 30.0) / 4.0))
    except Exception:
        return 5.0


def analyst_sentiment(rating_mean, num_analysts) -> Optional[float]:
    """yfinance recommendationMean: 1=Strong Buy ... 5=Sell.
    Returns 0-10 (higher = more bullish), or None when unusable."""
    if _is_missing(rating_mean) or _is_missing(num_analysts):
        return None
    try:
        n = int(float(num_analysts))
        if n < MIN_ANALYSTS:
            return None
        r = float(rating_mean)
        if not (1.0 <= r <= 5.0):
            return None
        # 1.0 -> 10, 3.0 -> 5, 5.0 -> 0  (linear, slope -2.5)
        score = 12.5 - 2.5 * r
        return min(10.0, max(0.0, score))
    except Exception:
        return None


def upside_sentiment(upside_pct, num_analysts) -> Optional[float]:
    """Price-target upside in percent. Piecewise-linear -> 0-10."""
    if _is_missing(upside_pct) or _is_missing(num_analysts):
        return None
    try:
        n = int(float(num_analysts))
        if n < MIN_ANALYSTS:
            return None
        u = float(upside_pct)
    except Exception:
        return None
    # <= -20% -> 0;  -20%..0% -> 0..5;  0%..+30% -> 5..10;  >= +30% -> 10
    if u <= -20.0:
        return 0.0
    if u <= 0.0:
        return 5.0 * (u + 20.0) / 20.0
    if u <= 30.0:
        return 5.0 + 5.0 * (u / 30.0)
    return 10.0


def news_sentiment(news_score_0_100) -> Optional[float]:
    """news_sent_score_30d is already 0..100; scale to 0..10."""
    if _is_missing(news_score_0_100):
        return None
    try:
        v = float(news_score_0_100)
    except Exception:
        return None
    return min(10.0, max(0.0, v / 10.0))


def blended_sentiment(rsi_s: float,
                      analyst_s: Optional[float],
                      upside_s: Optional[float],
                      news_s: Optional[float] = None) -> tuple[float, str]:
    """Blend components with RSI guaranteed at >= RSI_MIN_WEIGHT.

    Per-component target weights (used only when component is present):
      analyst -> 0.20, upside -> 0.20, news -> 0.10
    RSI absorbs the residual (0.50 plus any weight from missing components),
    so missing data does not penalise the ticker.

    Returns (score, source_label).
    """
    weights = {"rsi": RSI_MIN_WEIGHT, "analyst": 0.20, "upside": 0.20, "news": 0.10}

    present = {"rsi": rsi_s}
    if analyst_s is not None:
        present["analyst"] = analyst_s
    if upside_s is not None:
        present["upside"] = upside_s
    if news_s is not None:
        present["news"] = news_s

    # RSI gets its share + any missing-component weight, so coverage gaps
    # collapse smoothly back to the legacy RSI-only score.
    used_weight = sum(weights[k] for k in present)
    rsi_extra = 1.0 - used_weight  # weight of missing components
    effective = {k: weights[k] for k in present}
    effective["rsi"] = effective.get("rsi", 0.0) + rsi_extra

    score = sum(present[k] * effective[k] for k in present)
    score = min(10.0, max(0.0, score))

    extras = [k for k in ("analyst", "upside", "news") if k in present]
    source = "rsi_only" if not extras else "rsi+" + "+".join(extras)
    return score, source

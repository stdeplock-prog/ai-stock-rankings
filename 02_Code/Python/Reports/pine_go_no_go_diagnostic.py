"""Pine Go/No-Go Diagnostic — daily-OHLCV-only proxy for the Pine v6
go/no-go gate stack the user developed in TradingView. Pure diagnostic
read-out; this report does NOT alter scoring formulas or rankings.

Inputs (read-only, no network):
  - data/rankings.json (main universe + days_to_earnings)
  - data/watchlist_rankings.json (watchlist + supplemental tickers)
  - data/reports/external_benchmark_review.json (coverage)
  - data/reports/disagreement_queue.json (per-ticker external dissent)
  - data/raw/ohlcv_daily/<TICKER>_daily.csv (populated by fetch_ohlcv.py
    earlier in the workflow; absent locally between runs)

Outputs:
  - data/reports/pine_go_no_go_diagnostic.json
  - reports/pine-go-no-go-diagnostic.html
  - data/tasks.json row id=pine-go-no-go (stamped each run)

Caveats (also surfaced in the HTML):
  * Daily OHLCV only. No true intraday VWAP, no Open Drift, no minute bars.
  * Diagnostic / decision-support; rankings are NOT mutated.
  * RSI/MFI thresholds match Pine analysis doc; see pine_intent_analysis.md
    section 2-5 in /home/user/workspace.

Pine signal sources (see pine_intent_analysis.md):
  trend_sma_aligned : 5/8/13 SMA stack (Scripts 1, 2, 5, 7, 9)
  rsi_in_zone       : RSI(14) 55-70 (Scripts 1, 2, 5, 7, 9, 10, 11)
  rsi_slope_pos     : RSI(14) > RSI(14)[3] (3-bar slope)
  above_sma20       : close > SMA(close, 20) — trend anchor / BB midline
  above_bb_mid      : close > BB(20, 2) middle band (== SMA20 by definition)
  return_20d_ok     : close / close[20] - 1 >= 8% (main mode)
  rel_vol_ok        : volume / SMA(volume, 20) >= 1.3x
  bar_strength      : close > prior bar high
  ma50_rising       : SMA50 today > SMA50 5 bars ago
  above_sma50       : close > SMA(close, 50)
  mfi_above_avg     : MFI(14) > SMA(MFI, 14)
  near_20d_high     : close >= 0.995 * highest(close, 20)
  overextended_bb   : close > 1.015 * BB upper(20, 2)  (BLOCKER)
  low_vol_chop      : 10-bar (max(high) - min(low)) / close < 2%  (BLOCKER)
  earnings_near     : days_to_earnings <= 21 (BLOCKER, when known)
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"
OHLCV_DIR = DATA_DIR / "raw" / "ohlcv_daily"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"
EXT_REVIEW_FILE = DATA_REPORTS_DIR / "external_benchmark_review.json"
DISAGREEMENT_FILE = DATA_REPORTS_DIR / "disagreement_queue.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "pine-go-no-go-diagnostic.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/pine-go-no-go-diagnostic.html"
TASK_ID = "pine-go-no-go"

# Pine thresholds (see pine_intent_analysis.md sections 2 and 5).
RSI_FLOOR = 55.0
RSI_CAP = 70.0
RSI_SLOPE_LOOKBACK = 3
RETURN_20D_THRESHOLD = 0.08
REL_VOL_THRESHOLD = 1.3
NEAR_HIGH_FRAC = 0.995
OVEREXTEND_BB_FRAC = 1.015         # >1.5% above upper BB
LOW_VOL_CHOP_RANGE = 0.02          # 10-bar range / close < 2%
LOW_VOL_LOOKBACK = 10
EARNINGS_BLOCK_DAYS = 21
MA50_RISE_LOOKBACK = 5

GO_NO_GO_GATES = [
    "trend_sma_aligned", "above_sma20", "rsi_in_zone", "rsi_slope_pos",
    "above_bb_mid", "return_20d_ok", "above_sma50", "ma50_rising",
    "rel_vol_ok", "bar_strength",
]
ACCUMULATION_GATES = [
    "rel_vol_ok", "mfi_above_avg", "bar_strength", "near_20d_high",
    "rsi_in_zone",
]


# ----------------- pure indicator math (test-friendly) -----------------


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def _sma_series(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - n : i + 1]) / n)
    return out


def _rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI. Returns a list aligned with `closes`."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        out[n] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[n] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(n + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def _bb_upper(closes: list[float], n: int = 20, k: float = 2.0) -> float | None:
    if len(closes) < n:
        return None
    window = closes[-n:]
    mean = sum(window) / n
    var = sum((c - mean) ** 2 for c in window) / n
    return mean + k * math.sqrt(var)


def _mfi(highs: list[float], lows: list[float], closes: list[float],
         volumes: list[float], n: int = 14) -> list[float | None]:
    """Money Flow Index using typical price (HLC/3)."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    raw_flow = [tp[i] * volumes[i] for i in range(len(closes))]
    pos_flow = [0.0] * len(closes)
    neg_flow = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if tp[i] > tp[i - 1]:
            pos_flow[i] = raw_flow[i]
        elif tp[i] < tp[i - 1]:
            neg_flow[i] = raw_flow[i]
    for i in range(n, len(closes)):
        pos = sum(pos_flow[i - n + 1 : i + 1])
        neg = sum(neg_flow[i - n + 1 : i + 1])
        if neg == 0:
            out[i] = 100.0
        else:
            ratio = pos / neg
            out[i] = 100.0 - (100.0 / (1.0 + ratio))
    return out


# ----------------- gate evaluation -----------------


def evaluate_gates(opens: list[float], highs: list[float], lows: list[float],
                   closes: list[float], volumes: list[float],
                   days_to_earnings: int | None = None) -> dict:
    """Run all Pine-derived gates against a single ticker's daily series.
    Last bar is treated as 'today'. Returns a dict with gate booleans,
    raw metric values, scores, and blocker info. Gates that lack enough
    data return None and are excluded from the score denominator.
    """
    n = len(closes)
    res: dict = {
        "bars": n,
        "gates": {},
        "metrics": {},
        "blockers": [],
        "insufficient_data_reasons": [],
    }
    if n < 21:  # need 20-day windows + 1 prior bar
        res["insufficient_data_reasons"].append(f"only {n} bars (need >=21)")
        res["go_no_go_score"] = None
        res["go_no_go_score_normalized"] = None
        res["accumulation_score"] = None
        return res

    last_close = closes[-1]
    res["metrics"]["last_close"] = round(last_close, 4)

    # SMA stacks
    sma5 = _sma(closes, 5)
    sma8 = _sma(closes, 8)
    sma13 = _sma(closes, 13)
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50) if n >= 50 else None
    sma50_lag = _sma(closes[:-MA50_RISE_LOOKBACK], 50) if n >= 50 + MA50_RISE_LOOKBACK else None
    res["metrics"].update({
        "sma5": _round_or_none(sma5), "sma8": _round_or_none(sma8),
        "sma13": _round_or_none(sma13), "sma20": _round_or_none(sma20),
        "sma50": _round_or_none(sma50),
    })
    res["gates"]["trend_sma_aligned"] = (
        sma5 is not None and sma8 is not None and sma13 is not None
        and sma5 > sma8 > sma13
    )
    res["gates"]["above_sma20"] = sma20 is not None and last_close > sma20
    res["gates"]["above_bb_mid"] = res["gates"]["above_sma20"]  # BB mid == SMA20

    if sma50 is not None:
        res["gates"]["above_sma50"] = last_close > sma50
    else:
        res["gates"]["above_sma50"] = None
        res["insufficient_data_reasons"].append("sma50 needs >=50 bars")
    if sma50 is not None and sma50_lag is not None:
        res["gates"]["ma50_rising"] = sma50 > sma50_lag
    else:
        res["gates"]["ma50_rising"] = None

    # RSI
    rsi_series = _rsi(closes, 14)
    rsi_last = rsi_series[-1]
    rsi_lag = rsi_series[-1 - RSI_SLOPE_LOOKBACK] if len(rsi_series) > RSI_SLOPE_LOOKBACK else None
    res["metrics"]["rsi14"] = _round_or_none(rsi_last)
    res["metrics"]["rsi14_3bar_ago"] = _round_or_none(rsi_lag)
    if rsi_last is None:
        res["gates"]["rsi_in_zone"] = None
        res["gates"]["rsi_slope_pos"] = None
    else:
        res["gates"]["rsi_in_zone"] = RSI_FLOOR <= rsi_last <= RSI_CAP
        if rsi_lag is None:
            res["gates"]["rsi_slope_pos"] = None
        else:
            res["gates"]["rsi_slope_pos"] = rsi_last > rsi_lag

    # 20-day return
    if n >= 21:
        ret_20d = closes[-1] / closes[-21] - 1.0
        res["metrics"]["return_20d"] = round(ret_20d, 4)
        res["gates"]["return_20d_ok"] = ret_20d >= RETURN_20D_THRESHOLD
    else:
        res["gates"]["return_20d_ok"] = None

    # Relative volume
    vol_sma20 = _sma(volumes, 20)
    if vol_sma20 and vol_sma20 > 0:
        rel_vol = volumes[-1] / vol_sma20
        res["metrics"]["rel_vol_20d"] = round(rel_vol, 3)
        res["gates"]["rel_vol_ok"] = rel_vol >= REL_VOL_THRESHOLD
    else:
        res["gates"]["rel_vol_ok"] = None

    # Bar strength
    res["gates"]["bar_strength"] = (n >= 2) and (closes[-1] > highs[-2])

    # MFI
    mfi_series = _mfi(highs, lows, closes, volumes, 14)
    mfi_last = mfi_series[-1]
    mfi_sma14 = None
    if mfi_last is not None:
        clean = [m for m in mfi_series[-14:] if m is not None]
        if len(clean) == 14:
            mfi_sma14 = sum(clean) / 14.0
    res["metrics"]["mfi14"] = _round_or_none(mfi_last)
    if mfi_last is None or mfi_sma14 is None:
        res["gates"]["mfi_above_avg"] = None
        if mfi_last is None:
            res["insufficient_data_reasons"].append("mfi needs >=15 bars")
    else:
        res["gates"]["mfi_above_avg"] = mfi_last > mfi_sma14

    # Near 20-day high
    high_20 = max(closes[-20:])
    res["metrics"]["high_20d"] = round(high_20, 4)
    res["gates"]["near_20d_high"] = last_close >= high_20 * NEAR_HIGH_FRAC

    # Blockers
    bb_up = _bb_upper(closes, 20, 2.0)
    res["metrics"]["bb_upper_20"] = _round_or_none(bb_up)
    overextended = bb_up is not None and last_close > bb_up * OVEREXTEND_BB_FRAC
    if overextended:
        res["blockers"].append("overextended_bb (>1.5% above BB upper)")

    if n >= LOW_VOL_LOOKBACK:
        win_high = max(highs[-LOW_VOL_LOOKBACK:])
        win_low = min(lows[-LOW_VOL_LOOKBACK:])
        chop_range = (win_high - win_low) / last_close if last_close else None
        res["metrics"]["range_10d_pct"] = round(chop_range, 4) if chop_range is not None else None
        if chop_range is not None and chop_range < LOW_VOL_CHOP_RANGE:
            res["blockers"].append("low_volatility_chop (10d range <2%)")

    if isinstance(days_to_earnings, int) and 0 <= days_to_earnings <= EARNINGS_BLOCK_DAYS:
        res["blockers"].append(f"earnings_near ({days_to_earnings}d)")

    # Scores
    gate_pass = 0
    gate_total = 0
    for name in GO_NO_GO_GATES:
        v = res["gates"].get(name)
        if v is None:
            continue
        gate_total += 1
        if v:
            gate_pass += 1
    res["go_no_go_score"] = gate_pass
    res["go_no_go_gates_evaluated"] = gate_total
    res["go_no_go_score_normalized"] = (
        round(gate_pass / gate_total, 3) if gate_total else None
    )

    acc_pass = 0
    acc_total = 0
    for name in ACCUMULATION_GATES:
        v = res["gates"].get(name)
        if v is None:
            continue
        acc_total += 1
        if v:
            acc_pass += 1
    res["accumulation_score"] = acc_pass
    res["accumulation_gates_evaluated"] = acc_total
    res["accumulation_score_normalized"] = (
        round(acc_pass / acc_total, 3) if acc_total else None
    )

    res["blocker_count"] = len(res["blockers"])
    return res


def _round_or_none(v):
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return round(float(v), 4)


# ----------------- disagreement classification -----------------


def classify_disagreement(internal_direction: str, gate_result: dict | None) -> dict:
    """Map a Pine gate evaluation against the internal-vs-external dispute
    for one disagreement_queue ticker. Returns classification + recommended
    action. Action recommendations are advisory only; this report does not
    mutate scores.

    classifications:
      supports_internal         — Pine confirms the bullish internal view
      supports_external_caution — Pine agrees with external bearish caution
      mixed                     — Pine partial / inconclusive
      insufficient_data         — Not enough OHLCV to judge
    """
    if not gate_result or gate_result.get("go_no_go_score_normalized") is None:
        return {
            "classification": "insufficient_data",
            "action": "review",
            "rationale": "no Pine evaluation available",
        }

    score = gate_result.get("go_no_go_score_normalized") or 0.0
    blockers = gate_result.get("blockers") or []
    direction = (internal_direction or "").lower()
    is_internal_bullish = direction in ("bullish", "strong_bullish")

    # Strong internal bullish thesis disputed by external sources
    if is_internal_bullish:
        if score >= 0.6 and not blockers:
            return {
                "classification": "supports_internal",
                "action": "keep",
                "rationale": (f"Pine score {score:.2f} >= 0.60, no blockers "
                              "— internal bullish thesis confirmed by gate stack"),
            }
        if score < 0.4 or blockers:
            if blockers and score >= 0.4:
                rationale = (f"Pine score {score:.2f} OK but blockers fired "
                             f"({', '.join(blockers)}) — Pine flags caution")
            else:
                rationale = (f"Pine score {score:.2f} weak"
                             + (f"; blockers={blockers}" if blockers else "")
                             + " — Pine agrees with external bearish caution")
            return {
                "classification": "supports_external_caution",
                "action": "downgrade-watchlist-only",
                "rationale": rationale,
            }
        return {
            "classification": "mixed",
            "action": "review",
            "rationale": f"Pine score {score:.2f} mid-range, no clear support",
        }

    # Neutral / bearish internal — disagreement direction varies
    if score >= 0.7 and not blockers:
        return {
            "classification": "mixed",
            "action": "review",
            "rationale": (f"Pine score {score:.2f} bullish but internal "
                          f"direction is {direction!r} — possible upside miss"),
        }
    if score < 0.4:
        return {
            "classification": "supports_external_caution",
            "action": "downgrade-watchlist-only",
            "rationale": f"Pine score {score:.2f} weak, agrees with external caution",
        }
    return {
        "classification": "mixed",
        "action": "review",
        "rationale": f"Pine score {score:.2f} mid-range",
    }


# ----------------- IO helpers -----------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_chicago(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    chi_utc = dt.astimezone(timezone.utc)
    offset_h = -5 if 3 <= chi_utc.month <= 10 else -6
    return chi_utc.astimezone(timezone(timedelta(hours=offset_h)))


def _load_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_ohlcv(ticker: str, ohlcv_dir: Path = OHLCV_DIR
               ) -> tuple[list[float], list[float], list[float],
                          list[float], list[float]] | None:
    """Read fetch_ohlcv.py output for `ticker`. Returns (opens, highs,
    lows, closes, volumes) or None if missing/empty."""
    path = ohlcv_dir / f"{ticker}_daily.csv"
    if not path.exists():
        return None
    try:
        import pandas as pd  # noqa: F401  (import here so tests don't require it for math)
        import pandas as _pd
        df = _pd.read_csv(path)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    cols = {c.lower(): c for c in df.columns}
    needed = ("open", "high", "low", "close", "volume")
    if not all(k in cols for k in needed):
        return None
    try:
        opens = df[cols["open"]].astype(float).tolist()
        highs = df[cols["high"]].astype(float).tolist()
        lows = df[cols["low"]].astype(float).tolist()
        closes = df[cols["close"]].astype(float).tolist()
        volumes = df[cols["volume"]].astype(float).tolist()
    except (ValueError, TypeError):
        return None
    return opens, highs, lows, closes, volumes


# ----------------- report assembly -----------------


def _collect_targets(rankings: dict | None, watchlist: dict | None,
                     disagreement: dict | None,
                     main_top: int = 50, watch_top: int = 30) -> dict:
    """Build the candidate ticker set with metadata. Uses ranks/watchlist
    for company/sector/days_to_earnings/internal_ai_score."""
    targets: dict[str, dict] = {}

    def _add(row: dict, source: str) -> None:
        tic = row.get("ticker")
        if not tic:
            return
        cur = targets.setdefault(tic, {"ticker": tic, "sources": []})
        if source not in cur["sources"]:
            cur["sources"].append(source)
        for k in ("company", "sector", "ai_score", "swing_score",
                  "swing_tier", "days_to_earnings"):
            v = row.get(k)
            if v is not None and cur.get(k) is None:
                cur[k] = v

    if isinstance(rankings, dict):
        for r in (rankings.get("rows") or [])[:main_top]:
            if isinstance(r, dict):
                _add(r, "main")
    if isinstance(watchlist, dict):
        for r in (watchlist.get("rows") or [])[:watch_top]:
            if isinstance(r, dict):
                _add(r, "watchlist")
    if isinstance(disagreement, dict):
        for q in (disagreement.get("queue") or []):
            if isinstance(q, dict):
                row = {
                    "ticker": q.get("ticker"),
                    "sector": q.get("sector"),
                    "ai_score": q.get("internal_ai_score_0to10"),
                }
                _add(row, "disagreement")
    return targets


def build_report(rankings: dict | None, watchlist: dict | None,
                 ext_review: dict | None, disagreement: dict | None,
                 ohlcv_dir: Path = OHLCV_DIR) -> dict:
    targets = _collect_targets(rankings, watchlist, disagreement)
    per_ticker: list[dict] = []
    counts = {
        "evaluated": 0,
        "ohlcv_missing": 0,
        "insufficient_bars": 0,
        "blocked": 0,
        "go_normalized_ge_07": 0,
        "go_normalized_lt_04": 0,
    }

    # Build a quick disagreement direction lookup
    disagreement_directions: dict[str, str] = {}
    if isinstance(disagreement, dict):
        for q in (disagreement.get("queue") or []):
            if isinstance(q, dict) and q.get("ticker"):
                disagreement_directions[q["ticker"]] = (
                    q.get("internal_ai_direction") or "neutral"
                )

    for tic in sorted(targets.keys()):
        meta = targets[tic]
        ohlcv = load_ohlcv(tic, ohlcv_dir)
        if ohlcv is None:
            counts["ohlcv_missing"] += 1
            per_ticker.append({
                **meta,
                "evaluated": False,
                "reason": "ohlcv missing",
            })
            continue
        opens, highs, lows, closes, volumes = ohlcv
        days_to_earn = meta.get("days_to_earnings")
        try:
            days_to_earn_int = int(days_to_earn) if days_to_earn is not None else None
        except (TypeError, ValueError):
            days_to_earn_int = None
        gate_eval = evaluate_gates(opens, highs, lows, closes, volumes,
                                   days_to_earnings=days_to_earn_int)
        evaluated = gate_eval.get("go_no_go_score_normalized") is not None
        if not evaluated:
            counts["insufficient_bars"] += 1
        else:
            counts["evaluated"] += 1
            if gate_eval["go_no_go_score_normalized"] >= 0.7:
                counts["go_normalized_ge_07"] += 1
            if gate_eval["go_no_go_score_normalized"] < 0.4:
                counts["go_normalized_lt_04"] += 1
            if gate_eval.get("blocker_count"):
                counts["blocked"] += 1

        entry = {**meta, "evaluated": evaluated, **gate_eval}

        if tic in disagreement_directions:
            entry["disagreement"] = classify_disagreement(
                disagreement_directions[tic],
                gate_eval if evaluated else None,
            )
        per_ticker.append(entry)

    # Surface highlight lists (cap counts to keep JSON tight)
    supports_internal: list[dict] = []
    supports_external: list[dict] = []
    mixed: list[dict] = []
    insufficient: list[dict] = []
    for entry in per_ticker:
        d = entry.get("disagreement")
        if not d:
            continue
        item = {
            "ticker": entry.get("ticker"),
            "sector": entry.get("sector"),
            "ai_score": entry.get("ai_score"),
            "go_no_go_score_normalized": entry.get("go_no_go_score_normalized"),
            "blockers": entry.get("blockers") or [],
            "action": d.get("action"),
            "rationale": d.get("rationale"),
        }
        cls = d.get("classification")
        if cls == "supports_internal":
            supports_internal.append(item)
        elif cls == "supports_external_caution":
            supports_external.append(item)
        elif cls == "mixed":
            mixed.append(item)
        else:
            insufficient.append(item)

    # Top main-universe diagnostics: cleanest go/no-go signals
    evaluated_only = [
        e for e in per_ticker
        if e.get("evaluated") and "main" in (e.get("sources") or [])
    ]
    cleanest_go = sorted(
        [e for e in evaluated_only if not (e.get("blockers") or [])],
        key=lambda e: e.get("go_no_go_score_normalized") or 0.0,
        reverse=True,
    )[:15]
    weakest = sorted(
        evaluated_only,
        key=lambda e: e.get("go_no_go_score_normalized") or 0.0,
    )[:10]
    blocked = [
        e for e in evaluated_only if e.get("blockers")
    ][:15]

    overall = "OK"
    if counts["ohlcv_missing"] >= max(1, len(targets) * 0.7):
        overall = "WARN"
    if counts["evaluated"] == 0:
        overall = "FAIL"

    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    chi_label = "CDT" if chi_now.utcoffset().total_seconds() == -5 * 3600 else "CST"
    chi_str = chi_now.strftime("%Y-%m-%d %I:%M %p ") + chi_label

    report = {
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_chicago": chi_str,
        "overall": overall,
        "caveat": (
            "DAILY OHLCV ONLY: This report is a daily-bar proxy for the "
            "Pine v6 go/no-go gate stack. No true intraday VWAP, no Open "
            "Drift, no minute-bar logic. DIAGNOSTIC ONLY — rankings and "
            "scoring formulas are NOT mutated."
        ),
        "thresholds": {
            "rsi_floor": RSI_FLOOR, "rsi_cap": RSI_CAP,
            "rsi_slope_lookback_bars": RSI_SLOPE_LOOKBACK,
            "return_20d": RETURN_20D_THRESHOLD,
            "rel_vol": REL_VOL_THRESHOLD,
            "near_20d_high_frac": NEAR_HIGH_FRAC,
            "overextended_bb_frac": OVEREXTEND_BB_FRAC,
            "low_vol_chop_range": LOW_VOL_CHOP_RANGE,
            "low_vol_lookback": LOW_VOL_LOOKBACK,
            "earnings_block_days": EARNINGS_BLOCK_DAYS,
            "ma50_rise_lookback": MA50_RISE_LOOKBACK,
        },
        "counts": {**counts, "candidates": len(targets)},
        "highlights": {
            "cleanest_go_main": cleanest_go,
            "weakest_main": weakest,
            "blocked_main": blocked,
            "disagreement_supports_internal": supports_internal,
            "disagreement_supports_external_caution": supports_external,
            "disagreement_mixed": mixed,
            "disagreement_insufficient_data": insufficient,
        },
        "per_ticker": per_ticker,
        "inputs": {
            "rankings_present": rankings is not None,
            "watchlist_present": watchlist is not None,
            "external_benchmark_review_present": ext_review is not None,
            "disagreement_queue_present": disagreement is not None,
            "ohlcv_dir_exists": ohlcv_dir.exists(),
        },
    }
    report["summary"] = build_summary(report)
    return report


def build_summary(report: dict) -> str:
    c = report["counts"]
    parts = [
        f"Overall {report['overall']}",
        f"cands={c['candidates']}",
        f"evaluated={c['evaluated']}",
        f"missing_ohlcv={c['ohlcv_missing']}",
        f"go>=.7:{c['go_normalized_ge_07']}",
        f"weak<.4:{c['go_normalized_lt_04']}",
        f"blocked={c['blocked']}",
    ]
    h = report["highlights"]
    parts.append(f"dis_supports_internal={len(h['disagreement_supports_internal'])}")
    parts.append(f"dis_supports_external={len(h['disagreement_supports_external_caution'])}")
    return " · ".join(parts)


# ----------------- HTML rendering -----------------


def _badge_color(overall: str) -> str:
    return {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}.get(overall, "#666")


def _render_disagreement_table(rows: list[dict]) -> str:
    if not rows:
        return "<p class='kv'>—</p>"
    out = ["<table><thead><tr><th>Ticker</th><th>Sector</th><th>AI</th>"
           "<th>Pine score</th><th>Blockers</th><th>Action</th>"
           "<th>Rationale</th></tr></thead><tbody>"]
    for r in rows:
        score = r.get("go_no_go_score_normalized")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "—"
        bl = r.get("blockers") or []
        out.append(
            f"<tr><td><strong>{escape(str(r.get('ticker') or ''))}</strong></td>"
            f"<td>{escape(str(r.get('sector') or ''))}</td>"
            f"<td>{escape(str(r.get('ai_score') or ''))}</td>"
            f"<td>{escape(score_str)}</td>"
            f"<td>{escape(', '.join(bl) if bl else '—')}</td>"
            f"<td>{escape(str(r.get('action') or ''))}</td>"
            f"<td>{escape(str(r.get('rationale') or ''))}</td></tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def _render_main_table(rows: list[dict]) -> str:
    if not rows:
        return "<p class='kv'>—</p>"
    out = ["<table><thead><tr><th>Ticker</th><th>Sector</th><th>AI</th>"
           "<th>Pine score</th><th>Acc score</th>"
           "<th>Blockers</th><th>Notable gates</th></tr></thead><tbody>"]
    for r in rows:
        score = r.get("go_no_go_score_normalized")
        score_str = f"{score:.2f} ({r.get('go_no_go_score',0)}/{r.get('go_no_go_gates_evaluated',0)})" \
            if isinstance(score, (int, float)) else "—"
        acc = r.get("accumulation_score_normalized")
        acc_str = f"{acc:.2f}" if isinstance(acc, (int, float)) else "—"
        gates = r.get("gates") or {}
        notable = [name for name in GO_NO_GO_GATES if gates.get(name) is True]
        bl = r.get("blockers") or []
        out.append(
            f"<tr><td><strong>{escape(str(r.get('ticker') or ''))}</strong></td>"
            f"<td>{escape(str(r.get('sector') or ''))}</td>"
            f"<td>{escape(str(r.get('ai_score') or ''))}</td>"
            f"<td>{escape(score_str)}</td>"
            f"<td>{escape(acc_str)}</td>"
            f"<td>{escape(', '.join(bl) if bl else '—')}</td>"
            f"<td>{escape(', '.join(notable) if notable else '—')}</td></tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def _render_html(report: dict) -> str:
    overall = report["overall"]
    color = _badge_color(overall)
    h = report["highlights"]
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Pine Go/No-Go Diagnostic</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1180px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} .meta{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
.section h2{{margin:0 0 10px;font-size:18px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.kv{{font-size:13px;color:#444}}
.summary{{font-size:14px;background:#f6f8fa;padding:10px 12px;border-radius:6px;margin-top:8px}}
.caveat{{background:#fff6e0;border:1px solid #f0d49a;color:#8a5a00;padding:10px 12px;
        border-radius:6px;margin:14px 0;font-size:13px}}
.back{{font-size:13px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Pine Go/No-Go Diagnostic</h1>
<p class="meta">Generated {escape(report.get("generated_at_chicago",""))} &middot;
   Overall: <span class="badge">{escape(overall)}</span></p>
<div class="summary"><strong>Summary:</strong> {escape(report.get("summary",""))}</div>
<div class="caveat"><strong>Caveat:</strong> {escape(report.get("caveat",""))}</div>
""")

    c = report["counts"]
    parts.append(
        f"<div class='section'><h2>Coverage</h2>"
        f"<p class='kv'>candidates={c['candidates']}, evaluated={c['evaluated']}, "
        f"ohlcv_missing={c['ohlcv_missing']}, insufficient_bars={c['insufficient_bars']}, "
        f"blocked={c['blocked']}, "
        f"go_normalized&ge;0.7={c['go_normalized_ge_07']}, "
        f"go_normalized&lt;0.4={c['go_normalized_lt_04']}</p></div>"
    )

    parts.append("<div class='section'><h2>Disagreement queue · Pine supports internal bullish view</h2>")
    parts.append("<p class='kv'>Pine gate stack confirms the internal bullish thesis "
                 "even though external sources flagged caution. Review whether internal "
                 "score reflects this strength.</p>")
    parts.append(_render_disagreement_table(h["disagreement_supports_internal"]))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Disagreement queue · Pine agrees with external caution</h2>")
    parts.append("<p class='kv'>Pine gate stack is weak or blocked, agreeing with external bearish "
                 "signals. Consider downgrading to watchlist-only or removing from main top picks "
                 "(manual decision; scores not auto-mutated).</p>")
    parts.append(_render_disagreement_table(h["disagreement_supports_external_caution"]))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Disagreement queue · Mixed</h2>")
    parts.append(_render_disagreement_table(h["disagreement_mixed"]))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Disagreement queue · Insufficient data</h2>")
    parts.append(_render_disagreement_table(h["disagreement_insufficient_data"]))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Main top — cleanest Pine go signals</h2>")
    parts.append(_render_main_table(h["cleanest_go_main"]))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Main top — blocked names</h2>")
    parts.append(_render_main_table(h["blocked_main"]))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Main top — weakest Pine scores</h2>")
    parts.append(_render_main_table(h["weakest_main"]))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Thresholds</h2><pre class='kv' style=\"background:#f8f8f8;padding:8px;border-radius:4px;\">")
    parts.append(escape(json.dumps(report["thresholds"], indent=2)))
    parts.append("</pre></div>")

    parts.append("</body></html>")
    return "\n".join(parts)


# ----------------- tasks.json wiring -----------------


def _stamp_task(report: dict) -> None:
    if not TASKS_FILE.exists():
        return
    try:
        from _tasks_meta import update_task  # type: ignore
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            from _tasks_meta import update_task  # type: ignore
        except ImportError:
            return
    overall_lc = (report.get("overall") or "OK").lower()
    if overall_lc == "fail":
        status_label = "FAIL"
    elif overall_lc == "warn":
        status_label = "warn"
    else:
        status_label = "OK"
    update_task(TASKS_FILE, TASK_ID,
                status=status_label,
                summary=report.get("summary", ""),
                report_url=REPORT_URL)


def _ensure_task_row() -> None:
    if not TASKS_FILE.exists():
        return
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    if any(isinstance(t, dict) and t.get("id") == TASK_ID for t in tasks):
        return
    tasks.append({
        "id": TASK_ID,
        "name": "Pine Go/No-Go Diagnostic",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": "—",
        "next_run": "—",
        "status": "Not Run",
        "summary": "—",
        "report_url": REPORT_URL,
    })
    TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ----------------- entry point -----------------


def main() -> int:
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    ext_review = _load_json(EXT_REVIEW_FILE)
    disagreement = _load_json(DISAGREEMENT_FILE)
    report = build_report(rankings, watchlist, ext_review, disagreement)
    JSON_OUTPUT.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    _ensure_task_row()
    _stamp_task(report)
    print(f"[pine_go_no_go_diagnostic] overall={report['overall']} -> {JSON_OUTPUT}")
    print(f"[pine_go_no_go_diagnostic] summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

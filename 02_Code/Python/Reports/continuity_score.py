"""Continuity Score — diagnostic-only estimate of whether a setup is likely
to *keep working* rather than fade after it ranks well.

Motivation: the production ACT/GO/ACC adjustment improved the board, but some
high-ranked names stop performing shortly after they appear. This report
scores each ticker 0-100 on "continuity of performance" using only data that
already ships in the repo (plus an optional best-effort SPY/QQQ pull). It is
NOT a production signal — it never touches rankings.json scores or ranks.

Inputs (all read-only, all optional except rankings):
  - data/rankings.json                          (production board + closes[])
  - data/watchlist_rankings.json                (watchlist board + closes[])
  - data/raw/ohlcv_daily/<TICKER>_daily.csv     (full daily OHLCV if present)
  - data/reports/pine_go_no_go_diagnostic.json  (Pine gate context, optional)
  - data/reports/accumulation_signal_meter.json (accumulation context, optional)
  - SPY / QQQ daily closes via yfinance          (optional, degrades gracefully)
  - data/reports/continuity_snapshots.jsonl     (this script's own history)

Outputs:
  - data/reports/continuity_score.json
  - reports/continuity-score.html
  - data/reports/continuity_snapshots.jsonl     (rolling, capped at 7 snapshots)
  - data/tasks.json row id=continuity-score stamped on each run.

Formula (transparent, conservative). Each component yields a 0..1 sub-score
and carries a weight. The final continuity score is the weighted average of
the components that could actually be computed, rescaled to 0..100. Components
that cannot be computed from available data are *omitted from the average*
(they do not contribute a fabricated 0.5) and instead reduce a `confidence`
fraction = present_weight / total_weight. This keeps missing data honest:
a ticker with only the closes-array signals scores on those signals but is
flagged low-confidence.

Components and weights:
  a. rel_strength_5d   (0.18) — 5-day return vs SPY/QQQ (benchmark optional;
                                 falls back to raw 5d return percentile band).
  b. rel_strength_10d  (0.12) — 10-day return persistence vs benchmark / raw.
  c. close_location    (0.14) — today's close location within the day's range
                                 (needs OHLCV high/low; else uses close vs the
                                 closes-array recent min/max as a proxy).
  d. volume_support    (0.14) — sustained volume vs its own average, NOT a
                                 single-day spike (needs OHLCV volume; else uses
                                 volume_millions vs a soft reference).
  e. ma_distance       (0.12) — distance above 20DMA/50DMA — rewards being above
                                 trend but penalises overextension.
  f. reversal_health   (0.16) — few red / reversal days over the recent window
                                 (computed from the closes array, always present).
  g. sector_strength   (0.06) — ticker 5d return vs its sector cohort median
                                 (computed across the board, best-effort).
  h. earnings_risk     (0.08) — penalise imminent earnings (days_to_earnings).

Label / badge (concise, mirrors the GO/ACC dg-pos/mid/neg convention):
  HIGH  score >= 65   (dg-pos / green)
  MID   40 <= score < 65 (dg-mid / amber)
  LOW   score < 40    (dg-neg / red)
  —     no score (insufficient data) (dg-none / muted)

Recommendation surfaced in the report: diagnostic only; do not promote into
production scoring until the 7-snapshot forward-return tracking shows the
score actually correlates with subsequent performance.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from statistics import median

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"
OHLCV_DIR = DATA_DIR / "raw" / "ohlcv_daily"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"
PINE_FILE = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
ACCUM_FILE = DATA_REPORTS_DIR / "accumulation_signal_meter.json"
SNAPSHOTS_FILE = DATA_REPORTS_DIR / "continuity_snapshots.jsonl"
JSON_OUTPUT = DATA_REPORTS_DIR / "continuity_score.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "continuity-score.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/continuity-score.html"

# Retention: keep this many of the most recent trading-day snapshots.
MAX_SNAPSHOTS = 7

# Component weights. Kept here so tests and the report can introspect them.
COMPONENT_WEIGHTS = {
    "rel_strength_5d": 0.18,
    "rel_strength_10d": 0.12,
    "close_location": 0.14,
    "volume_support": 0.14,
    "ma_distance": 0.12,
    "reversal_health": 0.16,
    "sector_strength": 0.06,
    "earnings_risk": 0.08,
}

# Label thresholds (0..100).
HIGH_MIN = 65.0
LOW_MAX = 40.0

# Benchmarks pulled best-effort for relative strength.
BENCHMARK_TICKERS = ("SPY", "QQQ")


# ----------------- generic helpers -----------------


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


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _format_mmddyyyy(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except (TypeError, ValueError):
        return d or "—"


# ----------------- OHLCV loader (optional) -----------------


def load_ohlcv(ticker: str, ohlcv_dir: Path = OHLCV_DIR, max_rows: int = 80) -> list[dict] | None:
    """Load the most recent `max_rows` daily bars for a ticker from the
    workflow-generated CSV, if present. Returns a list of dicts with keys
    date/open/high/low/close/volume (floats), oldest-first, or None when the
    file is missing/unreadable. Tolerant of column-name casing."""
    path = ohlcv_dir / f"{ticker}_daily.csv"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return None
            # Map flexible header names to canonical keys.
            colmap: dict[str, str] = {}
            for name in reader.fieldnames:
                low = (name or "").strip().lower()
                if low in ("date", "datetime"):
                    colmap[name] = "date"
                elif low == "open":
                    colmap[name] = "open"
                elif low == "high":
                    colmap[name] = "high"
                elif low == "low":
                    colmap[name] = "low"
                elif low in ("close", "adj close", "adjclose"):
                    colmap.setdefault("close_src", name)  # remember source
                    colmap[name] = "close"
                elif low == "volume":
                    colmap[name] = "volume"
            rows: list[dict] = []
            for raw in reader:
                bar = {"date": None, "open": None, "high": None,
                       "low": None, "close": None, "volume": None}
                for src, canon in colmap.items():
                    if canon == "close_src":
                        continue
                    val = raw.get(src)
                    if canon == "date":
                        bar["date"] = (val or "").strip()[:10] or None
                    else:
                        bar[canon] = _safe_float(val)
                if bar["close"] is not None:
                    rows.append(bar)
    except OSError:
        return None
    if not rows:
        return None
    return rows[-max_rows:]


# ----------------- component computations -----------------
#
# Each returns (sub_score_0_1 | None, detail_str). Returning None means the
# component could not be computed from available data and must be omitted from
# the weighted average (it lowers confidence instead of contributing a guess).


def _pct_change(series: list[float], lookback: int, min_lookback: int | None = None) -> float | None:
    """Return fractional change over `lookback` bars: (last/ref - 1).

    When `min_lookback` is given and there aren't enough bars for the full
    `lookback`, fall back to the longest available lookback that is still
    >= min_lookback (used so a 10-bar closes array can still yield a ~9-day
    return for the 10d component instead of going missing)."""
    clean = [c for c in series if c is not None]
    if not clean:
        return None
    eff = lookback
    if len(clean) <= lookback:
        if min_lookback is None or len(clean) - 1 < min_lookback:
            return None
        eff = len(clean) - 1
    ref = clean[-1 - eff]
    last = clean[-1]
    if ref == 0:
        return None
    return (last - ref) / ref


def comp_rel_strength(closes: list[float], lookback: int,
                      bench_return: float | None,
                      min_lookback: int | None = None) -> tuple[float | None, str]:
    """Relative strength over `lookback` days. If a benchmark return is
    supplied (SPY/QQQ best of), score the ticker's excess return through a
    soft logistic band. Otherwise fall back to the raw return mapped through
    a gentler band (raw momentum is a weaker continuity signal, so the band
    is wider and a flat return lands near 0.5)."""
    r = _pct_change(closes, lookback, min_lookback=min_lookback)
    if r is None:
        return None, "insufficient closes"
    if bench_return is not None:
        excess = r - bench_return
        # +3% excess over the window -> ~0.81; -3% -> ~0.19.
        score = _clamp01(0.5 + excess / 0.075)
        return score, f"excess {excess*100:+.1f}% vs bench"
    # No benchmark: raw return band. +4% -> ~0.77, -4% -> ~0.23.
    score = _clamp01(0.5 + r / 0.115)
    return score, f"raw {r*100:+.1f}% (no bench)"


def comp_close_location(ohlcv: list[dict] | None,
                        closes: list[float]) -> tuple[float | None, str]:
    """Where today's close sits in the day's range. Strong setups close near
    the high. With OHLCV we use the true daily high/low; otherwise we proxy
    with the close vs the recent closes-array min/max."""
    if ohlcv:
        bar = ohlcv[-1]
        hi, lo, cl = bar.get("high"), bar.get("low"), bar.get("close")
        if hi is not None and lo is not None and cl is not None and hi > lo:
            loc = (cl - lo) / (hi - lo)
            return _clamp01(loc), f"close at {loc*100:.0f}% of daily range"
    # Proxy: close vs recent closes window min/max (last up-to-10 bars).
    window = [c for c in closes[-10:] if c is not None]
    if len(window) >= 3:
        lo, hi = min(window), max(window)
        if hi > lo:
            loc = (window[-1] - lo) / (hi - lo)
            # Proxy is weaker than true range; shrink toward 0.5 a touch.
            loc = 0.5 + (loc - 0.5) * 0.85
            return _clamp01(loc), f"close at {loc*100:.0f}% of 10d close range (proxy)"
    return None, "no range data"


def comp_volume_support(ohlcv: list[dict] | None,
                        volume_millions: float | None) -> tuple[float | None, str]:
    """Sustained volume, not a one-day spike. With OHLCV we compare the recent
    3-day average volume to the prior 20-day average and reward 1.0x-1.6x
    (healthy participation) while NOT rewarding a single blow-off bar. Without
    OHLCV we have only a single volume_millions figure, which cannot tell
    sustained from spike — so we return a low-confidence neutral-ish band only
    when it's clearly nonzero, else None."""
    if ohlcv and len(ohlcv) >= 6:
        vols = [b.get("volume") for b in ohlcv if b.get("volume") is not None]
        if len(vols) >= 6:
            recent = vols[-3:]
            base_window = vols[-23:-3] if len(vols) >= 23 else vols[:-3]
            if base_window:
                recent_avg = sum(recent) / len(recent)
                base_avg = sum(base_window) / len(base_window)
                if base_avg > 0:
                    ratio = recent_avg / base_avg
                    # Spike guard: if the single max bar dominates the recent
                    # average (one-day blow-off), discount the support read.
                    mx = max(recent)
                    spiky = recent_avg > 0 and mx > 2.2 * recent_avg
                    if ratio <= 1.0:
                        score = _clamp01(0.25 + 0.25 * ratio)  # below avg -> 0.25..0.50
                    elif ratio <= 1.6:
                        score = _clamp01(0.5 + (ratio - 1.0) * 0.66)  # 1.0..1.6 -> 0.5..~0.9
                    else:
                        score = _clamp01(0.9 - (ratio - 1.6) * 0.25)  # too hot -> fade
                    if spiky:
                        score = min(score, 0.55)
                    note = f"3d/20d vol {ratio:.2f}x" + (" (spike-capped)" if spiky else "")
                    return score, note
    # No OHLCV — single snapshot volume can't distinguish sustained vs spike.
    return None, "no multi-day volume"


def comp_ma_distance(ohlcv: list[dict] | None,
                     closes: list[float]) -> tuple[float | None, str]:
    """Distance above the 20/50DMA. Rewards being above trend but penalises
    overextension (parabolic names fade). Needs enough history; OHLCV (if
    present) gives the real MAs, otherwise the 10-bar closes array can only
    approximate a short MA, which we treat as low-resolution."""
    series = None
    if ohlcv and len(ohlcv) >= 20:
        series = [b.get("close") for b in ohlcv if b.get("close") is not None]
    elif closes and len([c for c in closes if c is not None]) >= 5:
        series = [c for c in closes if c is not None]
    if not series:
        return None, "insufficient history for MA"
    last = series[-1]
    if last is None or last == 0:
        return None, "no last close"
    n20 = min(20, len(series))
    ma20 = sum(series[-n20:]) / n20
    if ma20 <= 0:
        return None, "bad MA"
    dist = (last - ma20) / ma20  # fractional distance above the MA
    # Sweet spot: a few % above the MA. Below MA or far overextended fades.
    if dist < 0:
        score = _clamp01(0.5 + dist / 0.10)        # below MA -> down toward 0
    elif dist <= 0.08:
        score = _clamp01(0.6 + dist / 0.08 * 0.3)  # 0..8% above -> 0.6..0.9
    else:
        score = _clamp01(0.9 - (dist - 0.08) / 0.15)  # >8% above -> overextended fade
    return score, f"{dist*100:+.1f}% vs {n20}DMA"


def comp_reversal_health(closes: list[float]) -> tuple[float | None, str]:
    """Few red / reversal days over the recent window. Computed purely from
    the closes array (always present), so this anchors the score even when
    nothing else is available. Counts day-over-day up vs down moves over the
    last up-to-9 deltas and rewards a higher up-day fraction, with a small
    bonus for finishing on an up day (no immediate reversal)."""
    series = [c for c in closes if c is not None]
    if len(series) < 3:
        return None, "insufficient closes"
    deltas = [series[i] - series[i - 1] for i in range(1, len(series))]
    deltas = deltas[-9:]
    if not deltas:
        return None, "no deltas"
    ups = sum(1 for d in deltas if d > 0)
    frac_up = ups / len(deltas)
    # Map 0.4..0.8 up-fraction onto most of the 0..1 range; flat day counts as
    # not-up. Add a small last-bar bonus/penalty for immediate direction.
    score = _clamp01((frac_up - 0.4) / 0.4)
    score = 0.15 + 0.7 * score  # keep within 0.15..0.85 base
    if deltas[-1] > 0:
        score = _clamp01(score + 0.1)
    elif deltas[-1] < 0:
        score = _clamp01(score - 0.1)
    return score, f"{ups}/{len(deltas)} up days"


def comp_earnings_risk(days_to_earnings) -> tuple[float | None, str]:
    """Penalise imminent earnings — a binary catalyst can abruptly end a
    continuation regardless of setup quality. Far-out earnings are neutral-good."""
    d = _safe_float(days_to_earnings)
    if d is None:
        return None, "no earnings date"
    if d < 0:
        # Just reported (within our knowledge) — uncertainty, mild discount.
        return 0.5, "earnings recently passed"
    if d <= 3:
        return 0.1, f"earnings in {int(d)}d"
    if d <= 7:
        return 0.35, f"earnings in {int(d)}d"
    if d <= 14:
        return 0.65, f"earnings in {int(d)}d"
    return 0.85, f"earnings in {int(d)}d"


def comp_sector_strength(ticker_5d: float | None, sector: str | None,
                         sector_medians: dict) -> tuple[float | None, str]:
    """Ticker 5d return vs its sector cohort median. Best-effort: needs the
    ticker's own 5d return and a sector with >=2 members on the board."""
    if ticker_5d is None or not sector:
        return None, "no sector cohort"
    med = sector_medians.get(sector)
    if med is None:
        return None, "sector cohort too small"
    excess = ticker_5d - med
    score = _clamp01(0.5 + excess / 0.08)
    return score, f"{excess*100:+.1f}% vs sector median"


# ----------------- per-ticker scoring -----------------


def _best_benchmark_return(bench: dict, lookback_label: str) -> float | None:
    """Best (max) of SPY/QQQ return for the given lookback label, so a ticker
    must beat the stronger index — conservative. Returns None if unavailable."""
    vals = []
    for t in BENCHMARK_TICKERS:
        entry = (bench.get("tickers") or {}).get(t) if bench else None
        if isinstance(entry, dict):
            v = _safe_float(entry.get(f"return_{lookback_label}"))
            if v is not None:
                vals.append(v)
    if not vals:
        return None
    return max(vals)


def score_ticker(row: dict, *, ohlcv: list[dict] | None, bench: dict,
                 sector_medians: dict) -> dict:
    """Compute the continuity score for a single rankings row. Pure given its
    inputs. Returns a dict with score(0-100|None), label, confidence(0-1),
    and the per-component breakdown."""
    closes_raw = row.get("closes")
    closes = [c for c in (closes_raw or []) if isinstance(c, (int, float))] if isinstance(closes_raw, list) else []

    # Prefer the longer OHLCV close history (when present) for the return
    # components, so 5d/10d are true lookbacks; otherwise use the 10-bar
    # closes sparkline that always ships in rankings.json.
    rs_series = closes
    if ohlcv:
        oc = [b.get("close") for b in ohlcv if b.get("close") is not None]
        if len(oc) >= len(closes):
            rs_series = oc

    bench5 = _best_benchmark_return(bench, "5d")
    bench10 = _best_benchmark_return(bench, "10d")

    ticker_5d = _pct_change(rs_series, 5)

    components: dict[str, dict] = {}

    def add(name, result):
        sub, detail = result
        components[name] = {
            "score": round(sub, 4) if sub is not None else None,
            "weight": COMPONENT_WEIGHTS[name],
            "detail": detail,
            "present": sub is not None,
        }

    add("rel_strength_5d", comp_rel_strength(rs_series, 5, bench5))
    add("rel_strength_10d", comp_rel_strength(rs_series, 10, bench10, min_lookback=8))
    add("close_location", comp_close_location(ohlcv, closes))
    add("volume_support", comp_volume_support(ohlcv, _safe_float(row.get("volume_millions"))))
    add("ma_distance", comp_ma_distance(ohlcv, closes))
    add("reversal_health", comp_reversal_health(closes))
    add("sector_strength", comp_sector_strength(ticker_5d, row.get("sector"), sector_medians))
    add("earnings_risk", comp_earnings_risk(row.get("days_to_earnings")))

    present = {k: v for k, v in components.items() if v["present"]}
    total_weight = sum(COMPONENT_WEIGHTS.values())
    present_weight = sum(v["weight"] for v in present.values())
    confidence = round(present_weight / total_weight, 4) if total_weight else 0.0

    if present_weight > 0:
        weighted = sum(v["score"] * v["weight"] for v in present.values())
        score100 = round((weighted / present_weight) * 100.0, 1)
    else:
        score100 = None

    label = derive_label(score100, confidence)

    return {
        "ticker": row.get("ticker"),
        "company": row.get("company"),
        "sector": row.get("sector"),
        "rank": row.get("rank"),
        "ai_score": row.get("ai_score"),
        "go_label": row.get("go_label"),
        "acc_label": row.get("acc_label"),
        "close": closes[-1] if closes else None,
        "days_to_earnings": row.get("days_to_earnings"),
        "score": score100,
        "label": label,
        "confidence": confidence,
        "components": components,
        "has_ohlcv": ohlcv is not None,
    }


def derive_label(score100: float | None, confidence: float = 1.0) -> str:
    """HIGH / MID / LOW / — from a 0-100 score. A score computed from too
    little data (very low confidence) is reported as '—' rather than a band,
    so the badge never overstates a thin signal."""
    if score100 is None:
        return "—"
    if confidence < 0.30:
        return "—"
    if score100 >= HIGH_MIN:
        return "HIGH"
    if score100 < LOW_MAX:
        return "LOW"
    return "MID"


# ----------------- board assembly -----------------


def _compute_sector_medians(rows: list[dict]) -> dict:
    """Median 5d return per sector across the board, for sectors with >=2
    members that have a computable 5d return."""
    buckets: dict[str, list[float]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sector = r.get("sector")
        if not sector:
            continue
        closes_raw = r.get("closes")
        closes = [c for c in (closes_raw or []) if isinstance(c, (int, float))] if isinstance(closes_raw, list) else []
        r5 = _pct_change(closes, 5)
        if r5 is not None:
            buckets.setdefault(sector, []).append(r5)
    return {s: median(v) for s, v in buckets.items() if len(v) >= 2}


def score_board(rankings: dict | None, bench: dict, *, ohlcv_dir: Path = OHLCV_DIR) -> list[dict]:
    """Score every row in a rankings payload. Returns a list of per-ticker
    score dicts (empty if no rows)."""
    if not isinstance(rankings, dict):
        return []
    rows = rankings.get("rows")
    if not isinstance(rows, list) or not rows:
        return []
    sector_medians = _compute_sector_medians(rows)
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("ticker"):
            continue
        ohlcv = load_ohlcv(r["ticker"], ohlcv_dir=ohlcv_dir)
        out.append(score_ticker(r, ohlcv=ohlcv, bench=bench, sector_medians=sector_medians))
    return out


# ----------------- snapshots (rolling, capped at 7) -----------------


def _load_snapshots(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("date"):
                    out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("date") or "")
    return out


def _write_snapshots(path: Path, snapshots: list[dict]) -> None:
    lines = [json.dumps(s, default=str) for s in snapshots]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def build_today_snapshot(open_date: str, scored_main: list[dict],
                         scored_watch: list[dict]) -> dict | None:
    """Compact per-ticker snapshot keyed on open_date. Main board takes
    precedence over watchlist when a ticker appears on both."""
    if not open_date:
        return None
    tickers: dict[str, dict] = {}
    for src, board in (("watchlist", scored_watch), ("main", scored_main)):
        for s in board:
            t = s.get("ticker")
            if not t:
                continue
            tickers[t] = {
                "ticker": t,
                "score": s.get("score"),
                "label": s.get("label"),
                "confidence": s.get("confidence"),
                "rank": s.get("rank"),
                "close": s.get("close"),
                "board": src,
            }
    if not tickers:
        return None
    return {"date": open_date, "tickers": tickers}


def upsert_snapshot(snapshots: list[dict], today: dict, *, max_keep: int = MAX_SNAPSHOTS) -> list[dict]:
    if not isinstance(today, dict) or not today.get("date"):
        return snapshots
    out = [s for s in snapshots if s.get("date") != today["date"]]
    out.append(today)
    out.sort(key=lambda r: r.get("date") or "")
    if max_keep and len(out) > max_keep:
        out = out[-max_keep:]
    return out


def _prior_snapshot_close(snapshots: list[dict], ticker: str, before_date: str) -> tuple[str | None, float | None]:
    for s in reversed(snapshots):
        d = s.get("date")
        if not isinstance(d, str) or d >= before_date:
            continue
        rec = (s.get("tickers") or {}).get(ticker)
        if isinstance(rec, dict):
            c = rec.get("close")
            if isinstance(c, (int, float)):
                return d, float(c)
    return None, None


def build_tracking_table(snapshots: list[dict]) -> dict:
    """Wide tracking table over the retained snapshots. Dynamic ticker union:
    a ticker that appears in any retained snapshot keeps a row. For the latest
    snapshot, attach a forward return computed against the *earliest* prior
    snapshot close (so it accrues once enough days elapse)."""
    dates = sorted(s.get("date") for s in snapshots if s.get("date"))
    latest_date = dates[-1] if dates else None
    latest = next((s for s in snapshots if s.get("date") == latest_date), None) if latest_date else None
    latest_tickers = (latest or {}).get("tickers") or {}

    union: set[str] = set()
    for s in snapshots:
        union.update((s.get("tickers") or {}).keys())

    def _sort_key(t: str) -> tuple:
        rec = latest_tickers.get(t)
        sc = rec.get("score") if isinstance(rec, dict) else None
        try:
            return (0, -float(sc), t)
        except (TypeError, ValueError):
            return (1, 0.0, t)

    rows: list[dict] = []
    for t in sorted(union, key=_sort_key):
        cells = []
        for d in dates:
            snap = next((s for s in snapshots if s.get("date") == d), None)
            rec = ((snap or {}).get("tickers") or {}).get(t) if snap else None
            cell = {"date": d}
            if isinstance(rec, dict):
                cell["score"] = rec.get("score")
                cell["label"] = rec.get("label")
                cell["close"] = rec.get("close")
            cells.append(cell)
        # Forward return for the latest cell vs the earliest snapshot we have a
        # close for this ticker (no lookahead — uses only past snapshots).
        fwd = None
        fwd_from = None
        latest_rec = latest_tickers.get(t)
        if isinstance(latest_rec, dict) and isinstance(latest_rec.get("close"), (int, float)):
            for s in snapshots:
                d = s.get("date")
                if d == latest_date:
                    continue
                rec0 = (s.get("tickers") or {}).get(t)
                if isinstance(rec0, dict) and isinstance(rec0.get("close"), (int, float)) and rec0["close"]:
                    fwd = round((latest_rec["close"] - rec0["close"]) / rec0["close"] * 100.0, 2)
                    fwd_from = d
                    break
        rows.append({
            "ticker": t,
            "cells": cells,
            "fwd_return_pct": fwd,
            "fwd_from": fwd_from,
            "latest_label": (latest_rec or {}).get("label") if isinstance(latest_rec, dict) else None,
        })

    return {
        "dates": dates,
        "latest_date": latest_date,
        "rows": rows,
        "ticker_count": len(rows),
    }


# ----------------- summary -----------------


def summarize(scored_main: list[dict], scored_watch: list[dict]) -> dict:
    """Counts by label, top continuity names, and fade-risk callouts among
    high production ranks / strong ACT-GO-ACC names."""
    def counts(board):
        c = {"HIGH": 0, "MID": 0, "LOW": 0, "—": 0}
        for s in board:
            c[s.get("label", "—")] = c.get(s.get("label", "—"), 0) + 1
        return c

    main_scored = [s for s in scored_main if s.get("score") is not None]

    top = sorted(main_scored, key=lambda s: s["score"], reverse=True)[:10]

    # Fade risk among high production ranks (top 25 by production rank) with
    # LOW continuity.
    fade_risk = [
        s for s in main_scored
        if isinstance(s.get("rank"), int) and s["rank"] <= 25 and s.get("label") == "LOW"
    ]
    fade_risk.sort(key=lambda s: (s.get("rank") or 9999))

    # Strong production signal (GO and/or ACC HIGH) but weak continuity.
    weak_strong = [
        s for s in main_scored
        if (s.get("go_label") == "GO" or s.get("acc_label") == "HIGH")
        and s.get("label") in ("LOW", "MID")
    ]
    weak_strong.sort(key=lambda s: (s.get("score") if s.get("score") is not None else 999))

    def slim(s):
        return {
            "ticker": s.get("ticker"),
            "company": s.get("company"),
            "rank": s.get("rank"),
            "score": s.get("score"),
            "label": s.get("label"),
            "confidence": s.get("confidence"),
            "go_label": s.get("go_label"),
            "acc_label": s.get("acc_label"),
        }

    return {
        "main_counts": counts(scored_main),
        "watchlist_counts": counts(scored_watch),
        "main_scored_count": len(main_scored),
        "ohlcv_available": any(s.get("has_ohlcv") for s in scored_main),
        "top_continuity": [slim(s) for s in top],
        "fade_risk_high_rank": [slim(s) for s in fade_risk[:15]],
        "weak_continuity_strong_signal": [slim(s) for s in weak_strong[:15]],
    }


# ----------------- rendering -----------------


def _label_cls(label: str) -> str:
    return {
        "HIGH": "dg-pos",
        "MID": "dg-mid",
        "LOW": "dg-neg",
    }.get(label, "dg-none")


def _fmt_score(s) -> str:
    return f"{s:.0f}" if isinstance(s, (int, float)) else "—"


def _render_html(report: dict) -> str:
    summary = report["summary"]
    table = report["tracking_table"]
    mc = summary["main_counts"]
    wc = summary["watchlist_counts"]
    dates = table.get("dates") or []
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Continuity Score (Diagnostic)</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1300px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:22px 0 8px;font-size:17px}}
.meta{{color:#666;font-size:13px;margin-bottom:12px}}
.note{{background:#fff8e1;border:1px solid #ffe0a3;padding:10px 12px;border-radius:6px;font-size:13px;margin:10px 0 16px}}
.summary{{font-size:14px;background:#f6f8fa;padding:10px 12px;border-radius:6px;margin:8px 0 14px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;border:1px solid #ccc}}
.HIGH{{color:#1b7a36;border-color:#9cd9ad;background:#e9f8ee}}
.MID{{color:#9a6b00;border-color:#f0d28a;background:#fdf6e3}}
.LOW{{color:#b3261e;border-color:#f0a9a4;background:#fdeceb}}
.NA{{color:#888;border-color:#ddd;background:#fafafa}}
.scroll{{overflow-x:auto;border:1px solid #e3e3e3;border-radius:8px}}
table{{border-collapse:collapse;font-size:12px;min-width:100%}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap;vertical-align:top}}
th{{background:#fafafa;position:sticky;top:0;z-index:2}}
th.ticker,td.ticker{{position:sticky;left:0;background:#fff;border-right:1px solid #ddd;font-weight:600}}
.muted{{color:#999}} .pos{{color:#2e7d32}} .neg{{color:#c0392b}}
.back{{font-size:13px}}
ul.names{{margin:6px 0 0;padding-left:18px;font-size:13px}}
.confbar{{display:inline-block;height:8px;background:#cfe8d6;border-radius:3px;vertical-align:middle}}
</style></head><body>
<p class="back"><a href="../diagnostics.html">&larr; Back to diagnostics</a></p>
<h1>Continuity Score <span class="badge NA">DIAGNOSTIC</span></h1>
<p class="meta">Generated {escape(report.get("generated_at_chicago",""))}</p>
<div class="note"><strong>Diagnostic only.</strong> Estimates the likelihood a setup
<em>keeps working</em> rather than fades. It does NOT affect the production AI score or rank.
Do not promote into production scoring until the 7-snapshot forward-return tracking below
shows the score correlates with subsequent performance.</div>
""")

    parts.append('<div class="summary">')
    parts.append(
        f"<strong>Main board:</strong> "
        f"<span class='badge HIGH'>HIGH {mc.get('HIGH',0)}</span> "
        f"<span class='badge MID'>MID {mc.get('MID',0)}</span> "
        f"<span class='badge LOW'>LOW {mc.get('LOW',0)}</span> "
        f"<span class='badge NA'>— {mc.get('—',0)}</span> &nbsp;&middot;&nbsp; "
        f"<strong>Watchlist:</strong> "
        f"HIGH {wc.get('HIGH',0)} / MID {wc.get('MID',0)} / LOW {wc.get('LOW',0)} / — {wc.get('—',0)}"
    )
    parts.append(
        f"<br><span class='muted'>OHLCV detail "
        f"{'available' if summary.get('ohlcv_available') else 'NOT available — scores use the 10-bar closes array only, lower confidence'}.</span>"
    )
    parts.append("</div>")

    def names_table(title, items, extra_cols=()):
        parts.append(f"<h2>{escape(title)}</h2>")
        if not items:
            parts.append("<p class='muted'>None.</p>")
            return
        parts.append('<div class="scroll"><table><thead><tr>'
                     '<th>Ticker</th><th>Company</th><th>Prod Rank</th>'
                     '<th>Continuity</th><th>Score</th><th>Conf</th>')
        for c in extra_cols:
            parts.append(f"<th>{escape(c)}</th>")
        parts.append("</tr></thead><tbody>")
        for s in items:
            lbl = s.get("label", "—")
            cls = lbl if lbl in ("HIGH", "MID", "LOW") else "NA"
            conf = s.get("confidence")
            conf_str = f"{conf*100:.0f}%" if isinstance(conf, (int, float)) else "—"
            parts.append(
                f"<tr><td class='ticker'>{escape(str(s.get('ticker') or '—'))}</td>"
                f"<td>{escape(str(s.get('company') or '—'))}</td>"
                f"<td>{escape(str(s.get('rank') if s.get('rank') is not None else '—'))}</td>"
                f"<td><span class='badge {cls}'>{escape(lbl)}</span></td>"
                f"<td>{_fmt_score(s.get('score'))}</td>"
                f"<td>{conf_str}</td>"
            )
            for c in extra_cols:
                key = "go_label" if c == "GO" else "acc_label" if c == "ACC" else None
                parts.append(f"<td>{escape(str(s.get(key) or '—'))}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table></div>")

    names_table("Top continuity names (main board)", summary.get("top_continuity") or [])
    names_table("Fade-risk: LOW continuity inside production top 25",
                summary.get("fade_risk_high_rank") or [])
    names_table("Strong production signal (GO / ACC HIGH) but weak continuity",
                summary.get("weak_continuity_strong_signal") or [], extra_cols=("GO", "ACC"))

    # 7-snapshot tracking table.
    parts.append("<h2>7-snapshot tracking</h2>")
    parts.append(
        f"<p class='meta'>{len(dates)} snapshot(s) retained &middot; "
        f"{table.get('ticker_count',0)} ticker(s) in the dynamic union. "
        f"Forward return is the latest close vs the earliest retained snapshot close for that ticker.</p>"
    )
    rows = table.get("rows") or []
    if not rows:
        parts.append("<p class='muted'>No snapshots yet — the first run seeds the history.</p>")
    else:
        parts.append('<div class="scroll"><table><thead><tr><th class="ticker">Ticker</th>')
        for d in dates:
            parts.append(f"<th>{escape(_format_mmddyyyy(d))}</th>")
        parts.append("<th>Fwd Δ%</th></tr></thead><tbody>")
        for r in rows[:120]:
            parts.append(f"<tr><td class='ticker'>{escape(str(r.get('ticker')))}</td>")
            for cell in r["cells"]:
                sc = cell.get("score")
                lbl = cell.get("label") or "—"
                cls = lbl if lbl in ("HIGH", "MID", "LOW") else "NA"
                if sc is None:
                    parts.append("<td class='muted'>—</td>")
                else:
                    parts.append(f"<td><span class='badge {cls}'>{_fmt_score(sc)}</span></td>")
            fwd = r.get("fwd_return_pct")
            if isinstance(fwd, (int, float)):
                fcls = "pos" if fwd >= 0 else "neg"
                ffrom = r.get("fwd_from") or ""
                parts.append(f"<td class='{fcls}'>{fwd:+.2f}% <span class='muted'>(vs {escape(_format_mmddyyyy(ffrom))})</span></td>")
            else:
                parts.append("<td class='muted'>pending</td>")
            parts.append("</tr>")
        parts.append("</tbody></table></div>")
        if len(rows) > 120:
            parts.append(f"<p class='muted'>Showing top 120 of {len(rows)} tickers by latest continuity score.</p>")

    parts.append(
        "<p class='meta' style='margin-top:18px'>Formula: weighted average of up to 8 components "
        "(5d &amp; 10d relative strength, close location, sustained volume, MA distance, reversal "
        "health, sector-relative strength, earnings risk). Components that cannot be computed from "
        "available data are omitted and lower the confidence figure rather than being guessed.</p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


# ----------------- task stamp -----------------


def _stamp_task(report: dict) -> None:
    if not TASKS_FILE.exists():
        return
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    row_id = "continuity-score"
    existing = next((r for r in tasks if isinstance(r, dict) and r.get("id") == row_id), None)
    payload = {
        "id": row_id,
        "name": "Continuity Score (Diagnostic)",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": report.get("generated_at_chicago") or "—",
        "next_run": "—",
        "status": "OK",
        "summary": report.get("summary_line") or "Continuity score diagnostic",
        "report_url": REPORT_URL,
    }
    if existing is None:
        tasks.append(payload)
    else:
        existing.update(payload)
    TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ----------------- benchmark fetch (optional) -----------------


def fetch_benchmarks(tickers=BENCHMARK_TICKERS) -> dict:
    """Best-effort SPY/QQQ daily returns via yfinance. Degrades to
    {"available": False} when yfinance/network is unavailable."""
    out: dict = {"available": False, "tickers": {}}
    try:
        import yfinance as yf  # type: ignore[import-not-found]
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"yfinance not available: {type(e).__name__}"
        return out
    any_ok = False
    for t in tickers:
        entry: dict = {"ticker": t}
        try:
            hist = yf.Ticker(t).history(period="3mo", interval="1d", auto_adjust=False)
            closes = [float(x) for x in hist["Close"].tolist() if x == x] if hist is not None else []
            if len(closes) < 6:
                entry["error"] = "no_history"
            else:
                last = closes[-1]
                for label, lb in (("5d", 6), ("10d", 11), ("21d", 22)):
                    if len(closes) >= lb and closes[-lb]:
                        entry[f"return_{label}"] = (last - closes[-lb]) / closes[-lb]
                    else:
                        entry[f"return_{label}"] = None
                any_ok = True
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}"
        out["tickers"][t] = entry
    out["available"] = any_ok
    return out


# ----------------- core -----------------


def build_report(*, ohlcv_dir: Path = OHLCV_DIR, fetch_bench: bool = True) -> dict:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    bench = fetch_benchmarks() if fetch_bench else {"available": False, "tickers": {}}

    scored_main = score_board(rankings, bench, ohlcv_dir=ohlcv_dir)
    scored_watch = score_board(watchlist, bench, ohlcv_dir=ohlcv_dir)

    open_date = None
    if isinstance(rankings, dict):
        open_date = rankings.get("open_date")
    if not open_date and isinstance(watchlist, dict):
        open_date = watchlist.get("open_date")
    if not open_date:
        open_date = _to_chicago(_now_utc()).date().strftime("%Y-%m-%d")

    snapshots = _load_snapshots(SNAPSHOTS_FILE)
    today_snap = build_today_snapshot(open_date, scored_main, scored_watch)
    if today_snap is not None:
        snapshots = upsert_snapshot(snapshots, today_snap)
    tracking_table = build_tracking_table(snapshots)

    summary = summarize(scored_main, scored_watch)

    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    chi_label = "CDT" if chi_now.utcoffset().total_seconds() == -5 * 3600 else "CST"
    chi_str = chi_now.strftime("%Y-%m-%d %I:%M %p ") + chi_label

    mc = summary["main_counts"]
    summary_line = (
        f"{summary['main_scored_count']} scored; "
        f"HIGH {mc.get('HIGH',0)} / MID {mc.get('MID',0)} / LOW {mc.get('LOW',0)}; "
        f"{len(summary.get('fade_risk_high_rank') or [])} fade-risk in top 25; "
        f"OHLCV {'on' if summary.get('ohlcv_available') else 'off'}"
    )

    # Per-ticker badge map keyed by ticker for the JSON consumers (UI badge).
    badges = {}
    for s in scored_main:
        if s.get("ticker"):
            badges[s["ticker"]] = {
                "score": s.get("score"),
                "label": s.get("label"),
                "confidence": s.get("confidence"),
            }
    for s in scored_watch:
        if s.get("ticker") and s["ticker"] not in badges:
            badges[s["ticker"]] = {
                "score": s.get("score"),
                "label": s.get("label"),
                "confidence": s.get("confidence"),
            }

    return {
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_chicago": chi_str,
        "open_date": open_date,
        "overall": "OK",
        "diagnostic_only": True,
        "weights": COMPONENT_WEIGHTS,
        "thresholds": {"high_min": HIGH_MIN, "low_max": LOW_MAX, "min_confidence": 0.30},
        "benchmark_available": bench.get("available", False),
        "summary": summary,
        "summary_line": summary_line,
        "rows": scored_main,
        "watchlist_rows": scored_watch,
        "badges": badges,
        "tracking_table": tracking_table,
        "snapshots_to_persist": snapshots,
    }


def main() -> int:
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    _write_snapshots(SNAPSHOTS_FILE, report["snapshots_to_persist"])
    json_out = {k: v for k, v in report.items() if k != "snapshots_to_persist"}
    JSON_OUTPUT.write_text(json.dumps(json_out, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    _stamp_task(report)
    print(f"[continuity_score] {report['summary_line']} -> {JSON_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

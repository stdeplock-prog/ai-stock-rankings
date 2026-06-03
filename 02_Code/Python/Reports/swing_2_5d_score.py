"""SWING 2-5D Score — diagnostic-only estimate of *short-horizon* (2-5 trading
day) positive-swing / continuation potential per ticker.

How this differs from the Continuity Score (intentionally):
  - Continuity Score answers "will this setup *keep working* over the coming
    week(s) rather than fade" — a persistence/quality read.
  - SWING 2-5D answers a narrower, faster question: "does this ticker look
    primed for a 2-5 day *positive thrust* right now?" It leans on momentum
    thrust, trend strength (TECH), short-term follow-through (CONT), the Pine
    Go/No-Go gate (GO), accumulation (ACC) and liquidity participation (ACT),
    and it *penalises* setups that are overextended / in cool-off, have weak
    continuity, sit near a binary earnings catalyst, or are too illiquid to
    trade cleanly.

Both are diagnostic-only and never touch production rankings.json scores or
ranks. The existing production `swing_score` / `swing_tier` fields are a
separate, longer-horizon production signal and are LEFT UNTOUCHED — this badge
is surfaced under the `2-5D` label so the two never get confused.

Inputs (all read-only, all optional except rankings):
  - data/rankings.json                          (production board + closes[] +
                                                   technical/go_label/acc_label/
                                                   atr_pct/days_to_earnings/...)
  - data/watchlist_rankings.json                (watchlist board, same shape)
  - data/raw/ohlcv_daily/<TICKER>_daily.csv     (full daily OHLCV if present)
  - data/reports/continuity_score.json          (CONT badge map, optional)
  - data/reports/accumulation_signal_meter.json (ACC context, optional)
  - data/reports/activity_adjusted_review.json  (ACT overlay context, optional)

Outputs:
  - data/reports/swing_2_5d_score.json   (per-ticker score/label/components +
                                           a `missing_data_roadmap` block)
  - data/tasks.json row id=swing-2-5d-score stamped on each run.

No standalone HTML report is emitted on purpose — the user asked to avoid
adding another top-level diagnostic page. The badge is surfaced compactly in
the existing main + watchlist tables (the `2-5D` column) and the JSON artifact
is linked from the existing diagnostics index.

Formula (transparent, conservative). Each component yields a 0..1 sub-score and
carries a weight. Components that cannot be computed from available data are
*omitted* from the weighted average (never fabricated to 0.5) and instead lower
a `confidence` fraction = present_weight / total_weight. The positive composite
is the weighted average of present components, rescaled to 0..100. Penalties are
then applied as multiplicative haircuts (each in (0,1]) for conditions that make
a short swing risky regardless of the positive read.

Positive components and weights (sum = 1.0):
  tech_trend       (0.22) — production TECH sub-score (0-10) → trend strength.
  momentum_thrust  (0.20) — 2-3 day price thrust (recent acceleration), the
                            core "about to pop / just popped" short read.
  continuity       (0.16) — CONT continuity/follow-through (from continuity
                            _score.json badge, else a closes-array proxy).
  accumulation     (0.14) — ACC accumulation (acc_label HIGH/MID/LOW, else
                            accumulation_signal_meter.json score).
  go_gate          (0.12) — GO Pine Go/No-Go (GO / WAIT / WEAK).
  activity_liq     (0.10) — ACT/liquidity participation (volume_millions +
                            activity overlay promotion).
  sentiment_catalyst (0.06) — sentiment sub-score + catalyst_flag support
                            where already available.

Penalty haircuts (multiplicative, applied to the positive composite):
  overextension cool-off  — far above short MA / parabolic recent run (fade
                            risk over 2-5 days). Up to ×0.80.
  weak continuity         — CONT LOW → up to ×0.85.
  earnings proximity      — binary earnings inside the 2-5 day window → up to
                            ×0.70 (only when days_to_earnings is known).
  low liquidity           — very low volume_millions → up to ×0.85.

Label / badge (mirrors GO/ACC/CONT dg-pos/mid/neg convention):
  HIGH  score >= 65   (dg-pos / green)
  MID   45 <= score < 65 (dg-mid / amber)
  LOW   score < 45    (dg-neg / red)
  —     no score / confidence < 0.30 (dg-none / muted)

Recommendation: diagnostic only. Do not promote into production scoring until
forward 2-5 day returns are shown to correlate with the score.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
OHLCV_DIR = DATA_DIR / "raw" / "ohlcv_daily"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"
CONTINUITY_FILE = DATA_REPORTS_DIR / "continuity_score.json"
ACCUM_FILE = DATA_REPORTS_DIR / "accumulation_signal_meter.json"
ACTIVITY_FILE = DATA_REPORTS_DIR / "activity_adjusted_review.json"
JSON_OUTPUT = DATA_REPORTS_DIR / "swing_2_5d_score.json"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./data/reports/swing_2_5d_score.json"

# Positive-component weights. Sum to 1.0; kept here so tests + UI can introspect.
COMPONENT_WEIGHTS = {
    "tech_trend": 0.22,
    "momentum_thrust": 0.20,
    "continuity": 0.16,
    "accumulation": 0.14,
    "go_gate": 0.12,
    "activity_liq": 0.10,
    "sentiment_catalyst": 0.06,
}

# Label thresholds (0..100). HIGH band intentionally matches CONT; MID floor is
# a touch higher (45 vs 40) because a *short* swing needs a cleaner setup.
HIGH_MIN = 65.0
MID_MIN = 45.0
MIN_CONFIDENCE = 0.30


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


# ----------------- OHLCV loader (optional) -----------------


def load_ohlcv(ticker: str, ohlcv_dir: Path = OHLCV_DIR, max_rows: int = 60) -> list[dict] | None:
    """Load the most recent `max_rows` daily bars for a ticker from the
    workflow-generated CSV, if present. Returns oldest-first dicts with keys
    date/open/high/low/close/volume, or None when missing/unreadable."""
    path = ohlcv_dir / f"{ticker}_daily.csv"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return None
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
                    colmap[name] = "close"
                elif low == "volume":
                    colmap[name] = "volume"
            rows: list[dict] = []
            for raw in reader:
                bar = {"date": None, "open": None, "high": None,
                       "low": None, "close": None, "volume": None}
                for src, canon in colmap.items():
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


# ----------------- positive component computations -----------------
#
# Each returns (sub_score_0_1 | None, detail_str). None => omit from the
# weighted average and lower confidence (never fabricate a 0.5).


def comp_tech_trend(technical) -> tuple[float | None, str]:
    """Production TECH sub-score (0-10) mapped to 0..1. Trend strength is the
    backbone of a short swing — a high TECH means price structure/momentum
    indicators are already aligned."""
    t = _safe_float(technical)
    if t is None:
        return None, "no technical sub-score"
    return _clamp01(t / 10.0), f"TECH {t:.1f}/10"


def comp_momentum_thrust(closes: list[float],
                         ohlcv: list[dict] | None) -> tuple[float | None, str]:
    """2-3 day price thrust — the core short-horizon read. Compares the most
    recent 2-day return against the trailing typical 2-day move so a genuine
    acceleration scores high while a flat drift lands near 0.5. Uses OHLCV
    closes when they extend the series, else the rankings closes array."""
    series = [c for c in (closes or []) if isinstance(c, (int, float))]
    if ohlcv:
        oc = [b.get("close") for b in ohlcv if b.get("close") is not None]
        if len(oc) >= len(series):
            series = oc
    if len(series) < 4:
        return None, "insufficient closes for thrust"
    last = series[-1]
    ref2 = series[-3]
    if ref2 == 0:
        return None, "bad ref close"
    r2 = (last - ref2) / ref2  # 2-day return
    # Typical 2-day move magnitude over the available window as the scale.
    deltas2 = []
    for i in range(2, len(series)):
        base = series[i - 2]
        if base:
            deltas2.append(abs((series[i] - base) / base))
    typ = (sum(deltas2) / len(deltas2)) if deltas2 else 0.02
    typ = max(typ, 0.005)  # floor so quiet names don't divide by ~0
    # Positive thrust >= ~1.5x the typical move -> strong; negative -> weak.
    z = r2 / typ
    score = _clamp01(0.5 + z * 0.22)
    return score, f"2d {r2*100:+.1f}% ({z:+.1f}x typ)"


def comp_continuity(cont_entry: dict | None,
                    closes: list[float]) -> tuple[float | None, str]:
    """CONT continuity / short-term follow-through. Prefer the continuity_score
    artifact (0-100 → 0..1). Fall back to an up-day fraction over the recent
    closes array so the component still computes when the artifact is absent."""
    if isinstance(cont_entry, dict):
        sc = _safe_float(cont_entry.get("score"))
        lbl = cont_entry.get("label")
        if sc is not None and lbl not in (None, "—"):
            return _clamp01(sc / 100.0), f"CONT {lbl} {sc:.0f}"
    series = [c for c in (closes or []) if isinstance(c, (int, float))]
    if len(series) < 4:
        return None, "no continuity signal"
    deltas = [series[i] - series[i - 1] for i in range(1, len(series))][-6:]
    if not deltas:
        return None, "no deltas"
    ups = sum(1 for d in deltas if d > 0)
    frac = ups / len(deltas)
    score = _clamp01(0.2 + frac * 0.6)  # proxy is weaker -> compressed band
    return score, f"{ups}/{len(deltas)} up (proxy)"


def comp_accumulation(acc_label, acc_score) -> tuple[float | None, str]:
    """ACC accumulation. Prefer the acc_label (HIGH/MID/LOW) carried on the row;
    fall back to a 0-10 accumulation meter score when only that is present."""
    if isinstance(acc_label, str):
        lab = acc_label.strip().upper()
        if lab == "HIGH":
            return 0.85, "ACC HIGH"
        if lab == "MID":
            return 0.55, "ACC MID"
        if lab == "LOW":
            return 0.20, "ACC LOW"
    s = _safe_float(acc_score)
    if s is not None:
        return _clamp01(s / 10.0), f"ACC meter {s:.1f}/10"
    return None, "no accumulation signal"


def comp_go_gate(go_label) -> tuple[float | None, str]:
    """GO Pine Go/No-Go gate. GO = clean setup (good for an entry), WAIT =
    blocker / cool-off (mediocre), WEAK = poor structure."""
    if not isinstance(go_label, str):
        return None, "no go gate"
    g = go_label.strip().upper()
    if g == "GO":
        return 0.85, "GO"
    if g == "WAIT":
        return 0.45, "WAIT"
    if g == "WEAK":
        return 0.15, "WEAK"
    return None, "unknown go label"


def comp_activity_liq(volume_millions, rank_delta) -> tuple[float | None, str]:
    """ACT / liquidity participation. Rewards tradeable liquidity (volume in
    millions through a soft band) plus a small bonus when the activity overlay
    *promotes* the ticker (rank_delta > 0 means rel-vol/accumulation lifted it)."""
    v = _safe_float(volume_millions)
    if v is None:
        return None, "no volume"
    # Liquidity band: <1M weak, ~5M solid, >=20M saturated-good.
    liq = _clamp01((v - 0.5) / 12.0)
    liq = 0.2 + 0.7 * liq  # keep within 0.2..0.9 base
    d = _safe_float(rank_delta)
    bonus = 0.0
    note = f"{v:.1f}M vol"
    if d is not None and d > 0:
        bonus = min(0.1, d / 200.0)  # promoted by activity overlay
        note += f", ACT +{int(d)}"
    return _clamp01(liq + bonus), note


def comp_sentiment_catalyst(sentiment, catalyst_flag) -> tuple[float | None, str]:
    """Sentiment sub-score (0-10) plus a small catalyst-support nudge when a
    near-term catalyst is flagged. Low weight — supporting evidence only."""
    s = _safe_float(sentiment)
    if s is None and not catalyst_flag:
        return None, "no sentiment/catalyst"
    base = _clamp01(s / 10.0) if s is not None else 0.5
    if catalyst_flag:
        base = _clamp01(base + 0.12)
        return base, f"SENT {s:.1f}/10 +catalyst" if s is not None else "catalyst flagged"
    return base, f"SENT {s:.1f}/10"


# ----------------- penalty haircuts (multiplicative, in (0,1]) -----------------


def pen_overextension(closes: list[float],
                      ohlcv: list[dict] | None,
                      atr_pct) -> tuple[float, str]:
    """Cool-off / overextension haircut. A name far above its short MA or that
    has run parabolically is prone to a 2-5 day pullback rather than a fresh
    leg up. Returns a multiplier in [0.80, 1.0]."""
    series = None
    if ohlcv and len([b for b in ohlcv if b.get("close") is not None]) >= 10:
        series = [b.get("close") for b in ohlcv if b.get("close") is not None]
    else:
        s = [c for c in (closes or []) if isinstance(c, (int, float))]
        if len(s) >= 5:
            series = s
    if not series:
        return 1.0, ""
    last = series[-1]
    n = min(10, len(series))
    ma = sum(series[-n:]) / n
    if ma <= 0 or last is None:
        return 1.0, ""
    dist = (last - ma) / ma
    # ATR context: a high-ATR name can sit further from its MA without being
    # "overextended", so widen the tolerance proportionally.
    a = _safe_float(atr_pct)
    tol = 0.08 + (a / 100.0 * 1.5 if a is not None else 0.0)
    if dist <= tol:
        return 1.0, ""
    over = dist - tol
    mult = max(0.80, 1.0 - over * 1.5)
    return mult, f"overext {dist*100:+.0f}% vs {n}DMA (×{mult:.2f})"


def pen_weak_continuity(cont_entry: dict | None) -> tuple[float, str]:
    """Weak continuity haircut. A LOW continuity read means follow-through has
    been poor — a short swing is less likely to extend. ×0.85 on LOW."""
    if isinstance(cont_entry, dict) and str(cont_entry.get("label")) == "LOW":
        return 0.85, "CONT LOW (×0.85)"
    return 1.0, ""


def pen_earnings(days_to_earnings) -> tuple[float, str]:
    """Earnings-proximity haircut. A binary earnings event inside the 2-5 day
    window can blow up a swing in either direction. Only applies when the
    earnings date is known (else neutral)."""
    d = _safe_float(days_to_earnings)
    if d is None:
        return 1.0, ""
    if 0 <= d <= 2:
        return 0.70, f"earnings in {int(d)}d (×0.70)"
    if 2 < d <= 5:
        return 0.82, f"earnings in {int(d)}d (×0.82)"
    return 1.0, ""


def pen_low_liquidity(volume_millions) -> tuple[float, str]:
    """Low-liquidity haircut. Thinly traded names are hard to enter/exit cleanly
    on a 2-5 day swing and gap unpredictably. ×0.85 under ~1M shares."""
    v = _safe_float(volume_millions)
    if v is None:
        return 1.0, ""
    if v < 1.0:
        return 0.85, f"low liq {v:.2f}M (×0.85)"
    return 1.0, ""


# ----------------- per-ticker scoring -----------------


def score_ticker(row: dict, *, ohlcv: list[dict] | None,
                 cont_entry: dict | None, acc_score=None) -> dict:
    """Compute the SWING 2-5D score for one rankings row. Pure given inputs.
    Returns score(0-100|None), label, confidence(0-1), positive-component
    breakdown, and the applied penalty haircuts."""
    closes_raw = row.get("closes")
    closes = [c for c in (closes_raw or []) if isinstance(c, (int, float))] \
        if isinstance(closes_raw, list) else []

    components: dict[str, dict] = {}

    def add(name, result):
        sub, detail = result
        components[name] = {
            "score": round(sub, 4) if sub is not None else None,
            "weight": COMPONENT_WEIGHTS[name],
            "detail": detail,
            "present": sub is not None,
        }

    add("tech_trend", comp_tech_trend(row.get("technical")))
    add("momentum_thrust", comp_momentum_thrust(closes, ohlcv))
    add("continuity", comp_continuity(cont_entry, closes))
    add("accumulation", comp_accumulation(row.get("acc_label"), acc_score))
    add("go_gate", comp_go_gate(row.get("go_label")))
    add("activity_liq", comp_activity_liq(row.get("volume_millions"), row.get("rank_delta")))
    add("sentiment_catalyst", comp_sentiment_catalyst(row.get("sentiment"), row.get("catalyst_flag")))

    present = {k: v for k, v in components.items() if v["present"]}
    total_weight = sum(COMPONENT_WEIGHTS.values())
    present_weight = sum(v["weight"] for v in present.values())
    confidence = round(present_weight / total_weight, 4) if total_weight else 0.0

    penalties: dict[str, dict] = {}

    def add_pen(name, result):
        mult, detail = result
        if mult < 1.0:
            penalties[name] = {"mult": round(mult, 4), "detail": detail}
        return mult

    if present_weight > 0:
        weighted = sum(v["score"] * v["weight"] for v in present.values())
        positive100 = (weighted / present_weight) * 100.0
        m = 1.0
        m *= add_pen("overextension", pen_overextension(closes, ohlcv, row.get("atr_pct")))
        m *= add_pen("weak_continuity", pen_weak_continuity(cont_entry))
        m *= add_pen("earnings", pen_earnings(row.get("days_to_earnings")))
        m *= add_pen("low_liquidity", pen_low_liquidity(row.get("volume_millions")))
        score100 = round(positive100 * m, 1)
        positive100 = round(positive100, 1)
    else:
        positive100 = None
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
        "swing_score": row.get("swing_score"),
        "swing_tier": row.get("swing_tier"),
        "close": closes[-1] if closes else None,
        "days_to_earnings": row.get("days_to_earnings"),
        "positive_score": positive100,
        "score": score100,
        "label": label,
        "confidence": confidence,
        "components": components,
        "penalties": penalties,
        "has_ohlcv": ohlcv is not None,
    }


def derive_label(score100: float | None, confidence: float = 1.0) -> str:
    """HIGH / MID / LOW / — from a 0-100 score. A score computed from too little
    data (confidence < MIN_CONFIDENCE) collapses to '—' so the badge never
    overstates a thin signal."""
    if score100 is None:
        return "—"
    if confidence < MIN_CONFIDENCE:
        return "—"
    if score100 >= HIGH_MIN:
        return "HIGH"
    if score100 < MID_MIN:
        return "LOW"
    return "MID"


# ----------------- board assembly -----------------


def _cont_badges(continuity: dict | None) -> dict:
    """ticker -> {score,label,confidence} from continuity_score.json badges."""
    if not isinstance(continuity, dict):
        return {}
    badges = continuity.get("badges")
    return badges if isinstance(badges, dict) else {}


def _acc_scores(accum: dict | None) -> dict:
    """ticker -> 0-10 accumulation meter score, best-effort. The meter artifact
    shape may vary; we look for a per-ticker score in common shapes."""
    out: dict = {}
    if not isinstance(accum, dict):
        return out
    rows = accum.get("rows")
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("ticker"):
                s = _safe_float(r.get("score") if r.get("score") is not None else r.get("acc_score"))
                if s is not None:
                    out[r["ticker"]] = s
    return out


def score_board(rankings: dict | None, *, continuity: dict | None = None,
                accum: dict | None = None, ohlcv_dir: Path = OHLCV_DIR) -> list[dict]:
    """Score every row in a rankings payload. Returns a list of per-ticker
    score dicts (empty if no rows)."""
    if not isinstance(rankings, dict):
        return []
    rows = rankings.get("rows")
    if not isinstance(rows, list) or not rows:
        return []
    cont_map = _cont_badges(continuity)
    acc_map = _acc_scores(accum)
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("ticker"):
            continue
        ticker = r["ticker"]
        ohlcv = load_ohlcv(ticker, ohlcv_dir=ohlcv_dir)
        out.append(score_ticker(
            r,
            ohlcv=ohlcv,
            cont_entry=cont_map.get(ticker),
            acc_score=acc_map.get(ticker),
        ))
    return out


# ----------------- summary -----------------


def summarize(scored_main: list[dict], scored_watch: list[dict]) -> dict:
    def counts(board):
        c = {"HIGH": 0, "MID": 0, "LOW": 0, "—": 0}
        for s in board:
            c[s.get("label", "—")] = c.get(s.get("label", "—"), 0) + 1
        return c

    main_scored = [s for s in scored_main if s.get("score") is not None]
    top = sorted(main_scored, key=lambda s: s["score"], reverse=True)[:15]

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
            "penalties": list((s.get("penalties") or {}).keys()),
        }

    return {
        "main_counts": counts(scored_main),
        "watchlist_counts": counts(scored_watch),
        "main_scored_count": len(main_scored),
        "ohlcv_available": any(s.get("has_ohlcv") for s in scored_main),
        "top_swing_2_5d": [slim(s) for s in top],
    }


# ----------------- missing-data roadmap -----------------


def missing_data_roadmap() -> dict:
    """Concise roadmap of data that would sharpen this 2-5 day swing badge but
    is not yet available in the repo, prioritising free/reliable sources. Kept
    in the JSON artifact so the priority survives without a separate doc."""
    return {
        "note": ("Diagnostic-only. The fields below would improve short-horizon "
                 "swing detection but are NOT required for the current badge. "
                 "Prioritise free/reliable sources first; ticker-level options "
                 "flow likely needs a paid provider later."),
        "priorities": [
            {
                "signal": "intraday VWAP / position vs VWAP",
                "why": "Confirms intraday demand for a 2-5d entry; above-VWAP holds support a swing.",
                "source": "yfinance / EODHD intraday OHLCV (free-ish, rate-limited).",
                "priority": "high",
            },
            {
                "signal": "sector-relative intraday strength",
                "why": "A name leading its sector intraday is a stronger short swing than an isolated pop.",
                "source": "Derive from existing per-ticker + sector OHLCV (no new API).",
                "priority": "high",
            },
            {
                "signal": "real-time news / headline sentiment",
                "why": "Fresh catalyst news drives 2-5d thrusts; current sentiment is a slow placeholder.",
                "source": "Free: Google Trends (diagnostic only), RSS headlines; later a news API.",
                "priority": "medium",
            },
            {
                "signal": "market-wide put/call ratio",
                "why": "Regime context — extreme p/c shifts the odds for all short swings.",
                "source": "Cboe free daily put/call (market-wide), TradingView manual samples.",
                "priority": "medium",
            },
            {
                "signal": "ticker-level put/call / options flow",
                "why": "Unusual call flow precedes many 2-5d pops; the strongest missing edge.",
                "source": "Likely needs a paid provider (e.g. ORATS/Polygon) — defer.",
                "priority": "low (paid)",
            },
            {
                "signal": "external benchmark samples (TradingView/Fidelity/E*TRADE/Zacks/MarketBeat)",
                "why": "Cross-check short-term ratings; already partly wired via external benchmark review.",
                "source": "Manual periodic samples (free), reuse existing external_benchmark_review.",
                "priority": "medium",
            },
        ],
    }


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
    row_id = "swing-2-5d-score"
    existing = next((r for r in tasks if isinstance(r, dict) and r.get("id") == row_id), None)
    payload = {
        "id": row_id,
        "name": "Swing 2-5D Score (Diagnostic)",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": report.get("generated_at_chicago") or "—",
        "next_run": "—",
        "status": "OK",
        "summary": report.get("summary_line") or "2-5 day swing score diagnostic",
        "report_url": REPORT_URL,
    }
    if existing is None:
        tasks.append(payload)
    else:
        existing.update(payload)
    TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ----------------- core -----------------


def build_report(*, ohlcv_dir: Path = OHLCV_DIR) -> dict:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    continuity = _load_json(CONTINUITY_FILE)
    accum = _load_json(ACCUM_FILE)

    scored_main = score_board(rankings, continuity=continuity, accum=accum, ohlcv_dir=ohlcv_dir)
    scored_watch = score_board(watchlist, continuity=continuity, accum=accum, ohlcv_dir=ohlcv_dir)

    open_date = None
    if isinstance(rankings, dict):
        open_date = rankings.get("open_date")
    if not open_date and isinstance(watchlist, dict):
        open_date = watchlist.get("open_date")
    if not open_date:
        open_date = _to_chicago(_now_utc()).date().strftime("%Y-%m-%d")

    summary = summarize(scored_main, scored_watch)

    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    chi_label = "CDT" if chi_now.utcoffset().total_seconds() == -5 * 3600 else "CST"
    chi_str = chi_now.strftime("%Y-%m-%d %I:%M %p ") + chi_label

    mc = summary["main_counts"]
    summary_line = (
        f"{summary['main_scored_count']} scored; "
        f"HIGH {mc.get('HIGH',0)} / MID {mc.get('MID',0)} / LOW {mc.get('LOW',0)}; "
        f"OHLCV {'on' if summary.get('ohlcv_available') else 'off'}"
    )

    # Per-ticker badge map for the UI badge consumers (keyed by ticker).
    badges = {}
    for board in (scored_main, scored_watch):
        for s in board:
            t = s.get("ticker")
            if t and t not in badges:
                badges[t] = {
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
        "horizon": "2-5 trading days",
        "differs_from_continuity": (
            "Continuity Score = will the setup keep working over the coming week(s); "
            "SWING 2-5D = is this primed for a short 2-5 day positive thrust now."
        ),
        "weights": COMPONENT_WEIGHTS,
        "thresholds": {"high_min": HIGH_MIN, "mid_min": MID_MIN, "min_confidence": MIN_CONFIDENCE},
        "summary": summary,
        "summary_line": summary_line,
        "rows": scored_main,
        "watchlist_rows": scored_watch,
        "badges": badges,
        "missing_data_roadmap": missing_data_roadmap(),
    }


def main() -> int:
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    JSON_OUTPUT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    _stamp_task(report)
    print(f"[swing_2_5d_score] {report['summary_line']} -> {JSON_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

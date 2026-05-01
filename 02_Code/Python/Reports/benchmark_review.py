"""Benchmark Review — model-validation report.

Compares the leader buckets we publish (main rankings top 10/25, watchlist
top 10/25, SUPP top 10/25 if any) against practical benchmark proxies:

  * Momentum / trend from the per-row `closes` sparkline (10-day daily closes
    that ship inside data/rankings.json and data/watchlist_rankings.json).
  * Sector concentration of the top-25 buckets (flag if a single sector is
    >40% of a bucket).
  * Market context — recent return / above-50DMA flags for SPY / QQQ / IWM,
    fetched via yfinance when available. The report degrades gracefully if
    yfinance is unreachable (offline runners, blocked egress).
  * A forward-performance tracking scaffold: append a daily snapshot of the
    bucket leaders + their reference price into
    data/reports/benchmark_snapshots.jsonl, and re-evaluate prior snapshots
    once enough trading days have elapsed (no lookahead bias). Bounded to
    the most recent ~90 calendar days so the JSONL stays small for Pages.

Inputs (read-only):
  - data/rankings.json
  - data/watchlist_rankings.json

Outputs:
  - data/reports/benchmark_review.json
  - reports/benchmark-review.html
  - data/reports/benchmark_snapshots.jsonl   (append-safe scaffold)

This script is deliberately tolerant of missing inputs: missing yfinance,
missing closes, etc. all surface as "missing"/"pending" findings rather
than exceptions. It is *internal model validation* — no financial advice
language.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "benchmark_review.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "benchmark-review.html"
SNAPSHOTS_FILE = DATA_REPORTS_DIR / "benchmark_snapshots.jsonl"

BENCHMARK_TICKERS = ("SPY", "QQQ", "IWM")
SCORE_FIELDS = ("ai_score", "fundamental", "technical", "swing_score")
TOP_N_LEVELS = (10, 25)
SECTOR_CONCENTRATION_WARN_PCT = 0.40
SNAPSHOT_RETENTION_DAYS = 90
FORWARD_HORIZONS_TRADING_DAYS = (1, 3, 5, 10, 20)


# ---------- Generic helpers ----------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path) -> dict | list | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _is_missing_text(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s == "" or s == "—" or s.lower() in ("nan", "none", "n/a")
    return False


def _safe_div(a: float, b: float) -> float | None:
    if b == 0 or b is None:
        return None
    try:
        return a / b
    except (TypeError, ZeroDivisionError):
        return None


def _round(v: float | None, n: int = 4) -> float | None:
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return round(float(v), n)


# ---------- Return helpers ----------


def returns_from_closes(closes) -> dict:
    """Compute available return windows from a closes list.

    The pipeline ships a 10-day daily closes sparkline. We can therefore
    report 1d / 5d / 9d returns (using last vs. first), which we label
    accordingly. Anything longer is left None — the per-row sparkline
    doesn't carry a 1M/3M/6M window. The forward-performance scaffold
    is responsible for longer horizons.
    """
    if not isinstance(closes, list) or len(closes) < 2:
        return {"available": False}
    nums: list[float] = []
    for v in closes:
        if isinstance(v, (int, float)) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            nums.append(float(v))
    if len(nums) < 2 or nums[0] == 0:
        return {"available": False}
    last = nums[-1]
    out: dict = {"available": True, "n": len(nums), "last": last}
    out["return_1d"] = _round((last - nums[-2]) / nums[-2]) if nums[-2] else None
    if len(nums) >= 6 and nums[-6]:
        out["return_5d"] = _round((last - nums[-6]) / nums[-6])
    else:
        out["return_5d"] = None
    if len(nums) >= 2 and nums[0]:
        out["return_window"] = _round((last - nums[0]) / nums[0])
        out["window_len"] = len(nums) - 1  # bars, not calendar days
    else:
        out["return_window"] = None
        out["window_len"] = None
    return out


def _aggregate_returns(values: list[float | None]) -> dict:
    clean = [v for v in values if isinstance(v, (int, float)) and not math.isnan(v)]
    if not clean:
        return {"count": 0, "mean": None, "median": None, "pct_positive": None}
    pos = sum(1 for v in clean if v > 0)
    return {
        "count": len(clean),
        "mean": _round(mean(clean)),
        "median": _round(median(clean)),
        "pct_positive": _round(pos / len(clean), 4),
    }


def _aggregate_scores(rows: list[dict], field: str) -> dict:
    vals = []
    for r in rows:
        v = r.get(field)
        if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
            vals.append(float(v))
    if not vals:
        return {"count": 0, "mean": None, "median": None}
    return {"count": len(vals), "mean": _round(mean(vals), 3), "median": _round(median(vals), 3)}


# ---------- Bucket extraction ----------


def get_main_rows(rankings: dict | None) -> list[dict]:
    if not isinstance(rankings, dict):
        return []
    rows = rankings.get("rows") or []
    return [r for r in rows if isinstance(r, dict)]


def get_watchlist_rows(watchlist: dict | None) -> list[dict]:
    if not isinstance(watchlist, dict):
        return []
    rows = watchlist.get("rows") or []
    return [r for r in rows if isinstance(r, dict)]


def split_watchlist_buckets(rows: list[dict]) -> dict[str, list[dict]]:
    """Split watchlist rows into 'all' and 'supp' (supplemental_*) groups.

    Both lists preserve the watchlist's existing rank order — we don't
    re-sort. The expectation is that watchlist rankings already sorted by
    AI score / rank ahead of time.
    """
    supp = [r for r in rows if str(r.get("data_source", "")).startswith("supplemental")]
    return {"all": rows, "supp": supp}


def take_top(rows: list[dict], n: int) -> list[dict]:
    return rows[:n]


# ---------- Sector concentration ----------


def sector_distribution(rows: list[dict]) -> dict:
    counter: Counter = Counter()
    missing = 0
    for r in rows:
        sec = r.get("sector")
        if _is_missing_text(sec):
            missing += 1
            continue
        counter[sec] += 1
    total = sum(counter.values())
    dist = {}
    top_sector = None
    top_pct = 0.0
    for sec, cnt in counter.most_common():
        pct = cnt / total if total else 0.0
        dist[sec] = {"count": cnt, "pct": _round(pct, 4)}
        if pct > top_pct:
            top_pct = pct
            top_sector = sec
    return {
        "total_classified": total,
        "missing_sector": missing,
        "distribution": dist,
        "top_sector": top_sector,
        "top_pct": _round(top_pct, 4),
        "concentrated": top_pct >= SECTOR_CONCENTRATION_WARN_PCT,
    }


# ---------- Bucket metrics ----------


def bucket_metrics(name: str, rows: list[dict]) -> dict:
    """Compute the canonical block of metrics for one bucket."""
    if not rows:
        return {"name": name, "size": 0, "available": False}

    return_1d = []
    return_5d = []
    return_window = []
    missing_closes = 0
    window_lens = []
    for r in rows:
        ret = returns_from_closes(r.get("closes"))
        if not ret.get("available"):
            missing_closes += 1
            continue
        if ret.get("return_1d") is not None:
            return_1d.append(ret["return_1d"])
        if ret.get("return_5d") is not None:
            return_5d.append(ret["return_5d"])
        if ret.get("return_window") is not None:
            return_window.append(ret["return_window"])
        if ret.get("window_len") is not None:
            window_lens.append(ret["window_len"])

    score_block = {field: _aggregate_scores(rows, field) for field in SCORE_FIELDS}

    return {
        "name": name,
        "size": len(rows),
        "available": True,
        "tickers": [r.get("ticker") for r in rows],
        "missing_closes": missing_closes,
        "window_len_typical": Counter(window_lens).most_common(1)[0][0] if window_lens else None,
        "return_1d": _aggregate_returns(return_1d),
        "return_5d": _aggregate_returns(return_5d),
        "return_window": _aggregate_returns(return_window),
        "scores": score_block,
        "sector_concentration": sector_distribution(rows),
    }


# ---------- Market context (yfinance, optional) ----------


def fetch_market_context(tickers=BENCHMARK_TICKERS) -> dict:
    """Best-effort market-context fetch via yfinance.

    Returns a dict keyed by ticker with:
      - last, return_1d, return_5d, return_21d, return_63d
      - above_50dma, above_200dma
      - error (if fetch failed)
    Missing yfinance / network failures degrade to {"available": False, ...}.
    """
    out: dict = {"available": False, "tickers": {}, "fetched_at": _now_utc().isoformat() + "Z"}
    try:
        import yfinance as yf  # type: ignore[import-not-found]
    except Exception as e:
        out["reason"] = f"yfinance not available: {type(e).__name__}: {e}"
        return out

    any_ok = False
    for t in tickers:
        entry: dict = {"ticker": t}
        try:
            tk = yf.Ticker(t)
            hist = tk.history(period="1y", interval="1d", auto_adjust=False)
            if hist is None or len(hist) < 2:
                entry["error"] = "no_history"
                out["tickers"][t] = entry
                continue
            closes = [float(x) for x in hist["Close"].tolist() if x == x]
            if len(closes) < 2:
                entry["error"] = "no_closes"
                out["tickers"][t] = entry
                continue
            last = closes[-1]
            entry["last"] = _round(last, 4)
            entry["history_len"] = len(closes)
            entry["return_1d"] = _round((last - closes[-2]) / closes[-2]) if closes[-2] else None
            for label, lookback in (("5d", 6), ("21d", 22), ("63d", 64), ("126d", 127), ("252d", 253)):
                if len(closes) >= lookback and closes[-lookback]:
                    entry[f"return_{label}"] = _round((last - closes[-lookback]) / closes[-lookback])
                else:
                    entry[f"return_{label}"] = None
            if len(closes) >= 50:
                ma50 = sum(closes[-50:]) / 50
                entry["ma50"] = _round(ma50, 4)
                entry["above_50dma"] = bool(last > ma50)
            else:
                entry["above_50dma"] = None
            if len(closes) >= 200:
                ma200 = sum(closes[-200:]) / 200
                entry["ma200"] = _round(ma200, 4)
                entry["above_200dma"] = bool(last > ma200)
            else:
                entry["above_200dma"] = None
            any_ok = True
        except Exception as e:  # noqa: BLE001 - resilient against yfinance flakiness
            entry["error"] = f"{type(e).__name__}: {e}"
        out["tickers"][t] = entry

    out["available"] = any_ok
    if not any_ok:
        out["reason"] = "no benchmark fetch succeeded"
    return out


# ---------- Forward-performance snapshot scaffold ----------


def _last_close_from_row(row: dict) -> float | None:
    closes = row.get("closes")
    if not isinstance(closes, list):
        return None
    for v in reversed(closes):
        if isinstance(v, (int, float)) and v == v and not math.isinf(v):
            return float(v)
    return None


def _today_date_iso(payload: dict | None) -> str:
    """Prefer the rankings open_date when present, else today UTC."""
    if isinstance(payload, dict):
        d = payload.get("open_date")
        if isinstance(d, str) and len(d) == 10 and d[4] == "-" and d[7] == "-":
            return d
    return _now_utc().date().isoformat()


def build_snapshot_record(*, as_of_date: str, rankings: dict | None,
                          watchlist: dict | None) -> dict:
    """Build the snapshot dict to append for today.

    Captures top-10 ticker + reference last close per bucket. Forward
    returns are filled in *later runs* once enough trading days have
    elapsed.
    """
    record = {
        "as_of_date": as_of_date,
        "captured_at": _now_utc().isoformat() + "Z",
        "buckets": {},
    }
    main_rows = get_main_rows(rankings)
    record["buckets"]["main_top10"] = _bucket_snapshot(main_rows[:10])

    wl_rows = get_watchlist_rows(watchlist)
    record["buckets"]["watchlist_top10"] = _bucket_snapshot(wl_rows[:10])

    supp_rows = [r for r in wl_rows if str(r.get("data_source", "")).startswith("supplemental")]
    if supp_rows:
        record["buckets"]["supp_top10"] = _bucket_snapshot(supp_rows[:10])

    return record


def _bucket_snapshot(rows: list[dict]) -> dict:
    members = []
    for r in rows:
        members.append({
            "ticker": r.get("ticker"),
            "ai_score": r.get("ai_score"),
            "ref_close": _last_close_from_row(r),
            "sector": r.get("sector"),
        })
    return {"members": members, "size": len(members)}


def load_snapshots(path: Path = SNAPSHOTS_FILE) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def save_snapshots(records: list[dict], path: Path = SNAPSHOTS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, default=str) + "\n")
    os.replace(tmp, path)


def prune_snapshots(records: list[dict], today: date,
                    retention_days: int = SNAPSHOT_RETENTION_DAYS) -> list[dict]:
    cutoff = today - timedelta(days=retention_days)
    kept: list[dict] = []
    for r in records:
        d = _parse_iso_date(r.get("as_of_date"))
        if d is None or d >= cutoff:
            kept.append(r)
    return kept


def _parse_iso_date(s) -> date | None:
    if not isinstance(s, str) or len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _trading_days_between(start: date, end: date) -> int:
    """Approximate trading-day count using the simple Mon-Fri filter.

    Holidays are not modeled — that's acceptable for this scaffold; the
    minor undercount (a US trading year has ~252 vs. the ~261 weekday
    count) is well below the noise floor of 1d/3d/5d returns.
    """
    if end <= start:
        return 0
    days = 0
    cur = start
    one = timedelta(days=1)
    while cur < end:
        cur += one
        if cur.weekday() < 5:
            days += 1
    return days


def evaluate_snapshots(records: list[dict], *, today: date,
                       latest_prices: dict[str, float]) -> tuple[list[dict], dict]:
    """Compute forward returns on each snapshot using only `latest_prices`
    and the elapsed trading-day count. No lookahead: a horizon is filled
    only once enough trading days have elapsed since the snapshot date.

    Returns (updated_records, summary_dict).
    """
    summary: dict = {
        "snapshots_total": len(records),
        "horizons": {f"{h}d": {"completed": 0, "pending": 0, "buckets": {}}
                     for h in FORWARD_HORIZONS_TRADING_DAYS},
    }

    for rec in records:
        as_of_date = _parse_iso_date(rec.get("as_of_date"))
        if as_of_date is None:
            continue
        elapsed = _trading_days_between(as_of_date, today)
        rec.setdefault("forward", {})

        for horizon in FORWARD_HORIZONS_TRADING_DAYS:
            key = f"{horizon}d"
            horizon_done = elapsed >= horizon

            if not horizon_done:
                # Mark pending so the JSON tells the reader why the slot
                # is empty. Don't overwrite a previously-completed value.
                if key not in rec["forward"]:
                    rec["forward"][key] = {"status": "pending", "elapsed_trading_days": elapsed}
                continue

            # If the slot was previously marked pending, replace it with
            # the now-computable evaluation. If it was already completed
            # in a prior run, leave the existing value (the prices used
            # then are closer to the true horizon close than today's).
            existing = rec["forward"].get(key)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                _accumulate_summary(summary, key, existing, rec)
                continue

            evaluated = _evaluate_horizon(rec, latest_prices)
            rec["forward"][key] = {**evaluated, "status": "completed",
                                   "evaluated_at": _now_utc().isoformat() + "Z",
                                   "elapsed_trading_days": elapsed}
            _accumulate_summary(summary, key, rec["forward"][key], rec)

    # Final pass: for buckets we never computed, keep their entries empty
    return records, summary


def _evaluate_horizon(rec: dict, latest_prices: dict[str, float]) -> dict:
    bucket_results: dict = {}
    for bname, bucket in (rec.get("buckets") or {}).items():
        members = bucket.get("members") or []
        rets = []
        missing = 0
        for m in members:
            ticker = m.get("ticker")
            ref = m.get("ref_close")
            cur = latest_prices.get(ticker) if ticker else None
            if (not isinstance(ref, (int, float)) or not isinstance(cur, (int, float))
                    or ref <= 0):
                missing += 1
                continue
            rets.append((cur - ref) / ref)
        bucket_results[bname] = {
            "n": len(members),
            "evaluated": len(rets),
            "missing": missing,
            "mean_return": _round(mean(rets)) if rets else None,
            "median_return": _round(median(rets)) if rets else None,
            "pct_positive": _round(sum(1 for r in rets if r > 0) / len(rets), 4) if rets else None,
        }
    return {"buckets": bucket_results}


def _accumulate_summary(summary: dict, key: str, slot: dict, rec: dict) -> None:
    if slot.get("status") != "completed":
        summary["horizons"][key]["pending"] += 1
        return
    summary["horizons"][key]["completed"] += 1
    for bname, bres in (slot.get("buckets") or {}).items():
        agg = summary["horizons"][key]["buckets"].setdefault(
            bname, {"snapshots": 0, "mean_return_sum": 0.0, "n_with_return": 0,
                    "wins": 0, "losses": 0})
        agg["snapshots"] += 1
        if isinstance(bres.get("mean_return"), (int, float)):
            agg["mean_return_sum"] += bres["mean_return"]
            agg["n_with_return"] += 1
            if bres["mean_return"] > 0:
                agg["wins"] += 1
            elif bres["mean_return"] < 0:
                agg["losses"] += 1


def _finalize_summary(summary: dict) -> dict:
    """Convert running sums into reportable averages."""
    for key, hor in summary["horizons"].items():
        for bname, agg in hor["buckets"].items():
            n = agg["n_with_return"]
            agg["avg_mean_return"] = _round(agg["mean_return_sum"] / n) if n else None
            del agg["mean_return_sum"]
    return summary


# ---------- Latest-price source for snapshot evaluation ----------


def latest_price_index(rankings: dict | None, watchlist: dict | None,
                       market_context: dict | None) -> dict[str, float]:
    """Build a {ticker: latest_close} index from the freshest sources."""
    out: dict[str, float] = {}

    for payload in (rankings, watchlist):
        for r in (payload or {}).get("rows", []) if isinstance(payload, dict) else []:
            t = r.get("ticker")
            if not t:
                continue
            v = _last_close_from_row(r)
            if v is not None:
                out[t] = v  # later sources overwrite, that's fine

    for t, entry in (market_context or {}).get("tickers", {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("last"), (int, float)):
            out[t] = float(entry["last"])
    return out


# ---------- Top-level assembly ----------


def build_report(rankings: dict | None, watchlist: dict | None,
                 *, fetch_benchmarks: bool = True,
                 market_context_override: dict | None = None,
                 today: date | None = None) -> dict:
    today = today or _now_utc().date()
    main_rows = get_main_rows(rankings)
    wl = split_watchlist_buckets(get_watchlist_rows(watchlist))

    buckets: dict = {}
    for n in TOP_N_LEVELS:
        buckets[f"main_top{n}"] = bucket_metrics(f"main_top{n}", take_top(main_rows, n))
        buckets[f"watchlist_top{n}"] = bucket_metrics(
            f"watchlist_top{n}", take_top(wl["all"], n))
        if wl["supp"]:
            buckets[f"supp_top{n}"] = bucket_metrics(
                f"supp_top{n}", take_top(wl["supp"], n))

    if market_context_override is not None:
        market_context = market_context_override
    elif fetch_benchmarks:
        market_context = fetch_market_context()
    else:
        market_context = {"available": False, "tickers": {}, "reason": "fetch disabled"}

    # Compare main_top25 mean window-return to SPY return over a comparable
    # window (~21d) where we have it.
    bench_compare = _benchmark_compare(buckets, market_context)

    # Sector concentration findings
    findings: list[dict] = []
    for bname, b in buckets.items():
        sc = b.get("sector_concentration") or {}
        if sc.get("concentrated"):
            findings.append({
                "name": f"sector_concentration:{bname}",
                "status": "WARN",
                "message": f"{bname}: top sector {sc.get('top_sector')} = "
                           f"{(sc.get('top_pct') or 0) * 100:.0f}% of bucket",
                "data": {"top_sector": sc.get("top_sector"),
                         "top_pct": sc.get("top_pct")},
            })

    # Snapshot scaffold: load -> append today's -> evaluate -> save
    snapshots = load_snapshots()
    today_iso = _today_date_iso(rankings)
    today_record = build_snapshot_record(
        as_of_date=today_iso, rankings=rankings, watchlist=watchlist)

    if any(s.get("as_of_date") == today_iso for s in snapshots):
        # Replace the existing same-day record so re-runs within a day
        # always store the latest captured leaders.
        snapshots = [s for s in snapshots if s.get("as_of_date") != today_iso]
    snapshots.append(today_record)

    snapshots = prune_snapshots(snapshots, today=today)
    latest_prices = latest_price_index(rankings, watchlist, market_context)
    snapshots, snapshot_summary = evaluate_snapshots(
        snapshots, today=today, latest_prices=latest_prices)
    snapshot_summary = _finalize_summary(snapshot_summary)
    # Persist immediately so subsequent calls (next day's run) see today's
    # capture. main() doesn't need to know whether build_report persisted.
    save_snapshots(snapshots)

    return {
        "generated_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_rankings": (rankings or {}).get("as_of"),
        "as_of_watchlist": (watchlist or {}).get("as_of"),
        "open_date": today_iso,
        "buckets": buckets,
        "market_context": market_context,
        "benchmark_compare": bench_compare,
        "findings": findings,
        "snapshot_summary": snapshot_summary,
        "snapshots_kept": len(snapshots),
    }, snapshots


def _benchmark_compare(buckets: dict, market_context: dict) -> dict:
    out: dict = {"available": False}
    spy = (market_context.get("tickers") or {}).get("SPY") or {}
    spy_21 = spy.get("return_21d")
    if not isinstance(spy_21, (int, float)):
        return out

    out["available"] = True
    out["spy_return_21d"] = _round(spy_21)
    main25 = buckets.get("main_top25") or {}
    wl25 = buckets.get("watchlist_top25") or {}
    main_window = (main25.get("return_window") or {}).get("mean")
    wl_window = (wl25.get("return_window") or {}).get("mean")
    out["main_top25_mean_window_return"] = main_window
    out["watchlist_top25_mean_window_return"] = wl_window
    # The bucket window return is ~9 trading days, SPY 21d isn't a clean
    # apples-to-apples; the report exposes both numbers so the reader can
    # do the comparison eyes-open.
    out["note"] = ("bucket window return is from the ~10-day sparkline "
                   "shipped with rankings; SPY 21d is the closest "
                   "available benchmark window")
    return out


# ---------- HTML rendering ----------


def _render_html(report: dict) -> str:
    parts: list[str] = []
    overall = "OK"
    if report.get("findings"):
        overall = "WARN"
    color = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}[overall]

    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Benchmark Review</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1100px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:18px 0 6px;font-size:18px}}
.meta{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.OK{{color:#3c8c3c;font-weight:600}}
.WARN{{color:#b88a00;font-weight:600}}
.FAIL{{color:#c0392b;font-weight:600}}
.kv{{font-size:13px;color:#444}} .kv code{{background:#f3f3f3;padding:1px 4px;border-radius:3px}}
.muted{{color:#666;font-size:12px}}
.back{{font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media (max-width:760px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Benchmark Review</h1>
<p class="meta">Generated {escape(report['generated_at'])}
&middot; rankings as_of {escape(str(report.get('as_of_rankings')))}
&middot; <span class="badge">{overall}</span></p>
<p class="muted">Internal model validation — not financial advice.</p>
""")

    findings = report.get("findings") or []
    parts.append('<div class="section"><h2>Findings</h2>')
    if findings:
        parts.append('<table><thead><tr><th>Name</th><th>Status</th><th>Detail</th></tr></thead><tbody>')
        for c in findings:
            parts.append(
                f"<tr><td>{escape(c.get('name',''))}</td>"
                f"<td class=\"{escape(c.get('status',''))}\">{escape(c.get('status',''))}</td>"
                f"<td>{escape(c.get('message',''))}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append('<p class="muted">No concentration or coverage warnings.</p>')
    parts.append("</div>")

    parts.append('<div class="section"><h2>Market Context</h2>')
    mc = report.get("market_context") or {}
    if mc.get("available"):
        parts.append('<table><thead><tr><th>Ticker</th><th>Last</th><th>1d</th>'
                     '<th>21d</th><th>63d</th><th>252d</th>'
                     '<th>&gt;50DMA</th><th>&gt;200DMA</th></tr></thead><tbody>')
        for t, e in (mc.get("tickers") or {}).items():
            if not isinstance(e, dict) or e.get("error"):
                parts.append(
                    f'<tr><td>{escape(t)}</td><td colspan="7" class="muted">'
                    f'{escape(str((e or {}).get("error","missing")))}</td></tr>'
                )
                continue
            parts.append(
                f"<tr><td>{escape(t)}</td>"
                f"<td>{_fmt(e.get('last'))}</td>"
                f"<td>{_fmt_pct(e.get('return_1d'))}</td>"
                f"<td>{_fmt_pct(e.get('return_21d'))}</td>"
                f"<td>{_fmt_pct(e.get('return_63d'))}</td>"
                f"<td>{_fmt_pct(e.get('return_252d'))}</td>"
                f"<td>{_fmt_bool(e.get('above_50dma'))}</td>"
                f"<td>{_fmt_bool(e.get('above_200dma'))}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        reason = mc.get("reason") or "unavailable"
        parts.append(f'<p class="muted">Benchmarks unavailable: {escape(str(reason))}</p>')
    bc = report.get("benchmark_compare") or {}
    if bc.get("available"):
        parts.append(
            f'<p class="muted">SPY 21d: {_fmt_pct(bc.get("spy_return_21d"))} '
            f'&middot; Main top25 ~10d window: {_fmt_pct(bc.get("main_top25_mean_window_return"))} '
            f'&middot; Watchlist top25 ~10d window: {_fmt_pct(bc.get("watchlist_top25_mean_window_return"))} '
            f'<br>{escape(bc.get("note") or "")}</p>'
        )
    parts.append("</div>")

    # Buckets section
    parts.append('<div class="section"><h2>Leader Buckets</h2><div class="grid">')
    for bname, b in (report.get("buckets") or {}).items():
        parts.append(f'<div><h3 style="margin:6px 0;font-size:15px">{escape(bname)} '
                     f'<span class="muted">(n={b.get("size",0)})</span></h3>')
        if not b.get("available"):
            parts.append('<p class="muted">No rows.</p></div>')
            continue
        parts.append('<table><tbody>')
        ret_w = b.get("return_window") or {}
        ret_5 = b.get("return_5d") or {}
        ret_1 = b.get("return_1d") or {}
        parts.append(
            f"<tr><td>1d return (mean / median / %+)</td><td>"
            f"{_fmt_pct(ret_1.get('mean'))} / {_fmt_pct(ret_1.get('median'))} / "
            f"{_fmt_pct(ret_1.get('pct_positive'))}</td></tr>"
        )
        parts.append(
            f"<tr><td>5d return (mean / median / %+)</td><td>"
            f"{_fmt_pct(ret_5.get('mean'))} / {_fmt_pct(ret_5.get('median'))} / "
            f"{_fmt_pct(ret_5.get('pct_positive'))}</td></tr>"
        )
        parts.append(
            f"<tr><td>~10d window return (mean / median / %+)</td><td>"
            f"{_fmt_pct(ret_w.get('mean'))} / {_fmt_pct(ret_w.get('median'))} / "
            f"{_fmt_pct(ret_w.get('pct_positive'))}</td></tr>"
        )
        scores = b.get("scores") or {}
        for k, v in scores.items():
            parts.append(
                f"<tr><td>{escape(k)} (mean / median)</td><td>"
                f"{_fmt(v.get('mean'))} / {_fmt(v.get('median'))}</td></tr>"
            )
        sc = b.get("sector_concentration") or {}
        conc_badge = ' <span class="WARN">CONC</span>' if sc.get("concentrated") else ''
        parts.append(
            f"<tr><td>Top sector</td><td>{escape(str(sc.get('top_sector') or '—'))} "
            f"({_fmt_pct(sc.get('top_pct'))})"
            f"{conc_badge}"
            f"</td></tr>"
        )
        parts.append(
            f"<tr><td>Missing closes / sector</td><td>{b.get('missing_closes',0)} / "
            f"{(sc.get('missing_sector') or 0)}</td></tr>"
        )
        parts.append("</tbody></table></div>")
    parts.append("</div></div>")

    # Snapshot summary
    ss = report.get("snapshot_summary") or {}
    parts.append('<div class="section"><h2>Forward-performance scaffold</h2>')
    parts.append(f'<p class="muted">Snapshots tracked: {report.get("snapshots_kept",0)} '
                 f'(retention {SNAPSHOT_RETENTION_DAYS} days). '
                 f'No lookahead — horizons resolved only after the trading-day count elapses.</p>')
    horizons = ss.get("horizons") or {}
    if horizons:
        parts.append('<table><thead><tr><th>Horizon</th><th>Completed</th><th>Pending</th>'
                     '<th>Bucket</th><th>Avg of mean returns</th><th>Wins / Losses</th>'
                     '</tr></thead><tbody>')
        for h, hor in horizons.items():
            for bname, agg in (hor.get("buckets") or {}).items():
                parts.append(
                    f"<tr><td>{escape(h)}</td>"
                    f"<td>{hor.get('completed',0)}</td>"
                    f"<td>{hor.get('pending',0)}</td>"
                    f"<td>{escape(bname)}</td>"
                    f"<td>{_fmt_pct(agg.get('avg_mean_return'))}</td>"
                    f"<td>{agg.get('wins',0)} / {agg.get('losses',0)}</td></tr>"
                )
            if not (hor.get("buckets") or {}):
                parts.append(
                    f"<tr><td>{escape(h)}</td><td>{hor.get('completed',0)}</td>"
                    f"<td>{hor.get('pending',0)}</td><td colspan=\"3\" class=\"muted\">"
                    f"no completed horizons yet</td></tr>"
                )
        parts.append("</tbody></table>")
    parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}"
    return escape(str(v))


def _fmt_pct(v):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v * 100:.2f}%"
    return escape(str(v))


def _fmt_bool(v):
    if v is None:
        return "—"
    return "yes" if v else "no"


# ---------- Entry point ----------


def main(argv: list[str] | None = None) -> int:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)

    fetch = os.environ.get("BENCHMARK_REVIEW_FETCH", "1") not in ("0", "false", "False", "")

    report, snapshots = build_report(
        rankings if isinstance(rankings, dict) else None,
        watchlist if isinstance(watchlist, dict) else None,
        fetch_benchmarks=fetch,
    )

    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    # snapshots already persisted inside build_report.
    _ = snapshots
    _stamp_task_if_present(report)

    print(f"[benchmark_review] buckets={len(report['buckets'])} "
          f"snapshots={report['snapshots_kept']} "
          f"benchmarks_available={(report.get('market_context') or {}).get('available')}")
    return 0


def _stamp_task_if_present(report: dict) -> None:
    """Stamp tasks.json's benchmark-review row with last_run/status/summary.

    Mirrors the data_quality_audit pattern. No-op if the row is absent.
    """
    tasks_path = DATA_DIR / "tasks.json"
    if not tasks_path.exists():
        return
    try:
        with tasks_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    findings = report.get("findings") or []
    overall = "warn" if findings else "OK"
    summary = _short_summary(report)
    changed = False
    for row in tasks:
        if isinstance(row, dict) and row.get("id") == "benchmark-review":
            row["last_run"] = report.get("as_of_rankings") or row.get("last_run") or "—"
            row["status"] = overall
            row["summary"] = summary
            row["report_url"] = "./reports/benchmark-review.html"
            changed = True
    if changed:
        tasks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _short_summary(report: dict) -> str:
    parts = []
    main25 = (report.get("buckets") or {}).get("main_top25") or {}
    rw = (main25.get("return_window") or {}).get("mean")
    if isinstance(rw, (int, float)):
        parts.append(f"main25 ~10d={rw * 100:+.1f}%")
    wl25 = (report.get("buckets") or {}).get("watchlist_top25") or {}
    rw2 = (wl25.get("return_window") or {}).get("mean")
    if isinstance(rw2, (int, float)):
        parts.append(f"wl25 ~10d={rw2 * 100:+.1f}%")
    findings = report.get("findings") or []
    if findings:
        parts.append(f"{len(findings)} concentration warning(s)")
    parts.append(f"snapshots={report.get('snapshots_kept', 0)}")
    return "; ".join(parts) if parts else "benchmark review generated"


if __name__ == "__main__":
    sys.exit(main())

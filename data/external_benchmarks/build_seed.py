"""Normalize raw benchmark captures into per-source JSON seed files.

Reads `raw/*.{json,md,csv}` and writes:

  - tradingview_2026-05-07.json
  - etrade_2026-05-07.json
  - fidelity_2026-05-07.json
  - zacks_2026-05-07.json
  - marketbeat_2026-05-07.json

Each output file has shape:

  {
    "source": "tradingview",
    "as_of_date": "2026-05-07",
    "rows": [
      {"ticker": "GS", "covered": true, "raw": {...}, "normalized": {...}},
      ...
    ]
  }

Run from the repo root:  python data/external_benchmarks/build_seed.py
This is a one-off normalizer — its outputs are the inputs the review report
consumes on every workflow run.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
AS_OF = "2026-05-07"


# ---------- Shared helpers ----------


def _safe_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("%", "").replace("$", "").replace(",", "")
        if not s or s.lower() in ("n/a", "none", "—", "na"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _label_to_5(label: str | None, mapping: dict[str, int]) -> int | None:
    if not isinstance(label, str):
        return None
    s = label.strip().lower()
    for k, v in mapping.items():
        if k.lower() == s:
            return v
    return None


# TradingView label -> 1..5 (5 = strongest bullish)
TV_LABEL_TO_5 = {
    "Strong Sell": 1,
    "Sell": 2,
    "Neutral": 3,
    "Buy": 4,
    "Strong Buy": 5,
}

# Fidelity label -> 1..5 (matches the documented ESS bucket cut-points)
FIDELITY_LABEL_TO_5 = {
    "Very Bearish": 1,
    "Bearish": 2,
    "Neutral": 3,
    "Bullish": 4,
    "Very Bullish": 5,
}

# Zacks Rank "1-Strong Buy" .. "5-Strong Sell" inverted to 5..1 bullish
ZACKS_RANK_TO_BULLISH = {
    1: 5,
    2: 4,
    3: 3,
    4: 2,
    5: 1,
}

# Zacks letter style score A..F -> 1..5 (5 best). I/NA = unknown.
ZACKS_LETTER_TO_5 = {
    "A": 5,
    "B": 4,
    "C": 3,
    "D": 2,
    "F": 1,
}

# MarketBeat consensus -> 1..5
MARKETBEAT_LABEL_TO_5 = {
    "Strong Sell": 1,
    "Sell": 2,
    "Reduce": 2,
    "Hold": 3,
    "Moderate Buy": 4,
    "Buy": 5,
    "Strong Buy": 5,
}


# ---------- TradingView ----------


def build_tradingview() -> dict:
    src = RAW / "tradingview_benchmark_comparison_20260507.json"
    data = json.loads(src.read_text())
    rows: list[dict] = []
    for r in data.get("rows", []):
        ticker = r.get("ticker")
        overall = r.get("tv_overall")
        osc = r.get("tv_oscillator")
        ma = r.get("tv_ma")
        overall_5 = _label_to_5(overall, TV_LABEL_TO_5)
        osc_5 = _label_to_5(osc, TV_LABEL_TO_5)
        ma_5 = _label_to_5(ma, TV_LABEL_TO_5)
        covered = overall_5 is not None
        rows.append({
            "ticker": ticker,
            "covered": covered,
            "raw": {
                "tv_symbol": r.get("tv_symbol"),
                "tv_overall_label": overall,
                "tv_oscillator_label": osc,
                "tv_ma_label": ma,
                "counts": r.get("counts"),
                "url": r.get("url"),
                "notes": r.get("notes"),
            },
            "normalized": {
                "overall_1to5": overall_5,
                "oscillator_1to5": osc_5,
                "ma_1to5": ma_5,
            },
        })
    return {"source": "tradingview", "as_of_date": AS_OF, "rows": rows}


# ---------- E*TRADE / LSEG ----------


def _lseg_to_bullish(avg: float | None) -> float | None:
    """LSEG 1=Strong Buy .. 5=Sell. Convert to a 1..5 bullish scale.

    Use 6 - avg to invert (so 1 -> 5 bullish, 5 -> 1 bullish).
    """
    if avg is None:
        return None
    return round(6 - avg, 3)


def build_etrade() -> dict:
    src = RAW / "etrade_benchmark_comparison_20260507.json"
    data = json.loads(src.read_text())
    rows: list[dict] = []
    for r in data.get("rows", []):
        avg = _safe_float(r.get("lseg_avg_1strong_5sell"))
        bullish = _lseg_to_bullish(avg)
        covered = bullish is not None
        rows.append({
            "ticker": r.get("ticker"),
            "covered": covered,
            "raw": {
                "etrade_symbol": r.get("etrade_symbol"),
                "lseg_avg_raw": avg,
                "tipranks_consensus": r.get("tipranks_consensus"),
                "morgan_stanley": r.get("morgan_stanley"),
                "smart_consensus": r.get("smart_consensus"),
                "marketedge_technical": r.get("marketedge_technical"),
                "lseg_counts": r.get("lseg_counts"),
                "notes": r.get("notes"),
            },
            "normalized": {
                "lseg_bullish_1to5": bullish,
            },
        })
    return {"source": "etrade", "as_of_date": AS_OF, "rows": rows}


# ---------- Fidelity ----------


def build_fidelity() -> dict:
    src = RAW / "fidelity_benchmark_full_comparison_20260507.json"
    data = json.loads(src.read_text())
    rows: list[dict] = []
    for r in data.get("rows", []):
        ess = _safe_float(r.get("fidelity_ess_0to10"))
        ess_1to5 = round(ess / 2, 3) if ess is not None else None
        label = r.get("fidelity_label")
        label_5 = _label_to_5(label, FIDELITY_LABEL_TO_5)
        covered = ess_1to5 is not None
        rows.append({
            "ticker": r.get("ticker"),
            "covered": covered,
            "raw": {
                "fidelity_ess_0to10": ess,
                "fidelity_label": label,
                "fidelity_color_flag": r.get("fidelity_color_flag"),
                "notes": r.get("notes"),
            },
            "normalized": {
                "ess_1to5": ess_1to5,
                "label_1to5": label_5,
            },
        })
    return {"source": "fidelity", "as_of_date": AS_OF, "rows": rows}


# ---------- Zacks ----------


def _parse_zacks_rank(s: str) -> int | None:
    """Parse '1-Strong Buy' / '3-Hold' / 'N/A' -> 1..5 or None."""
    if not isinstance(s, str):
        return None
    m = re.match(r"\s*([1-5])\s*[-–]", s)
    if m:
        return int(m.group(1))
    return None


def _parse_zacks_industry_pct(s: str) -> float | None:
    """Parse 'Top 41% (99/244)' or 'Bottom 18% (199/244)' -> percentile (higher=better)."""
    if not isinstance(s, str):
        return None
    m = re.search(r"(Top|Bottom)\s+(\d+)\s*%", s)
    if not m:
        return None
    direction = m.group(1).lower()
    pct = float(m.group(2))
    return 100 - pct if direction == "top" else pct


def build_zacks() -> dict:
    src = RAW / "zacks_benchmark_data.md"
    text = src.read_text()
    rows: list[dict] = []
    # Parse the markdown table.
    in_table = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("**"):
            continue
        if line.startswith("|--"):
            in_table = True
            continue
        if not in_table or not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 11:
            continue
        if cols[0].lower() == "ticker":
            continue
        ticker = cols[0]
        zacks_rank_str = cols[2]
        rank_n = _parse_zacks_rank(zacks_rank_str)
        bullish = ZACKS_RANK_TO_BULLISH.get(rank_n) if rank_n else None
        value_letter = cols[3]
        growth_letter = cols[4]
        momentum_letter = cols[5]
        vgm_letter = cols[6]
        ind_pct = _parse_zacks_industry_pct(cols[7])
        covered = bullish is not None
        rows.append({
            "ticker": ticker,
            "covered": covered,
            "raw": {
                "zacks_rank_label": zacks_rank_str,
                "value_score": value_letter,
                "growth_score": growth_letter,
                "momentum_score": momentum_letter,
                "vgm_score": vgm_letter,
                "industry_rank": cols[7],
                "earnings_esp": cols[8] if len(cols) > 8 else None,
                "url": cols[9] if len(cols) > 9 else None,
                "notes": cols[10] if len(cols) > 10 else None,
            },
            "normalized": {
                "zacks_rank_1to5": rank_n,
                "zacks_rank_bullish_1to5": bullish,
                "value_1to5": ZACKS_LETTER_TO_5.get(value_letter),
                "growth_1to5": ZACKS_LETTER_TO_5.get(growth_letter),
                "momentum_1to5": ZACKS_LETTER_TO_5.get(momentum_letter),
                "vgm_1to5": ZACKS_LETTER_TO_5.get(vgm_letter),
                "industry_percentile": ind_pct,
            },
        })
    return {"source": "zacks", "as_of_date": AS_OF, "rows": rows}


# ---------- MarketBeat ----------


def _parse_upside(s: str) -> float | None:
    """Parse '+21.5%' / '~-12.7%' / '-0.9%' -> percent (positive = upside)."""
    if not isinstance(s, str):
        return None
    s = s.strip().replace("~", "").replace("%", "")
    if not s or s.lower() in ("n/a", "none"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def build_marketbeat() -> dict:
    src = RAW / "marketbeat_benchmark_sample.md"
    text = src.read_text()
    rows: list[dict] = []
    in_table = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("**"):
            continue
        if line.startswith("|--"):
            in_table = True
            continue
        if not in_table or not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 8:
            continue
        if cols[0].lower() == "ticker":
            continue
        ticker = cols[0]
        consensus = cols[2]
        if consensus.lower() in ("n/a", "none", ""):
            consensus = None
        analyst_counts = cols[3]
        price_target = _safe_float(cols[4])
        upside = _parse_upside(cols[5])
        score = cols[6]
        url = cols[7] if len(cols) > 7 else None
        notes = cols[8] if len(cols) > 8 else None
        consensus_5 = _label_to_5(consensus, MARKETBEAT_LABEL_TO_5)
        covered = consensus_5 is not None
        rows.append({
            "ticker": ticker,
            "covered": covered,
            "raw": {
                "marketbeat_symbol": cols[1],
                "consensus_rating": consensus,
                "analyst_counts": analyst_counts,
                "price_target": price_target,
                "upside_pct_raw": cols[5],
                "marketbeat_score": score,
                "url": url,
                "notes": notes,
            },
            "normalized": {
                "consensus_1to5": consensus_5,
                "upside_pct": upside,
            },
        })
    return {"source": "marketbeat", "as_of_date": AS_OF, "rows": rows}


# ---------- main ----------


def main():
    builders = [
        ("tradingview", build_tradingview),
        ("etrade", build_etrade),
        ("fidelity", build_fidelity),
        ("zacks", build_zacks),
        ("marketbeat", build_marketbeat),
    ]
    for name, fn in builders:
        try:
            payload = fn()
        except Exception as e:
            print(f"ERROR building {name}: {type(e).__name__}: {e}", file=sys.stderr)
            raise
        out_path = HERE / f"{name}_{AS_OF}.json"
        out_path.write_text(json.dumps(payload, indent=2) + "\n")
        covered = sum(1 for r in payload["rows"] if r.get("covered"))
        print(f"[build_seed] {name}: {len(payload['rows'])} rows ({covered} covered) -> {out_path.name}")


if __name__ == "__main__":
    main()

"""Unit tests for apply_production_scoring_adjustment.py.

Run: python 02_Code/Python/Scoring_Engine/test_apply_production_scoring_adjustment.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import apply_production_scoring_adjustment as apa  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# --- _go_band ---------------------------------------------------------------

def test_go_band_clean_go():
    band, delta, norm = apa._go_band({"go_no_go_score_normalized": 0.85, "blockers": []})
    if band != "GO" or delta != apa.GO_BUMP_GO:
        fail(f"clean GO expected ({apa.GO_BUMP_GO}), got ({band},{delta})")


def test_go_band_wait_from_blocker():
    band, delta, _ = apa._go_band({"go_no_go_score_normalized": 0.9,
                                   "blockers": ["overextended_bb"]})
    if band != "WAIT" or delta != apa.GO_BUMP_WAIT:
        fail(f"blocker should force WAIT, got ({band},{delta})")


def test_go_band_weak():
    band, delta, _ = apa._go_band({"go_no_go_score_normalized": 0.2, "blockers": []})
    if band != "WEAK" or delta != apa.GO_BUMP_WEAK:
        fail(f"low norm should be WEAK, got ({band},{delta})")


def test_go_band_partial_is_wait():
    band, delta, _ = apa._go_band({"go_no_go_score_normalized": 0.5, "blockers": []})
    if band != "WAIT" or delta != apa.GO_BUMP_WAIT:
        fail(f"partial norm should be WAIT, got ({band},{delta})")


def test_go_band_missing_returns_none():
    band, delta, _ = apa._go_band(None)
    if band is not None or delta != apa.GO_BUMP_MISSING:
        fail(f"missing pine row should be no-op, got ({band},{delta})")


# --- _acc_band --------------------------------------------------------------

def test_acc_band_high():
    band, delta, _ = apa._acc_band({"score": 7.5})
    if band != "HIGH" or delta != apa.ACC_BUMP_HIGH:
        fail(f"HIGH expected, got ({band},{delta})")


def test_acc_band_low():
    band, delta, _ = apa._acc_band({"score": 2.0})
    if band != "LOW" or delta != apa.ACC_BUMP_LOW:
        fail(f"LOW expected, got ({band},{delta})")


def test_acc_band_mid():
    band, delta, _ = apa._acc_band({"score": 5.0})
    if band != "MID" or delta != apa.ACC_BUMP_MID:
        fail(f"MID expected, got ({band},{delta})")


def test_acc_band_missing():
    band, delta, _ = apa._acc_band(None)
    if band is not None or delta != apa.ACC_BUMP_MISSING:
        fail(f"missing acc row should be no-op, got ({band},{delta})")


# --- _act_multiplier_delta --------------------------------------------------

def test_act_delta_scaling():
    # +10% activity bump should produce +10% * ACT_WEIGHT on the production bump.
    act_row = {"activity_score": 8.8, "ai_score": 8.0}
    delta, components = apa._act_multiplier_delta(act_row, 8.0)
    expected = 0.10 * apa.ACT_WEIGHT
    if not approx(delta, expected):
        fail(f"expected ACT delta {expected}, got {delta}")
    if components is None or "raw_bump" not in components:
        fail("expected components dict")


def test_act_delta_missing_safe():
    delta, components = apa._act_multiplier_delta(None, 8.0)
    if delta != 0.0 or components is not None:
        fail(f"missing act row should yield 0, got ({delta},{components})")


# --- compute_row_adjustment -------------------------------------------------

def test_total_bump_capped_positive():
    # Stack everything maximally positive: ACT +15% raw -> +9% scaled,
    # GO +4%, ACC +4% = 17% raw, must cap to MAX_MULT-1 = +15%.
    act_row = {"activity_score": 11.5, "ai_score": 10.0}  # +15% raw
    pine_row = {"go_no_go_score_normalized": 0.85, "blockers": []}
    acc_row = {"score": 9.0}
    final, info = apa.compute_row_adjustment(10.0, act_row, pine_row, acc_row)
    if not approx(info["final_bump"], apa.MAX_MULT - 1.0):
        fail(f"expected positive cap, got final_bump {info['final_bump']}")
    if not info["capped"]:
        fail("expected capped flag true")
    if not approx(final, 10.0 * apa.MAX_MULT):
        fail(f"expected final {10.0 * apa.MAX_MULT}, got {final}")


def test_total_bump_capped_negative():
    act_row = {"activity_score": 8.5, "ai_score": 10.0}  # -15% raw
    pine_row = {"go_no_go_score_normalized": 0.1, "blockers": []}  # WEAK
    acc_row = {"score": 1.0}                                       # LOW
    final, info = apa.compute_row_adjustment(10.0, act_row, pine_row, acc_row)
    if not approx(info["final_bump"], apa.MIN_MULT - 1.0):
        fail(f"expected negative cap, got final_bump {info['final_bump']}")
    if not info["capped"]:
        fail("expected capped flag true")
    if not approx(final, 10.0 * apa.MIN_MULT):
        fail(f"expected final {10.0 * apa.MIN_MULT}, got {final}")


def test_base_score_zero_yields_zero():
    final, info = apa.compute_row_adjustment(0.0, None, None, None)
    if final != 0.0:
        fail(f"base 0 should stay 0, got {final}")
    if info["base_ai_score"] != 0.0:
        fail("base preserved as 0")


def test_neutral_inputs_neutral_output():
    # No ACT, partial pine in WAIT band (=> -0.02), MID acc (=> 0.0).
    final, info = apa.compute_row_adjustment(
        8.0,
        None,
        {"go_no_go_score_normalized": 0.5, "blockers": []},
        {"score": 5.0},
    )
    expected_bump = apa.GO_BUMP_WAIT  # -0.02
    if not approx(info["final_bump"], expected_bump):
        fail(f"expected bump {expected_bump}, got {info['final_bump']}")
    if not approx(final, round(8.0 * (1 + expected_bump), 2)):
        fail(f"final score wrong: {final}")


# --- apply_adjustment + idempotency ----------------------------------------

def test_apply_preserves_base_and_reranks():
    payload = {
        "rows": [
            {"rank": 1, "ticker": "AAA", "ai_score": 8.0},
            {"rank": 2, "ticker": "BBB", "ai_score": 7.9},
        ],
    }
    # BBB gets a +bump big enough to flip ordering (max +13% scaled+GO+ACC).
    act = {
        "AAA": {"activity_score": 8.0, "ai_score": 8.0},  # 0%
        "BBB": {"activity_score": 9.1, "ai_score": 7.9},  # +15% raw -> +9% scaled
    }
    pine = {"BBB": {"go_no_go_score_normalized": 0.85, "blockers": []}}  # +4%
    acc = {"BBB": {"score": 9.0}}                                         # +4%

    audit = apa.apply_adjustment(payload, act, pine, acc)
    if len(audit) != 2:
        fail(f"expected 2 audit rows, got {len(audit)}")

    by_ticker = {r["ticker"]: r for r in payload["rows"]}
    aaa = by_ticker["AAA"]
    bbb = by_ticker["BBB"]

    if aaa["base_ai_score"] != 8.0:
        fail(f"AAA base lost: {aaa.get('base_ai_score')}")
    if bbb["base_ai_score"] != 7.9:
        fail(f"BBB base lost: {bbb.get('base_ai_score')}")
    if aaa["ai_score"] != 8.0:
        fail(f"AAA should be unadjusted, got {aaa['ai_score']}")
    if bbb["ai_score"] <= bbb["base_ai_score"]:
        fail(f"BBB should be boosted: {bbb['ai_score']} vs base {bbb['base_ai_score']}")
    # BBB should have flipped to rank 1.
    if bbb["rank"] != 1 or aaa["rank"] != 2:
        fail(f"rerank failed: AAA={aaa['rank']}, BBB={bbb['rank']}")


def test_apply_is_idempotent():
    """Re-running adjustment on already-adjusted JSON must yield the same
    final ai_score (no compounding) — required so a workflow retry doesn't
    drift the score."""
    payload = {
        "rows": [
            {"rank": 1, "ticker": "AAA", "ai_score": 8.0},
        ],
    }
    act = {"AAA": {"activity_score": 8.4, "ai_score": 8.0}}  # +5% raw -> +3% scaled
    pine = {"AAA": {"go_no_go_score_normalized": 0.85, "blockers": []}}
    acc = {"AAA": {"score": 9.0}}

    apa.apply_adjustment(payload, act, pine, acc)
    first_score = payload["rows"][0]["ai_score"]
    first_base = payload["rows"][0]["base_ai_score"]

    # Now act / pine / acc still reference the ORIGINAL base, so the
    # adjuster must re-read base_ai_score (8.0) — not the just-written
    # ai_score — to avoid compounding.
    apa.apply_adjustment(payload, act, pine, acc)
    second_score = payload["rows"][0]["ai_score"]
    second_base = payload["rows"][0]["base_ai_score"]

    if not approx(first_score, second_score):
        fail(f"adjustment not idempotent: {first_score} -> {second_score}")
    if not approx(first_base, second_base):
        fail(f"base score drifted on replay: {first_base} -> {second_base}")
    if not approx(second_base, 8.0):
        fail(f"base score must remain original 8.0, got {second_base}")


def test_apply_handles_empty_rows():
    audit = apa.apply_adjustment({"rows": []}, {}, {}, {})
    if audit != []:
        fail("empty rows should produce empty audit")
    audit = apa.apply_adjustment(None, {}, {}, {})
    if audit != []:
        fail("None payload should produce empty audit")


# --- EXT exclusion -----------------------------------------------------------

def test_no_ext_input_path():
    """Sanity check: the adjuster signature does not accept any ext lookup
    and no part of the pipeline pulls from external_benchmark_review.json.
    If someone wires EXT in here later, this test reminds them to revisit
    the user agreement before doing so."""
    import inspect
    sig = inspect.signature(apa.compute_row_adjustment)
    params = set(sig.parameters)
    if params != {"base_score", "act_row", "pine_row", "acc_row"}:
        fail(f"compute_row_adjustment signature changed; review EXT exclusion: {params}")
    # Strip docstrings/comments before scanning so doc explanations of "we
    # deliberately exclude EXT" don't trip the guard — what we care about
    # is no executable reference to EXT artifacts.
    src = (HERE / "apply_production_scoring_adjustment.py").read_text()
    code_lines = []
    in_docstring = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # Toggle on opener; treat single-line docstrings as fully skipped.
            if stripped.count('"""') == 2 or stripped.count("'''") == 2:
                continue
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    for needle in ("external_benchmark_review", "disagreement_queue", "ext_rating",
                   "extByTicker", "external_benchmarks"):
        if needle in code:
            fail(f"unexpected EXT reference '{needle}' found in adjuster (executable code)")


# --- build_change_review -----------------------------------------------------

def test_change_review_detects_movers():
    before = [
        {"rank": 1, "ticker": "AAA", "ai_score": 9.0, "base_ai_score": 9.0, "sector": "Tech"},
        {"rank": 2, "ticker": "BBB", "ai_score": 8.0, "base_ai_score": 8.0, "sector": "Tech"},
        {"rank": 3, "ticker": "CCC", "ai_score": 7.5, "base_ai_score": 7.5, "sector": "Energy"},
    ]
    after = [
        {"rank": 1, "ticker": "BBB", "ai_score": 9.2, "base_ai_score": 8.0,
         "go_label": "GO", "acc_label": "HIGH", "adjustment_bump": 0.15, "sector": "Tech"},
        {"rank": 2, "ticker": "AAA", "ai_score": 9.0, "base_ai_score": 9.0,
         "go_label": None, "acc_label": None, "adjustment_bump": 0.0, "sector": "Tech"},
        {"rank": 3, "ticker": "CCC", "ai_score": 7.5, "base_ai_score": 7.5,
         "go_label": "WAIT", "acc_label": "MID", "adjustment_bump": -0.02, "sector": "Energy"},
    ]
    review = apa.build_change_review(before, after, [], [])
    bbb_mover = next(m for m in review["biggest_movers"] if m["ticker"] == "BBB")
    if bbb_mover["rank_delta"] != 1:
        fail(f"BBB should be +1, got {bbb_mover['rank_delta']}")
    if review["verdict"] not in ("OK", "REVIEW"):
        fail(f"unexpected verdict {review['verdict']}")
    if review["summary"]["rows_processed"] != 3:
        fail("rows_processed wrong")


def test_change_review_flags_extreme_moves():
    before = [{"rank": i, "ticker": f"T{i:03d}", "ai_score": 10 - i*0.01,
               "base_ai_score": 10 - i*0.01, "sector": "S"} for i in range(1, 101)]
    after = before[:]
    # Move T001 (rank 1) all the way to rank 100 — change of -99 places, well
    # past the 50-place extreme threshold.
    moved = after.pop(0)
    after.append({**moved, "rank": 100, "go_label": None, "acc_label": None,
                  "adjustment_bump": 0.0})
    for i, r in enumerate(after, 1):
        r["rank"] = i
    review = apa.build_change_review(before, after, [], [])
    if "extreme_moves" not in " ".join(review["guardrail_flags"]):
        fail(f"expected extreme_moves flag, got {review['guardrail_flags']}")
    if review["verdict"] != "REVIEW":
        fail(f"expected REVIEW verdict, got {review['verdict']}")


# --- End-to-end smoke (uses temp files) -------------------------------------

def test_end_to_end_writes_expected_files(tmp_root=None):
    """Drive main() against a temp REPO_ROOT-style directory and verify
    that rankings.json gains a base_ai_score column and that the change
    review artifact is written."""
    tmp = Path(tempfile.mkdtemp(prefix="apa_test_"))
    try:
        (tmp / "data" / "reports").mkdir(parents=True)
        rankings = {
            "as_of": "2026-05-28 09:00 CDT",
            "universe": "test",
            "rows": [
                {"rank": 1, "ticker": "AAA", "ai_score": 8.0,
                 "company": "A Co", "sector": "Tech", "volume_millions": 10,
                 "closes": [100.0]},
                {"rank": 2, "ticker": "BBB", "ai_score": 7.5,
                 "company": "B Co", "sector": "Energy", "volume_millions": 20,
                 "closes": [50.0]},
            ],
        }
        (tmp / "data" / "rankings.json").write_text(json.dumps(rankings))
        activity = {
            "rows": [
                {"ticker": "AAA", "ai_score": 8.0, "activity_score": 8.4,
                 "liquidity_bump": 0.02, "relvol_bump": 0.0, "pine_bump": 0.03,
                 "overextended_bb": False},
                {"ticker": "BBB", "ai_score": 7.5, "activity_score": 7.3,
                 "liquidity_bump": -0.02, "relvol_bump": 0.0, "pine_bump": 0.0,
                 "overextended_bb": False},
            ],
            "watchlist_rows": [],
        }
        (tmp / "data" / "reports" / "activity_adjusted_review.json").write_text(json.dumps(activity))
        pine = {
            "per_ticker": [
                {"ticker": "AAA", "evaluated": True,
                 "go_no_go_score_normalized": 0.8, "blockers": []},
                {"ticker": "BBB", "evaluated": True,
                 "go_no_go_score_normalized": 0.2, "blockers": []},
            ],
        }
        (tmp / "data" / "reports" / "pine_go_no_go_diagnostic.json").write_text(json.dumps(pine))
        acc = {"rows": [
            {"ticker": "AAA", "score": 8.0},
            {"ticker": "BBB", "score": 2.0},
        ]}
        (tmp / "data" / "reports" / "accumulation_signal_meter.json").write_text(json.dumps(acc))

        # Monkey-patch module paths and call main().
        saved = {
            "REPO_ROOT": apa.REPO_ROOT,
            "DATA_DIR": apa.DATA_DIR,
            "DATA_REPORTS_DIR": apa.DATA_REPORTS_DIR,
            "RANKINGS_FILE": apa.RANKINGS_FILE,
            "WATCHLIST_FILE": apa.WATCHLIST_FILE,
            "ACTIVITY_FILE": apa.ACTIVITY_FILE,
            "PINE_FILE": apa.PINE_FILE,
            "ACC_FILE": apa.ACC_FILE,
            "CHANGE_REVIEW_FILE": apa.CHANGE_REVIEW_FILE,
        }
        try:
            apa.REPO_ROOT = tmp
            apa.DATA_DIR = tmp / "data"
            apa.DATA_REPORTS_DIR = tmp / "data" / "reports"
            apa.RANKINGS_FILE = tmp / "data" / "rankings.json"
            apa.WATCHLIST_FILE = tmp / "data" / "watchlist_rankings.json"
            apa.ACTIVITY_FILE = tmp / "data" / "reports" / "activity_adjusted_review.json"
            apa.PINE_FILE = tmp / "data" / "reports" / "pine_go_no_go_diagnostic.json"
            apa.ACC_FILE = tmp / "data" / "reports" / "accumulation_signal_meter.json"
            apa.CHANGE_REVIEW_FILE = tmp / "data" / "reports" / "production_scoring_change_review.json"

            rc = apa.main()
            if rc != 0:
                fail(f"main() returned {rc}")
        finally:
            for k, v in saved.items():
                setattr(apa, k, v)

        out = json.loads((tmp / "data" / "rankings.json").read_text())
        for r in out["rows"]:
            if "base_ai_score" not in r:
                fail(f"row {r['ticker']} missing base_ai_score")
            if "adjustment_bump" not in r:
                fail(f"row {r['ticker']} missing adjustment_bump")
        if "production_adjustment" not in out:
            fail("production_adjustment metadata missing on rankings.json")
        review = json.loads((tmp / "data" / "reports" / "production_scoring_change_review.json").read_text())
        for key in ("verdict", "constants", "summary", "top_before",
                    "top_after", "biggest_movers"):
            if key not in review:
                fail(f"change review missing {key}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- Table column / tooltip presence in index.html -------------------------

def test_index_html_keeps_diag_columns():
    """index.html and watchlist.html must still expose ACT/ACT Δ/GO/ACC/EXT
    columns under Diagnostics, and the AI cell must use the new aiCell helper."""
    root = HERE.parent.parent.parent
    for page in ("index.html", "watchlist.html"):
        html = (root / page).read_text()
        for needle in (">ACT<", ">ACT Δ<", ">GO<", ">ACC<", ">EXT<"):
            if needle not in html:
                fail(f"{page} missing column header {needle!r}")
        if "aiCell(r)" not in html:
            fail(f"{page} should render AI via aiCell to show base_ai_score tooltip")
        if "base_ai_score" not in html:
            fail(f"{page} should reference base_ai_score for tooltips")
        # The old "DOES NOT change" wording should be gone for ACT/GO/ACC
        # (kept only for EXT which remains diagnostic-only).
        diag_block = html.split("Diagnostics")[1].split("</tr>")[0]
        for needle in ("ACT", "GO", "ACC"):
            # Only fail if "DOES NOT change" appears next to one of ACT/GO/ACC
            # tooltips — we leave the EXT tooltip alone.
            for line in diag_block.splitlines():
                if (needle + "<") in line and "DOES NOT" in line:
                    fail(f"{page}: stale wording near {needle} tooltip: {line.strip()}")


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()

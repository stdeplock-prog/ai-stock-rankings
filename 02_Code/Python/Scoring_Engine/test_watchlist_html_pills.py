"""Static check that watchlist.html no longer renders TV/CSV/BOTH source
pills inline in the table rows, while still preserving:

  * the source filter dropdown (CSV / TradingView / Both)
  * the SUPP data-quality pill rendering path
  * the export CSV "Source" column

The check is intentionally a string scan over the rendered file (no JS
runtime needed in CI), focused on signals that would regress if someone
re-introduced the TV/CSV/BOTH inline pills.

Run: python 02_Code/Python/Scoring_Engine/test_watchlist_html_pills.py
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
WATCHLIST_HTML = os.path.join(REPO_ROOT, "watchlist.html")


def _read():
    with open(WATCHLIST_HTML, "r", encoding="utf-8") as f:
        return f.read()


def test_no_tv_csv_both_text_in_srcpill_function():
    """The srcPill JS function must not emit literal 'TV', 'CSV', or
    'BOTH' tokens. SUPP is allowed (it's a data-quality indicator)."""
    html = _read()
    m = re.search(r"function srcPill\([^)]*\)\s*\{(.+?)\n\s*\}", html, re.DOTALL)
    assert m, "srcPill function not found in watchlist.html"
    body = m.group(1)
    for forbidden in ("'TV'", "'CSV'", "'BOTH'", '"TV"', '"CSV"', '"BOTH"'):
        assert forbidden not in body, (
            f"srcPill still emits {forbidden}; origin pill should be hidden")
    # The SUPP path must remain so we keep surfacing supplemental fetches.
    assert "SUPP" in body, "SUPP pill rendering removed unexpectedly"


def test_source_filter_options_still_present():
    """We hid the inline pills, not the ability to filter by source."""
    html = _read()
    for opt in ('id="sourceFilter"', 'value="csv"',
                'value="tradingview"', 'value="both"'):
        assert opt in html, f"missing source filter element: {opt}"


def test_export_csv_still_includes_source_column():
    html = _read()
    # Header row of the CSV export must include a Source column.
    assert "Sector,Industry,Source" in html or ",Source" in html, (
        "Export CSV header missing Source column"
    )
    # And the row mapper must still emit r.source.
    assert "r.source" in html, "Export CSV body no longer references r.source"


if __name__ == "__main__":
    failed = 0
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in funcs:
        try:
            fn()
        except AssertionError as exc:
            print(f"FAIL  {fn.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"OK    {fn.__name__}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)

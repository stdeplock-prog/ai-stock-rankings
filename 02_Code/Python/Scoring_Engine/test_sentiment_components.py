"""Unit tests for sentiment_components.py.

Run: python 02_Code/Python/Scoring_Engine/test_sentiment_components.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sentiment_components import (  # noqa: E402
    RSI_MIN_WEIGHT,
    MIN_ANALYSTS,
    rsi_sentiment,
    analyst_sentiment,
    upside_sentiment,
    news_sentiment,
    blended_sentiment,
)


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


# -------------------- rsi_sentiment --------------------

def test_rsi_sentiment_anchors():
    assert _close(rsi_sentiment(30), 0.0)
    assert _close(rsi_sentiment(50), 5.0)
    assert _close(rsi_sentiment(70), 10.0)


def test_rsi_sentiment_clipping():
    assert rsi_sentiment(10) == 0.0
    assert rsi_sentiment(95) == 10.0


def test_rsi_sentiment_missing_neutral():
    assert rsi_sentiment(None) == 5.0
    assert rsi_sentiment(float("nan")) == 5.0


# -------------------- analyst_sentiment --------------------

def test_analyst_sentiment_anchors():
    assert _close(analyst_sentiment(1.0, 10), 10.0)
    assert _close(analyst_sentiment(3.0, 10), 5.0)
    assert _close(analyst_sentiment(5.0, 10), 0.0)


def test_analyst_sentiment_below_min_analysts():
    # Single-analyst coverage is too thin to act on
    assert analyst_sentiment(1.5, MIN_ANALYSTS - 1) is None


def test_analyst_sentiment_missing_returns_none():
    assert analyst_sentiment(None, 10) is None
    assert analyst_sentiment(2.0, None) is None
    assert analyst_sentiment(float("nan"), 10) is None
    assert analyst_sentiment(2.0, float("nan")) is None


def test_analyst_sentiment_out_of_range_returns_none():
    # Bad upstream data: defensive None instead of garbage
    assert analyst_sentiment(0.5, 10) is None
    assert analyst_sentiment(6.0, 10) is None


# -------------------- upside_sentiment --------------------

def test_upside_sentiment_anchors():
    assert _close(upside_sentiment(0.0, 5), 5.0)
    assert _close(upside_sentiment(30.0, 5), 10.0)
    assert _close(upside_sentiment(-20.0, 5), 0.0)


def test_upside_sentiment_clipping():
    assert upside_sentiment(100.0, 5) == 10.0
    assert upside_sentiment(-50.0, 5) == 0.0


def test_upside_sentiment_midpoints():
    assert _close(upside_sentiment(15.0, 5), 7.5)
    assert _close(upside_sentiment(-10.0, 5), 2.5)


def test_upside_sentiment_below_min_analysts():
    assert upside_sentiment(15.0, MIN_ANALYSTS - 1) is None


def test_upside_sentiment_missing_returns_none():
    assert upside_sentiment(None, 5) is None
    assert upside_sentiment(10.0, None) is None
    assert upside_sentiment(float("nan"), 5) is None


# -------------------- news_sentiment --------------------

def test_news_sentiment_scale():
    assert _close(news_sentiment(0), 0.0)
    assert _close(news_sentiment(50), 5.0)
    assert _close(news_sentiment(100), 10.0)


def test_news_sentiment_missing():
    assert news_sentiment(None) is None
    assert news_sentiment(float("nan")) is None


# -------------------- blended_sentiment --------------------

def test_blend_no_external_matches_rsi():
    """When all external signals are missing, output equals RSI sentiment exactly."""
    s, src = blended_sentiment(7.0, None, None, None)
    assert _close(s, 7.0)
    assert src == "rsi_only"


def test_blend_all_external_present():
    # rsi=5, analyst=10, upside=10, news=10 -> 0.5*5 + 0.2*10 + 0.2*10 + 0.1*10 = 7.5
    s, src = blended_sentiment(5.0, 10.0, 10.0, 10.0)
    assert _close(s, 7.5)
    assert src == "rsi+analyst+upside+news"


def test_blend_rsi_keeps_min_weight():
    """RSI weight never drops below RSI_MIN_WEIGHT even when external signals are present."""
    # All bullish: rsi=0, analyst=10, upside=10. RSI weight is 0.5+0.1=0.6 (news missing).
    # Expected: 0.6*0 + 0.2*10 + 0.2*10 = 4.0
    s, _src = blended_sentiment(0.0, 10.0, 10.0, None)
    assert _close(s, 4.0)
    # Sanity: RSI weight is at least RSI_MIN_WEIGHT
    assert RSI_MIN_WEIGHT >= 0.5


def test_blend_missing_components_shift_weight_to_rsi():
    # Only analyst present: rsi gets 0.5 + (0.2 + 0.1) = 0.8, analyst gets 0.2
    # rsi=5, analyst=10 -> 0.8*5 + 0.2*10 = 6.0
    s, src = blended_sentiment(5.0, 10.0, None, None)
    assert _close(s, 6.0)
    assert src == "rsi+analyst"


def test_blend_source_labels():
    _, src = blended_sentiment(5.0, 5.0, 5.0, None)
    assert src == "rsi+analyst+upside"
    _, src = blended_sentiment(5.0, None, 5.0, None)
    assert src == "rsi+upside"
    _, src = blended_sentiment(5.0, None, None, 5.0)
    assert src == "rsi+news"


def test_blend_output_clipped():
    s, _src = blended_sentiment(10.0, 10.0, 10.0, 10.0)
    assert 0.0 <= s <= 10.0
    s, _src = blended_sentiment(0.0, 0.0, 0.0, 0.0)
    assert 0.0 <= s <= 10.0


def test_blend_weights_sum_to_one():
    """Sanity: effective weights always sum to 1.0 (no penalty drift)."""
    # We can't directly inspect weights, but for uniform inputs the blended
    # score must equal that uniform value.
    for v in (0.0, 2.5, 5.0, 7.5, 10.0):
        s, _src = blended_sentiment(v, v, v, v)
        assert _close(s, v), f"uniform {v} -> {s}"
        s, _src = blended_sentiment(v, v, None, None)
        assert _close(s, v), f"partial {v} -> {s}"
        s, _src = blended_sentiment(v, None, None, None)
        assert _close(s, v), f"rsi-only {v} -> {s}"


def test_blend_legacy_compat_when_catalysts_absent():
    """Old behaviour: Sentiment was (RSI-30)/4. Verify that's preserved when
    no external signals are present, for the full RSI range."""
    for rsi in (20, 30, 40, 50, 60, 70, 80):
        legacy = min(10.0, max(0.0, (rsi - 30) / 4.0))
        s, src = blended_sentiment(rsi_sentiment(rsi), None, None, None)
        assert _close(s, legacy), f"rsi={rsi} legacy={legacy} new={s}"
        assert src == "rsi_only"


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)

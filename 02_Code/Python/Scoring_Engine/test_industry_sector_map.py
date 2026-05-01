"""Tests for industry_sector_map.resolve_sector and the INDUSTRY_TO_SECTOR table.

Run: python 02_Code/Python/Scoring_Engine/test_industry_sector_map.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from industry_sector_map import INDUSTRY_TO_SECTOR, resolve_sector


def test_yf_sector_wins_over_universe_and_industry():
    out = resolve_sector(
        yf_sector="Energy",
        universe_sector="Industrials",
        industry="Software - Application",
    )
    assert out == "Energy", out


def test_universe_sector_used_when_yf_blank():
    out = resolve_sector(
        yf_sector=None,
        universe_sector="Health Care",
        industry="REIT - Mortgage",
    )
    assert out == "Health Care", out


def test_universe_na_treated_as_blank():
    out = resolve_sector(
        yf_sector=None,
        universe_sector="N/A",
        industry="Banks - Regional",
    )
    assert out == "Financials", out


def test_industry_fallback_for_each_observed_industry():
    # Industries actually observed missing in production rankings.json today.
    expected = {
        "REIT - Mortgage":                    "Real Estate",
        "Metal Fabrication":                  "Industrials",
        "Electronics & Computer Distribution":"Information Technology",
        "Banks - Regional":                   "Financials",
        "Insurance - Reinsurance":            "Financials",
        "Engineering & Construction":         "Industrials",
        "Insurance - Specialty":              "Financials",
        "Steel":                              "Materials",
        "Oil & Gas E&P":                      "Energy",
        "Industrial Distribution":            "Industrials",
        "REIT - Healthcare Facilities":       "Real Estate",
        "Medical Devices":                    "Health Care",
        "Insurance - Life":                   "Financials",
        "Oil & Gas Equipment & Services":     "Energy",
        "Asset Management":                   "Financials",
        "Capital Markets":                    "Financials",
        "Auto Parts":                         "Consumer Discretionary",
        "Specialty Retail":                   "Consumer Discretionary",
        "Software - Application":             "Information Technology",
        "Telecom Services":                   "Communication Services",
        "Travel Services":                    "Consumer Discretionary",
        "Marine Shipping":                    "Industrials",
        "Rental & Leasing Services":          "Industrials",
        "Electrical Equipment & Parts":       "Industrials",
        "Oil & Gas Midstream":                "Energy",
        "REIT - Specialty":                   "Real Estate",
        "REIT - Industrial":                  "Real Estate",
    }
    for industry, sector in expected.items():
        out = resolve_sector(
            yf_sector=None, universe_sector="N/A", industry=industry,
        )
        assert out == sector, f"{industry!r} -> {out!r}, expected {sector!r}"


def test_unknown_industry_returns_blank():
    out = resolve_sector(
        yf_sector=None,
        universe_sector=None,
        industry="Some Industry That Does Not Exist",
    )
    assert out == "", out


def test_all_blank_returns_blank():
    assert resolve_sector(None, None, None) == ""
    assert resolve_sector("", "", "") == ""
    assert resolve_sector("nan", "N/A", "none") == ""


def test_mapping_table_values_are_canonical_sectors():
    valid = {
        "Communication Services", "Consumer Discretionary", "Consumer Staples",
        "Energy", "Financials", "Health Care", "Industrials",
        "Information Technology", "Materials", "Real Estate", "Utilities",
    }
    bad = [(k, v) for k, v in INDUSTRY_TO_SECTOR.items() if v not in valid]
    assert not bad, f"Non-canonical sector values: {bad}"


if __name__ == "__main__":
    test_yf_sector_wins_over_universe_and_industry()
    test_universe_sector_used_when_yf_blank()
    test_universe_na_treated_as_blank()
    test_industry_fallback_for_each_observed_industry()
    test_unknown_industry_returns_blank()
    test_all_blank_returns_blank()
    test_mapping_table_values_are_canonical_sectors()
    print("All industry_sector_map tests passed.")

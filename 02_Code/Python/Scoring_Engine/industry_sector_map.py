"""industry_sector_map.py

Deterministic yfinance-industry -> GICS-style-sector mapping.

The master universe CSV has many tickers with Sector="N/A" (NDX100 plain-table
scrape, Wikipedia fallback for Russell 1000), and yfinance fundamentals
sometimes return a populated `industry` but a missing `sector`. This module
provides a transparent fallback so downstream consumers (rankings.json,
data quality audit) can still classify a row by sector.

Mapping is intentionally conservative: every key is a yfinance industry
string, and the value is the standard GICS sector name used by S&P 500.
Industries without an unambiguous sector are omitted; callers fall back to
"" (empty) rather than guess.
"""

INDUSTRY_TO_SECTOR = {
    # Communication Services
    "Advertising Agencies":               "Communication Services",
    "Broadcasting":                       "Communication Services",
    "Electronic Gaming & Multimedia":     "Communication Services",
    "Entertainment":                      "Communication Services",
    "Internet Content & Information":     "Communication Services",
    "Publishing":                         "Communication Services",
    "Telecom Services":                   "Communication Services",

    # Consumer Discretionary
    "Apparel Manufacturing":              "Consumer Discretionary",
    "Apparel Retail":                     "Consumer Discretionary",
    "Auto & Truck Dealerships":           "Consumer Discretionary",
    "Auto Manufacturers":                 "Consumer Discretionary",
    "Auto Parts":                         "Consumer Discretionary",
    "Department Stores":                  "Consumer Discretionary",
    "Footwear & Accessories":             "Consumer Discretionary",
    "Furnishings, Fixtures & Appliances": "Consumer Discretionary",
    "Gambling":                           "Consumer Discretionary",
    "Home Improvement Retail":            "Consumer Discretionary",
    "Internet Retail":                    "Consumer Discretionary",
    "Leisure":                            "Consumer Discretionary",
    "Lodging":                            "Consumer Discretionary",
    "Luxury Goods":                       "Consumer Discretionary",
    "Packaging & Containers":             "Consumer Discretionary",
    "Personal Services":                  "Consumer Discretionary",
    "Recreational Vehicles":              "Consumer Discretionary",
    "Residential Construction":           "Consumer Discretionary",
    "Resorts & Casinos":                  "Consumer Discretionary",
    "Restaurants":                        "Consumer Discretionary",
    "Specialty Retail":                   "Consumer Discretionary",
    "Textile Manufacturing":              "Consumer Discretionary",
    "Travel Services":                    "Consumer Discretionary",

    # Consumer Staples
    "Beverages - Brewers":                "Consumer Staples",
    "Beverages - Non-Alcoholic":          "Consumer Staples",
    "Beverages - Wineries & Distilleries":"Consumer Staples",
    "Confectioners":                      "Consumer Staples",
    "Discount Stores":                    "Consumer Staples",
    "Education & Training Services":      "Consumer Staples",
    "Farm Products":                      "Consumer Staples",
    "Food Distribution":                  "Consumer Staples",
    "Grocery Stores":                     "Consumer Staples",
    "Household & Personal Products":      "Consumer Staples",
    "Packaged Foods":                     "Consumer Staples",
    "Tobacco":                            "Consumer Staples",

    # Energy
    "Oil & Gas Drilling":                 "Energy",
    "Oil & Gas E&P":                      "Energy",
    "Oil & Gas Equipment & Services":     "Energy",
    "Oil & Gas Integrated":                "Energy",
    "Oil & Gas Midstream":                "Energy",
    "Oil & Gas Refining & Marketing":     "Energy",
    "Thermal Coal":                       "Energy",
    "Uranium":                            "Energy",

    # Financials
    "Asset Management":                   "Financials",
    "Banks - Diversified":                "Financials",
    "Banks - Regional":                   "Financials",
    "Capital Markets":                    "Financials",
    "Credit Services":                    "Financials",
    "Financial Conglomerates":            "Financials",
    "Financial Data & Stock Exchanges":   "Financials",
    "Insurance - Diversified":            "Financials",
    "Insurance - Life":                   "Financials",
    "Insurance - Property & Casualty":    "Financials",
    "Insurance - Reinsurance":            "Financials",
    "Insurance - Specialty":              "Financials",
    "Insurance Brokers":                  "Financials",
    "Mortgage Finance":                   "Financials",
    "Shell Companies":                    "Financials",

    # Health Care
    "Biotechnology":                      "Health Care",
    "Diagnostics & Research":             "Health Care",
    "Drug Manufacturers - General":       "Health Care",
    "Drug Manufacturers - Specialty & Generic": "Health Care",
    "Health Information Services":        "Health Care",
    "Healthcare Plans":                   "Health Care",
    "Medical Care Facilities":            "Health Care",
    "Medical Devices":                    "Health Care",
    "Medical Distribution":               "Health Care",
    "Medical Instruments & Supplies":     "Health Care",
    "Pharmaceutical Retailers":           "Health Care",

    # Industrials
    "Aerospace & Defense":                "Industrials",
    "Airlines":                           "Industrials",
    "Airports & Air Services":            "Industrials",
    "Building Products & Equipment":      "Industrials",
    "Business Equipment & Supplies":      "Industrials",
    "Conglomerates":                      "Industrials",
    "Consulting Services":                "Industrials",
    "Electrical Equipment & Parts":       "Industrials",
    "Engineering & Construction":         "Industrials",
    "Farm & Heavy Construction Machinery":"Industrials",
    "Industrial Distribution":            "Industrials",
    "Infrastructure Operations":          "Industrials",
    "Integrated Freight & Logistics":     "Industrials",
    "Marine Shipping":                    "Industrials",
    "Metal Fabrication":                  "Industrials",
    "Pollution & Treatment Controls":     "Industrials",
    "Railroads":                          "Industrials",
    "Rental & Leasing Services":          "Industrials",
    "Security & Protection Services":     "Industrials",
    "Specialty Business Services":        "Industrials",
    "Specialty Industrial Machinery":     "Industrials",
    "Staffing & Employment Services":     "Industrials",
    "Tools & Accessories":                "Industrials",
    "Trucking":                           "Industrials",
    "Waste Management":                   "Industrials",

    # Information Technology
    "Communication Equipment":            "Information Technology",
    "Computer Hardware":                  "Information Technology",
    "Consumer Electronics":               "Information Technology",
    "Electronic Components":              "Information Technology",
    "Electronics & Computer Distribution":"Information Technology",
    "Information Technology Services":    "Information Technology",
    "Scientific & Technical Instruments": "Information Technology",
    "Semiconductor Equipment & Materials":"Information Technology",
    "Semiconductors":                     "Information Technology",
    "Software - Application":             "Information Technology",
    "Software - Infrastructure":          "Information Technology",
    "Solar":                              "Information Technology",

    # Materials
    "Agricultural Inputs":                "Materials",
    "Aluminum":                           "Materials",
    "Building Materials":                 "Materials",
    "Chemicals":                          "Materials",
    "Coking Coal":                        "Materials",
    "Copper":                             "Materials",
    "Gold":                               "Materials",
    "Lumber & Wood Production":           "Materials",
    "Other Industrial Metals & Mining":   "Materials",
    "Other Precious Metals & Mining":     "Materials",
    "Paper & Paper Products":             "Materials",
    "Silver":                             "Materials",
    "Specialty Chemicals":                "Materials",
    "Steel":                              "Materials",

    # Real Estate
    "Real Estate - Development":          "Real Estate",
    "Real Estate - Diversified":          "Real Estate",
    "Real Estate Services":               "Real Estate",
    "REIT - Diversified":                 "Real Estate",
    "REIT - Healthcare Facilities":       "Real Estate",
    "REIT - Hotel & Motel":               "Real Estate",
    "REIT - Industrial":                  "Real Estate",
    "REIT - Mortgage":                    "Real Estate",
    "REIT - Office":                      "Real Estate",
    "REIT - Residential":                 "Real Estate",
    "REIT - Retail":                      "Real Estate",
    "REIT - Specialty":                   "Real Estate",

    # Utilities
    "Utilities - Diversified":            "Utilities",
    "Utilities - Independent Power Producers": "Utilities",
    "Utilities - Regulated Electric":     "Utilities",
    "Utilities - Regulated Gas":          "Utilities",
    "Utilities - Regulated Water":        "Utilities",
    "Utilities - Renewable":              "Utilities",
}


def _is_blank(val):
    if val is None:
        return True
    s = str(val).strip()
    return s == "" or s.lower() in ("nan", "none", "n/a")


def resolve_sector(yf_sector=None, universe_sector=None, industry=None):
    """Return the best-effort sector string given the available signals.

    Preference order:
      1. yfinance `sector` (most accurate, ticker-specific)
      2. universe `Sector` (S&P 500 GICS Sector or IWB Sector)
      3. INDUSTRY_TO_SECTOR[industry] (deterministic fallback)
      4. "" (caller decides what to render)
    """
    if not _is_blank(yf_sector):
        return str(yf_sector).strip()
    if not _is_blank(universe_sector):
        return str(universe_sector).strip()
    if not _is_blank(industry):
        mapped = INDUSTRY_TO_SECTOR.get(str(industry).strip())
        if mapped:
            return mapped
    return ""

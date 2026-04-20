# AI Stock Rankings Dashboard

**Live Dashboard**: [https://stdeplock-prog.github.io/ai-stock-rankings/](https://stdeplock-prog.github.io/ai-stock-rankings/)

Automated stock ranking system that combines fundamental, technical, and sentiment analysis to generate AI-powered scores for S&P 500 and NASDAQ-100 stocks. Rankings update automatically 3x daily via GitHub Actions.

## Features

- **AI Composite Scores**: Weighted scoring combining fundamental strength, technical momentum, sentiment, and risk metrics
- **Live Dashboard**: Real-time sortable table with color-coded AI scores and rank-change indicators
- **Automated Updates**: Scheduled runs at 8:45 AM, 12:30 PM, and 3:35 PM CST (Monday-Friday)
- **VS Open Tracking**: Green/red arrows show how each stock's rank moved since the day's first run
- **Sector Filtering**: Filter by industry sector or search by ticker/company name
- **Next-Run Countdown**: Dashboard header shows time until next scheduled refresh
- **Export to CSV**: Download full rankings data for offline analysis

## Architecture

### Workflow (GitHub Actions)

**File**: `.github/workflows/update-rankings.yml`

#### Job 1: `update-rankings`
1. Fetches OHLCV price data from EODHD API
2. Downloads fundamentals and indicators (RSI, MACD, moving averages)
3. Runs sentiment analysis placeholder (expandable to news/social APIs)
4. Scores each stock on 4 dimensions:
   - Fundamental (earnings growth, P/E, margins)
   - Technical (trend strength, momentum)
   - Sentiment (news/social signals)
   - Risk (volatility, drawdown)
5. Generates `rankings.csv` → exports to `data/rankings.json`
6. Uploads `data/` directory as GitHub Pages artifact

#### Job 2: `deploy-pages`
1. Deploys the artifact to GitHub Pages
2. Makes updated rankings.json instantly available to the live site

### Dashboard (`index.html`)

- **Stack**: Vanilla HTML/CSS/JS (no build step)
- **Styling**: Custom dark theme with sticky header and responsive layout
- **Data Loading**: Fetches `data/rankings.json` with cache-busting on page load and every 15 minutes
- **Interactivity**:
  - Click column headers to sort
  - Search box for ticker/company filtering
  - Sector dropdown for industry-specific views
  - Refresh button to manually pull latest data
- **Color-Coded Pills**:
  - Green: AI score ≥ 9.0
  - Yellow: AI score 7.0-8.9
  - Gray: AI score < 7.0
- **VS Open Indicators**:
  - ▲ (green): Stock moved up in rank since 8:45 AM
  - ▼ (red): Stock moved down in rank
  - — (gray): No change or first run of the day

### Data Flow

```
EODHD API → Python Scripts → rankings.csv
    ↓
export_to_json.py
    ↓
rankings.json (saved to data/)
    ↓
GitHub Pages deploy
    ↓
Live Dashboard (index.html)
```

### Scoring Logic

Each stock receives a 0-10 score in 4 categories:

| Dimension | Weight | Key Metrics |
|-----------|--------|-------------|
| **Fundamental** | 30% | Earnings growth, P/E ratio, profit margins, debt levels |
| **Technical** | 30% | RSI, MACD, moving average crossovers, trend strength |
| **Sentiment** | 20% | News volume/tone, social mentions, analyst ratings |
| **Low Risk** | 20% | Volatility, max drawdown, beta |

**AI Score Formula**: `(Fund × 0.3) + (Tech × 0.3) + (Sent × 0.2) + (Risk × 0.2)`

Stocks are ranked 1-100 by AI score descending.

## File Structure

```
ai-stock-rankings/
├── .github/workflows/
│   └── update-rankings.yml        # Scheduled workflow
├── 02_Code/Python/
│   ├── Data_Fetch/                # EODHD API fetch scripts
│   ├── Indicators/                # Technical indicator calculations
│   └── Scoring_Engine/
│       ├── score_tickers.py       # Main scoring logic
│       └── export_to_json.py      # CSV → JSON converter
├── data/
│   ├── raw/ohlcv_daily/           # Price data cache
│   ├── processed/scoring_outputs/ # rankings.csv + daily_open baseline
│   └── rankings.json              # Live dashboard data (deployed)
├── index.html                     # Live dashboard frontend
└── README.md
```

## Scheduled Runs

**Cron Schedule** (Monday-Friday, US Central Time):
- **8:45 AM CST**: Morning run (sets VS Open baseline)
- **12:30 PM CST**: Midday refresh
- **3:35 PM CST**: End-of-day update

Workflows can also be manually triggered via GitHub Actions → "Run workflow".

## Configuration

### API Key Setup

Set `EODHD_API_KEY` in repository secrets:
1. Go to Settings → Secrets and variables → Actions
2. New repository secret: `EODHD_API_KEY` = `your_key_here`

### Modify Schedule

Edit `.github/workflows/update-rankings.yml`:

```yaml
schedule:
  - cron: '45 13 * * 1-5'  # 8:45 AM CST (UTC-5 DST)
  - cron: '30 17 * * 1-5'  # 12:30 PM CST
  - cron: '35 20 * * 1-5'  # 3:35 PM CST
```

*(Times in UTC; adjust for DST)*

### Customize Scoring Weights

Edit `02_Code/Python/Scoring_Engine/score_tickers.py`:

```python
# Line ~120: adjust weights
ai_score = (
    fundamental_score * 0.3 +
    technical_score * 0.3 +
    sentiment_score * 0.2 +
    low_risk_score * 0.2
)
```

## Local Development

### Run Scoring Engine Locally

```bash
# 1. Set API key
export EODHD_API_KEY="your_key_here"

# 2. Navigate to project root
cd ai-stock-rankings/

# 3. Run scoring pipeline
python 02_Code/Python/Scoring_Engine/score_tickers.py

# 4. Export to JSON
python 02_Code/Python/Scoring_Engine/export_to_json.py

# 5. View results
cat data/rankings.json
```

### Test Dashboard Locally

```bash
# Serve with Python HTTP server
python -m http.server 8000

# Open browser
open http://localhost:8000
```

## Deployment

**GitHub Pages** is configured to deploy from GitHub Actions:
- Settings → Pages → Source: "GitHub Actions"
- Each workflow run automatically deploys updated `index.html` and `data/rankings.json`

## Roadmap

- [ ] Integrate real-time sentiment from news/Twitter APIs
- [ ] Add sparkline price charts to each row
- [ ] Watchlist / favorites feature (localStorage)
- [ ] Email/Slack notifications for top movers
- [ ] Historical rank tracking and charting
- [ ] Mobile-optimized card view layout
- [ ] Backtest mode to validate scoring accuracy

## License

MIT License - See LICENSE file for details.

---

**Questions?** Open an issue or contact [@stdeplock-prog](https://github.com/stdeplock-prog)

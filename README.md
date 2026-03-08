# Buffett-Style Stock Screener

A simple, repeatable stock screener based on Warren Buffett's criteria for identifying good companies to invest in.

## Criteria

| # | Criterion | Threshold | Automated? |
|---|-----------|-----------|------------|
| 1 | **Cash Available** | Net Income ≥ $75M | Yes |
| 2 | **Consistent Growth** | Positive EPS growth each of the past 5 years | Yes |
| 3 | **Good ROI** | Return on Equity > 10% | Yes |
| 4 | **Low Debt Obligations** | Interest Coverage Ratio > 1 | Yes |
| 5 | **Committed Managers** | Insider ownership % (flagged for review) | Partial |
| 6 | **Simple Business Model** | Manual review required | No |

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Screen default set of 40 large-cap stocks
python screener.py

# Screen specific tickers
python screener.py --tickers AAPL MSFT GOOGL BRK-B KO

# Screen tickers from a file
python screener.py --file tickers.txt

# Customize thresholds
python screener.py --tickers AAPL MSFT --min-income 50000000 --min-roe 0.15

# Export results to CSV
python screener.py --csv results.csv
```

## Output

Stocks are scored 0-4 based on the quantitative criteria and sorted by score. Results are grouped into:

- **FULL PASS** — All 4 quantitative criteria met
- **PARTIAL PASS** — Some criteria met
- **FAIL** — No criteria met

Insider ownership % is displayed for manual review of the "Committed Managers" criterion. The "Simple Business Model" criterion requires your own research into the company.

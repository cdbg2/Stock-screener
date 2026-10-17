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

## Automated Runs

A GitHub Actions workflow runs every weekday at 21:00 UTC (5 PM Eastern during daylight saving time, 4 PM during standard time). It also runs when screener code or the workflow changes on `main`.

Each run resumes the next missing batch (0–4), regardless of delays or the weekday the runner starts. Once all five batches exist, it refreshes `results.csv` and clears the partials for the next cycle. Partial progress is committed between runs. Runs are serialized to prevent conflicting updates.

You can trigger it manually from the **Actions** tab. Leave `batch` empty to resume, or specify 0–4 to rerun a particular batch. For local use: `python screener.py --next-batch --pass-only --csv results.csv`.

The `screener-results` artifact is uploaded only when final results were regenerated. A completed cycle with no passing stocks produces a header-only CSV, replacing previous results.

Provider errors are summarized in the run log and Actions summary, including affected tickers. Stocks with unavailable data cannot be fully evaluated; a successful run with warnings is incomplete coverage, not proof that those stocks failed the criteria. If all income or all balance-sheet requests fail, the run fails without saving that batch or replacing published results. Check coverage warnings before relying on a report. Existing partials from before this change do not contain coverage diagnostics.

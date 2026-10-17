# Buffett-Style Stock Screener

Free, repeatable screening of the bundled S&P 500, NASDAQ 100, or Dow stock lists using annual financial facts from SEC EDGAR. No FMP account, subscription, or API key is needed.

## Criteria

| Criterion | Test |
|---|---|
| Earnings size | Latest annual net income ≥ $75 million |
| Consistent EPS growth | EPS increased in each of five consecutive year-over-year comparisons: **six annual observations** |
| Return on equity | Latest annual net income / same-year closing shareholders' equity > 10% |
| Interest coverage | Same-year operating income / interest expense > 1 |

Management commitment and business simplicity require manual research. This implementation does not retrieve insider ownership. Net income is earnings, not cash on hand. ROE uses closing equity, not average equity. These are screening rules, not a valuation model.

## Run locally

```bash
pip install -r requirements.txt
python -m pytest -q
python screener.py --pass-only --csv results.csv
python screener.py --tickers AAPL MSFT KO BRK.B --pass-only --csv results.csv
python screener.py --index nasdaq100 --pass-only --csv results.csv
python screener.py --max-candidates 10 --csv results.csv
python screener.py --file tickers.txt --csv results.csv
```

Thresholds can be changed with `--min-income`, `--min-roe` (decimal), and `--min-interest-coverage`. Without `--pass-only`, the primary CSV includes every attempted stock.

SEC requests identify this project in the User-Agent, run sequentially at no more than four requests per second, and retry temporary errors with backoff. You can override the identification with `SEC_USER_AGENT` or `--user-agent` (SEC recommends an organization/name and contact email). No authentication is involved. Responses are cached locally in `.cache/sec/` for the current UTC day; that cache is not committed.

## Reports

- **results.csv:** passing stocks when `--pass-only` is used. A completed scan with no passes replaces old results with a header-only CSV.
- **coverage.csv:** every attempted stock, including `PASS`, `FAIL`, and `NOT EVALUATED`, with reasons for missing data. Includes raw metric values, fiscal periods, retrieval timestamps, SEC filing links, and the exact source tags/accessions used. This is the audit for coverage; a short pass list does not imply complete evaluation of the index.
- **report.json:** source, thresholds, generation time, attempted/pass/fail/not-evaluated counts.

Reports are committed to the repository and attached as a `screener-results` artifact only when regenerated. The same three reports are written alongside a custom `--csv` path. `--coverage` can override the audit file path.

## Data handling and limits

Data comes from the SEC's [Company Facts and submissions APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), with tickers mapped through a bundled snapshot of the [SEC ticker file](https://www.sec.gov/files/company_tickers.json), dated in `sec_tickers.json`. This avoids downloading the directory from `www.sec.gov`, which can block hosted runners even when the financial APIs are accessible. Refresh the snapshot when ticker/CIK associations change; unmapped symbols are marked not evaluated. The snapshot retains all ticker/CIK pairs from the official directory.

- Identify the latest annual filing in SEC submissions. Select full-year (330–380 day) observations from annual filings, not quarterly or trailing periods. Use actual start/end dates; the XBRL `fy` field can describe the filing rather than a comparative fact's year.
- Match income, operating income, interest expense, and closing equity to the same fiscal period. Prefer the latest filed/restated fact within a supported tag. Keep all six EPS observations on one definition (diluted when available, otherwise basic). Reject gaps or incompatible stock-split histories rather than guessing an adjustment.
- Only entity-wide US-GAAP facts in USD (USD/share for EPS) and supported standard tags are mapped. Custom company tags, IFRS-only data, new issuers, class-specific EPS, and unavailable fields may prevent evaluation. This means all listed symbols can be attempted, but **not every company will have enough supported data to receive a score**.
- Missing interest expense stays unknown. Explicitly reported zero interest expense can pass the coverage test if operating income is positive; it does not imply the company has no debt. Non-positive equity is marked not evaluated for ROE.
- A stock missing any required criterion is `NOT EVALUATED`, never a full pass or an ordinary business failure. SEC access denial, exhausted rate-limit/connection retries, or no retrieved company data fails the run without replacing the final reports.
- Index lists are bundled snapshots in `constituents.py`, not automatically refreshed index membership. Check that file's update date; unmapped/outdated tickers are visible in the audit.

These conservative rules trade some coverage for traceable calculations. The fixture tests include extracted public SEC data for Apple, Microsoft, and Coca-Cola, as downloaded October 17, 2026; fixture files retain the source URL and period metadata.

## Automation

The GitHub Actions workflow runs weekdays at 21:00 UTC (5 PM Eastern during daylight saving time, 4 PM during standard time). Each scheduled run resumes the next missing batch, independent of the weekday on which the runner actually starts. Five completed batches produce a full report and clear the partials for the next cycle. Runs are serialized.

Code changes on `main` trigger a full rebuild, so source changes take effect immediately. In **Actions → Buffett Stock Screener → Run workflow**, leave inputs empty to resume a batch, enter a batch number 0–4 to rerun one, or select **full_scan** to rebuild the entire index now. Full scans take precedence over the batch input and discard the previous SEC batch cycle only after successfully writing their reports.

SEC batch files live in `partials/sec-v1/`, separate from legacy FMP partials. To resume locally:

```bash
python screener.py --next-batch --pass-only --csv results.csv
```

FMP secrets are no longer read or required. An optional repository variable `SEC_USER_AGENT` overrides the default project identification.

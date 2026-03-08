#!/usr/bin/env python3
"""
Buffett-Style Stock Screener
=============================
Screens stocks based on Warren Buffett's investment criteria:

1. Cash Available      - Net income >= $75 million
2. Consistent Growth   - Positive EPS growth each of the past 5 years
3. Good ROI            - Return on Equity > 10%
4. Low Debt            - Interest coverage ratio > 1
5. Committed Managers  - Insider ownership % (flagged for review)
6. Simple Business     - Number of business segments (flagged for review)

Usage:
    python screener.py                        # screen default S&P 500 tickers
    python screener.py --tickers AAPL MSFT    # screen specific tickers
    python screener.py --file tickers.txt     # screen tickers from file
"""

import argparse
import csv
import io
import sys
import time
import urllib.request

import pandas as pd
import yfinance as yf
from tabulate import tabulate


# ── Buffett criteria thresholds (edit these to taste) ────────────────────────
MIN_NET_INCOME = 75_000_000       # $75 million minimum net income
MIN_ROE = 0.10                    # 10% return on equity
MIN_INTEREST_COVERAGE = 1.0       # interest coverage ratio > 1
EPS_GROWTH_YEARS = 5              # positive EPS each of the past 5 years


# ── Default tickers (a representative set of large-cap US stocks) ────────────
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "BRK-B", "JNJ", "JPM", "V", "PG", "UNH",
    "HD", "MA", "DIS", "NVDA", "KO", "PEP", "PFE", "MRK", "ABT", "CSCO",
    "AVGO", "COST", "WMT", "XOM", "CVX", "LLY", "MCD", "TXN", "NEE", "LOW",
    "UPS", "CAT", "DE", "MMM", "GS", "AXP", "BLK", "SCHW", "CL", "GIS",
]


def fetch_us_tickers():
    """Fetch all common-stock tickers listed on NYSE, NASDAQ, and AMEX.

    Uses the official NASDAQ Trader daily file which lists every security
    traded on the NASDAQ system (including NYSE and AMEX via UTP).
    Filters out ETFs, preferred shares, warrants, units, and test symbols.
    """
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
    print("  Fetching all US-traded tickers from NASDAQ...")

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")

    tickers = []
    lines = raw.strip().split("\n")
    # Header is first line, last line is a timestamp/footer
    header = lines[0].split("|")
    col = {name: i for i, name in enumerate(header)}

    for line in lines[1:]:
        fields = line.split("|")
        if len(fields) < len(header):
            continue

        symbol = fields[col.get("Symbol", col.get("NASDAQ Symbol", 0))].strip()
        etf = fields[col.get("ETF", -1)].strip() if "ETF" in col else "N"
        test = fields[col.get("Test Issue", -1)].strip() if "Test Issue" in col else "N"

        # Skip ETFs and test issues
        if etf == "Y" or test == "Y":
            continue

        # Skip symbols with special characters (preferred shares, warrants, units)
        if not symbol or any(c in symbol for c in [".", "$", " ", "/"]):
            continue

        # Skip very long symbols (usually warrants/units like ABCDW)
        if len(symbol) > 5:
            continue

        tickers.append(symbol)

    print(f"  Found {len(tickers)} common-stock tickers.\n")
    return sorted(set(tickers))


def get_eps_history(ticker_obj):
    """Return a list of annual EPS values (oldest to newest)."""
    financials = ticker_obj.financials  # annual income statement
    if financials is None or financials.empty:
        return []

    # "Basic EPS" or "Diluted EPS" row
    for label in ["Basic EPS", "Diluted EPS"]:
        if label in financials.index:
            eps_series = financials.loc[label].dropna().sort_index()
            return eps_series.tolist()

    # Fallback: compute from Net Income / Shares Outstanding
    earnings = ticker_obj.earnings_history if hasattr(ticker_obj, "earnings_history") else None
    if earnings is not None and not earnings.empty:
        return earnings["epsActual"].dropna().tolist()

    return []


def check_consistent_eps_growth(eps_list, years=EPS_GROWTH_YEARS):
    """Return True if EPS increased every year for the past `years` years."""
    if len(eps_list) < years:
        return None  # not enough data
    recent = eps_list[-years:]
    for i in range(1, len(recent)):
        if recent[i] <= recent[i - 1]:
            return False
    return True


def get_net_income(ticker_obj):
    """Return the most recent annual net income in dollars."""
    financials = ticker_obj.financials
    if financials is None or financials.empty:
        return None
    for label in ["Net Income", "Net Income Common Stockholders"]:
        if label in financials.index:
            values = financials.loc[label].dropna().sort_index()
            if not values.empty:
                return values.iloc[-1]
    return None


def get_roe(ticker_obj):
    """Return the most recent Return on Equity."""
    info = ticker_obj.info
    roe = info.get("returnOnEquity")
    if roe is not None:
        return roe

    # Fallback: Net Income / Total Stockholder Equity
    try:
        net_income = get_net_income(ticker_obj)
        bs = ticker_obj.balance_sheet
        if bs is not None and not bs.empty:
            for label in ["Stockholders Equity", "Total Stockholder Equity",
                          "Stockholders' Equity"]:
                if label in bs.index:
                    equity = bs.loc[label].dropna().sort_index().iloc[-1]
                    if equity and equity != 0:
                        return net_income / equity
    except Exception:
        pass
    return None


def get_interest_coverage(ticker_obj):
    """Return Interest Coverage Ratio = EBIT / Interest Expense."""
    financials = ticker_obj.financials
    if financials is None or financials.empty:
        return None

    ebit = None
    for label in ["EBIT", "Operating Income"]:
        if label in financials.index:
            vals = financials.loc[label].dropna().sort_index()
            if not vals.empty:
                ebit = vals.iloc[-1]
                break

    interest = None
    for label in ["Interest Expense", "Interest Expense Non Operating"]:
        if label in financials.index:
            vals = financials.loc[label].dropna().sort_index()
            if not vals.empty:
                interest = abs(vals.iloc[-1])
                break

    if ebit is not None and interest and interest != 0:
        return ebit / interest
    if ebit is not None and (interest is None or interest == 0):
        return float("inf")  # no debt → passes easily
    return None


def get_insider_ownership(ticker_obj):
    """Return insider holding percentage (0-100), or None."""
    info = ticker_obj.info
    pct = info.get("heldPercentInsiders")
    if pct is not None:
        return pct * 100
    return None


def get_business_segments(ticker_obj):
    """Return count of business segments if available, else None."""
    # yfinance doesn't provide segment data directly; use sector as a proxy
    # and flag for manual review.
    return None


def screen_ticker(symbol):
    """Screen a single ticker. Returns a dict of results."""
    try:
        t = yf.Ticker(symbol)
        info = t.info
        name = info.get("shortName", info.get("longName", symbol))

        net_income = get_net_income(t)
        eps_list = get_eps_history(t)
        consistent_eps = check_consistent_eps_growth(eps_list)
        roe = get_roe(t)
        interest_cov = get_interest_coverage(t)
        insider_pct = get_insider_ownership(t)

        # ── Apply pass/fail for each criterion ───────────────────────────
        pass_income = net_income is not None and net_income >= MIN_NET_INCOME
        pass_eps = consistent_eps is True
        pass_roe = roe is not None and roe >= MIN_ROE
        pass_int_cov = interest_cov is not None and interest_cov > MIN_INTEREST_COVERAGE

        total_pass = sum([pass_income, pass_eps, pass_roe, pass_int_cov])

        return {
            "Ticker": symbol,
            "Company": name[:30],
            "Net Income ($M)": f"{net_income / 1e6:,.0f}" if net_income else "N/A",
            "Income ≥$75M": "PASS" if pass_income else "FAIL",
            "EPS 5yr Growth": "PASS" if pass_eps else ("N/A" if consistent_eps is None else "FAIL"),
            "ROE (%)": f"{roe * 100:.1f}" if roe else "N/A",
            "ROE >10%": "PASS" if pass_roe else "FAIL",
            "Int. Coverage": f"{interest_cov:.1f}" if interest_cov and interest_cov != float("inf") else ("No Debt" if interest_cov == float("inf") else "N/A"),
            "Int.Cov >1": "PASS" if pass_int_cov else "FAIL",
            "Insider %": f"{insider_pct:.1f}" if insider_pct else "N/A",
            "Score": f"{total_pass}/4",
            "_score": total_pass,
        }
    except Exception as e:
        return {
            "Ticker": symbol,
            "Company": "ERROR",
            "Net Income ($M)": "N/A",
            "Income ≥$75M": "ERR",
            "EPS 5yr Growth": "ERR",
            "ROE (%)": "N/A",
            "ROE >10%": "ERR",
            "Int. Coverage": "N/A",
            "Int.Cov >1": "ERR",
            "Insider %": "N/A",
            "Score": "0/4",
            "_score": 0,
        }


def run_screener(tickers):
    """Screen all tickers and return a sorted DataFrame."""
    results = []
    total = len(tickers)
    for i, symbol in enumerate(tickers, 1):
        print(f"  [{i}/{total}] Screening {symbol}...", end="\r")
        results.append(screen_ticker(symbol))
        time.sleep(0.2)  # be nice to the API

    print(" " * 60, end="\r")  # clear progress line
    df = pd.DataFrame(results)
    df = df.sort_values("_score", ascending=False)
    return df


def print_results(df):
    """Pretty-print the screening results."""
    # Separate into passing (all 4) and partial
    display_cols = [c for c in df.columns if c != "_score"]
    passing = df[df["_score"] == 4]
    partial = df[(df["_score"] > 0) & (df["_score"] < 4)]
    failing = df[df["_score"] == 0]

    print("\n" + "=" * 80)
    print("  BUFFETT-STYLE STOCK SCREENER RESULTS")
    print("=" * 80)

    print(f"\n  Criteria: Net Income ≥ ${MIN_NET_INCOME/1e6:.0f}M | "
          f"EPS growth 5yr | ROE > {MIN_ROE*100:.0f}% | "
          f"Interest Coverage > {MIN_INTEREST_COVERAGE}")
    print(f"  Stocks screened: {len(df)}")

    if not passing.empty:
        print(f"\n{'─' * 80}")
        print(f"  ★ FULL PASS — All 4 quantitative criteria met ({len(passing)} stocks)")
        print(f"{'─' * 80}")
        print(tabulate(passing[display_cols], headers="keys", tablefmt="simple",
                        showindex=False))
        print("\n  → Review these for Insider Ownership (committed managers)")
        print("    and business simplicity before investing.")

    if not partial.empty:
        print(f"\n{'─' * 80}")
        print(f"  PARTIAL PASS — Some criteria met ({len(partial)} stocks)")
        print(f"{'─' * 80}")
        print(tabulate(partial[display_cols], headers="keys", tablefmt="simple",
                        showindex=False))

    if not failing.empty:
        print(f"\n{'─' * 80}")
        print(f"  FAIL — No criteria met ({len(failing)} stocks)")
        print(f"{'─' * 80}")
        tickers_str = ", ".join(failing["Ticker"].tolist())
        print(f"  {tickers_str}")

    print(f"\n{'=' * 80}")
    print("  NOTE: 'Committed Managers' and 'Simple Business Model' require")
    print("  qualitative judgment. Check insider ownership % and research the")
    print("  company's business segments before making investment decisions.")
    print("=" * 80 + "\n")


def load_tickers_from_file(filepath):
    """Load ticker symbols from a text file (one per line)."""
    with open(filepath) as f:
        return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]


def main():
    global MIN_NET_INCOME, MIN_ROE, MIN_INTEREST_COVERAGE

    parser = argparse.ArgumentParser(
        description="Buffett-Style Stock Screener — screen stocks using Warren Buffett's criteria"
    )
    parser.add_argument(
        "--tickers", nargs="+", metavar="SYM",
        help="Ticker symbols to screen (e.g., AAPL MSFT GOOGL)"
    )
    parser.add_argument(
        "--file", metavar="PATH",
        help="Path to a text file with ticker symbols (one per line)"
    )
    parser.add_argument(
        "--min-income", type=float, default=MIN_NET_INCOME,
        help=f"Minimum net income in dollars (default: {MIN_NET_INCOME:,})"
    )
    parser.add_argument(
        "--min-roe", type=float, default=MIN_ROE,
        help=f"Minimum ROE as decimal (default: {MIN_ROE})"
    )
    parser.add_argument(
        "--min-interest-coverage", type=float, default=MIN_INTEREST_COVERAGE,
        help=f"Minimum interest coverage ratio (default: {MIN_INTEREST_COVERAGE})"
    )
    parser.add_argument(
        "--csv", metavar="PATH",
        help="Export results to CSV file"
    )
    parser.add_argument(
        "--all-us", action="store_true",
        help="Screen ALL U.S. publicly traded stocks (fetches tickers dynamically)"
    )
    parser.add_argument(
        "--pass-only", action="store_true",
        help="Only include passing stocks (score 4/4) in output and CSV"
    )
    args = parser.parse_args()

    MIN_NET_INCOME = args.min_income
    MIN_ROE = args.min_roe
    MIN_INTEREST_COVERAGE = args.min_interest_coverage

    # Determine ticker list
    if args.all_us:
        tickers = fetch_us_tickers()
    elif args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif args.file:
        tickers = load_tickers_from_file(args.file)
    else:
        tickers = DEFAULT_TICKERS

    print(f"\n  Buffett Stock Screener — screening {len(tickers)} stocks...\n")
    df = run_screener(tickers)

    if args.pass_only:
        df = df[df["_score"] == 4]
        if df.empty:
            print("\n  No stocks passed all 4 criteria.\n")
        else:
            print_results(df)
    else:
        print_results(df)

    if args.csv:
        export = df[[c for c in df.columns if c != "_score"]]
        export.to_csv(args.csv, index=False)
        print(f"  Results exported to {args.csv}\n")


if __name__ == "__main__":
    main()

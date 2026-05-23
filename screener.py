#!/usr/bin/env python3
"""
Buffett-Style Stock Screener
=============================
Screens U.S. mid-to-large cap stocks using Warren Buffett's criteria:

1. Cash Available      - Net income >= $75 million
2. Consistent Growth   - Positive EPS growth each of the past 5 years
3. Good ROI            - Return on Equity > 10%
4. Low Debt            - Interest coverage ratio > 1
5. Committed Managers  - Insider ownership % (flagged for review)
6. Simple Business     - Manual review required

Uses the Financial Modeling Prep (FMP) API for fast data retrieval.

Usage:
    python screener.py                             # screen all US mid-to-large cap
    python screener.py --api-key YOUR_KEY          # provide API key directly
    python screener.py --pass-only --csv out.csv   # export only full-pass stocks
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from tabulate import tabulate

from batch import split_into_batches, save_partial, all_batches_complete, combine_partials, clear_partials
from constituents import SP500, NASDAQ100, DOW30

NUM_BATCHES = 5
PARTIALS_DIR = "partials"


FMP_BASE = "https://financialmodelingprep.com/stable"

# ── Buffett criteria thresholds (edit these to taste) ────────────────────────
MIN_NET_INCOME = 75_000_000
MIN_ROE = 0.10
MIN_INTEREST_COVERAGE = 1.0
EPS_GROWTH_YEARS = 5


def fmp_get(endpoint, api_key, params=None):
    url = f"{FMP_BASE}/{endpoint}"
    p = {"apikey": api_key}
    if params:
        p.update(params)
    resp = requests.get(url, params=p, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_candidates(index="sp500"):
    """Return index constituents from bundled static lists (no API call needed)."""
    ticker_lists = {"sp500": SP500, "nasdaq100": NASDAQ100, "dow": DOW30}
    tickers = ticker_lists.get(index, SP500)
    return [{"symbol": t, "companyName": t} for t in tickers]


def fetch_income_statements(symbol, api_key):
    return fmp_get("income-statement", api_key, {"symbol": symbol, "limit": 5, "period": "annual"})


def fetch_balance_sheet(symbol, api_key):
    data = fmp_get("balance-sheet-statement", api_key, {"symbol": symbol, "limit": 1, "period": "annual"})
    return data[0] if data else {}


def check_income_criteria(statements):
    if not statements:
        return None, [], None, None

    sorted_stmts = sorted(statements, key=lambda x: x.get("date", ""))

    net_income = sorted_stmts[-1].get("netIncome")

    eps_list = []
    for s in sorted_stmts:
        eps = s.get("epsdiluted") or s.get("eps")
        if eps is not None:
            eps_list.append(eps)

    eps_growing = None
    if len(eps_list) >= EPS_GROWTH_YEARS:
        recent = eps_list[-EPS_GROWTH_YEARS:]
        eps_growing = all(recent[i] > recent[i - 1] for i in range(1, len(recent)))

    latest = sorted_stmts[-1]
    op_income = latest.get("operatingIncome") or 0
    int_expense = abs(latest.get("interestExpense") or 0)
    if int_expense > 0:
        interest_cov = op_income / int_expense
    elif op_income > 0:
        interest_cov = float("inf")
    else:
        interest_cov = None

    return net_income, eps_list, eps_growing, interest_cov


def compute_roe(net_income, balance_sheet):
    equity = balance_sheet.get("totalStockholdersEquity")
    if equity and equity != 0 and net_income is not None:
        return net_income / equity
    return None


def run_screener(api_key, max_candidates=None, index="sp500", ticker_subset=None):
    t0 = time.time()

    # ── Phase 1: Get index constituents (from bundled list, no API call) ──
    index_label = {"sp500": "S&P 500", "nasdaq100": "NASDAQ 100", "dow": "Dow 30"}[index]
    if ticker_subset:
        candidates = [{"symbol": t, "companyName": t} for t in ticker_subset]
        print(f"  Phase 1: Screening batch of {len(candidates)} {index_label} stocks...")
    else:
        print(f"  Phase 1: Loading {index_label} constituents...")
        candidates = get_candidates(index=index)
        if max_candidates:
            candidates = candidates[:max_candidates]

    api_calls = 0
    print(f"           {len(candidates)} stocks to screen\n")

    # ── Phase 2: Income statements — check 3 of 4 criteria (N calls) ────
    print("  Phase 2: Checking income, EPS growth & interest coverage...")
    phase2_pass = []
    done = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for c in candidates:
            f = executor.submit(fetch_income_statements, c["symbol"], api_key)
            futures[f] = c

        for f in as_completed(futures):
            done += 1
            c = futures[f]
            print(f"           [{done}/{len(candidates)}] {c['symbol']}", end="\r")
            try:
                statements = f.result()
                api_calls += 1
                net_income, eps_list, eps_growing, int_cov = check_income_criteria(statements)

                pass_income = net_income is not None and net_income >= MIN_NET_INCOME
                if not pass_income:
                    continue

                phase2_pass.append({
                    "symbol": c["symbol"],
                    "name": (c.get("companyName") or c["symbol"])[:30],
                    "net_income": net_income,
                    "eps_list": eps_list,
                    "eps_growing": eps_growing,
                    "int_cov": int_cov,
                    "pass_income": True,
                    "pass_eps": eps_growing is True,
                    "pass_int_cov": int_cov is not None and int_cov > MIN_INTEREST_COVERAGE,
                })
            except Exception:
                api_calls += 1

    print(f"           {len(phase2_pass)} stocks pass ${MIN_NET_INCOME / 1e6:.0f}M income threshold{' ' * 20}\n")

    if not phase2_pass:
        return pd.DataFrame()

    # ── Phase 3: Balance sheets — ROE check for survivors only (M calls) ─
    print(f"  Phase 3: Checking ROE for {len(phase2_pass)} candidates...")
    done = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for r in phase2_pass:
            f = executor.submit(fetch_balance_sheet, r["symbol"], api_key)
            futures[f] = r

        for f in as_completed(futures):
            done += 1
            r = futures[f]
            print(f"           [{done}/{len(phase2_pass)}] {r['symbol']}", end="\r")
            try:
                bs = f.result()
                api_calls += 1
                roe = compute_roe(r["net_income"], bs)
                r["roe"] = roe
                r["pass_roe"] = roe is not None and roe >= MIN_ROE
            except Exception:
                api_calls += 1
                r["roe"] = None
                r["pass_roe"] = False

    elapsed = time.time() - t0
    print(f"           Done. ({api_calls} API calls, {elapsed:.1f}s){' ' * 20}\n")

    # ── Build results DataFrame ──────────────────────────────────────────
    rows = []
    for r in phase2_pass:
        score = sum([r["pass_income"], r["pass_eps"], r.get("pass_roe", False), r["pass_int_cov"]])
        int_cov = r["int_cov"]
        roe = r.get("roe")
        rows.append({
            "Ticker": r["symbol"],
            "Company": r["name"],
            "Net Income ($M)": f"{r['net_income'] / 1e6:,.0f}",
            "Income ≥$75M": "PASS",
            "EPS 5yr Growth": "PASS" if r["pass_eps"] else ("N/A" if r["eps_growing"] is None else "FAIL"),
            "ROE (%)": f"{roe * 100:.1f}" if roe else "N/A",
            "ROE >10%": "PASS" if r.get("pass_roe") else "FAIL",
            "Int. Coverage": (
                f"{int_cov:.1f}" if int_cov and int_cov != float("inf")
                else ("No Debt" if int_cov == float("inf") else "N/A")
            ),
            "Int.Cov >1": "PASS" if r["pass_int_cov"] else "FAIL",
            "Score": f"{score}/4",
            "_score": score,
        })

    df = pd.DataFrame(rows)
    return df.sort_values("_score", ascending=False)


def print_results(df):
    display_cols = [c for c in df.columns if c != "_score"]
    passing = df[df["_score"] == 4]
    partial = df[(df["_score"] > 0) & (df["_score"] < 4)]

    print("\n" + "=" * 80)
    print("  BUFFETT-STYLE STOCK SCREENER RESULTS")
    print("=" * 80)

    print(f"\n  Criteria: Net Income ≥ ${MIN_NET_INCOME / 1e6:.0f}M | "
          f"EPS growth {EPS_GROWTH_YEARS}yr | ROE > {MIN_ROE * 100:.0f}% | "
          f"Interest Coverage > {MIN_INTEREST_COVERAGE}")
    print(f"  Stocks passing income threshold: {len(df)}")

    if not passing.empty:
        print(f"\n{'─' * 80}")
        print(f"  ★ FULL PASS — All 4 quantitative criteria met ({len(passing)} stocks)")
        print(f"{'─' * 80}")
        print(tabulate(passing[display_cols], headers="keys", tablefmt="simple",
                        showindex=False))
        print("\n  → Review these for committed management and business simplicity")
        print("    before investing.")

    if not partial.empty:
        print(f"\n{'─' * 80}")
        print(f"  PARTIAL PASS — Some criteria met ({len(partial)} stocks)")
        print(f"{'─' * 80}")
        print(tabulate(partial[display_cols], headers="keys", tablefmt="simple",
                        showindex=False))

    print(f"\n{'=' * 80}")
    print("  NOTE: 'Committed Managers' and 'Simple Business Model' require")
    print("  qualitative judgment. Research the company before investing.")
    print("=" * 80 + "\n")


def main():
    global MIN_NET_INCOME, MIN_ROE, MIN_INTEREST_COVERAGE

    parser = argparse.ArgumentParser(
        description="Buffett-Style Stock Screener — powered by FMP API"
    )
    parser.add_argument(
        "--api-key", metavar="KEY",
        help="FMP API key (or set FMP_API_KEY env var)"
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
        "--max-candidates", type=int, default=120,
        help="Max stocks to screen per run (default: 120, fits free API tier)"
    )
    parser.add_argument(
        "--index", choices=["sp500", "nasdaq100", "dow"], default="sp500",
        help="Stock index to screen (default: sp500)"
    )
    parser.add_argument(
        "--csv", metavar="PATH",
        help="Export results to CSV file"
    )
    parser.add_argument(
        "--pass-only", action="store_true",
        help="Only include passing stocks (score 4/4) in output and CSV"
    )
    parser.add_argument(
        "--batch", type=int, metavar="N",
        help="Run batch N (0-4) of the index. Saves partial results to partials/ dir. "
             "When all 5 batches are done, combines into final results."
    )
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("FMP_API_KEY")
    if not api_key:
        print("  ERROR: FMP API key required. Use --api-key or set FMP_API_KEY env var.\n")
        sys.exit(1)

    MIN_NET_INCOME = args.min_income
    MIN_ROE = args.min_roe
    MIN_INTEREST_COVERAGE = args.min_interest_coverage

    if args.batch is not None:
        run_batch_mode(api_key, args)
    else:
        run_full_mode(api_key, args)


def run_full_mode(api_key, args):
    print(f"\n  Buffett Stock Screener — scanning US market...\n")
    df = run_screener(api_key, max_candidates=args.max_candidates, index=args.index)
    output_results(df, args)


def run_batch_mode(api_key, args):
    batch_index = args.batch
    if not 0 <= batch_index < NUM_BATCHES:
        print(f"  ERROR: --batch must be 0-{NUM_BATCHES - 1}\n")
        sys.exit(1)

    all_tickers = get_candidates(index=args.index)
    all_symbols = [c["symbol"] for c in all_tickers]
    batches = split_into_batches(all_symbols, NUM_BATCHES)
    ticker_subset = batches[batch_index]

    print(f"\n  Buffett Stock Screener — batch {batch_index + 1}/{NUM_BATCHES} "
          f"({len(ticker_subset)} stocks)...\n")

    df = run_screener(api_key, index=args.index, ticker_subset=ticker_subset)

    if df.empty:
        df = pd.DataFrame(columns=["Ticker", "Company", "Net Income ($M)",
                                    "Income ≥$75M", "EPS 5yr Growth", "ROE (%)",
                                    "ROE >10%", "Int. Coverage", "Int.Cov >1",
                                    "Score", "_score"])

    save_partial(df, batch_index, PARTIALS_DIR)
    print(f"  Saved batch {batch_index} results to {PARTIALS_DIR}/\n")

    if all_batches_complete(PARTIALS_DIR, NUM_BATCHES):
        print("  All batches complete! Combining results...\n")
        combined = combine_partials(PARTIALS_DIR, NUM_BATCHES)
        output_results(combined, args)
        clear_partials(PARTIALS_DIR, NUM_BATCHES)
    else:
        done = sum(1 for i in range(NUM_BATCHES)
                   if os.path.exists(os.path.join(PARTIALS_DIR, f"batch_{i}.csv")))
        print(f"  Progress: {done}/{NUM_BATCHES} batches complete. "
              f"Run remaining batches to get final results.\n")


def output_results(df, args):
    if df.empty:
        print("  No results found.\n")
        return

    if args.pass_only:
        df = df[df["_score"] == 4]
        if df.empty:
            print("\n  No stocks passed all 4 criteria.\n")
        else:
            print_results(df)
    else:
        print_results(df)

    if args.csv and not df.empty:
        export = df[[c for c in df.columns if c != "_score"]]
        export.to_csv(args.csv, index=False)
        print(f"  Results exported to {args.csv}\n")


if __name__ == "__main__":
    main()

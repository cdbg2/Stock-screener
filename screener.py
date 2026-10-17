#!/usr/bin/env python3
"""Buffett-style screening from free SEC EDGAR annual financial facts."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path

import pandas as pd
import requests
from tabulate import tabulate

from batch import (split_into_batches, save_partial, all_batches_complete,
                   combine_partials, clear_partials, next_missing_batch)
from constituents import SP500, NASDAQ100, DOW30
from sec_data import SecClient, SecAccessError

NUM_BATCHES = 5
# Separate schema/source namespace: never combine old FMP partials with SEC rows.
PARTIALS_DIR = "partials/sec-v1"
MIN_NET_INCOME = 75_000_000
MIN_ROE = .10
MIN_INTEREST_COVERAGE = 1.0
COLUMNS = ["Ticker", "Company", "Net Income ($M)", "Income ≥$75M", "EPS 5yr Growth",
           "ROE (%)", "ROE >10%", "Int. Coverage", "Int.Cov >1", "Score",
           "Evaluation", "Reason", "Fiscal Year End", "As Of UTC", "SEC CIK",
           "SEC Filing", "SEC Facts", "Net Income (USD)", "Equity (USD)",
           "Operating Income (USD)", "Interest Expense (USD)", "EPS Annual Values",
           "Fact Sources", "_score"]


def get_candidates(index="sp500"):
    lists = {"sp500": SP500, "nasdaq100": NASDAQ100, "dow": DOW30}
    return [{"symbol": t, "companyName": t} for t in lists[index]]


def evaluate(symbol, metrics):
    """Unknown data cannot pass a criterion or be presented as a failed business."""
    income = metrics.get("net_income")
    equity = metrics.get("equity")
    operating = metrics.get("operating_income")
    interest = metrics.get("interest_expense")
    eps = metrics.get("eps", [])
    issues = list(metrics.get("issues", []))
    roe = income / equity if income is not None and equity is not None and equity > 0 else None
    if equity is not None and equity <= 0:
        issues.append("ROE not meaningful with non-positive shareholders' equity")
    coverage = None
    if operating is not None and interest is not None:
        if interest > 0:
            coverage = operating / interest
        elif interest == 0 and operating > 0:
            coverage = math.inf
        elif interest == 0:
            coverage = 0.0
        else:
            issues.append("Negative interest expense requires manual review")
    checks = [income >= MIN_NET_INCOME if income is not None else None,
              all(b > a for a, b in zip(eps, eps[1:])) if len(eps) == 6 else None,
              roe > MIN_ROE if roe is not None else None,
              coverage > MIN_INTEREST_COVERAGE if coverage is not None else None]
    score = sum(c is True for c in checks)
    missing = any(c is None for c in checks)
    status = "NOT EVALUATED" if missing else ("PASS" if score == 4 else "FAIL")
    def label(value):
        return "NOT EVALUATED" if value is None else ("PASS" if value else "FAIL")
    filing = metrics.get("filing", {})
    cik = metrics.get("cik", "")
    accession = filing.get("accession", "")
    filing_url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.html"
                  if cik and accession else "")
    return {"Ticker": symbol, "Company": metrics.get("company") or symbol,
            "Net Income ($M)": round(income / 1e6, 2) if income is not None else None,
            "Income ≥$75M": label(checks[0]), "EPS 5yr Growth": label(checks[1]),
            "ROE (%)": round(roe * 100, 2) if roe is not None else None,
            "ROE >10%": label(checks[2]),
            "Int. Coverage": "No interest expense" if coverage == math.inf else (round(coverage, 2) if coverage is not None else None),
            "Int.Cov >1": label(checks[3]), "Score": f"{score}/4" if not missing else "NOT EVALUATED",
            "Evaluation": status, "Reason": "; ".join(dict.fromkeys(issues)),
            "Fiscal Year End": metrics.get("period_end", ""),
            "As Of UTC": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "SEC CIK": cik, "SEC Filing": filing_url, "SEC Facts": metrics.get("source_url", ""),
            "Net Income (USD)": income, "Equity (USD)": equity,
            "Operating Income (USD)": operating, "Interest Expense (USD)": interest,
            "EPS Annual Values": json.dumps(eps), "Fact Sources": json.dumps(metrics.get("tags", {}), sort_keys=True),
            "_score": score}


def run_screener(client=None, max_candidates=None, index="sp500", ticker_subset=None):
    client = client or SecClient()
    symbols = ticker_subset if ticker_subset is not None else [c["symbol"] for c in get_candidates(index)]
    if max_candidates is not None:
        symbols = symbols[:max_candidates]
    rows = []
    fetched = 0
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}", flush=True)
        try:
            metrics = client.financials(symbol)
            fetched += 1
        except SecAccessError:
            raise
        except (requests.RequestException, ValueError, KeyError) as error:
            reason = (f"SEC HTTP {error.response.status_code}" if isinstance(error, requests.HTTPError)
                      and error.response is not None else str(error))
            metrics = {"issues": [reason]}
        rows.append(evaluate(symbol, metrics))
    if symbols and not fetched:
        raise RuntimeError("No SEC company data retrieved; results were not replaced")
    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        df = df.sort_values(["_score", "Ticker"], ascending=[False, True]).reset_index(drop=True)
    counts = Counter(df["Evaluation"])
    print(f"SEC coverage: {len(df)} attempted; {counts['PASS']} pass; {counts['FAIL']} fail; "
          f"{counts['NOT EVALUATED']} not evaluated", flush=True)
    if counts["NOT EVALUATED"] and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{counts['NOT EVALUATED']}/{len(df)} stocks could not be fully evaluated; see coverage.csv")
    return df


def output_results(df, args):
    if df.empty and not len(df.columns):
        df = pd.DataFrame(columns=COLUMNS)
    public = df.drop(columns=["_score"], errors="ignore")
    selected = public[public["Evaluation"] == "PASS"] if args.pass_only else public
    print(tabulate(selected[["Ticker", "Company", "Score", "Evaluation"]], headers="keys", showindex=False))
    if not args.csv:
        return False
    path = Path(args.csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Always publish an audit of ALL attempted companies, including insufficient data.
    coverage_path = Path(getattr(args, "coverage", None) or path.with_name("coverage.csv"))
    if coverage_path.resolve() == path.resolve():
        raise ValueError("Passing results and coverage audit need different file paths")
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    public.to_csv(coverage_path, index=False)
    selected.to_csv(path, index=False)
    counts = Counter(public["Evaluation"])
    summary = (f"SEC EDGAR report: {len(public)} attempted; {counts['PASS']} pass; "
               f"{counts['FAIL']} fail; {counts['NOT EVALUATED']} not evaluated. "
               "See coverage.csv for reasons, source periods, and filing links.")
    print(summary)
    report = {"source": "SEC EDGAR Company Facts + submissions", "schema": "sec-v1",
              "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "attempted": len(public), "pass": counts["PASS"], "fail": counts["FAIL"],
              "not_evaluated": counts["NOT EVALUATED"], "eps_growth_years": 5,
              "eps_annual_observations": 6, "min_net_income_usd": MIN_NET_INCOME,
              "min_roe_exclusive": MIN_ROE, "min_interest_coverage_exclusive": MIN_INTEREST_COVERAGE,
              "universe": "custom" if getattr(args, "tickers", None) else args.index}
    path.with_name("report.json").write_text(json.dumps(report, indent=2) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"\n## Buffett screener\n\n{summary}\n\nFive YoY EPS increases require six consecutive annual EPS values.\n")
    return True


def run_full_mode(client, args):
    df = run_screener(client, max_candidates=args.max_candidates, index=args.index,
                      ticker_subset=getattr(args, "tickers", None))
    updated = output_results(df, args)
    # A full scan supersedes an unfinished batch cycle, but only after successful output.
    if updated and not getattr(args, "tickers", None) and args.max_candidates is None:
        clear_partials(PARTIALS_DIR, NUM_BATCHES)
    return updated


def run_batch_mode(client, args):
    batch_index = next_missing_batch(PARTIALS_DIR, NUM_BATCHES) if args.next_batch else args.batch
    if batch_index is None:
        return finish_batches(args)
    if not 0 <= batch_index < NUM_BATCHES:
        raise ValueError(f"--batch must be 0-{NUM_BATCHES - 1}")
    symbols = [c["symbol"] for c in get_candidates(args.index)]
    df = run_screener(client, index=args.index, ticker_subset=split_into_batches(symbols, NUM_BATCHES)[batch_index])
    save_partial(df, batch_index, PARTIALS_DIR)
    if all_batches_complete(PARTIALS_DIR, NUM_BATCHES):
        return finish_batches(args)
    print(f"Saved SEC batch {batch_index}; waiting for remaining batches")
    return False


def finish_batches(args):
    combined = combine_partials(PARTIALS_DIR, NUM_BATCHES)
    updated = output_results(combined, args)
    if updated:
        clear_partials(PARTIALS_DIR, NUM_BATCHES)
    return updated


def main():
    global MIN_NET_INCOME, MIN_ROE, MIN_INTEREST_COVERAGE
    parser = argparse.ArgumentParser(description="Buffett-style stock screener using free SEC EDGAR data")
    parser.add_argument("--min-income", type=float, default=MIN_NET_INCOME)
    parser.add_argument("--min-roe", type=float, default=MIN_ROE)
    parser.add_argument("--min-interest-coverage", type=float, default=MIN_INTEREST_COVERAGE)
    parser.add_argument("--max-candidates", type=int, help="Limit stocks for a test run (default: entire bundled index)")
    parser.add_argument("--index", choices=["sp500", "nasdaq100", "dow"], default="sp500")
    parser.add_argument("--csv", help="Passing-stock CSV; coverage.csv and report.json are written alongside it")
    parser.add_argument("--coverage", help="Optional path for the full coverage audit CSV")
    parser.add_argument("--pass-only", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--batch", type=int, choices=range(NUM_BATCHES))
    mode.add_argument("--next-batch", action="store_true")
    parser.add_argument("--tickers", nargs="+", help="Screen specific symbols instead of an index")
    parser.add_argument("--file", help="Read one ticker per line")
    parser.add_argument("--user-agent", help="SEC request identification; defaults to SEC_USER_AGENT or project URL")
    args = parser.parse_args()
    if args.max_candidates is not None and args.max_candidates < 1:
        parser.error("--max-candidates must be positive")
    if args.file:
        args.tickers = [line.strip() for line in Path(args.file).read_text().splitlines() if line.strip()]
    if args.tickers and (args.batch is not None or args.next_batch):
        parser.error("Custom tickers cannot be combined with index batches")
    if args.tickers:
        args.tickers = list(dict.fromkeys(t.upper() for t in args.tickers))
    if args.csv and args.coverage and Path(args.csv).resolve() == Path(args.coverage).resolve():
        parser.error("Passing results and coverage audit need different file paths")
    MIN_NET_INCOME, MIN_ROE, MIN_INTEREST_COVERAGE = args.min_income, args.min_roe, args.min_interest_coverage
    client = SecClient(args.user_agent)
    updated = run_batch_mode(client, args) if args.batch is not None or args.next_batch else run_full_mode(client, args)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"results_updated={str(bool(updated)).lower()}\n")


if __name__ == "__main__":
    main()

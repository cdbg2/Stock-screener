from types import SimpleNamespace
from datetime import date
import json
import math
from pathlib import Path

import pandas as pd
import pytest

import screener
from batch import next_missing_batch, save_partial
from sec_data import SecAccessError, extract_financials


def metrics(**changes):
    data = dict(company="Test Inc.", net_income=100_000_000, equity=500_000_000,
                operating_income=200, interest_expense=10, eps=[1, 2, 3, 4, 5, 6],
                issues=[], tags={}, period_end="2025-12-31")
    data.update(changes)
    return data


def result(score=4):
    return pd.DataFrame([screener.evaluate("TEST", metrics(eps=[1, 2, 3, 4, 5, 6] if score == 4 else [6, 5, 4, 3, 2, 1]))])


def args(tmp_path):
    return SimpleNamespace(batch=None, next_batch=True, index="sp500", max_candidates=None,
                           pass_only=True, csv=str(tmp_path / "results.csv"))


def test_resume_missing_zero_then_publish_and_clear(tmp_path, monkeypatch):
    partials = tmp_path / "partials"
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(partials))
    monkeypatch.setattr(screener, "get_candidates", lambda *a: [{"symbol": s} for s in "ABCDE"])
    for i in range(1, 5):
        save_partial(result(), i, partials)
    seen = []
    monkeypatch.setattr(screener, "run_screener", lambda *a, **kw: seen.append(kw["ticker_subset"]) or result())
    assert screener.run_batch_mode(None, args(tmp_path)) is True
    assert seen == [["A"]]
    assert len(pd.read_csv(tmp_path / "results.csv")) == 5
    assert not list(partials.glob("*.csv"))
    assert next_missing_batch(partials, 5) == 0


def test_incomplete_cycle_preserves_previous_report(tmp_path, monkeypatch):
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(tmp_path / "partials"))
    monkeypatch.setattr(screener, "run_screener", lambda *a, **kw: result())
    report = tmp_path / "results.csv"
    report.write_text("previous report\n")
    assert screener.run_batch_mode(None, args(tmp_path)) is False
    assert report.read_text() == "previous report\n"


def test_complete_saved_cycle_combines_without_refetch(tmp_path, monkeypatch):
    partials = tmp_path / "partials"
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(partials))
    for i in range(5):
        save_partial(result(), i, partials)
    monkeypatch.setattr(screener, "run_screener", lambda *a, **kw: pytest.fail("Should not refetch"))
    assert screener.run_batch_mode(None, args(tmp_path)) is True


@pytest.mark.parametrize("df", [result(3), pd.DataFrame()])
def test_empty_report_replaces_stale_passes(tmp_path, df):
    report = tmp_path / "results.csv"
    report.write_text("stale passing stocks\n")
    assert screener.output_results(df, args(tmp_path)) is True
    assert pd.read_csv(report).empty
    assert "Ticker" in pd.read_csv(report).columns


def test_audit_includes_not_evaluated_and_failed_rows(tmp_path):
    rows = [screener.evaluate("PASS", metrics()), screener.evaluate("FAIL", metrics(eps=[6, 5, 4, 3, 2, 1])),
            screener.evaluate("MISSING", metrics(interest_expense=None, issues=["Interest unavailable"]))]
    screener.output_results(pd.DataFrame(rows), args(tmp_path))
    assert pd.read_csv(tmp_path / "results.csv").Ticker.tolist() == ["PASS"]
    assert len(pd.read_csv(tmp_path / "coverage.csv")) == 3
    report = json.loads((tmp_path / "report.json").read_text())
    assert (report["attempted"], report["pass"], report["fail"], report["not_evaluated"]) == (3, 1, 1, 1)


def test_total_provider_failure_preserves_report_and_partial(tmp_path, monkeypatch):
    partials = tmp_path / "partials"
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(partials))
    report = tmp_path / "results.csv";report.write_text("previous report\n")
    def fail(symbol):
        raise SecAccessError("SEC access denied")
    with pytest.raises(SecAccessError):
        screener.run_batch_mode(SimpleNamespace(financials=fail), args(tmp_path))
    assert report.read_text() == "previous report\n"
    assert not partials.exists()


def test_unmapped_ticker_is_explicit_not_evaluated():
    def get(symbol):
        if symbol == "OLD":raise ValueError("Ticker not found in SEC ticker map")
        return metrics()
    df = screener.run_screener(SimpleNamespace(financials=get), ticker_subset=["GOOD", "OLD"])
    row = df[df.Ticker == "OLD"].iloc[0]
    assert row.Evaluation == "NOT EVALUATED"
    assert "Ticker not found" in row.Reason


def test_unknown_interest_never_passes_as_zero():
    row = screener.evaluate("TEST", metrics(interest_expense=None))
    assert row["Int.Cov >1"] == "NOT EVALUATED"
    assert row["Evaluation"] == "NOT EVALUATED"
    zero = screener.evaluate("TEST", metrics(interest_expense=0))
    assert zero["Int. Coverage"] == "No interest expense"
    assert zero["Int.Cov >1"] == "PASS"


def test_negative_equity_and_exact_roe_threshold_do_not_pass():
    assert screener.evaluate("TEST", metrics(equity=-100))["ROE >10%"] == "NOT EVALUATED"
    assert screener.evaluate("TEST", metrics(equity=1_000_000_000))["ROE >10%"] == "FAIL"


def test_six_eps_values_and_zero_values_are_preserved():
    assert screener.evaluate("TEST", metrics(eps=[0, 1, 2, 3, 4, 5]))["EPS 5yr Growth"] == "PASS"
    assert screener.evaluate("TEST", metrics(eps=[1, 2, 3, 4, 5]))["EPS 5yr Growth"] == "NOT EVALUATED"


@pytest.mark.parametrize("symbol,income,equity,eps,interest,status", [
    ("AAPL",112010000000,73733000000,7.46,None,"NOT EVALUATED"),
    ("MSFT",133749000000,442387000000,17.95,3051000000,"PASS"),
    ("KO",13107000000,32169000000,3.04,1654000000,"FAIL"),
])
def test_real_sec_filing_snapshots(symbol,income,equity,eps,interest,status):
    sample = json.loads((Path("tests/fixtures") / f"{symbol}.json").read_text())
    data = extract_financials(sample["facts"], sample["submissions"], today=date(2026,10,17))
    assert data["net_income"] == income
    assert data["equity"] == equity
    assert data["eps"][-1] == eps
    assert data["interest_expense"] == interest
    assert screener.evaluate(symbol,data)["Evaluation"] == status


def test_custom_scan_does_not_discard_index_progress(tmp_path,monkeypatch):
    partials=tmp_path/"partials"
    monkeypatch.setattr(screener,"PARTIALS_DIR",str(partials))
    save_partial(result(),0,partials)
    monkeypatch.setattr(screener,"run_screener",lambda *a,**kw:result())
    options=args(tmp_path);options.tickers=["TEST"]
    assert screener.run_full_mode(None,options)
    assert (partials/"batch_0.csv").exists()


def test_default_audit_path_cannot_overwrite_passing_report(tmp_path):
    options=args(tmp_path);options.csv=str(tmp_path/"coverage.csv")
    with pytest.raises(ValueError,match="different file paths"):
        screener.output_results(result(),options)

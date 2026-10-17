from types import SimpleNamespace

import pandas as pd
import pytest
import requests

import screener
from batch import next_missing_batch, save_partial


def result(score=4):
    return pd.DataFrame({"Ticker": ["TEST"], "Score": [f"{score}/4"], "_score": [score]})


def args(tmp_path):
    return SimpleNamespace(batch=None, next_batch=True, index="sp500",
                           pass_only=True, csv=str(tmp_path / "results.csv"))


def test_resume_missing_zero_then_publish_and_clear(tmp_path, monkeypatch):
    """Reproduce the delayed Monday run: batches 1–4 exist, zero is missing."""
    partials = tmp_path / "partials"
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(partials))
    monkeypatch.setattr(screener, "get_candidates", lambda **kw: [{"symbol": s} for s in "ABCDE"])
    for i in range(1, 5):
        save_partial(result(), i, partials)
    seen = []
    monkeypatch.setattr(screener, "run_screener",
                        lambda *a, **kw: seen.append(kw["ticker_subset"]) or result())
    assert screener.run_batch_mode("unused", args(tmp_path)) is True
    assert seen == [["A"]]
    assert len(pd.read_csv(tmp_path / "results.csv")) == 5
    assert not list(partials.glob("*.csv"))
    assert next_missing_batch(partials, 5) == 0


def test_incomplete_cycle_preserves_previous_report(tmp_path, monkeypatch):
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(tmp_path / "partials"))
    monkeypatch.setattr(screener, "run_screener", lambda *a, **kw: result())
    report = tmp_path / "results.csv"
    report.write_text("previous report\n")
    assert screener.run_batch_mode("unused", args(tmp_path)) is False
    assert report.read_text() == "previous report\n"
    assert next_missing_batch(tmp_path / "partials", 5) == 1


def test_complete_saved_cycle_combines_without_refetch(tmp_path, monkeypatch):
    partials = tmp_path / "partials"
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(partials))
    for i in range(5):
        save_partial(result(), i, partials)
    assert next_missing_batch(partials, 5) is None
    monkeypatch.setattr(screener, "run_screener", lambda *a, **kw: pytest.fail("Should not refetch"))
    assert screener.run_batch_mode("unused", args(tmp_path)) is True


@pytest.mark.parametrize("df", [result(3), pd.DataFrame()])
def test_empty_report_replaces_stale_passes(tmp_path, df):
    report = tmp_path / "results.csv"
    report.write_text("stale passing stocks\n")
    assert screener.output_results(df, args(tmp_path)) is True
    assert pd.read_csv(report).empty
    assert "Ticker" in pd.read_csv(report).columns


def provider_error():
    response = requests.Response()
    response.status_code = 402
    return requests.HTTPError("secret URL?apikey=do-not-print", response=response)


def test_total_provider_failure_fails_without_saving(tmp_path, monkeypatch, capsys):
    partials = tmp_path / "partials"
    monkeypatch.setattr(screener, "PARTIALS_DIR", str(partials))
    monkeypatch.setattr(screener, "get_candidates", lambda **kw: [{"symbol": "TEST"}])
    def fail(*a):
        raise provider_error()
    monkeypatch.setattr(screener, "fetch_income_statements", fail)
    with pytest.raises(RuntimeError, match="No income data"):
        screener.run_batch_mode("unused", args(tmp_path))
    output = capsys.readouterr().out
    assert "HTTP 402: 1" in output
    assert "do-not-print" not in output
    assert not partials.exists()


def test_partial_provider_failure_is_visible(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    def income(symbol, key):
        if symbol == "BLOCKED":
            raise provider_error()
        return [{"date": f"202{i}-12-31", "netIncome": 100_000_000,
                 "eps": i + 1, "operatingIncome": 200, "interestExpense": 10}
                for i in range(5)]
    monkeypatch.setattr(screener, "fetch_income_statements", income)
    monkeypatch.setattr(screener, "fetch_balance_sheet", lambda *a: {"totalStockholdersEquity": 500_000_000})
    df = screener.run_screener("unused", ticker_subset=["GOOD", "BLOCKED"])
    assert df.Ticker.tolist() == ["GOOD"]
    assert "::warning::Income statements: data unavailable for 1/2" in capsys.readouterr().out
    assert "BLOCKED" in (tmp_path / "summary.md").read_text()


def test_total_balance_failure_fails(monkeypatch):
    monkeypatch.setattr(screener, "fetch_income_statements", lambda *a: [
        {"date": "2025-12-31", "netIncome": 100_000_000}])
    monkeypatch.setattr(screener, "fetch_balance_sheet", lambda *a: {})
    with pytest.raises(RuntimeError, match="No balance sheets"):
        screener.run_screener("unused", ticker_subset=["TEST"])


def test_provider_error_payload_is_rejected(monkeypatch):
    monkeypatch.setattr(screener.requests, "get", lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"Error Message": "Bad key"}))
    with pytest.raises(ValueError, match="Unexpected provider response"):
        screener.fmp_get("income-statement", "unused")

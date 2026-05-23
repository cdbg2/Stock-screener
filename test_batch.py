import pandas as pd
import pytest

from batch import split_into_batches, save_partial, load_partial, all_batches_complete, combine_partials, clear_partials


def test_split_into_batches_divides_evenly():
    tickers = ["A", "B", "C", "D", "E", "F"]
    batches = split_into_batches(tickers, 3)
    assert len(batches) == 3
    assert batches[0] == ["A", "B"]
    assert batches[1] == ["C", "D"]
    assert batches[2] == ["E", "F"]


def test_split_into_batches_handles_remainder():
    tickers = ["A", "B", "C", "D", "E"]
    batches = split_into_batches(tickers, 3)
    assert len(batches) == 3
    assert len(batches[0]) == 2
    assert len(batches[1]) == 2
    assert len(batches[2]) == 1
    all_tickers = [t for b in batches for t in b]
    assert all_tickers == tickers


def test_split_preserves_all_tickers():
    tickers = list("ABCDEFGHIJKLM")
    batches = split_into_batches(tickers, 5)
    assert len(batches) == 5
    all_tickers = [t for b in batches for t in b]
    assert all_tickers == tickers


def test_save_and_load_partial(tmp_path):
    df = pd.DataFrame({"Ticker": ["AAPL", "MSFT"], "Score": ["4/4", "3/4"]})
    save_partial(df, batch_index=0, output_dir=tmp_path)
    loaded = load_partial(batch_index=0, output_dir=tmp_path)
    pd.testing.assert_frame_equal(loaded, df)


def test_load_missing_partial_returns_none(tmp_path):
    result = load_partial(batch_index=99, output_dir=tmp_path)
    assert result is None


def test_all_batches_complete_false_when_missing(tmp_path):
    assert not all_batches_complete(tmp_path, num_batches=2)
    save_partial(pd.DataFrame({"Ticker": ["AAPL"]}), 0, tmp_path)
    assert not all_batches_complete(tmp_path, num_batches=2)


def test_all_batches_complete_true_when_all_present(tmp_path):
    save_partial(pd.DataFrame({"Ticker": ["AAPL"]}), 0, tmp_path)
    save_partial(pd.DataFrame({"Ticker": ["MSFT"]}), 1, tmp_path)
    assert all_batches_complete(tmp_path, num_batches=2)


def test_combine_partials_merges_and_sorts(tmp_path):
    df1 = pd.DataFrame({"Ticker": ["MSFT"], "_score": [3]})
    df2 = pd.DataFrame({"Ticker": ["AAPL"], "_score": [4]})
    save_partial(df1, 0, tmp_path)
    save_partial(df2, 1, tmp_path)
    combined = combine_partials(tmp_path, num_batches=2)
    assert len(combined) == 2
    assert combined.iloc[0]["Ticker"] == "AAPL"
    assert combined.iloc[1]["Ticker"] == "MSFT"


def test_clear_partials_removes_batch_files(tmp_path):
    save_partial(pd.DataFrame({"Ticker": ["AAPL"]}), 0, tmp_path)
    save_partial(pd.DataFrame({"Ticker": ["MSFT"]}), 1, tmp_path)
    assert all_batches_complete(tmp_path, num_batches=2)
    clear_partials(tmp_path, num_batches=2)
    assert not all_batches_complete(tmp_path, num_batches=2)
    assert load_partial(0, tmp_path) is None
    assert load_partial(1, tmp_path) is None

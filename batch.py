"""Multi-day batch scheduling for FMP free tier (250 calls/day)."""

import os

import pandas as pd


def split_into_batches(tickers, num_batches):
    batch_size = len(tickers) // num_batches
    remainder = len(tickers) % num_batches
    batches = []
    start = 0
    for i in range(num_batches):
        end = start + batch_size + (1 if i < remainder else 0)
        batches.append(tickers[start:end])
        start = end
    return batches


def _partial_path(batch_index, output_dir):
    return os.path.join(output_dir, f"batch_{batch_index}.csv")


def save_partial(df, batch_index, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    df.to_csv(_partial_path(batch_index, output_dir), index=False)


def load_partial(batch_index, output_dir):
    path = _partial_path(batch_index, output_dir)
    if not os.path.exists(path):
        return None
    return pd.read_csv(path)


def all_batches_complete(output_dir, num_batches):
    return all(
        os.path.exists(_partial_path(i, output_dir))
        for i in range(num_batches)
    )


def combine_partials(output_dir, num_batches):
    frames = []
    for i in range(num_batches):
        df = load_partial(i, output_dir)
        if df is not None:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values("_score", ascending=False).reset_index(drop=True)


def clear_partials(output_dir, num_batches):
    for i in range(num_batches):
        path = _partial_path(i, output_dir)
        if os.path.exists(path):
            os.remove(path)

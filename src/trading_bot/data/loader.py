"""Market data loading with a local parquet cache.

Phase-1 source is yfinance (free, daily bars). The rest of the codebase only
sees the normalized DataFrame returned by ``load_ohlcv`` below, so swapping in a
higher-quality, point-in-time source (Polygon, Databento, Alpaca) later means
changing only this module.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Columns every downstream component can rely on.
OHLCV = ["open", "high", "low", "close", "volume"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten yfinance output to lowercase single-level OHLCV columns."""
    if isinstance(df.columns, pd.MultiIndex):
        # Single-ticker download still returns a 2-level column index in
        # recent yfinance versions; keep the price-field level.
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df = df[[c for c in OHLCV if c in df.columns]].copy()
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df.dropna()


def load_ohlcv(
    ticker: str,
    start: str,
    end: str,
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Return adjusted daily OHLCV for ``ticker`` between ``start`` and ``end``.

    Prices are auto-adjusted (splits/dividends) so returns are comparable across
    time. Results are cached to parquet keyed by ticker+range.
    """
    cache_file = CACHE_DIR / f"{ticker}_{start}_{end}.parquet"
    if use_cache and cache_file.exists():
        return pd.read_parquet(cache_file)

    import yfinance as yf

    raw = yf.download(
        ticker,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        multi_level_index=False,
    )
    if raw is None or raw.empty:
        raise ValueError(f"No data returned for {ticker} between {start} and {end}.")

    df = _normalize(raw)
    df.to_parquet(cache_file)
    return df


def train_test_split(
    df: pd.DataFrame, train_frac: float = 0.7
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split — never shuffle time series.

    The test slice is held out so we can measure out-of-sample performance and
    detect overfitting. For real validation we'll graduate to walk-forward
    analysis; this simple split is the phase-1 guardrail.
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError("train_frac must be in (0, 1).")
    cut = int(len(df) * train_frac)
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()

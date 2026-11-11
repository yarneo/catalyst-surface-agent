"""Historical crypto bars from Alpaca's market-data API, at any timeframe
(daily / hourly / multi-hour). Used for the trading-frequency comparison —
yfinance can't provide reliable intraday history.
"""

from __future__ import annotations

import pandas as pd

from ..broker import AlpacaConfig

# label -> (alpaca TimeFrame args, bars per day, periods/year for annualization)
TIMEFRAMES = {
    "1Day": (("Day", None), 1, 365),
    "4Hour": (("Hour", 4), 6, 365 * 6),
    "1Hour": (("Hour", 1), 24, 365 * 24),
}


def bars_per_day(label: str) -> int:
    return TIMEFRAMES[label][1]


def periods_per_year(label: str) -> int:
    return TIMEFRAMES[label][2]


def load_stock_bars(
    symbol: str, start: str, timeframe: str = "5Min", end: str | None = None,
    feed: str = "iex", regular_hours: bool = True,
) -> pd.DataFrame:
    """Intraday stock bars from Alpaca (free IEX feed by default), indexed in
    US/Eastern. ``regular_hours`` keeps only the 09:30–16:00 ET session."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    tf = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "30Min": TimeFrame(30, TimeFrameUnit.Minute),
    }[timeframe]
    cfg = AlpacaConfig.from_env()
    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end, feed=feed)
    import time as _time

    df = None
    for attempt in range(4):  # large paginated fetch can hit transient resets
        try:
            df = client.get_stock_bars(req).df
            break
        except Exception:
            if attempt == 3:
                raise
            _time.sleep(3 * (attempt + 1))
    if df is None or df.empty:
        raise ValueError(f"No Alpaca stock bars for {symbol} @ {timeframe}")
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level=0)
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index).tz_convert("America/New_York")
    if regular_hours:
        df = df.between_time("09:30", "15:59")
    return df.dropna()


def load_crypto_bars(symbol: str, start: str, timeframe: str = "1Day", end: str | None = None) -> pd.DataFrame:
    """OHLCV bars for an Alpaca crypto symbol (e.g. 'BTC/USD') at ``timeframe``."""
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    unit_name, amount = TIMEFRAMES[timeframe][0]
    tf = TimeFrame.Day if unit_name == "Day" else TimeFrame(amount, TimeFrameUnit.Hour)

    cfg = AlpacaConfig.from_env()
    client = CryptoHistoricalDataClient(cfg.api_key, cfg.secret_key)
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
    df = client.get_crypto_bars(req).df
    if df is None or df.empty:
        raise ValueError(f"No Alpaca bars for {symbol} @ {timeframe}")
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level=0)
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df.dropna()

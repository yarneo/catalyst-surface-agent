"""Safe price accessors.

Every live executor used to reach for ``yf.Ticker(sym).history(...)["Close"]
.iloc[-1]`` directly. yfinance routinely appends a trailing row for the current
session whose Close is ``NaN`` (partial or not-yet-settled bar), so that idiom
returns ``NaN`` — silently, with no exception.

That is fail-*dangerous*, because ``NaN`` propagates into comparisons as
``False``:

    last, sma = NaN, NaN
    regime_on = last > sma          # -> False  ("risk-OFF")

A momentary data glitch therefore reads as a confirmed bear-market signal. Caught
in the 2026-08-04 dry-run, where the regime sleeve announced::

    Filter: QQQ $nan vs SMA200 $nan  → regime OFF (hold cash)
    Regime OFF → closing QLD ($30,316)

With ``--live`` that would have liquidated a $30k position — and the router and
cash-park sleeves would have submitted ``nan``-quantity orders behind it — while
QQQ was in fact 17.5% *above* its 200-DMA.

The rule these helpers enforce: **an indeterminate signal must never produce a
trade.** They raise ``PriceUnavailable`` rather than return a value a caller
might compare against. Callers are expected to let it propagate and skip the
cycle: holding yesterday's position through a data outage is always cheaper than
liquidating on a phantom signal.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf
import numpy as np
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

from .sessions import last_completed_session

_AS_OF: ContextVar[date | None] = ContextVar("signal_session", default=None)
_CACHE: ContextVar[dict | None] = ContextVar("signal_cache", default=None)
_CAPTURE: ContextVar[dict | None] = ContextVar("signal_capture", default=None)


@contextmanager
def signal_session(day: date):
    """Freeze the signal date and source series for one entire portfolio cycle."""
    token = _AS_OF.set(day)
    cache_token = _CACHE.set({})
    capture_token = _CAPTURE.set({})
    try:
        yield
    finally:
        _AS_OF.reset(token)
        _CACHE.reset(cache_token)
        _CAPTURE.reset(capture_token)


def captured_signals() -> dict:
    """Exact completed bars used by this cycle, for private production replay."""
    return dict(_CAPTURE.get() or {})


def completed_bars(frame: pd.DataFrame, symbol: str, column: str = "close") -> pd.DataFrame:
    expected = _AS_OF.get() or last_completed_session()
    out = frame.copy().sort_index()
    out = out.loc[~out.index.duplicated(keep="last")]
    out = out.loc[pd.to_datetime(out.index).date <= expected]
    if out.empty or pd.Timestamp(out.index[-1]).date() != expected:
        raise PriceUnavailable(f"{symbol}: missing completed session {expected}")
    price = pd.to_numeric(out[column], errors="coerce")
    if not np.isfinite(price.iloc[-1]) or price.iloc[-1] <= 0:
        raise PriceUnavailable(f"{symbol}: invalid completed-session price")
    out = out.loc[np.isfinite(price) & (price > 0)]
    capture = _CAPTURE.get()
    if capture is not None:
        import json
        normalized = out.rename(columns=str.lower)
        existing = capture.get(symbol)
        if existing is None or len(existing["data"]) < len(out):
            capture[symbol] = json.loads(normalized.to_json(orient="split", date_format="iso"))
    return out


class PriceUnavailable(RuntimeError):
    """Raised when a usable price/series cannot be obtained.

    Callers must treat this as "skip this cycle", never as a signal value.
    """


def close_series(symbol: str, period: str = "400d", min_bars: int = 1) -> pd.Series:
    """Adjusted closes with NaN rows dropped.

    Raises ``PriceUnavailable`` if fewer than ``min_bars`` usable bars remain,
    so a short or empty history can't be mistaken for a valid signal.
    """
    cache = _CACHE.get()
    key = (symbol, period)
    if cache is not None and key in cache:
        closes = cache[key]
        if len(closes) < min_bars:
            raise PriceUnavailable(f"{symbol}: insufficient completed history")
        return closes.copy()
    try:
        hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
    except Exception as exc:  # noqa: BLE001 — network/parse errors all mean "no data"
        raise PriceUnavailable(f"{symbol}: history fetch failed: {exc}") from exc

    if hist is None or hist.empty or "Close" not in hist:
        raise PriceUnavailable(f"{symbol}: no history returned for period={period}")

    closes = completed_bars(hist, symbol, "Close")["Close"]
    if len(closes) < min_bars:
        raise PriceUnavailable(
            f"{symbol}: only {len(closes)} usable bars after dropping NaN "
            f"(need {min_bars}) for period={period}"
        )
    if cache is not None:
        cache[key] = closes.copy()
    return closes


def last_close(symbol: str, period: str = "5d") -> float:
    """Most recent non-NaN adjusted close. Raises if unavailable."""
    price = float(close_series(symbol, period=period, min_bars=1).iloc[-1])
    if not (price > 0):
        raise PriceUnavailable(f"{symbol}: last close is not positive ({price})")
    return price


def last_close_and_sma(symbol: str, sma_n: int, period: str | None = None) -> tuple[float, float]:
    """``(last_close, sma)`` for a moving-average regime filter.

    Requires ``sma_n`` usable bars, so the SMA is never computed over a window
    padded with NaN — the other way this filter can silently return garbage.
    """
    # Provider periods count calendar days, while the SMA needs trading
    # sessions. 260 calendar days cannot supply a 200-session window.
    period = period or f"{sma_n * 2 + 60}d"
    closes = close_series(symbol, period=period, min_bars=sma_n)
    sma = float(closes.rolling(sma_n).mean().iloc[-1])
    last = float(closes.iloc[-1])
    if pd.isna(sma) or pd.isna(last):
        raise PriceUnavailable(
            f"{symbol}: SMA{sma_n} computed to NaN over {len(closes)} bars"
        )
    return last, sma


def realized_vol(symbol: str, window: int = 20, periods_per_year: int = 252) -> float:
    """Annualised realised volatility over the last ``window`` sessions.

    Used to scale exposure inversely to volatility. Raises PriceUnavailable on
    bad data like every other accessor here — a vol estimate built from a NaN
    bar would silently size a position wrong, which is the failure this module
    exists to prevent.
    """
    closes = close_series(symbol, period=f"{window * 3 + 40}d", min_bars=window + 1)
    rets = closes.pct_change().dropna().iloc[-window:]
    if len(rets) < window:
        raise PriceUnavailable(
            f"{symbol}: only {len(rets)} returns for a {window}-day vol estimate")
    vol = float(rets.std() * (periods_per_year ** 0.5))
    if not (vol > 0):
        raise PriceUnavailable(f"{symbol}: realised vol is not positive ({vol})")
    return vol

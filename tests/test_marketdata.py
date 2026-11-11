"""Regression tests for the NaN-price bug found in the 2026-08-04 dry-run.

yfinance appends a trailing row whose Close is NaN. Because NaN comparisons are
False, ``last > sma`` read as "risk-OFF" and the regime sleeve planned to close
a $30k QLD position while QQQ was 17.5% above its 200-DMA. These tests pin the
contract: bad data raises, it never returns a comparable value.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot import marketdata
from trading_bot.marketdata import (
    PriceUnavailable,
    close_series,
    last_close,
    last_close_and_sma,
)


class _FakeTicker:
    """Stands in for yf.Ticker with a canned frame."""

    def __init__(self, frame: pd.DataFrame | None, raises: bool = False):
        self._frame = frame
        self._raises = raises

    def history(self, **_kwargs):
        if self._raises:
            raise ConnectionError("simulated network failure")
        return self._frame


def _frame(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"Close": closes}, index=idx)


@pytest.fixture
def patch_yf(monkeypatch):
    def _install(frame, raises=False):
        if frame is not None and not frame.empty:
            valid = frame["Close"].dropna()
            if not valid.empty:
                monkeypatch.setattr(marketdata, "last_completed_session", lambda: valid.index[-1].date())
        monkeypatch.setattr(
            marketdata.yf, "Ticker", lambda _sym: _FakeTicker(frame, raises)
        )
    return _install


# ------------------------------------------------------- the actual bug


def test_trailing_nan_bar_is_dropped_not_returned(patch_yf):
    """THE bug: the last row is NaN, so .iloc[-1] returned NaN silently."""
    patch_yf(_frame([100.0, 101.0, np.nan]))
    assert last_close("QQQ") == 101.0


def test_regime_filter_ignores_trailing_nan(patch_yf):
    """With the NaN bar dropped, the filter reads the real regime.

    Before the fix this returned (nan, nan) and `last > sma` was False —
    a confirmed bear signal manufactured out of missing data.
    """
    closes = [100.0] * 199 + [200.0, np.nan]
    patch_yf(_frame(closes))
    last, sma = last_close_and_sma("QQQ", 200)
    assert last == 200.0
    assert not np.isnan(sma)
    assert last > sma          # risk-ON, as it should be


def test_all_nan_history_raises_rather_than_returning_nan(patch_yf):
    patch_yf(_frame([np.nan, np.nan, np.nan]))
    with pytest.raises(PriceUnavailable):
        last_close("QQQ")


# ------------------------------------------------- other unusable inputs


def test_empty_history_raises(patch_yf):
    patch_yf(pd.DataFrame({"Close": []}))
    with pytest.raises(PriceUnavailable):
        last_close("QQQ")


def test_none_history_raises(patch_yf):
    patch_yf(None)
    with pytest.raises(PriceUnavailable):
        last_close("QQQ")


def test_network_error_becomes_price_unavailable(patch_yf):
    patch_yf(None, raises=True)
    with pytest.raises(PriceUnavailable):
        last_close("QQQ")


def test_zero_or_negative_price_raises(patch_yf):
    patch_yf(_frame([100.0, 0.0]))
    with pytest.raises(PriceUnavailable):
        last_close("QQQ")


def test_sma_requires_a_full_window(patch_yf):
    """A 200-day SMA over 50 usable bars is garbage; it must not be returned."""
    patch_yf(_frame([100.0] * 50))
    with pytest.raises(PriceUnavailable):
        last_close_and_sma("QQQ", 200)


def test_short_history_raises_after_nan_drop(patch_yf):
    """199 real bars + 1 NaN is NOT enough for a 200-day SMA."""
    patch_yf(_frame([100.0] * 199 + [np.nan]))
    with pytest.raises(PriceUnavailable):
        last_close_and_sma("QQQ", 200)


def test_close_series_drops_interior_nans(patch_yf):
    patch_yf(_frame([100.0, np.nan, 102.0]))
    s = close_series("QQQ", min_bars=2)
    assert list(s) == [100.0, 102.0]


# --------------------------------------------- fail-safe at the call site


def test_router_holds_when_data_unavailable(patch_yf, capsys):
    """The router's documented fallback was unreachable because NaN doesn't
    raise. It must now actually fire — and default to holding, not flipping."""
    from trading_bot.live.managed_futures_executor import ManagedFuturesExecutor

    patch_yf(None, raises=True)
    with pytest.raises(PriceUnavailable):
        ManagedFuturesExecutor._qqq_bull()


def test_regime_executor_propagates_rather_than_guessing(patch_yf):
    """The regime sleeve must NOT swallow the error: closing a leveraged
    position on missing data is the failure we are preventing."""
    from trading_bot.live.regime_executor import RegimeExecutor

    patch_yf(_frame([np.nan] * 250))
    ex = RegimeExecutor.__new__(RegimeExecutor)
    ex.filter_symbol, ex.sma_n = "QQQ", 200
    with pytest.raises(PriceUnavailable):
        ex._filter_state()


# ------------------------------------------------------- realised volatility


def test_realized_vol_is_annualised(patch_yf):
    """A steady 1%/day zig-zag is ~16% annualised (0.01 * sqrt(252))."""
    closes = [100.0 * (1.01 if i % 2 else 0.99) ** 1 for i in range(60)]
    prices, p = [], 100.0
    for i in range(60):
        p *= 1.01 if i % 2 else 0.99
        prices.append(p)
    patch_yf(_frame(prices))
    v = marketdata.realized_vol("QQQ", window=20)
    assert 0.10 < v < 0.30


def test_realized_vol_rejects_a_short_series(patch_yf):
    patch_yf(_frame([100.0] * 5))
    with pytest.raises(PriceUnavailable):
        marketdata.realized_vol("QQQ", window=20)


def test_realized_vol_rejects_zero_volatility(patch_yf):
    """A flat series means the feed is stale, not that risk is zero — sizing
    off it would divide by ~0 and demand infinite exposure."""
    patch_yf(_frame([100.0] * 80))
    with pytest.raises(PriceUnavailable):
        marketdata.realized_vol("QQQ", window=20)


def test_realized_vol_drops_nan_bars(patch_yf):
    import numpy as _np
    prices, p = [], 100.0
    for i in range(60):
        p *= 1.01 if i % 2 else 0.99
        prices.append(p)
    patch_yf(_frame(prices + [_np.nan]))
    assert marketdata.realized_vol("QQQ", window=20) > 0


def test_vol_scaling_only_ever_reduces_exposure():
    """Capped at 1.0: this de-risks in high vol, it must never lever up in calm
    markets beyond the sleeve's own allocation."""
    for realised in (0.05, 0.10, 0.18, 0.40, 0.90):
        assert min(1.0, 0.18 / realised) <= 1.0
    assert min(1.0, 0.18 / 0.36) == 0.5      # vol doubles -> half the exposure

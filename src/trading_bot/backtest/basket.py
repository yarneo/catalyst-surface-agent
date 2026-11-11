"""Equal-weight long/flat trend basket: hold 1/N of each asset whose trend signal
is long, cash otherwise. Shared by the research scripts and the live executor so
backtest and production use identical logic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..strategies.base import Strategy


def trend_basket_panels(data: dict[str, pd.DataFrame], strat: Strategy) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (returns panel R, target-weight panel W) aligned on a common index.

    W[t, coin] = 1/N when that coin's trend signal is long at t, else 0. Weights
    are NOT yet lagged — the caller lags before applying returns (no look-ahead).
    """
    n = len(data)
    rets, weights = {}, {}
    for c, df in data.items():
        sig = strat.generate_signals(df).reindex(df.index).clip(lower=0, upper=1)
        weights[c] = sig / n
        rets[c] = df["close"].pct_change()
    R = pd.DataFrame(rets).sort_index()
    W = pd.DataFrame(weights).reindex(R.index).fillna(0.0)
    return R, W


def basket_returns(R: pd.DataFrame, W: pd.DataFrame, cost_bps: float = 20.0) -> pd.Series:
    """Net per-bar basket returns: lag weights, apply returns, subtract turnover cost."""
    pos = W.shift(1).fillna(0.0)
    gross = (pos * R).sum(axis=1)
    turnover = pos.diff().abs().sum(axis=1).fillna(pos.abs().sum(axis=1))
    return (gross - turnover * cost_bps / 10_000.0).fillna(0.0)


def vol_target_scale(
    R: pd.DataFrame,
    W: pd.DataFrame,
    target_ann_vol: float = 0.20,
    lookback: int = 30,
    periods_per_year: int = 365,
    max_leverage: float = 1.0,
) -> pd.Series:
    """Per-bar scaling factor that targets a fixed annualized basket volatility.

    Estimated from the trailing realized vol of the *unscaled* basket; capped at
    ``max_leverage`` (default 1.0 = never lever up, only de-risk). Both the
    estimate and the resulting scale use only past data; callers still lag the
    final weights by one bar, so there is no look-ahead.
    """
    unscaled = (W.shift(1) * R).sum(axis=1)
    realized = unscaled.rolling(lookback).std() * np.sqrt(periods_per_year)
    return (target_ann_vol / realized).clip(upper=max_leverage).fillna(0.0)


def vol_targeted_basket_returns(
    R: pd.DataFrame,
    W: pd.DataFrame,
    *,
    target_ann_vol: float = 0.20,
    lookback: int = 30,
    periods_per_year: int = 365,
    max_leverage: float = 1.0,
    cost_bps: float = 20.0,
) -> tuple[pd.Series, pd.DataFrame]:
    """Conservative deployment sizing: scale the equal-weight trend basket to a
    target volatility. Returns (net per-bar returns, lagged position weights)."""
    scale = vol_target_scale(R, W, target_ann_vol, lookback, periods_per_year, max_leverage)
    scaled_W = W.mul(scale, axis=0)
    pos = scaled_W.shift(1).fillna(0.0)
    gross = (pos * R).sum(axis=1)
    turnover = pos.diff().abs().sum(axis=1).fillna(pos.abs().sum(axis=1))
    net = (gross - turnover * cost_bps / 10_000.0).fillna(0.0)
    return net, pos

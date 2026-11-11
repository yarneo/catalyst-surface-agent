"""Position sizing.

Sizing matters as much as the signal: the same entries can compound or blow up
depending on bet size. Two tools here:

* **Volatility targeting** — scale exposure so the position's expected
  volatility stays near a target. Calmer markets -> bigger size, turbulent
  markets -> smaller. This alone usually improves risk-adjusted returns and
  tames drawdowns, independent of the signal's quality.
* **Kelly fraction** — the growth-optimal bet size given an edge. We only ever
  use a *fraction* of full Kelly (full Kelly is famously too aggressive and
  assumes you know your edge exactly, which you never do).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..strategies.base import Strategy

TRADING_DAYS = 252


def kelly_fraction(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """Continuous Kelly fraction f* = mean / variance (per-period).

    Returned unclamped; callers should apply a fraction (e.g. 0.25 * f*) and a
    leverage cap. A negative value means no edge — don't bet.
    """
    r = returns.dropna()
    var = r.var(ddof=1)
    if var == 0 or len(r) < 2:
        return 0.0
    return float(r.mean() / var)


class VolatilityTargeted(Strategy):
    """Wrap a base strategy and scale its positions to a volatility target.

    Scaling uses the *asset's* trailing realized volatility (known at decision
    time); the backtester then lags the whole position by one bar, so there's no
    look-ahead. Leverage is capped.
    """

    def __init__(
        self,
        base: Strategy,
        target_ann_vol: float = 0.10,
        lookback: int = 20,
        max_leverage: float = 1.0,
        periods_per_year: int = TRADING_DAYS,
    ):
        self.base = base
        self.target_ann_vol = target_ann_vol
        self.lookback = lookback
        self.max_leverage = max_leverage
        self.periods_per_year = periods_per_year
        self.name = f"{base.name} + vol-target {target_ann_vol:.0%}"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        base_sig = self.base.generate_signals(df)
        asset_ret = df["close"].pct_change()
        realized = asset_ret.rolling(self.lookback).std() * np.sqrt(self.periods_per_year)
        scale = (self.target_ann_vol / realized).clip(upper=self.max_leverage)
        return (base_sig * scale).fillna(0.0).rename("target")

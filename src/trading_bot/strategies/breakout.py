"""Donchian channel breakout — the classic Turtle-style trend strategy.

Go long when price closes above the highest high of the last ``entry`` bars;
exit when it closes below the lowest low of the last ``exit`` bars. Channels use
*prior* bars (``shift(1)``) so the current bar can't break its own channel —
i.e. no look-ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy


class DonchianBreakout(Strategy):
    name = "Donchian breakout"

    def __init__(self, entry: int = 20, exit: int = 10, long_short: bool = False):
        self.entry = entry
        self.exit = exit
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {
            "entry": [20, 50, 100],
            "exit": [10, 20, 50],
            "long_short": [False],
        }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        upper = df["high"].rolling(self.entry).max().shift(1).to_numpy()
        lower = df["low"].rolling(self.exit).min().shift(1).to_numpy()
        close = df["close"].to_numpy()

        pos = np.zeros(len(close))
        state = 0.0
        for i in range(len(close)):
            u, l, c = upper[i], lower[i], close[i]
            if np.isnan(u) or np.isnan(l):
                pos[i] = 0.0
                state = 0.0
                continue
            if state == 0.0:
                if c > u:
                    state = 1.0
                elif self.long_short and c < l:
                    state = -1.0
            elif state == 1.0 and c < l:
                state = 0.0
            elif state == -1.0 and c > u:
                state = 0.0
            pos[i] = state
        return pd.Series(pos, index=df.index, name="target")


class KeltnerBreakout(Strategy):
    """Volatility breakout: long when price closes above an ATR-based upper band
    around an EMA; exit when it falls back below the EMA midline."""

    name = "Keltner breakout"

    def __init__(self, ema: int = 20, atr_period: int = 10, mult: float = 2.0, long_short: bool = False):
        self.ema = ema
        self.atr_period = atr_period
        self.mult = mult
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {"ema": [20, 50], "atr_period": [10, 20], "mult": [1.5, 2.0], "long_short": [False]}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        mid = close.ewm(span=self.ema, min_periods=self.ema).mean()
        prev = close.shift(1)
        tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()
        upper = (mid + self.mult * atr).shift(1).to_numpy()
        lower = (mid - self.mult * atr).shift(1).to_numpy()
        midv = mid.shift(1).to_numpy()
        c = close.to_numpy()

        pos = np.zeros(len(c))
        state = 0.0
        for i in range(len(c)):
            u, l, m, ci = upper[i], lower[i], midv[i], c[i]
            if np.isnan(u) or np.isnan(m):
                pos[i] = 0.0
                state = 0.0
                continue
            if state == 0.0:
                if ci > u:
                    state = 1.0
                elif self.long_short and ci < l:
                    state = -1.0
            elif state == 1.0 and ci < m:
                state = 0.0
            elif state == -1.0 and ci > m:
                state = 0.0
            pos[i] = state
        return pd.Series(pos, index=df.index, name="target")

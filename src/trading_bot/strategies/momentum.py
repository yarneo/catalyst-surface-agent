"""Trend-following baseline: simple moving-average crossover.

Go long when the fast SMA is above the slow SMA (uptrend), flat otherwise.
This is a deliberately plain, well-understood baseline — before trusting any
fancy model we need to beat (or at least understand) this.
"""

from __future__ import annotations

import pandas as pd

from .base import Strategy


class SMACrossover(Strategy):
    name = "SMA crossover"

    def __init__(self, fast: int = 20, slow: int = 100, long_short: bool = False):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window.")
        self.fast = fast
        self.slow = slow
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {
            "fast": [10, 20, 50],
            "slow": [50, 100, 200],
            "long_short": [False],
        }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        fast = close.rolling(self.fast).mean()
        slow = close.rolling(self.slow).mean()
        long = (fast > slow).astype(float)
        if self.long_short:
            signal = long - (fast < slow).astype(float)  # -1 / +1
        else:
            signal = long  # 0 / +1
        # No position until the slow window has enough history.
        return signal.where(slow.notna(), 0.0).rename("target")


class TimeSeriesMomentum(Strategy):
    """Absolute (time-series) momentum: long when the trailing return is positive.

    The most robustly documented anomaly across asset classes (Moskowitz et al.).
    Long-only by default; long/short shorts negative-momentum regimes.
    """

    name = "time-series momentum (ROC)"

    def __init__(self, lookback: int = 90, threshold: float = 0.0, long_short: bool = False):
        self.lookback = lookback
        self.threshold = threshold
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {
            "lookback": [20, 60, 120, 200],
            "threshold": [0.0, 0.02],
            "long_short": [False],
        }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        roc = df["close"].pct_change(self.lookback)
        long = (roc > self.threshold).astype(float)
        if self.long_short:
            signal = long - (roc < -self.threshold).astype(float)
        else:
            signal = long
        return signal.where(roc.notna(), 0.0).rename("target")


class MACD(Strategy):
    """MACD: go long when the MACD line is above its signal line (momentum on)."""

    name = "MACD"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, long_short: bool = False):
        if fast >= slow:
            raise ValueError("fast must be shorter than slow.")
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {"fast": [8, 12], "slow": [21, 26], "signal": [9], "long_short": [False]}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        macd = (
            close.ewm(span=self.fast, min_periods=self.slow).mean()
            - close.ewm(span=self.slow, min_periods=self.slow).mean()
        )
        sig = macd.ewm(span=self.signal, min_periods=self.slow).mean()
        long = (macd > sig).astype(float)
        out = long - (macd < sig).astype(float) if self.long_short else long
        return out.where(macd.notna() & sig.notna(), 0.0).rename("target")

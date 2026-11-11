"""Mean-reversion strategies. These dominate the survivors on liquid index/sector
ETFs (which mean-revert), per large backtest sweeps.

Stateful entry/exit (hysteresis) is implemented over NumPy arrays for speed —
important now that the retraining loop searches every family across a whole
universe.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy


def _run_hysteresis(
    signal_vals: np.ndarray,
    *,
    long_entry,
    long_exit,
    short_entry=None,
    short_exit=None,
    long_short: bool = False,
) -> np.ndarray:
    """Generic stateful long/short state machine over an indicator array.

    The ``*_entry``/``*_exit`` callables take the indicator value and return bool.
    NaN indicator -> flat. Returns a position array of -1/0/+1.
    """
    pos = np.zeros(len(signal_vals))
    state = 0.0
    for i, v in enumerate(signal_vals):
        if np.isnan(v):
            state = 0.0
            pos[i] = 0.0
            continue
        if state == 0.0:
            if long_entry(v):
                state = 1.0
            elif long_short and short_entry is not None and short_entry(v):
                state = -1.0
        elif state == 1.0 and long_exit(v):
            state = 0.0
        elif state == -1.0 and short_exit is not None and short_exit(v):
            state = 0.0
        pos[i] = state
    return pos


class MeanReversion(Strategy):
    """Buy when price is far below its rolling mean (low z-score); exit on reversion."""

    name = "mean reversion (z-score)"

    def __init__(
        self,
        lookback: int = 20,
        entry_z: float = 1.0,
        exit_z: float = 0.0,
        long_short: bool = False,
    ):
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {
            "lookback": [10, 20, 50],
            "entry_z": [1.0, 1.5, 2.0],
            "exit_z": [0.0, 0.5],
            "long_short": [False],
        }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        mean = close.rolling(self.lookback).mean()
        std = close.rolling(self.lookback).std()
        z = ((close - mean) / std).to_numpy()
        pos = _run_hysteresis(
            z,
            long_entry=lambda v: v <= -self.entry_z,
            long_exit=lambda v: v >= -self.exit_z,
            short_entry=lambda v: v >= self.entry_z,
            short_exit=lambda v: v <= self.exit_z,
            long_short=self.long_short,
        )
        return pd.Series(pos, index=df.index, name="target")


class RSIReversion(Strategy):
    """RSI mean-reversion: buy oversold, exit on recovery toward the midline."""

    name = "RSI mean-reversion"

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        exit_level: float = 50.0,
        long_short: bool = False,
    ):
        self.period = period
        self.oversold = oversold
        self.exit_level = exit_level
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {
            "period": [7, 14, 21],
            "oversold": [20.0, 30.0],
            "exit_level": [50.0, 60.0],
            "long_short": [False],
        }

    def _rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.period).mean()
        rs = gain / loss
        return 100 - 100 / (1 + rs)

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        rsi = self._rsi(df["close"]).to_numpy()
        pos = _run_hysteresis(
            rsi,
            long_entry=lambda v: v <= self.oversold,
            long_exit=lambda v: v >= self.exit_level,
            short_entry=lambda v: v >= (100 - self.oversold),
            short_exit=lambda v: v <= (100 - self.exit_level),
            long_short=self.long_short,
        )
        return pd.Series(pos, index=df.index, name="target")


class BollingerReversion(Strategy):
    """Bollinger %B mean-reversion (the video's `bb_reversion`/`percent_b_reversion`).

    %B = (close - lower band) / (upper band - lower band), bands at mean ± k·std.
    Buy when %B is low (near/below the lower band), exit when it reverts up.
    """

    name = "Bollinger %B reversion"

    def __init__(
        self,
        lookback: int = 20,
        num_std: float = 2.0,
        entry_b: float = 0.0,
        exit_b: float = 0.5,
        long_short: bool = False,
    ):
        self.lookback = lookback
        self.num_std = num_std
        self.entry_b = entry_b
        self.exit_b = exit_b
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {
            "lookback": [10, 20, 50],
            "num_std": [2.0, 2.5],
            "entry_b": [0.0, 0.05],
            "exit_b": [0.5, 0.8],
            "long_short": [False],
        }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        mean = close.rolling(self.lookback).mean()
        std = close.rolling(self.lookback).std()
        upper = mean + self.num_std * std
        lower = mean - self.num_std * std
        pb = ((close - lower) / (upper - lower)).to_numpy()
        pos = _run_hysteresis(
            pb,
            long_entry=lambda v: v <= self.entry_b,
            long_exit=lambda v: v >= self.exit_b,
            short_entry=lambda v: v >= (1.0 - self.entry_b),
            short_exit=lambda v: v <= (1.0 - self.exit_b),
            long_short=self.long_short,
        )
        return pd.Series(pos, index=df.index, name="target")


class CCIReversion(Strategy):
    """Commodity Channel Index reversion: buy deep-negative CCI, exit on recovery."""

    name = "CCI reversion"

    def __init__(self, period: int = 20, entry: float = -100.0, exit_cci: float = 0.0, long_short: bool = False):
        self.period = period
        self.entry = entry
        self.exit_cci = exit_cci
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {"period": [14, 20], "entry": [-100.0, -150.0], "exit_cci": [0.0, 100.0], "long_short": [False]}

    def _cci(self, df: pd.DataFrame) -> pd.Series:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        sma = tp.rolling(self.period).mean()
        mad = (tp - sma).abs().rolling(self.period).mean().replace(0, np.nan)
        return (tp - sma) / (0.015 * mad)

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        cci = self._cci(df).to_numpy()
        pos = _run_hysteresis(
            cci,
            long_entry=lambda v: v <= self.entry,
            long_exit=lambda v: v >= self.exit_cci,
            short_entry=lambda v: v >= -self.entry,
            short_exit=lambda v: v <= -self.exit_cci,
            long_short=self.long_short,
        )
        return pd.Series(pos, index=df.index, name="target")


class BollingerRSICombo(Strategy):
    """Confluence: enter long only when price is below the lower Bollinger band
    AND RSI is oversold (two independent oversold signals agree); exit on %B recovery."""

    name = "Bollinger+RSI combo"

    def __init__(
        self,
        lookback: int = 20,
        num_std: float = 2.0,
        rsi_period: int = 14,
        oversold: float = 30.0,
        exit_b: float = 0.5,
        long_short: bool = False,
    ):
        self.lookback = lookback
        self.num_std = num_std
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.exit_b = exit_b
        self.long_short = long_short

    @staticmethod
    def param_grid() -> dict[str, list]:
        return {
            "lookback": [20],
            "num_std": [2.0, 2.5],
            "rsi_period": [14],
            "oversold": [30.0, 40.0],
            "exit_b": [0.5, 0.8],
            "long_short": [False],
        }

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        close = df["close"]
        mean = close.rolling(self.lookback).mean()
        std = close.rolling(self.lookback).std()
        upper = mean + self.num_std * std
        lower = mean - self.num_std * std
        pb = ((close - lower) / (upper - lower)).to_numpy()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self.rsi_period).mean()
        rsi = (100 - 100 / (1 + gain / loss)).to_numpy()

        pos = np.zeros(len(pb))
        state = 0.0
        for i in range(len(pb)):
            b, r = pb[i], rsi[i]
            if np.isnan(b) or np.isnan(r):
                pos[i] = 0.0
                state = 0.0
                continue
            if state == 0.0 and b <= 0.0 and r <= self.oversold:
                state = 1.0
            elif state == 1.0 and b >= self.exit_b:
                state = 0.0
            pos[i] = state
        return pd.Series(pos, index=df.index, name="target")

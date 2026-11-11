"""Ensemble trend strategy: the average of several independent trend rules.

Each member emits a long/flat (1/0) signal; the ensemble holds a coin in
proportion to how many members agree it's trending up (signal in {0, 1/3, 2/3,
1}). Averaging diversifies away the risk of any single rule being wrong or
overfit — the most robust choice from the crypto validation, where Keltner,
Donchian and MACD all independently cleared a high Deflated Sharpe.
"""

from __future__ import annotations

import pandas as pd

from .base import Strategy
from .breakout import DonchianBreakout, KeltnerBreakout
from .momentum import MACD


class EnsembleTrend(Strategy):
    name = "ensemble trend (Keltner+Donchian+MACD)"

    def __init__(self, members: list[Strategy] | None = None):
        # Validated, pre-specified members (textbook params, not fitted).
        self.members = members or [
            KeltnerBreakout(ema=20, atr_period=10, mult=2.0),
            DonchianBreakout(entry=50, exit=20),
            MACD(fast=12, slow=26, signal=9),
        ]

    @staticmethod
    def param_grid() -> dict[str, list]:
        # Fixed ensemble — a single configuration (no fitting, no overfitting).
        return {}

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        sigs = [m.generate_signals(df).clip(lower=0, upper=1) for m in self.members]
        return pd.concat(sigs, axis=1).mean(axis=1).rename("target")

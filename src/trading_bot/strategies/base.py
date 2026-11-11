"""Strategy interface.

A strategy maps price history to a *target position* series in [-1, 1], where
the value at bar ``t`` is the desired exposure decided using information up to
and including the close of bar ``t``. The backtester is responsible for lagging
this by one bar before applying returns, so strategies must NOT peek ahead.

Long-only strategies return values in [0, 1]; long/short use [-1, 1].
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    #: Human-readable name used in reports.
    name: str = "strategy"

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return target positions aligned to ``df.index``.

        Implementations may only use data at or before each timestamp. Use
        rolling windows and ``shift`` where needed; never reference future rows.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        params = ", ".join(f"{k}={v}" for k, v in vars(self).items())
        return f"{self.__class__.__name__}({params})"

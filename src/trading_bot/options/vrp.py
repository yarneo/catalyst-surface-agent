"""Variance risk premium: what options charge for vol vs what vol we expect.

The strategy in one line: sell volatility where it is priced above what we
forecast, buy it where it is priced below, and stand aside otherwise.

Why volatility and not direction — this is the whole justification, and it is
measured, not assumed (see research/ml-cross-sectional and
research/vol-forecasting):

    predicting next-day RETURNS      linear OOS R² +0.008, gradient boosting -0.136
    predicting next-month VOLATILITY linear OOS R² +0.253, HAR-RV        +0.231

Direction is unpredictable from public price data; volatility is not. Options
are the instrument that pays for a volatility view, so that is what we trade.

**The forecast uses HAR-RV, and that is deliberate even though the volatility
study REJECTED HAR-RV.** It was rejected as a *risk control*, where the loss is
asymmetric — over-sizing in turbulence costs far more than under-sizing in calm,
so raw realised vol's conservative bias beat the more accurate model. Here the
loss is symmetric: we are comparing a forecast against a market price, and being
wrong in either direction costs the same. Accuracy is the right objective now,
so the more accurate model wins. Same research, opposite conclusion, because the
question changed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..marketdata import PriceUnavailable, close_series

ANN = float(np.sqrt(252))

# Fitted on QQQ 2006-2018 (train only, never refitted on test) — see
# research/vol-forecasting/benchmark_forecasts.py. Coefficients sum to ~0.71
# with a +0.056 intercept, i.e. the forecast shrinks toward the long-run mean,
# which is exactly the mean reversion raw realised vol fails to capture.
HAR_CONST, HAR_D, HAR_W, HAR_M = 0.0555, 0.0323, 0.1991, 0.4772


@dataclass(frozen=True)
class VolForecast:
    symbol: str
    realized_20d: float
    forecast: float

    @property
    def shrinkage(self) -> float:
        """How far the forecast pulls toward the mean. Large values mean the
        recent window is unrepresentative — worth surfacing rather than hiding."""
        return self.forecast - self.realized_20d


def forecast_vol(symbol: str) -> VolForecast:
    """HAR-RV forecast of realised vol over roughly the next month."""
    closes = close_series(symbol, period="200d", min_bars=70)
    r = closes.pct_change().dropna()
    if len(r) < 66:
        raise PriceUnavailable(f"{symbol}: {len(r)} returns, need 66 for HAR-RV")
    rv_d = float(abs(r.iloc[-1]) * ANN)
    rv_w = float(r.iloc[-5:].std() * ANN)
    rv_m = float(r.iloc[-22:].std() * ANN)
    f = HAR_CONST + HAR_D * rv_d + HAR_W * rv_w + HAR_M * rv_m
    if not (f > 0):
        raise PriceUnavailable(f"{symbol}: HAR-RV produced {f}")
    return VolForecast(symbol, rv_m, float(f))


@dataclass(frozen=True)
class VRPSignal:
    symbol: str
    implied: float
    forecast: float
    realized_20d: float
    n_strikes: int          # how many contracts the IV was averaged over
    expiry: str
    dte: int

    @property
    def vrp(self) -> float:
        """Implied minus forecast, in vol points. Positive = options expensive."""
        return self.implied - self.forecast

    @property
    def vrp_ratio(self) -> float:
        """Implied / forecast. Scale-free, so a 3-point premium on a 10-vol name
        is not treated the same as 3 points on a 60-vol name — which matters,
        because the second is noise and the first is a real edge."""
        return self.implied / self.forecast if self.forecast > 0 else float("nan")

    def direction(self, sell_ratio: float = 1.15, buy_ratio: float = 0.85) -> str:
        """SELL / BUY / STAND_ASIDE.

        Thresholds are on the RATIO, not the point difference, for the reason
        above. Deliberately wide: the variance risk premium is small and the
        bid-ask on options is not, so a marginal signal is a losing trade after
        costs. Standing aside is the default and should be the common case.
        """
        if self.n_strikes < 2:
            return "STAND_ASIDE"          # one strike is not a surface
        if self.vrp_ratio >= sell_ratio:
            return "SELL_VOL"
        if self.vrp_ratio <= buy_ratio:
            return "BUY_VOL"
        return "STAND_ASIDE"

    def explain(self) -> str:
        d = self.direction()
        verb = {"SELL_VOL": "options rich", "BUY_VOL": "options cheap",
                "STAND_ASIDE": "fairly priced"}[d]
        return (f"{self.symbol}: implied {self.implied:.1%} vs forecast "
                f"{self.forecast:.1%} ({self.vrp_ratio:.2f}x) — {verb} → {d} "
                f"[{self.dte}d, {self.n_strikes} strikes]")

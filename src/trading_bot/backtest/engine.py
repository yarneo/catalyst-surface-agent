"""Vectorized, look-ahead-safe backtester.

Design choices that keep results honest:

* **No look-ahead.** A target position decided at the close of bar ``t`` is
  lagged one bar before it earns the bar's return — i.e. we trade *after* the
  signal is known, never on the same bar that produced it.
* **Realistic costs.** Every change in position pays ``cost_bps`` (commission +
  modeled slippage) on the traded notional. Free commissions don't mean free
  trading; the spread and slippage still bite.
* **Same code path as live (later).** Strategies emit positions; this engine
  only applies returns and costs. The live executor will consume the identical
  signal series, so backtest and production can't silently diverge.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..strategies.base import Strategy
from .metrics import Metrics, compute_metrics


@dataclass
class BacktestResult:
    equity: pd.Series           # portfolio value over time
    returns: pd.Series          # per-bar strategy returns (net of costs)
    position: pd.Series         # held position per bar (already lagged)
    benchmark_equity: pd.Series  # buy & hold the asset, same capital
    trades: pd.DataFrame        # one row per round-trip
    metrics: Metrics
    benchmark_metrics: Metrics


class Backtester:
    def __init__(
        self,
        initial_capital: float = 100_000.0,
        cost_bps: float = 5.0,
        periods_per_year: int = 252,
    ):
        # cost_bps applies to the *traded* notional on each position change.
        self.initial_capital = initial_capital
        self.cost_rate = cost_bps / 10_000.0
        self.periods_per_year = periods_per_year

    def run(self, df: pd.DataFrame, strategy: Strategy) -> BacktestResult:
        target = strategy.generate_signals(df).reindex(df.index).fillna(0.0)
        # Lag by one bar: act on yesterday's signal -> no look-ahead.
        position = target.shift(1).fillna(0.0)

        asset_ret = df["close"].pct_change().fillna(0.0)
        gross_ret = position * asset_ret

        # Cost charged on the fraction of capital traded when position changes.
        turnover = position.diff().abs().fillna(position.abs())
        cost = turnover * self.cost_rate
        net_ret = gross_ret - cost

        equity = (1 + net_ret).cumprod() * self.initial_capital
        benchmark_equity = (1 + asset_ret).cumprod() * self.initial_capital

        trades = self._extract_trades(df, position)

        metrics = compute_metrics(net_ret, equity, position, self.periods_per_year)
        benchmark_metrics = compute_metrics(
            asset_ret,
            benchmark_equity,
            pd.Series(1.0, index=df.index),
            self.periods_per_year,
        )
        return BacktestResult(
            equity=equity,
            returns=net_ret,
            position=position,
            benchmark_equity=benchmark_equity,
            trades=trades,
            metrics=metrics,
            benchmark_metrics=benchmark_metrics,
        )

    @staticmethod
    def _extract_trades(df: pd.DataFrame, position: pd.Series) -> pd.DataFrame:
        """Collapse the position series into round-trip trades for reporting."""
        rows: list[dict] = []
        price = df["close"]
        in_pos = 0.0
        entry_date = None
        entry_price = 0.0
        for date, pos in position.items():
            if pos != in_pos:
                if in_pos != 0.0:  # closing (or flipping) an open position
                    ret = (price.loc[date] / entry_price - 1.0) * (1 if in_pos > 0 else -1)
                    rows.append(
                        {
                            "entry_date": entry_date,
                            "exit_date": date,
                            "direction": "long" if in_pos > 0 else "short",
                            "entry_price": entry_price,
                            "exit_price": float(price.loc[date]),
                            "return": float(ret),
                        }
                    )
                if pos != 0.0:  # opening a new position
                    entry_date = date
                    entry_price = float(price.loc[date])
                in_pos = pos
        return pd.DataFrame(rows)

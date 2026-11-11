"""Cross-sectional (relative) momentum — a.k.a. sector/asset rotation.

Instead of an independent bet per symbol, this is ONE strategy applied across the
whole universe: each rebalance, rank every asset by trailing momentum and hold an
equal-weight basket of the top-k. Because it makes few decisions with few
parameters across many assets, it's far less prone to overfitting than tuning a
separate strategy per ticker — which is why it's a more honest use of a big
universe than the per-symbol sweep.

Long-only by default (rotate into the strongest assets, cash otherwise via fewer
names). Vectorized and look-ahead-safe: weights are decided on a rebalance bar
and lagged one bar before earning returns.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .metrics import Metrics, compute_metrics


def build_price_panel(data_by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Align per-symbol OHLCV frames into one (date × symbol) close-price panel."""
    closes = {s: df["close"] for s, df in data_by_symbol.items()}
    return pd.DataFrame(closes).sort_index()


@dataclass
class CrossSectionalResult:
    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    metrics: Metrics
    benchmark_equity: pd.Series      # equal-weight, buy & hold whole universe
    benchmark_metrics: Metrics


def cross_sectional_momentum(
    prices: pd.DataFrame,
    *,
    lookback: int = 126,
    top_k: int = 3,
    rebalance: int = 21,
    cost_bps: float = 5.0,
    initial_capital: float = 100_000.0,
    periods_per_year: int = 252,
) -> CrossSectionalResult:
    rets = prices.pct_change()
    momentum = prices.pct_change(lookback)

    # Decide weights only on rebalance bars; hold them in between.
    weights = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    for d in prices.index[::rebalance]:
        m = momentum.loc[d].dropna()
        if len(m) < top_k:
            continue
        row = pd.Series(0.0, index=prices.columns)
        winners = m.nlargest(top_k).index
        # Only hold assets with positive momentum (else stay in cash on that sleeve).
        winners = [s for s in winners if m[s] > 0]
        if winners:
            row[winners] = 1.0 / top_k
        weights.loc[d] = row
    weights = weights.ffill().fillna(0.0)

    pos = weights.shift(1).fillna(0.0)              # no look-ahead
    gross = (pos * rets).sum(axis=1)
    turnover = pos.diff().abs().sum(axis=1).fillna(pos.abs().sum(axis=1))
    net = (gross - turnover * cost_bps / 10_000.0).fillna(0.0)
    equity = (1 + net).cumprod() * initial_capital

    # Benchmark: equal-weight, always-invested whole universe.
    bench_ret = rets.mean(axis=1).fillna(0.0)
    bench_equity = (1 + bench_ret).cumprod() * initial_capital

    return CrossSectionalResult(
        equity=equity,
        returns=net,
        weights=weights,
        metrics=compute_metrics(net, equity, (pos != 0).any(axis=1).astype(float), periods_per_year),
        benchmark_equity=bench_equity,
        benchmark_metrics=compute_metrics(
            bench_ret, bench_equity, pd.Series(1.0, index=prices.index), periods_per_year
        ),
    )

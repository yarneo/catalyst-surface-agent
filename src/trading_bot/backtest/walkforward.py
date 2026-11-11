"""Walk-forward analysis.

We slide a (train, test) window across history. In each fold we optimize the
strategy on the *train* slice, then trade those frozen params on the immediately
following *test* slice — data the optimizer never saw. Stitching all the test
slices together yields one continuous out-of-sample equity curve that mimics how
the strategy would actually have been run: periodically re-tuned on the past,
deployed on the unknown future.

An ``embargo`` gap can be inserted between train and test so that rolling-window
features computed near the boundary can't leak future information.

Anchored (expanding) vs. rolling windows are both supported.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..strategies.base import Strategy
from .engine import Backtester
from .metrics import Metrics, compute_metrics
from .optimize import grid_search, iter_param_grid


@dataclass
class WalkForwardResult:
    oos_equity: pd.Series         # stitched out-of-sample portfolio value
    oos_returns: pd.Series
    oos_position: pd.Series
    benchmark_equity: pd.Series   # buy & hold over the same OOS span
    folds: pd.DataFrame           # per-fold windows, chosen params, scores
    metrics: Metrics
    benchmark_metrics: Metrics
    n_unique_configs: int         # grid breadth -> use for Sharpe deflation


class WalkForward:
    def __init__(
        self,
        train_size: int,
        test_size: int,
        embargo: int = 0,
        anchored: bool = False,
    ):
        self.train_size = train_size
        self.test_size = test_size
        self.embargo = embargo
        self.anchored = anchored

    def _fold_bounds(self, n: int) -> list[tuple[int, int, int, int]]:
        """Return (train_start, train_end, test_start, test_end) index bounds.

        Consecutive test windows are contiguous, so the stitched OOS curve has
        no gaps (the embargo only offsets the very first test window).
        """
        folds = []
        train_end = self.train_size
        while True:
            test_start = train_end + self.embargo
            test_end = test_start + self.test_size
            if test_end > n:
                break
            train_start = 0 if self.anchored else max(0, train_end - self.train_size)
            folds.append((train_start, train_end, test_start, test_end))
            train_end += self.test_size
        return folds

    def run(
        self,
        df: pd.DataFrame,
        strategy_cls: type[Strategy],
        param_grid: dict[str, list],
        backtester: Backtester,
        metric: str = "sharpe",
    ) -> WalkForwardResult:
        bounds = self._fold_bounds(len(df))
        if not bounds:
            raise ValueError(
                f"Not enough data ({len(df)} bars) for train={self.train_size} "
                f"+ test={self.test_size}."
            )

        oos_ret_parts, oos_pos_parts, rows = [], [], []
        for tr_start, tr_end, te_start, te_end in bounds:
            train_df = df.iloc[tr_start:tr_end]
            search = grid_search(train_df, strategy_cls, param_grid, backtester, metric)

            # Evaluate on the test slice, but feed prior bars as warmup so
            # rolling features are populated before the OOS window begins.
            eval_df = df.iloc[tr_start:te_end]
            res = backtester.run(eval_df, strategy_cls(**search.best_params))
            test_index = df.index[te_start:te_end]
            te_ret = res.returns.reindex(test_index)
            te_pos = res.position.reindex(test_index)

            oos_ret_parts.append(te_ret)
            oos_pos_parts.append(te_pos)
            rows.append(
                {
                    "train_start": df.index[tr_start].date(),
                    "train_end": df.index[tr_end - 1].date(),
                    "test_start": df.index[te_start].date(),
                    "test_end": df.index[te_end - 1].date(),
                    "best_params": search.best_params,
                    "train_sharpe": round(search.best_score, 2),
                    "test_sharpe": round(
                        compute_metrics(
                            te_ret, (1 + te_ret).cumprod(), te_pos,
                            backtester.periods_per_year,
                        ).sharpe,
                        2,
                    ),
                }
            )

        oos_returns = pd.concat(oos_ret_parts).fillna(0.0)
        oos_position = pd.concat(oos_pos_parts).fillna(0.0)
        capital = backtester.initial_capital
        oos_equity = (1 + oos_returns).cumprod() * capital

        bench_ret = df["close"].pct_change().reindex(oos_returns.index).fillna(0.0)
        benchmark_equity = (1 + bench_ret).cumprod() * capital

        n_unique = sum(1 for _ in iter_param_grid(param_grid))
        return WalkForwardResult(
            oos_equity=oos_equity,
            oos_returns=oos_returns,
            oos_position=oos_position,
            benchmark_equity=benchmark_equity,
            folds=pd.DataFrame(rows),
            metrics=compute_metrics(
                oos_returns, oos_equity, oos_position, backtester.periods_per_year
            ),
            benchmark_metrics=compute_metrics(
                bench_ret, benchmark_equity, pd.Series(1.0, index=oos_returns.index),
                backtester.periods_per_year,
            ),
            n_unique_configs=n_unique,
        )

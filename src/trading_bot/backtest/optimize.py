"""Grid-search parameter optimization over a single data slice.

Used *inside* each walk-forward fold: we optimize on the fold's training slice
only, then the walk-forward validator evaluates the chosen params on the
following unseen slice. Optimizing on the whole history and reporting that
number is the classic way to produce a beautiful, worthless backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import pandas as pd

from ..strategies.base import Strategy
from .engine import Backtester


@dataclass
class SearchResult:
    best_params: dict
    best_score: float
    n_evaluated: int  # valid param combos actually tested
    scores: tuple[float, ...] = ()  # metric for every evaluated config — feeds the
    #                                 trial-Sharpe dispersion in the Deflated Sharpe gate


def iter_param_grid(param_grid: dict[str, list]):
    """Yield every combination of params as a dict."""
    keys = list(param_grid)
    for combo in product(*(param_grid[k] for k in keys)):
        yield dict(zip(keys, combo))


def grid_search(
    df: pd.DataFrame,
    strategy_cls: type[Strategy],
    param_grid: dict[str, list],
    backtester: Backtester,
    metric: str = "sharpe",
) -> SearchResult:
    """Return the params maximizing ``metric`` on ``df``.

    Invalid combinations (those a strategy rejects in ``__init__``, e.g. a fast
    window not shorter than the slow one) are skipped.
    """
    best_params: dict | None = None
    best_score = float("-inf")
    n = 0
    scores: list[float] = []
    for params in iter_param_grid(param_grid):
        try:
            strat = strategy_cls(**params)
        except ValueError:
            continue
        res = backtester.run(df, strat)
        score = getattr(res.metrics, metric)
        n += 1
        if score == score and abs(score) != float("inf"):  # keep finite scores only
            scores.append(float(score))
        if score > best_score:
            best_score, best_params = score, params
    if best_params is None:
        raise ValueError("No valid parameter combinations in the grid.")
    return SearchResult(best_params=best_params, best_score=best_score,
                        n_evaluated=n, scores=tuple(scores))

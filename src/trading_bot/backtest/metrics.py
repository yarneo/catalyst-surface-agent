"""Performance metrics, including overfitting-aware ones.

Standard metrics (Sharpe, Sortino, drawdown) tell you how a single backtest
looked. The Probabilistic and Deflated Sharpe ratios tell you how much to
*believe* it — the latter discounts your Sharpe by how many strategy variants
you tried, which is the main way backtests lie. See Bailey & Lopez de Prado,
"The Deflated Sharpe Ratio" (2014).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

TRADING_DAYS = 252
EULER_MASCHERONI = 0.5772156649015329


@dataclass
class Metrics:
    total_return: float
    cagr: float
    ann_volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    exposure: float  # fraction of time holding a position

    def as_dict(self) -> dict[str, float]:
        return self.__dict__.copy()


def _annualization(periods_per_year: int) -> float:
    return float(np.sqrt(periods_per_year))


def sharpe_ratio(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    if r.std(ddof=1) == 0 or len(r) < 2:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * _annualization(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    downside = r[r < 0]
    dd = downside.std(ddof=1)
    if dd == 0 or len(r) < 2:
        return 0.0
    return float(r.mean() / dd * _annualization(periods_per_year))


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def compute_metrics(
    returns: pd.Series,
    equity: pd.Series,
    position: pd.Series,
    periods_per_year: int = TRADING_DAYS,
) -> Metrics:
    r = returns.dropna()
    n_years = len(r) / periods_per_year
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / n_years) - 1.0) if n_years > 0 else 0.0
    ann_vol = float(r.std(ddof=1) * _annualization(periods_per_year))
    mdd = max_drawdown(equity)
    return Metrics(
        total_return=total_return,
        cagr=cagr,
        ann_volatility=ann_vol,
        sharpe=sharpe_ratio(r, periods_per_year),
        sortino=sortino_ratio(r, periods_per_year),
        max_drawdown=mdd,
        calmar=float(cagr / abs(mdd)) if mdd != 0 else 0.0,
        exposure=float((position != 0).mean()),
    )


def probabilistic_sharpe(
    returns: pd.Series, benchmark_sr: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> float:
    """P(true Sharpe > benchmark_sr), adjusting for skew & fat tails.

    Returns a probability in [0, 1]. Both Sharpes are expressed per-period
    internally. A PSR near 1 means the positive Sharpe is unlikely to be luck.
    """
    r = returns.dropna()
    n = len(r)
    if n < 3 or r.std(ddof=1) == 0:
        return 0.0
    sr = r.mean() / r.std(ddof=1)  # per-period
    sr_star = benchmark_sr / np.sqrt(periods_per_year)  # de-annualize benchmark
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))  # non-excess
    denom = np.sqrt(1 - skew * sr + ((kurt - 1) / 4) * sr**2)
    if denom == 0:
        return 0.0
    z = (sr - sr_star) * np.sqrt(n - 1) / denom
    return float(stats.norm.cdf(z))


def deflated_sharpe(
    returns: pd.Series,
    n_trials: int,
    trial_sharpe_std: float | None = None,
    periods_per_year: int = TRADING_DAYS,
) -> float:
    """Deflated Sharpe Ratio: PSR against the Sharpe you'd expect from the best
    of ``n_trials`` random strategies.

    The more variants you test, the higher the bar a real edge must clear.
    ``trial_sharpe_std`` is the std-dev of (per-period) Sharpes across the
    variants tried; if unknown we fall back to this run's estimation noise,
    which makes DSR a conservative single-run sanity check.
    """
    r = returns.dropna()
    n = len(r)
    if n < 3 or n_trials < 1:
        return 0.0
    if trial_sharpe_std is None:
        trial_sharpe_std = 1.0 / np.sqrt(n)  # ~SE of a Sharpe estimate
    # Expected maximum of n_trials draws from N(0, trial_sharpe_std^2).
    if n_trials == 1:
        sr_star_pp = 0.0
    else:
        e1 = stats.norm.ppf(1 - 1 / n_trials)
        e2 = stats.norm.ppf(1 - 1 / (n_trials * np.e))
        sr_star_pp = trial_sharpe_std * ((1 - EULER_MASCHERONI) * e1 + EULER_MASCHERONI * e2)
    benchmark_sr_ann = sr_star_pp * np.sqrt(periods_per_year)
    return probabilistic_sharpe(r, benchmark_sr=benchmark_sr_ann, periods_per_year=periods_per_year)

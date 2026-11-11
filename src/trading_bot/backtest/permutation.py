"""Monte Carlo bar-permutation tests for strategy validation.

Implements the OHLC shuffling procedure from Timothy Masters'
"Permutation and Randomization Tests for Trading System Development" — the
same test the NeuroTrader video uses. The idea is to keep every marginal
distribution of the bar series intact (drift, volatility, skew, kurt of the
returns, gap distribution, intrabar range distribution) while destroying the
serial structure a strategy might exploit. If the strategy's edge is real, it
should die on permuted data.

Two distinct tests are useful:

1. **IS (in-sample) permutation test** — re-runs the FULL grid search on each
   permutation. Returns the fraction of permutations whose best score >= the
   real best score. This tests the strategy *family's* ability to find
   patterns in noise (the same thing DSR estimates analytically). A real edge
   should give p < 1% here.

2. **OOS permutation test** — freezes the chosen params, permutes a held-out
   slice, evaluates the frozen strategy on the permuted slice. p = fraction of
   permutations with OOS Sharpe >= real OOS Sharpe. Complements DSR — DSR
   handles in-sample multiple testing, this tests whether the OOS result
   itself is statistically significant.

Both are expensive. Default n_perm=100 gives p-value resolution of 0.01,
which is enough for a 5% gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

LOG = logging.getLogger("backtest.permutation")


@dataclass
class PermResult:
    real_score: float
    perm_scores: np.ndarray   # shape (n_perm,)
    p_value: float            # fraction of perm_scores >= real_score
    n_perm: int
    mode: str                 # "is_reoptimize" | "oos_frozen"


def permute_ohlc(df: pd.DataFrame, start_index: int = 0,
                 rng: np.random.Generator | None = None) -> pd.DataFrame:
    """Return an OHLC permutation of `df` per Masters' algorithm.

    Decompose each bar into gap, intrabar (h, l, c relative to open). Shuffle
    gaps independently; shuffle (h, l, c) tuples together to keep each bar
    internally consistent. Reconstruct prices from shuffled components.

    `start_index` preserves the first `start_index` bars unchanged — used by
    the walk-forward perm test where the first training fold must stay real.

    Volume (if present) is shuffled independently of price.
    """
    if rng is None:
        rng = np.random.default_rng()
    if df.empty or len(df) < 3:
        return df.copy()
    if start_index < 0 or start_index >= len(df):
        raise ValueError(f"start_index {start_index} outside [0, {len(df)})")
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        raise ValueError("df must have open/high/low/close columns")

    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    n = len(df)

    # Decompose in log space — multiplicative reconstruction
    gap   = np.log(o[1:] / c[:-1])         # shape n-1
    h_rel = np.log(h / o)                  # shape n
    l_rel = np.log(l / o)                  # shape n
    c_rel = np.log(c / o)                  # shape n

    # The shuffle window starts at start_index. Indices < start_index are
    # frozen. The first bar (index 0) is always frozen — there's no gap into
    # it. So bars to permute are start_index+1 .. n-1 for gaps, start_index .. n-1
    # for intrabar moves (but only if start_index > 0).
    sw = max(start_index, 1)   # gap index sw-1 maps to bar sw

    # Shuffle gaps for bars >= sw
    perm_gap_idx = rng.permutation(np.arange(sw - 1, n - 1))
    new_gap = gap.copy()
    new_gap[sw - 1: n - 1] = gap[perm_gap_idx]

    # Shuffle intrabar (h_rel, l_rel, c_rel) tuples for bars >= sw
    perm_intra_idx = rng.permutation(np.arange(sw, n))
    new_h_rel = h_rel.copy()
    new_l_rel = l_rel.copy()
    new_c_rel = c_rel.copy()
    new_h_rel[sw:n] = h_rel[perm_intra_idx]
    new_l_rel[sw:n] = l_rel[perm_intra_idx]
    new_c_rel[sw:n] = c_rel[perm_intra_idx]

    # Reconstruct: close[0] (and everything before sw) unchanged
    new_o = o.copy()
    new_h = h.copy()
    new_l = l.copy()
    new_c = c.copy()
    for i in range(sw, n):
        new_o[i] = new_c[i - 1] * np.exp(new_gap[i - 1])
        new_h[i] = new_o[i] * np.exp(new_h_rel[i])
        new_l[i] = new_o[i] * np.exp(new_l_rel[i])
        new_c[i] = new_o[i] * np.exp(new_c_rel[i])

    out = df.copy()
    out["open"]  = new_o
    out["high"]  = new_h
    out["low"]   = new_l
    out["close"] = new_c
    if "volume" in out.columns:
        vol = out["volume"].to_numpy()
        new_vol = vol.copy()
        new_vol[sw:n] = vol[rng.permutation(np.arange(sw, n))]
        out["volume"] = new_vol
    return out


def mc_pvalue(
    real_score: float,
    score_fn,
    df: pd.DataFrame,
    n_perm: int = 100,
    *,
    start_index: int = 0,
    seed: int | None = None,
    mode: str = "oos_frozen",
) -> PermResult:
    """Generic Monte Carlo p-value.

    `score_fn(permuted_df) -> float` is a callback the caller defines: for an
    IS-reoptimize test it should run grid_search on the permuted slice and
    return the best Sharpe; for an OOS-frozen test it should evaluate fixed
    params on the permuted slice.

    Returns the p-value = P(perm_score >= real_score) under the null that the
    strategy has no edge. Lower is better — p < 0.05 means we'd expect the
    observed score on noise less than 5% of the time.
    """
    rng = np.random.default_rng(seed)
    perm_scores = np.zeros(n_perm, dtype=float)
    n_finite = 0
    for i in range(n_perm):
        try:
            pdf = permute_ohlc(df, start_index=start_index, rng=rng)
            s = float(score_fn(pdf))
        except Exception as e:
            LOG.warning("perm %d failed: %s", i, e)
            s = float("nan")
        perm_scores[i] = s
        if np.isfinite(s):
            n_finite += 1

    finite = perm_scores[np.isfinite(perm_scores)]
    if len(finite) == 0:
        p = float("nan")
    else:
        # "+1 / +1" smoothing so p is never exactly 0 (Davison & Hinkley convention).
        # Coerce out of numpy types — callers serialize this to JSON.
        p = float((np.sum(finite >= real_score) + 1) / (len(finite) + 1))
    LOG.info("mc_pvalue: real=%.3f perm_n=%d (finite=%d) p=%s mode=%s",
             real_score, n_perm, n_finite, f"{p:.3f}" if np.isfinite(p) else "nan", mode)
    return PermResult(real_score=real_score, perm_scores=perm_scores,
                      p_value=p, n_perm=n_perm, mode=mode)

"""The retraining loop — disciplined, promotion-gated self-improvement.

For each symbol:
  1. Split history into a *selection* span and a recent *judge* window.
  2. For every strategy family in the registry, re-optimize params on the
     selection span only -> a per-family candidate. The best across all families
     on the judge window is the **challenger**.
  3. Evaluate the incumbent **champion** on the same judge window (true OOS).
  4. Promote the challenger ONLY if it (a) beats the champion's judge Sharpe by a
     margin AND (b) clears the **Deflated Sharpe gate** — its Sharpe discounted by
     how many configs we tried must still imply real edge. With the gate on, an
     incumbent that can no longer clear the bar is *retired* (the executor then
     falls back to a default), so only confidently-validated strategies trade.
  5. Record the full decision to an append-only ledger.

This is the antidote to "I tested 100,000 strategies and 65 worked": the more you
search, the higher the bar — and a config must prove itself on unseen data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..backtest import Backtester, deflated_sharpe, grid_search
from ..backtest.metrics import Metrics, TRADING_DAYS, compute_metrics
from ..backtest.permutation import mc_pvalue
from ..strategies import REGISTRY
from .champion import Champion, ChampionStore

LEDGER_PATH = Path(__file__).resolve().parents[3] / "data" / "experiments.jsonl"


def _evaluate_on_window(
    df: pd.DataFrame, strategy, judge_start: int, bt: Backtester
) -> tuple[Metrics, pd.Series]:
    """Metrics + per-bar returns over the judge window, warmed up on prior bars."""
    res = bt.run(df, strategy)
    idx = df.index[judge_start:]
    ret = res.returns.reindex(idx).fillna(0.0)
    pos = res.position.reindex(idx).fillna(0.0)
    eq = (1 + ret).cumprod()
    return compute_metrics(ret, eq, pos), ret


def _append_ledger(record: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def retrain_symbol(
    symbol: str,
    df: pd.DataFrame,
    store: ChampionStore,
    *,
    strategies: list[str] | str | None = None,
    judge_size: int = 252,
    margin: float = 0.10,
    require_dsr: float = 0.90,
    require_perm_p: float | None = None,  # opt-in MC perm-test gate; None = skip
    perm_n: int = 100,
    perm_seed: int | None = 42,
    dry_run: bool = False,  # if True: compute everything, but don't mutate champion store
    selection_breadth: int = 1,
    bt: Backtester | None = None,
) -> dict:
    """Run one retraining cycle for a symbol across strategy families.

    ``selection_breadth`` is the number of symbols in the surrounding sweep. It
    multiplies the trial count used for deflation, and defaults to 1 (this
    symbol alone) so a single-symbol call is unaffected.

    Why it exists: the DSR deflates by the configs tried *for this symbol*
    (~108). But a sweep also selects across symbols — run 15 symbols and the
    search has really evaluated ~1,620 candidates, then reported whichever
    cleared the bar. The per-symbol DSR does not see that outer loop, so the
    survivors of a wide sweep look better-validated than they are. Measured
    2026-08-04: at a 0.90 gate over 15 symbols, 6 survived — and 4 of those 6
    were the *same* family (cci_reversion) on correlated sector ETFs, which is
    what selection bias looks like when it is not corrected for.

    This is deliberately conservative — closer to a Bonferroni bound than an
    exact family-wise correction, since the symbols are correlated and their
    trials are not independent. It errs toward under-deploying, which is the
    right direction for a system whose failure mode is trading noise.
    """
    bt = bt or Backtester()
    if strategies is None:
        strategies = list(REGISTRY)
    elif isinstance(strategies, str):
        strategies = [strategies]

    n = len(df)
    if n < judge_size + 60:
        raise ValueError(f"Not enough data for {symbol} ({n} bars).")
    judge_start = n - judge_size
    selection_df = df.iloc[:judge_start]

    # Search every family; the best on the judge window is the challenger.
    best = None  # (name, params, metrics, returns)
    total_configs = 0
    trial_scores: list[float] = []  # Sharpe of EVERY config tried, across families
    for name in strategies:
        cls = REGISTRY[name]
        search = grid_search(selection_df, cls, cls.param_grid(), bt)
        total_configs += search.n_evaluated
        trial_scores.extend(search.scores)
        metrics, ret = _evaluate_on_window(df, cls(**search.best_params), judge_start, bt)
        if best is None or metrics.sharpe > best[2].sharpe:
            best = (name, search.best_params, metrics, ret)

    chall_name, chall_params, chall, chall_ret = best
    # Deflate against the ACTUAL dispersion of the trial Sharpes (the real
    # multiple-testing correction) instead of the 1/√n estimation-noise proxy.
    # grid_search reports annualized Sharpes → convert to per-period for the DSR.
    trial_std = (
        float(pd.Series(trial_scores).std(ddof=1)) / (TRADING_DAYS ** 0.5)
        if len(trial_scores) > 1 else None
    )
    if not trial_std or trial_std <= 0:
        trial_std = None  # degenerate spread → fall back to the conservative default
    # Count the outer symbol loop as part of the search (see docstring).
    effective_trials = max(total_configs, 1) * max(selection_breadth, 1)
    chall_dsr = deflated_sharpe(chall_ret, n_trials=effective_trials,
                                trial_sharpe_std=trial_std)
    dsr_ok = chall_dsr >= require_dsr

    # Optional Monte Carlo permutation test on the JUDGE window with frozen
    # challenger params. This complements DSR — DSR penalizes in-sample
    # multiple testing analytically; the perm test asks "could a worthless
    # strategy have produced this OOS Sharpe by chance?". p < 0.05 ≈ 95%
    # confident the OOS edge isn't noise. Expensive but bounded:
    # perm_n × (1 backtest per family on the judge window).
    chall_perm_p = None
    perm_ok = True
    if require_perm_p is not None:
        judge_df = df.iloc[judge_start:].copy()
        chosen_cls = REGISTRY[chall_name]
        chosen_strategy = chosen_cls(**chall_params)

        def _score_judge(permuted_df: pd.DataFrame) -> float:
            """Score = Sharpe of the FROZEN challenger on the permuted judge slice."""
            res = bt.run(permuted_df, chosen_strategy)
            return float(res.metrics.sharpe)

        perm_res = mc_pvalue(
            real_score=chall.sharpe, score_fn=_score_judge, df=judge_df,
            n_perm=perm_n, seed=perm_seed, mode="oos_frozen",
        )
        chall_perm_p = perm_res.p_value
        perm_ok = chall_perm_p <= require_perm_p

    champ = store.get(symbol)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    champ_sharpe = champ_dsr = None
    retired = False

    if champ is None:
        beats = True
        promote = dsr_ok and perm_ok
    else:
        champ_metrics, champ_ret = _evaluate_on_window(
            df, REGISTRY[champ.strategy](**champ.params), judge_start, bt
        )
        champ_sharpe = champ_metrics.sharpe
        champ_dsr = deflated_sharpe(champ_ret, n_trials=effective_trials,
                                    trial_sharpe_std=trial_std)
        beats = chall.sharpe > champ_sharpe + margin and chall.sharpe > 0
        promote = beats and dsr_ok and perm_ok

    if promote:
        base = f"PROMOTE {chall_name}: Sharpe {chall.sharpe:.2f}, DSR {chall_dsr:.0%}"
        reason = base + (f" > champ {champ_sharpe:.2f}" if champ_sharpe is not None else " (bootstrap)")
    else:
        bits = []
        if champ is not None and not beats:
            bits.append(f"challenger {chall.sharpe:.2f} !> champ {champ_sharpe:.2f}+{margin}")
        if not dsr_ok:
            bits.append(f"DSR {chall_dsr:.0%} < gate {require_dsr:.0%}")
        if not perm_ok and chall_perm_p is not None:
            bits.append(f"perm-p {chall_perm_p:.2f} > gate {require_perm_p:.2f}")
        prefix = "kept champion" if champ is not None else "no bootstrap"
        reason = f"{prefix}: " + "; ".join(bits)
        # Retire an incumbent that no longer clears the confidence gate.
        if champ is not None and require_dsr > 0 and champ_dsr is not None and champ_dsr < require_dsr:
            if not dry_run:
                store.remove(symbol)
            retired = True
            reason += f" | RETIRED champion (DSR {champ_dsr:.0%} < {require_dsr:.0%})"

    record = {
        "ts": now,
        "symbol": symbol,
        "configs_tested": total_configs,
        "selection_breadth": selection_breadth,
        "effective_trials": effective_trials,
        "require_dsr": require_dsr,
        "judge_size": judge_size,
        "judge_start": df.index[judge_start].date().isoformat(),
        "champion_strategy": champ.strategy if champ else None,
        "champion_judge_sharpe": champ_sharpe,
        "champion_deflated_sharpe": champ_dsr,
        "challenger_strategy": chall_name,
        "challenger_params": chall_params,
        "challenger_judge_sharpe": chall.sharpe,
        "challenger_deflated_sharpe": chall_dsr,
        "challenger_perm_p": float(chall_perm_p) if chall_perm_p is not None else None,
        "require_perm_p": require_perm_p,
        # Coerce to Python bool — numpy.bool_ from comparisons isn't json-serializable
        # in some stdlib versions.
        "promoted": bool(promote),
        "retired": bool(retired),
        "dry_run": bool(dry_run),
        "reason": reason if not dry_run else f"{reason} (DRY-RUN: no store mutation)",
    }
    _append_ledger(record)

    if dry_run:
        # Dry-run: skip champion-store mutation. The record still goes to the
        # ledger so the user can review what would have happened.
        return record

    if promote:
        store.set(
            symbol,
            Champion(
                strategy=chall_name,
                params=chall_params,
                judge_sharpe=chall.sharpe,
                promoted_at=now,
                promotions=(champ.promotions + 1) if champ else 1,
                notes=f"{reason} | over {total_configs} configs x {selection_breadth} symbols = {effective_trials} trials",
            ),
        )
    return record


def retrain_universe(
    symbols: list[str],
    data_by_symbol: dict[str, pd.DataFrame],
    store: ChampionStore | None = None,
    **kwargs,
) -> list[dict]:
    store = store or ChampionStore()
    return [retrain_symbol(s, data_by_symbol[s], store, **kwargs) for s in symbols]

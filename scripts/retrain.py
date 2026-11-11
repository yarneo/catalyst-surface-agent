"""Run a retraining cycle: search all strategy families, judge, promote-or-keep.

    uv run python scripts/retrain.py --symbols SPY QQQ
    uv run python scripts/retrain.py --symbols SPY --strategies sma_crossover donchian_breakout

Edits data/champion.json only when a challenger beats the incumbent out-of-sample,
and appends every decision (incl. Deflated Sharpe) to data/experiments.jsonl.
"""

from __future__ import annotations

import argparse
from datetime import date

from trading_bot.config import load_universe
from trading_bot.data import load_ohlcv
from trading_bot.learning import ChampionStore, retrain_symbol
from trading_bot.strategies import REGISTRY


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", default=load_universe())
    p.add_argument("--strategies", nargs="+", default=list(REGISTRY), choices=list(REGISTRY),
                   help="families to search (default: all)")
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--end", default=date.today().isoformat(),
                   help="defaults to today, so each run judges on fresh data")
    p.add_argument("--judge-size", type=int, default=252, help="OOS judge window (bars)")
    p.add_argument("--margin", type=float, default=0.10, help="Sharpe edge needed to promote")
    p.add_argument("--require-dsr", type=float, default=0.90,
                   help="Deflated-Sharpe gate: min confidence to promote/retain (0=off)")
    p.add_argument("--require-perm-p", type=float, default=None,
                   help="Monte Carlo permutation-test gate: max p-value to promote. "
                        "Computed on the judge window with frozen challenger params. "
                        "None = skip (default). Typical: 0.05.")
    p.add_argument("--perm-n", type=int, default=100,
                   help="Number of permutations for the MC test (default 100; 200 for serious gating).")
    p.add_argument("--no-cross-symbol-deflation", action="store_true",
                   help="deflate each symbol by its own configs only. The default "
                        "also counts the outer symbol loop, because a sweep over N "
                        "symbols really evaluates N x configs candidates and reports "
                        "the survivors — see learning/retrain.py.")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute everything (DSR, perm-p, promote/retire decisions) and "
                        "append to the ledger, but DO NOT mutate the champion store. "
                        "Use this to preview what a real retrain would do.")
    args = p.parse_args()

    store = ChampionStore()
    print(f"\n=== Retraining on {', '.join(args.symbols)} ===")
    print(f"families: {', '.join(args.strategies)}")
    print(f"judge window {args.judge_size} bars | promote margin {args.margin} Sharpe\n")

    for sym in args.symbols:
        df = load_ohlcv(sym, args.start, args.end)
        rec = retrain_symbol(
            sym, df, store,
            strategies=args.strategies, judge_size=args.judge_size,
            margin=args.margin, require_dsr=args.require_dsr,
            require_perm_p=args.require_perm_p, perm_n=args.perm_n,
            dry_run=args.dry_run,
            selection_breadth=1 if args.no_cross_symbol_deflation else len(args.symbols),
        )
        dry_tag = " [DRY-RUN]" if args.dry_run else ""
        flag = ("🟢 WOULD PROMOTE" + dry_tag if (args.dry_run and rec["promoted"])
                else "🟢 PROMOTED" if rec["promoted"]
                else "🔴 WOULD RETIRE champion" + dry_tag if (args.dry_run and rec.get("retired"))
                else "🔴 RETIRED champion" if rec.get("retired")
                else "⚪ kept champion" + dry_tag)
        champ_sh = rec["champion_judge_sharpe"]
        champ_str = f"{rec['champion_strategy']} ({champ_sh:.2f})" if champ_sh is not None else "—(none)"
        print(f"{sym}: {flag}   [{rec['configs_tested']} configs tested across families]")
        print(f"   champion   : {champ_str}")
        print(f"   challenger : {rec['challenger_strategy']} {rec['challenger_params']}")
        perm_p = rec.get("challenger_perm_p")
        perm_str = f" | perm-p {perm_p:.2f}" if perm_p is not None else ""
        print(f"                judge Sharpe {rec['challenger_judge_sharpe']:.2f} | "
              f"Deflated SR {rec['challenger_deflated_sharpe']:.1%}{perm_str}")
        print(f"   -> {rec['reason']}\n")

    print("Current deployed champions:")
    for sym, ch in store.all().items():
        print(f"   {sym}: {ch.strategy} {ch.params} "
              f"(judge Sharpe {ch.judge_sharpe:.2f}, promotions {ch.promotions})")


if __name__ == "__main__":
    main()

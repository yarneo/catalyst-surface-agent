"""Live (paper) executor: turn signals into managed orders on a daily cadence.

One process per phase (premarket / open / close), so any state that must outlive
a single run is persisted to ``data/state.json``:

* ``peak_equity``      — for the drawdown kill switch
* ``day_start_equity`` — for the daily-loss breaker
* ``session_date``     — to detect a new trading day
* ``halted_today`` / ``killed`` — breaker flags, restored into the RiskManager

Decision flow each cycle, per symbol:
    fresh daily bars -> strategy signal (last completed bar) -> risk manager
    (sizing already baked into the strategy; manager clamps & can halt) ->
    target notional -> target qty -> order = target - current.

**Dry-run by default.** Nothing is sent unless ``live=True``. Even then it only
touches the paper account (the broker refuses live trading).
"""

from __future__ import annotations

import json
import copy
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..broker import AlpacaBroker
from ..attribution import (
    AI_DECISION_LEDGER,
    ai_authority_gate,
    append_ai_decision,
    read_jsonl,
    summarize_ai_decisions,
)
from ..data import load_ohlcv
from ..marketdata import completed_bars
from ..runtime_state import atomic_json
from ..sessions import now_et, iso_now
from ..risk import RiskManager
from ..strategies.base import Strategy

STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "state.json"


@dataclass
class ExecutorState:
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    session_date: str = ""
    halted_today: bool = False
    killed: bool = False
    baseline_source: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path = STATE_PATH, *, require_existing: bool = False) -> "ExecutorState":
        backup = path.with_suffix(".previous.json")
        values = []
        damaged = False
        for source in (path, backup):
            if source.exists():
                try:
                    candidate = cls(**json.loads(source.read_text()))
                    import math
                    if not all(math.isfinite(v) and v >= 0 for v in (candidate.peak_equity, candidate.day_start_equity)):
                        raise ValueError("invalid risk-state equity")
                    values.append(candidate)
                except (ValueError, TypeError):
                    damaged = True
        if not values:
            if require_existing or damaged:
                raise RuntimeError("risk state missing for an established account")
            return cls()
        latest = max(values, key=lambda s: s.updated_at)
        latest.peak_equity = max(s.peak_equity for s in values)
        latest.killed = any(s.killed for s in values)
        latest.halted_today = damaged or any(s.halted_today for s in values if s.session_date == latest.session_date)
        return latest

    def save(self, path: Path = STATE_PATH) -> None:
        self.updated_at = iso_now()
        atomic_json(path.with_suffix(".previous.json"), asdict(self))
        atomic_json(path, asdict(self))


class PaperExecutor:
    def __init__(
        self,
        strategies: dict[str, Strategy],
        broker: AlpacaBroker | None = None,
        risk_manager: RiskManager | None = None,
        overlay=None,
        live: bool = False,
        lookback_days: int = 400,
    ):
        self.strategies = strategies          # symbol -> (vol-targeted) strategy
        self.broker = broker or AlpacaBroker()
        self.risk = risk_manager or RiskManager()
        self.overlay = overlay                # optional SignalOverlay (sentiment/earnings)
        self.live = live
        self.lookback_days = lookback_days

    # -- helpers -----------------------------------------------------------
    def _recent_bars(self, symbol: str) -> pd.DataFrame:
        end = now_et().date() + timedelta(days=1)
        start = now_et().date() - timedelta(days=self.lookback_days)
        return completed_bars(load_ohlcv(symbol, start.isoformat(), end.isoformat(), use_cache=False), symbol)

    def _positions_by_symbol(self) -> dict[str, object]:
        return {p.symbol: p for p in self.broker.get_positions()}

    def _log(self, msg: str) -> None:
        print(msg)

    # -- main entry --------------------------------------------------------
    def run_phase(self, phase: str) -> ExecutorState:
        assert phase in {"premarket", "open", "close"}
        state = ExecutorState.load()
        # Restore breaker flags so they persist across the day's separate runs.
        self.risk._killed = state.killed
        self.risk._halted_today = state.halted_today

        acct = self.broker.get_account()
        equity = float(acct.portfolio_value)
        today = now_et().date().isoformat()

        # New session bookkeeping (premarket is the natural place, but guard
        # anyway in case premarket didn't run).
        if state.session_date != today:
            state.session_date = today
            state.day_start_equity = float(acct.last_equity)
            state.baseline_source = "broker_prior_close"
            state.halted_today = False
            self.risk.reset_day()
        state.peak_equity = max(state.peak_equity, equity)

        mode = "LIVE (paper)" if self.live else "DRY-RUN"
        self._log(f"\n=== {phase.upper()} | {today} | {mode} ===")
        self._log(
            f"Equity ${equity:,.0f} | day-start ${state.day_start_equity:,.0f} "
            f"| peak ${state.peak_equity:,.0f}"
        )

        if phase == "close":
            self._reconcile(equity, state)
            state.save()
            return state

        # premarket = plan only (no orders); open = execute.
        execute = phase == "open"
        # Guard: if a scheduled run fires while the market is closed (holiday, or
        # the Mac woke from sleep off-hours), plan but never submit stale orders.
        if execute and not self.broker.get_clock().is_open:
            self._log("Market is CLOSED — planning only, no orders this run.")
            execute = False
        positions = self._positions_by_symbol()
        n = len(self.strategies)
        capital_per_symbol = equity / n if n else 0.0

        plans = []
        for symbol, strat in self.strategies.items():
            proposed = self._handle_symbol(
                symbol, strat, positions.get(symbol),
                equity, state, capital_per_symbol, execute, phase,
            )
            if proposed:
                plans.append(proposed)
        for proposed in sorted(plans, key=lambda p: p['side'] == 'buy'):
            order = self.broker.submit_market_order(**proposed)
            self._log(f"    filled {proposed['side']} {order.filled_qty} {proposed['symbol']}")

        # Persist any breaker changes the risk manager made this cycle.
        state.halted_today = self.risk._halted_today
        state.killed = self.risk._killed
        state.save()
        return state

    # -- per-symbol decision ----------------------------------------------
    def _handle_symbol(self, symbol, strat, position, equity, state,
                       capital_per_symbol, execute, phase) -> dict | None:
        bars = self._recent_bars(symbol)
        last_price = float(bars["close"].iloc[-1])
        quant_weight = float(strat.generate_signals(bars).iloc[-1])

        # Alternative-data overlay: sentiment / earnings can only dampen or veto.
        # It remains shadow-advisory until its cost-adjusted counterfactual has
        # enough independent changed decisions and positive measured value-add.
        overlay_weight = quant_weight
        if self.overlay is not None:
            ov = self.overlay.apply(symbol, quant_weight)
            for r in ov.reasons:
                self._log(f"  · {r}")
            overlay_weight = ov.adjusted_weight

        try:
            authority = ai_authority_gate(
                summarize_ai_decisions(read_jsonl(AI_DECISION_LEDGER))
            )
        except Exception as exc:
            authority = {
                "eligible": False,
                "mode": "shadow_advisory",
                "reasons": [f"evidence unavailable: {exc}"],
            }
        desired_weight = overlay_weight if authority["eligible"] else quant_weight
        if self.overlay is not None and overlay_weight != quant_weight and not authority["eligible"]:
            self._log("  · AI proposal measured in shadow; quant target retains order authority")

        cur_qty = float(position.qty) if position else 0.0
        entry_price = float(position.avg_entry_price) if position else None

        # Evaluate both counterfactual branches against exact copies of risk
        # state. Breaker mutations in either shadow branch can never affect the
        # actual decision below.
        no_ai_risk = copy.deepcopy(self.risk)
        no_ai_decision = no_ai_risk.evaluate(
            symbol=symbol,
            desired_weight=quant_weight,
            equity=equity,
            peak_equity=state.peak_equity,
            day_start_equity=state.day_start_equity,
            position_entry_price=entry_price,
            last_price=last_price,
            position_qty=cur_qty,
        )

        with_ai_risk = copy.deepcopy(self.risk)
        with_ai_decision = with_ai_risk.evaluate(
            symbol=symbol,
            desired_weight=overlay_weight,
            equity=equity,
            peak_equity=state.peak_equity,
            day_start_equity=state.day_start_equity,
            position_entry_price=entry_price,
            last_price=last_price,
            position_qty=cur_qty,
        )
        decision = self.risk.evaluate(
            symbol=symbol,
            desired_weight=desired_weight,
            equity=equity,
            peak_equity=state.peak_equity,
            day_start_equity=state.day_start_equity,
            position_entry_price=entry_price,
            last_price=last_price,
            position_qty=cur_qty,
        )
        for r in decision.reasons:
            self._log(f"  ⚠ {r}")

        target_qty = round(decision.target_weight * capital_per_symbol / last_price)
        with_ai_target_qty = round(
            with_ai_decision.target_weight * capital_per_symbol / last_price
        )
        no_ai_target_qty = round(
            no_ai_decision.target_weight * capital_per_symbol / last_price
        )
        delta = target_qty - cur_qty
        self._log(
            f"  {symbol}: quant {quant_weight:+.2f} -> AI proposal {overlay_weight:+.2f} "
            f"-> actual {decision.target_weight:+.2f} ({authority['mode']}) | have {cur_qty:g}, "
            f"want {target_qty:g} @ ${last_price:.2f}"
        )

        # Only the scheduled, actionable live-open cycle enters the evidence
        # ledger. Premarket and dry-run repeats would double-count one decision.
        if phase == "open" and self.live and execute:
            try:
                from ..portfolio_research import fingerprint
                from dataclasses import asdict
                from ..learning import ChampionStore
                from ..signals.sentiment import DEFAULT_MODEL
                import hashlib
                source = Path(__file__).resolve().parents[1] / "signals/sentiment.py"
                cohort = fingerprint({"champions": {s: c.as_dict() for s,c in ChampionStore().all().items()},
                    "model": DEFAULT_MODEL, "prompt_source": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "overlay": asdict(self.overlay.config) if self.overlay else None,
                    "evidence_version": "ai-v2"})
                append_ai_decision({
                    "evidence_version": "ai-v2", "cohort": cohort,
                    "session_date": state.session_date,
                    "symbol": symbol,
                    "price": round(last_price, 6),
                    "equity": round(equity, 2),
                    "capital_per_symbol": round(capital_per_symbol, 2),
                    "quant_weight": round(quant_weight, 8),
                    "overlay_weight": round(overlay_weight, 8),
                    "approved_weight_with_ai": round(with_ai_decision.target_weight, 8),
                    "approved_weight_without_ai": round(no_ai_decision.target_weight, 8),
                    "target_qty_with_ai": with_ai_target_qty,
                    "target_qty_without_ai": no_ai_target_qty,
                    "actual_target_qty": target_qty,
                    "ai_authority_eligible": bool(authority["eligible"]),
                    "ai_authority_mode": authority["mode"],
                    "overlay_changed_target": with_ai_target_qty != no_ai_target_qty,
                })
            except Exception as exc:  # evidence failure cannot block trading
                self._log(f"  ⚠ AI attribution recorder failed: {exc}")

        if abs(delta) < 1:
            self._log("    no action (delta < 1 share)")
            return
        if decision.halt_trading and not decision.kill_switch:
            # Daily halt blocks expansion but allows the strategy to reduce or
            # exit. Its weight is relative to this symbol's capital allocation.
            return
        side = "buy" if delta > 0 else "sell"
        if not execute:
            self._log(f"    [plan] would {side} {abs(delta):g} {symbol}")
            return
        if not self.live:
            self._log(f"    [dry-run] would {side} {abs(delta):g} {symbol}")
            return
        return {"symbol": symbol, "qty": abs(delta), "side": side,
                "reference_price": last_price, "sleeve": "etf"}

    def _reconcile(self, equity, state) -> None:
        daily = equity / state.day_start_equity - 1.0 if state.day_start_equity else 0.0
        dd = equity / state.peak_equity - 1.0 if state.peak_equity else 0.0
        self._log(f"  Day P&L {daily:+.2%} | drawdown {dd:+.2%}")
        for p in self.broker.get_positions():
            self._log(
                f"    {p.symbol}: {p.qty} @ {p.avg_entry_price} "
                f"-> mkt {p.current_price} ({float(p.unrealized_plpc):+.2%})"
            )
        if state.killed:
            self._log("  🛑 KILL SWITCH ACTIVE — manual restart required.")

"""FastAPI + Jinja2 + HTMX dashboard for the trading bot.

Replaces the Streamlit dashboard with a server-rendered HTML app that:
  - Loads in <1s (no script-rerun-on-every-click, no WebSocket, no in-process cache).
  - Heavy backtests are pre-computed nightly into data/dashboard/*.json snapshots.
  - Live Alpaca calls hit small endpoints with short TTL caches.
  - Plotly.js renders charts client-side from JSON payloads.

Tabs (MVP, the 3 daily-use views per user pick):
  /            → Live
  /strategy    → Strategy vs Hold
  /crash       → Crash performance

Run locally:
    uv run uvicorn web.server:app --reload --port 8502
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAPSHOTS = DATA / "dashboard"  # nightly precomputed JSONs live here
STATIC = Path(__file__).parent / "static"
TEMPLATES = Path(__file__).parent / "templates"

log = logging.getLogger("uvicorn.error")
# Refresh interval for the background pre-warmer. Must be shorter than every
# cache's TTL so the user never hits an expired-cache request.
WARM_INTERVAL_S = 45


@asynccontextmanager
async def lifespan(_: FastAPI):
    """On startup: warm every live-data cache once (PEAD takes ~60s the first
    time; the rest are sub-second). Then a single background task refreshes
    every WARM_INTERVAL_S. Simple and robust."""
    log.info("dashboard: warming caches…")
    await _warm_all()
    log.info("dashboard: initial warm done; starting refresher")
    task = asyncio.create_task(_warm_loop())
    try:
        yield
    finally:
        task.cancel()
        try: await task
        except (asyncio.CancelledError, Exception): pass


app = FastAPI(title="Yarden's super awesome AI trader",
              docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES))


def _asset_v() -> str:
    """Cache-busting token = newest mtime across the JS bundle. Changes on every
    deploy (scp updates mtimes), so browsers re-fetch JS without a hard refresh
    and without us bumping a version by hand. Stat is cheap (a handful of files)."""
    try:
        js = (STATIC / "js")
        return str(int(max(p.stat().st_mtime for p in js.glob("*.js"))))
    except Exception:
        return "0"


templates.env.globals["asset_v"] = _asset_v


# ---------------------------- caching utilities ----------------------------
_warmable: list = []   # list of (name, wrapper) pairs the warmer iterates over


def _ttl_cache(seconds: int, *, warm: bool = True):
    """Tiny in-memory TTL cache decorator. Process-local but unlike Streamlit's
    cache it lives across requests. We snapshot heavy things to disk in the
    precompute cron so even restarts stay fast.

    Each wrapped function exposes:
      .cache_clear()  — drop cached value, force fresh on next call
      .refresh()      — call underlying fn unconditionally, store result.
                        Used by the background warmer so users never hit cold.
    If ``warm=True`` (default) the wrapper auto-registers with ``_warmable``."""

    def deco(fn):
        import threading
        lock = threading.RLock()
        cache = {}
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            with lock:
                entry = cache.get(key)
                if entry is None or time.time() >= entry[1]:
                    value = fn(*args, **kwargs)
                    cache[key] = (value, time.time() + seconds)
                return cache[key][0]
        def refresh():
            with lock:
                value = fn()
                cache[((), ())] = (value, time.time() + seconds)
                return value
        def clear():
            with lock:
                cache.clear()
        wrapper.cache_clear = clear
        wrapper.refresh = refresh
        if warm:
            _warmable.append((fn.__name__, wrapper))
        return wrapper
    return deco


async def _warm_one(name, wrapper) -> None:
    """Refresh a single cache (off-event-loop, since the underlying fetchers
    are synchronous I/O — Alpaca SDK, yfinance, file reads)."""
    t0 = time.time()
    try:
        await asyncio.to_thread(wrapper.refresh)
        log.info(f"  warmed {name} in {time.time() - t0:.2f}s")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  warm {name} failed: {exc}")


async def _warm_all() -> None:
    """Refresh every registered cache. The Alpaca SDK has nested deferred
    imports that aren't safe under parallel module-import races (Python's
    _ModuleLock deadlocks when multiple threads hit `import` for the same
    module simultaneously). We pre-touch the broker once in a single thread
    so the imports settle, then fan out the rest in parallel."""
    try:
        await asyncio.to_thread(_broker)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  broker pre-warm failed: {exc}")
    await asyncio.gather(*(_warm_one(n, w) for n, w in _warmable))


async def _warm_loop() -> None:
    """Refresh all caches every WARM_INTERVAL_S until cancelled. The interval
    is < every cache's TTL, so a user request will always read a warm value."""
    while True:
        try:
            await asyncio.sleep(WARM_INTERVAL_S)
            await _warm_all()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning(f"warm loop iteration failed: {exc}")




def _read_snapshot(name: str) -> dict | None:
    """Read a precomputed JSON snapshot. Returns None if missing — the page
    will render a fallback message and a hint to run the precompute script."""
    p = SNAPSHOTS / f"{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---------------------------- live data helpers ----------------------------
@lru_cache(maxsize=1)
def _broker():
    from trading_bot.broker import AlpacaBroker
    return AlpacaBroker()


def _latest_stock_prices(symbols: list[str]) -> dict:
    """Latest TRADE price per STOCK symbol (Alpaca market data). Used to value the
    book AND the index lines at the same live price, so the bot-vs-index comparison
    stays FAIR off-hours instead of pitting Alpaca's lagging position marks against
    settled index closes. (User's explicit choice 2026-06-23: fair comparison over
    matching the Alpaca app's off-hours number.) Best-effort → {} on any failure so
    callers fall back to the broker mark. NOT @_ttl_cache'd — it's called from inside
    already-cached snapshots, and nesting caches is what deadlocked the warmer.
    Crypto excluded (24/7 marks are fresh)."""
    syms = [s for s in symbols if "/" not in s and not s.endswith("USD")]
    if not syms:
        return {}
    try:
        import os
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        cl = StockHistoricalDataClient(
            os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID"),
            os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY"))
        tr = cl.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=syms))
        return {sym: float(t.price) for sym, t in tr.items()}
    except Exception as e:  # noqa: BLE001
        log.warning(f"_latest_stock_prices: {e}")
        return {}


@_ttl_cache(120)
def account_snapshot() -> dict:
    a = _broker().get_account()
    equity, prior = float(a.portfolio_value), float(a.last_equity)
    try:
        market_open = bool(_broker().get_clock().is_open)
    except Exception:
        market_open = None
    return {
        "equity": equity, "cash": float(a.cash), "last_equity": prior,
        "buying_power": float(a.buying_power), "equity_raw": equity,
        "equity_broker_raw": equity, "last_equity_raw": prior,
        "sim_div_to_date": 0.0, "sim_div_yest": 0.0,
        "valuation": {"label": "Broker account equity", "basis": "broker_including_credited_income",
                      "observed_at": datetime.now(timezone.utc).isoformat(),
                      "market_open": market_open, "broker_mark": equity,
                      "fresh_price_adjustment": 0.0, "simulated_income_adjustment": 0.0},
    }


@_ttl_cache(120)
def positions_snapshot() -> list[dict]:
    out = []
    _pos = _broker().get_positions()
    _fresh = {}  # Keep holdings and account equity on the same broker valuation basis.
    for p in _pos:
        qty = float(p.qty); entry = float(p.avg_entry_price)
        px = _fresh.get(p.symbol, float(p.current_price))
        out.append({
            "symbol":         p.symbol,
            "qty":            qty,
            "market_value":   qty * px,
            "avg_entry":      entry,
            "price":          px,
            "unrealized_pl":  (px - entry) * qty,
            "unrealized_plpc": (px / entry - 1) if entry else 0.0,
        })
    income = income_snapshot().get("by_symbol", {})
    for row in out:
        row["income"] = income.get(row["symbol"], 0.0)
    return out


@_ttl_cache(120)
def orders_snapshot(limit: int = 25) -> list[dict]:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
    out = []
    for o in _broker()._client.get_orders(filter=req):
        out.append({
            "order_id":   str(o.id),
            "symbol":     o.symbol,
            "side":       o.side.value,
            "qty":        float(getattr(o, "filled_qty", 0) or o.qty or 0),
            "price":      float(o.filled_avg_price or 0) or None,
            "status":     o.status.value,
            "submitted":  o.submitted_at.isoformat() if o.submitted_at else None,
            "filled":     o.filled_at.isoformat() if o.filled_at else None,
        })
    return out


# Reference SEC yield used to simulate BIL distributions in the paper account.
# (Alpaca paper trading does NOT credit dividends/interest — confirmed empirically.
# This is the paper-account fudge factor that makes the equity curve reflect what
# a live account would actually do.)
BIL_SIM_APR = 0.05
BIL_PRICE_PROXY = 91.5  # daily moves are sub-bp; flat is fine for the sim


def _bil_qty_by_date(fills: list[dict]) -> dict[str, float]:
    """Reconstruct end-of-day BIL position from filled-order history.
    Returns {iso_date: qty_held_after_that_date}, sorted asc."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    bil_fills = sorted(
        [o for o in fills if o.get("symbol") == "BIL" and o.get("filled")],
        key=lambda o: o["filled"],
    )
    out: dict[str, float] = {}
    qty = 0.0
    for o in bil_fills:
        dt_et = datetime.fromisoformat(o["filled"].replace("Z", "+00:00")).astimezone(et)
        sign = +1 if o["side"] == "buy" else -1
        qty += sign * float(o.get("qty") or 0)
        out[dt_et.date().isoformat()] = qty
    return out


def _bil_qty_on_date(by_date_asc: list[tuple[str, float]], target_date: str) -> float:
    """Carry-forward: most recent BIL qty change on or before target_date."""
    qty = 0.0
    for d, q in by_date_asc:
        if d <= target_date:
            qty = q
        else:
            break
    return qty


@_ttl_cache(300)
def _bil_qty_history() -> list:
    return sorted(_bil_qty_by_date([r for r in _orders_full_history() if r["symbol"] == "BIL"]).items())


def _total_sim_div_to(target_iso: str) -> float:
    """Cumulative simulated BIL interest from inception through `target_iso`.

    Walks order history to reconstruct day-by-day BIL holdings, accrues
    BIL_SIM_APR / 365 per day on each day's end-of-day BIL position. Uses the
    dedicated `_bil_qty_history` (complete, cache-poisoning-proof) and floors qty
    at 0 as a final safety net (you can't short BIL, so interest is never < 0).
    """
    try:
        by_date = _bil_qty_history()
    except Exception:
        return 0.0
    if not by_date:
        return 0.0
    start = datetime.fromisoformat(by_date[0][0]).date()
    target = datetime.fromisoformat(target_iso).date()
    if target < start:
        return 0.0
    cum = 0.0
    cur = start
    while cur < target:
        cur = cur.fromordinal(cur.toordinal() + 1)
        qty = max(0.0, _bil_qty_on_date(by_date, cur.isoformat()))
        cum += qty * BIL_PRICE_PROXY * BIL_SIM_APR / 365.0
    return cum


@_ttl_cache(120)
def equity_curve_snapshot() -> list[dict]:
    """Broker daily equity, labeled in ET and restricted to completed sessions.

    Leading padded zero rows are removed. Live account marks and modeled
    income are never inserted into the historical closing series.
    """
    from zoneinfo import ZoneInfo
    from alpaca.trading.requests import GetPortfolioHistoryRequest
    et = ZoneInfo("America/New_York")
    try:
        req = GetPortfolioHistoryRequest(period="all", timeframe="1D")
        hist = _broker()._client.get_portfolio_history(req)
    except Exception:
        return []
    if not hist.equity:
        return []
    pairs = list(zip(hist.timestamp, hist.equity))
    while pairs and (pairs[0][1] is None or float(pairs[0][1]) <= 0):
        pairs.pop(0)
    out = []
    from trading_bot.sessions import last_completed_session, session_bounds
    completed = last_completed_session()
    for ts, eq in pairs:
        if eq is None:
            continue
        dt_et = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(et)
        if dt_et.date() > completed or session_bounds(dt_et.date()) is None:
            continue
        out.append({"date": dt_et.date().isoformat(), "equity": float(eq)})

    # Broker history already includes credited income. Never add model income
    # or relabel an intraday estimate as an official closing observation.
    return out


@_ttl_cache(600)
def regime_snapshot(sma_n: int = 200) -> dict | None:
    from trading_bot.marketdata import close_series, PriceUnavailable
    try:
        closes = close_series("QQQ", period=f"{sma_n * 2 + 60}d", min_bars=sma_n)
    except PriceUnavailable:
        return None
    sma = closes.rolling(sma_n).mean()
    # Restrict to rows with a VALID SMA before comparing. `close > NaN` is
    # False, so the first sma_n-1 rows read as "below the SMA" and a long
    # below-streak walks straight into them, inflating streak_days (e.g. a
    # 61-day real streak reported as 260). dropna() after astype(int) was a
    # no-op — ints are never NaN.
    above = (closes[sma.notna()] > sma[sma.notna()]).astype(int)
    if above.empty:
        return None
    last_val = int(above.iloc[-1])
    streak = 0
    for v in reversed(above.values):
        if v == last_val:
            streak += 1
        else:
            break
    # Guard NaN — yfinance can return NaN closes for partial days and
    # rolling SMA needs `sma_n` non-NaN values to produce a number.
    # JSONResponse rejects NaN, so any NaN slipping through crashes the endpoint.
    def _f(v):
        v = float(v)
        return v if v == v else None
    return {
        "regime_on":    bool(last_val),
        "close":        _f(closes.iloc[-1]),
        "sma":          _f(sma.iloc[-1]),
        "distance_pct": _f((closes.iloc[-1] / sma.iloc[-1] - 1.0) * 100)
                         if (sma.iloc[-1] == sma.iloc[-1] and sma.iloc[-1] != 0) else None,
        "streak_days":  streak,
        "as_of":        closes.index[-1].strftime("%Y-%m-%d"),
        "sma_n":        sma_n,
    }


def allocations_snapshot() -> dict:
    try:
        from trading_bot.allocations import (
            DEFAULT_SLEEVE_ENABLEMENT,
            read_allocations,
        )
        data = read_allocations(enabled_sleeves=DEFAULT_SLEEVE_ENABLEMENT)
    except Exception:
        return {}
    # The policy `weights` above are TARGETS the adaptive engine decided. The
    # REAL book can be far more idle — crypto parks in cash when DECAYING — and
    # that idle is what the idle router claims (QQQ in a bull, managed futures in
    # a bear). Attribute each live position to a sleeve so the banner can show
    # actual deployment (truth) next to the policy intent. (PEAD retired 2026-06-26.)
    try:
        eq = account_snapshot().get("equity_raw") or 0.0
        if eq > 0:
            pos = {p["symbol"]: p["market_value"] for p in positions_snapshot()}
            def _is_crypto(s: str) -> bool:
                return s.endswith("USD") and "/" not in s and len(s) <= 8
            regime = pos.get("QLD", 0.0)
            qqq = pos.get("QQQ", 0.0)            # idle router (bull)
            mf = pos.get("DBMF", 0.0) + pos.get("KMLM", 0.0)
            bil = pos.get("BIL", 0.0)
            crypto = sum(v for s, v in pos.items() if _is_crypto(s))
            data["live"] = {
                "equity": round(eq, 2),
                "weights": {k: round(v / eq, 4) for k, v in
                            {"regime": regime, "qqq": qqq, "crypto": crypto,
                             "mf": mf, "bil": bil}.items()},
                "mf_pending": mf <= 0,
            }
    except Exception:
        pass
    return data


def latest_revalidation() -> dict | None:
    f = DATA / "revalidation.jsonl"
    if not f.exists():
        return None
    lines = f.read_text().strip().splitlines()
    return json.loads(lines[-1]) if lines else None


# ------------------------------ derived data ------------------------------
@_ttl_cache(300)
def _orders_full_history() -> list[dict]:
    """Paginated fill activities, reconciled with complete order status."""
    from trading_bot.broker.history import all_orders, all_activities, fill_ledger
    broker = _broker()
    return fill_ledger(all_activities(broker.config, ["FILL"]), all_orders(broker._client))


def realized_trades() -> list[dict]:
    """Reconstruct realized round-trip P&L from filled orders (avg-cost basis).
    MUST use the full order history — a truncated window drops early buys,
    corrupting every later sell's cost basis."""
    orders = sorted(_orders_full_history(), key=lambda o: o["filled"])
    by_sym: dict[str, list] = {}
    for o in orders:
        by_sym.setdefault(o["symbol"], []).append(o)
    rows = []
    for sym, fills in by_sym.items():
        qty = 0.0
        cost = 0.0
        for f in fills:
            if not f["price"]:
                continue
            if f["side"] == "buy":
                new_qty = qty + f["qty"]
                cost = (cost * qty + f["price"] * f["qty"]) / new_qty if new_qty else f["price"]
                qty = new_qty
            else:
                sell_qty = min(f["qty"], qty)
                if sell_qty <= 0:
                    continue
                rows.append({
                    "symbol": sym,
                    "closed_at": f["filled"],
                    "qty": round(sell_qty, 4),
                    "entry": round(cost, 4),
                    "exit": f["price"],
                    "pnl": round((f["price"] - cost) * sell_qty, 2),
                    "pnl_pct": round((f["price"] / cost - 1) * 100, 2) if cost else 0.0,
                })
                qty -= sell_qty
    return rows


@lru_cache(maxsize=128)
def _sleeve_name(symbol: str) -> str:
    """Map either Alpaca's position or order symbol form to one owner."""
    from trading_bot.allocations import DEFAULT_SLEEVE_ENABLEMENT
    from trading_bot.ownership import build_sleeves, owner_of
    normalized = symbol.replace("/", "")
    if normalized == "QQQ":
        return "nasdaq_router"
    owners = owner_of(normalized, build_sleeves(DEFAULT_SLEEVE_ENABLEMENT))
    return owners[0].name if len(owners) == 1 else "unowned"


@_ttl_cache(900, warm=False)
def _position_market_history():
    """Current weights and aligned returns shared by risk diagnostics."""
    import pandas as pd
    from trading_bot.data import load_ohlcv

    positions = positions_snapshot()
    equity = float(account_snapshot().get("equity_raw") or 0.0)
    if equity <= 0:
        return {}, pd.DataFrame(), equity, ["account equity unavailable"]
    end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    start = (datetime.now(timezone.utc).date() - timedelta(days=180)).isoformat()
    closes = {}
    weights = {}
    missing = []
    for p in positions:
        symbol = p["symbol"]
        # The current production book is ETF-only. If crypto is re-enabled,
        # its 365-day calendar needs a separate aligned calculation.
        if symbol.endswith("USD"):
            missing.append(symbol)
            continue
        try:
            frame = load_ohlcv(symbol, start, end)
            series = frame["close"].sort_index()
            series = series.loc[~series.index.duplicated(keep="last")]
            if len(series) >= 20:
                closes[symbol] = series
                weights[symbol] = float(p["market_value"]) / equity
            else:
                missing.append(symbol)
        except Exception as exc:  # one bad asset must not hide all attribution
            log.warning(f"risk contribution {symbol}: {exc}")
            missing.append(symbol)
    if not closes:
        return {}, pd.DataFrame(), equity, missing
    prices = pd.concat(closes, axis=1).sort_index()
    returns = prices.pct_change(fill_method=None)
    return weights, returns, equity, missing


@_ttl_cache(900, warm=False)
def _position_risk_contributions() -> dict[str, float]:
    """Recent covariance-based component risk for current positions."""
    from trading_bot.attribution import component_risk_contributions

    weights, returns, _equity, missing = _position_market_history()
    if missing:
        return {}
    return component_risk_contributions(weights, returns)


@_ttl_cache(900, warm=False)
def portfolio_risk_snapshot() -> dict:
    """Advisory current-weight tail risk; never controls portfolio sizing."""
    from trading_bot.attribution import portfolio_tail_risk

    weights, returns, equity, missing = _position_market_history()
    if missing:
        return {
            "status": "UNAVAILABLE",
            "method": "historical simulation on current weights; advisory only",
            "missing_assets": sorted(set(missing)),
            "reason": "held assets lack aligned return history",
        }
    return portfolio_tail_risk(weights, returns, equity=equity)


@_ttl_cache(300)
def sleeve_attribution_snapshot() -> dict:
    """Realized + open P&L and current component risk by strategy sleeve."""
    names = ("etf", "regime", "nasdaq_router", "managed_futures", "cash_park", "crypto", "pead", "unowned")
    rows = {
        name: {"sleeve": name, "market_value": 0.0, "realized_pnl": 0.0,
               "unrealized_pnl": 0.0, "income": 0.0, "risk_contribution": None}
        for name in names
    }
    for trade in realized_trades():
        rows[_sleeve_name(trade["symbol"])]["realized_pnl"] += float(trade["pnl"])
    positions = positions_snapshot()
    for p in positions:
        row = rows[_sleeve_name(p["symbol"])]
        row["market_value"] += float(p["market_value"])
        row["unrealized_pnl"] += float(p["unrealized_pl"])
    for symbol, amount in income_snapshot().get("by_symbol", {}).items():
        rows[_sleeve_name(symbol)]["income"] += float(amount)

    risk_by_symbol = _position_risk_contributions()
    risk_by_sleeve: dict[str, float] = {}
    for symbol, contribution in risk_by_symbol.items():
        sleeve = _sleeve_name(symbol)
        risk_by_sleeve[sleeve] = risk_by_sleeve.get(sleeve, 0.0) + contribution

    equity = float(account_snapshot().get("equity_raw") or 0.0)
    result = []
    for name, row in rows.items():
        row["total_pnl"] = row["realized_pnl"] + row["unrealized_pnl"] + row["income"]
        row["weight"] = row["market_value"] / equity if equity else 0.0
        row["risk_contribution"] = risk_by_sleeve.get(name)
        for key in ("market_value", "realized_pnl", "unrealized_pnl", "income", "total_pnl"):
            row[key] = round(row[key], 2)
        if row["risk_contribution"] is not None:
            row["risk_contribution"] = round(row["risk_contribution"], 6)
        if row["market_value"] or row["realized_pnl"] or row["income"]:
            result.append(row)
    result.sort(key=lambda r: r["market_value"], reverse=True)
    return {
        "rows": result,
        "risk_method": "recent 180-calendar-day covariance component contribution",
        "pnl_method": "average-cost realized + broker open mark + credited income; QQQ beta separated from managed futures",
    }


def ai_attribution_snapshot() -> dict:
    from trading_bot.attribution import (
        AI_DECISION_LEDGER,
        ai_authority_gate,
        read_jsonl,
        summarize_ai_decisions,
    )
    summary = summarize_ai_decisions(read_jsonl(AI_DECISION_LEDGER))
    summary["authority"] = ai_authority_gate(summary)
    return summary


@_ttl_cache(120)
def execution_quality_snapshot() -> dict:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from trading_bot.attribution import read_jsonl, summarize_execution_quality
    from trading_bot.broker.alpaca_client import EXECUTION_INTENT_LEDGER

    intents = read_jsonl(EXECUTION_INTENT_LEDGER)
    journal = DATA / "order_journal.json"
    if journal.exists():
        intents += list(json.loads(journal.read_text()).get("orders", {}).values())
    fills = [{"order_id": o["order_id"], "filled_avg_price": o["price"],
              "filled_qty": o["qty"]} for o in _orders_full_history()]
    return summarize_execution_quality(intents, fills)


def shadow_regime_snapshot() -> dict:
    from trading_bot.attribution import read_jsonl
    from trading_bot.shadow_regime import SHADOW_LEDGER
    rows = read_jsonl(SHADOW_LEDGER)
    measured = [row for row in rows if row.get("variants")]
    latest = measured[-1] if measured else (rows[-1] if rows else None)
    return {
        "observations": len(rows),
        "measured_observations": len(measured),
        "latest": latest,
        "variants": latest.get("variants", {}) if latest else {},
        "method": "virtual close-to-close portfolios with turnover costs",
    }


def event_options_shadow_snapshot() -> dict:
    from trading_bot.event_options_shadow import shadow_snapshot
    return shadow_snapshot(DATA / "event_options_shadow")


def research_autopilot_snapshot() -> dict:
    """Current autonomous champion/shadow/rollback state; filesystem-only."""
    from trading_bot.attribution import read_jsonl
    from trading_bot.portfolio_policy import load_policy
    from trading_bot.research_autopilot import load_state

    state = load_state()
    journal = read_jsonl(DATA / "research" / "weekly.jsonl")
    latest = journal[-1] if journal else None
    latest_run = None
    if latest:
        latest_run = {
            "ts": latest.get("ts"),
            "evaluated": latest.get("n_evaluated", 0),
            "survivors": [row.get("name") for row in latest.get("survivors", [])],
            "reviews": [row.get("name") for row in latest.get("reviews", [])],
            "champion": latest.get("champion"),
            "autopilot": latest.get("autopilot", []),
        }
    return {
        "status": "ERROR" if state.get("state_error") else "MONITORING",
        "policy": load_policy(),
        "shadow": state.get("shadow"),
        "deployment": state.get("deployment"),
        "history_count": len(state.get("history") or []),
        "latest_run": latest_run,
        "error": state.get("state_error"),
    }


def automation_snapshot() -> dict:
    from trading_bot.dashboard_overview import automation_snapshot as summarize
    return summarize(DATA / "state.json")


# --------------------------------- pages ----------------------------------
# Single SPA shell — JS handles hash-based tab routing (#live, #strategy, #crash).
# We accept the old per-tab URLs and 302 them to the shell with the right hash
# so existing bookmarks still land on the right tab.
@app.get("/", response_class=HTMLResponse)
def page_root(request: Request):
    return templates.TemplateResponse(request, "base.html", {})


@app.get("/strategy", response_class=HTMLResponse)
def page_strategy(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/#strategy")


@app.get("/crash", response_class=HTMLResponse)
def page_crash(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/#crash")


# --------------------------------- API ----------------------------------
# Live-data API: tell browsers/proxies NOT to cache responses. The server
# already TTL-caches in memory (120s); we don't want the browser to add a
# second cache layer that holds stale data longer.
NOCACHE = {"Cache-Control": "no-store, max-age=0, must-revalidate",
           "Pragma": "no-cache", "Expires": "0"}


@app.get("/api/account")
def api_account():
    return JSONResponse(account_snapshot(), headers=NOCACHE)


@app.get("/api/positions")
def api_positions():
    return JSONResponse({"positions": positions_snapshot()}, headers=NOCACHE)


def orders_with_pnl() -> list[dict]:
    """orders_snapshot() augmented: each SELL gets a `pnl` and `pnl_pct` field
    from realized_trades() (avg-cost basis). BUY rows pass through unchanged.
    The join key is (symbol, filled_at) — unique for any executed fill."""
    orders = orders_snapshot()
    realized = {(r["symbol"], r["closed_at"]): r for r in realized_trades()}
    out = []
    for o in orders:
        row = dict(o)
        if o.get("side") == "sell" and o.get("filled"):
            match = realized.get((o["symbol"], o["filled"]))
            if match is not None:
                row["pnl"] = match["pnl"]
                row["pnl_pct"] = match["pnl_pct"]
                row["entry"] = match["entry"]
        out.append(row)
    return out


@app.get("/api/orders")
def api_orders():
    return JSONResponse({"orders": orders_with_pnl()}, headers=NOCACHE)


@app.get("/api/equity_curve")
def api_equity_curve():
    return JSONResponse({"curve": equity_curve_snapshot()}, headers=NOCACHE)


# Strategy went live on the fresh $100k account 2026-06-27; first trading day is Mon
# 2026-06-29. "Since launch" is anchored here so the bot and the QQQ/SPY benchmarks all
# start at 100 on the SAME day — excluding the idle-cash days before deployment.
# Fallback only. The real launch date is the ACCOUNT's creation date — see
# _launch_date(). Hardcoding it went stale the moment the account changed: this
# read 2026-06-29 (the retired VM account) while the live account was created
# 2026-08-04, so the "since launch" chart anchored six weeks before the account
# existed. Alpaca pads portfolio history backwards with the opening cash value,
# so the bot's line sat flat at $100,000 across days it could not have traded
# while SPY and QQQ showed real moves — crediting the indices with a +2.9%/+4.1%
# rally the account was not open for, and reporting the bot 4.85 points behind
# QQQ when it was actually 0.27 ahead.
LAUNCH_DATE_FALLBACK = "2026-06-29"


@lru_cache(maxsize=1)
def _launch_date() -> str:
    """First date this account could actually trade."""
    try:
        acct = _broker().get_account()
        created = getattr(acct, "created_at", None)
        if created:
            return str(created)[:10]
    except Exception as exc:  # noqa: BLE001
        log.warning(f"launch date: falling back ({exc})")
    return LAUNCH_DATE_FALLBACK


@_ttl_cache(600, warm=False)
def _account_created_on_or_after(launch: str) -> bool:
    """True when the account itself was opened at/after `launch`, i.e. there is
    no genuine pre-launch positioning to anchor against."""
    try:
        acct = _broker().get_account()
        created = str(getattr(acct, "created_at", ""))[:10]
        return bool(created) and created >= launch
    except Exception:  # noqa: BLE001
        return False


def equity_vs_indices_snapshot() -> dict:
    """Bot equity vs SPY/QQQ since launch, all normalized to start = 100 on the account creation date.
    Three aligned series so a single chart can show outperformance/underperformance.

    SPY/QQQ daily closes come from yfinance — same source the strategy backtests
    use. We align on the bot's session dates (skipping market holidays where
    Alpaca has no equity bar)."""
    full = equity_curve_snapshot()
    launch = _launch_date()
    post = [c for c in full if c["date"] >= launch]
    pre = [c for c in full if c["date"] < launch]
    # Anchor the 100 baseline to the last pre-launch close (the Friday before launch)
    # so all three lines start together at 100 entering launch Monday AND launch-day
    # performance is visible from the very first session. Fall back to the first
    # post-launch point if there's no pre-launch history. No weekend points are
    # plotted (equity_curve has only trading days), so nothing drifts below 100.
    # Prepend the last pre-launch close ONLY if the bot was actually invested
    # before launch — then day-one performance is visible against a shared
    # baseline. For a freshly funded account there is no such thing: it held
    # cash, and Alpaca's padded pre-history is just the opening balance repeated.
    # Anchoring there hands the indices a full session the account sat out
    # (Aug 3->4 was +1.8% SPY, +3.4% QQQ), which is most of the gap the chart
    # was reporting.
    account_opened_at_launch = (not pre) or _account_created_on_or_after(launch)
    curve = ([pre[-1]] if (pre and not account_opened_at_launch) else []) + post
    if len(curve) < 2:
        return {"dates": [], "bot": [], "spy": [], "qqq": [], "launch_date": launch, "pending": True}
    dates = [c["date"] for c in curve]
    start_d, end_d = dates[0], dates[-1]
    try:
        import yfinance as yf
        import pandas as pd
        # +1 day on end so we get the last close too
        end = (datetime.fromisoformat(end_d) + timedelta(days=1)).date().isoformat()
        idx = yf.download(["SPY", "QQQ"], start=start_d, end=end,
                          progress=False, auto_adjust=True, group_by="ticker")
        # Drop NaN closes — yfinance can leave the just-closed session as NaN
        # for a few hours after close, breaking the carry-forward in
        # _series_at_dates (it picks up the NaN as "latest" instead of the
        # prior valid bar).
        spy = idx["SPY"]["Close"].dropna() if ("SPY" in idx.columns.levels[0]) else pd.Series(dtype=float)
        qqq = idx["QQQ"]["Close"].dropna() if ("QQQ" in idx.columns.levels[0]) else pd.Series(dtype=float)
    except Exception as e:
        log.warning(f"equity_vs_indices: yfinance fetch failed: {e}")
        return {"dates": dates, "launch_date": launch,
                "bot": [c["equity"] / curve[0]["equity"] * 100 for c in curve],
                "spy": [None] * len(dates), "qqq": [None] * len(dates)}

    def _series_at_dates(s, target_dates):
        if s.empty: return [None] * len(target_dates)
        s.index = pd.to_datetime(s.index).date
        base = None
        out = []
        for d in target_dates:
            d_obj = datetime.fromisoformat(d).date()
            # Find the closest prior trading day if exact match missing
            avail = [k for k in s.index if k <= d_obj]
            if not avail:
                out.append(None); continue
            v = float(s.loc[max(avail)])
            if base is None: base = v
            out.append(v / base * 100 if base else None)
        return out

    # Every series ends at the same completed-session close. Intraday index
    # quotes must never overwrite a prior-date point in a daily broker curve.
    bot_base = curve[0]["equity"]
    spy_n = _series_at_dates(spy, dates)
    qqq_n = _series_at_dates(qqq, dates)
    return {
        "basis": "matched completed-session closes",
        "dates": dates,
        "launch_date": launch,
        "bot": [round(c["equity"] / bot_base * 100, 4) for c in curve],
        # `round(NaN)` returns NaN, which Starlette's JSON encoder rejects.
        # Guard both None and NaN — fall back to None for chart-friendly gaps.
        "spy": [round(v, 4) if (v is not None and v == v) else None for v in spy_n],
        "qqq": [round(v, 4) if (v is not None and v == v) else None for v in qqq_n],
    }


@app.get("/api/equity_vs_indices")
def api_equity_vs_indices():
    return JSONResponse(equity_vs_indices_snapshot(), headers=NOCACHE)


@app.get("/api/regime")
def api_regime():
    return JSONResponse(regime_snapshot() or {})


@app.get("/api/allocations")
def api_allocations():
    return JSONResponse(allocations_snapshot())


@app.get("/api/revalidation")
def api_revalidation():
    return JSONResponse(latest_revalidation() or {})


@app.get("/api/realized_trades")
def api_realized_trades():
    return JSONResponse({"trades": realized_trades()})


@app.get("/api/sleeve_attribution")
def api_sleeve_attribution():
    return JSONResponse(sleeve_attribution_snapshot(), headers=NOCACHE)


@app.get("/api/ai_attribution")
def api_ai_attribution():
    return JSONResponse(ai_attribution_snapshot(), headers=NOCACHE)


@app.get("/api/execution_quality")
def api_execution_quality():
    return JSONResponse(execution_quality_snapshot(), headers=NOCACHE)


@app.get("/api/shadow_regime")
def api_shadow_regime():
    return JSONResponse(shadow_regime_snapshot(), headers=NOCACHE)


@app.get("/api/event_options_shadow")
def api_event_options_shadow():
    return JSONResponse(event_options_shadow_snapshot(), headers=NOCACHE)


@app.get("/api/portfolio_risk")
def api_portfolio_risk():
    return JSONResponse(portfolio_risk_snapshot(), headers=NOCACHE)


@app.get("/api/research_autopilot")
def api_research_autopilot():
    return JSONResponse(research_autopilot_snapshot(), headers=NOCACHE)


@app.get("/api/automation")
def api_automation():
    return JSONResponse(automation_snapshot(), headers=NOCACHE)


@_ttl_cache(300)
def income_snapshot() -> dict:
    from trading_bot.broker.history import all_activities
    from trading_bot.sessions import last_completed_session
    activities = all_activities(_broker().config, ["DIV", "INT"])
    out = [{"date": a.get("date") or (a.get("transaction_time") or "")[:10],
            "type": a.get("activity_type"), "symbol": a.get("symbol") or "",
            "amount": float(a.get("net_amount", 0) or 0),
            "per_share": float(a.get("per_share_amount", 0) or 0)} for a in activities]
    by_symbol = {}
    for row in out:
        symbol = row["symbol"] or "(unknown)"
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + row["amount"]
    credited = sum(r["amount"] for r in out)
    modeled = _total_sim_div_to(last_completed_session().isoformat())
    return {"total": round(credited, 2), "real_total": round(credited, 2),
            "simulated_total": 0.0, "modeled_bil_total": round(modeled, 2),
            "model_only_uncredited_estimate": round(max(0, modeled - by_symbol.get("BIL", 0)), 2),
            "model_included_in_equity": False,
            "by_symbol": {k: round(v, 2) for k, v in by_symbol.items()},
            "recent": out[:10], "count": len(out)}


@app.get("/api/income")
def api_income():
    return JSONResponse(income_snapshot())


@_ttl_cache(1800, warm=False)   # 30 min TTL; NOT warmed at startup — that
                                  # would block the server boot for 60s. First
                                  # user click pays the cost; subsequent clicks
                                  # are instant for the next 30 min.
def pead_snapshot() -> dict:
    """Current PEAD positions + recent earnings events in the universe +
    upcoming earnings dates. Used by the PEAD tab on the dashboard."""
    import pandas as pd
    import yfinance as yf
    from trading_bot.backtest.pead import earnings_events
    from trading_bot.config import load_pead_universe

    universe = load_pead_universe()
    today = datetime.now(timezone.utc).date()

    # Current open PEAD positions (state file + live Alpaca P&L)
    state_path = DATA / "pead_state.json"
    positions = []
    if state_path.exists():
        try:
            st = json.loads(state_path.read_text())
            live_pos_map = {p["symbol"]: p for p in positions_snapshot()}
            for p in st.get("positions", []):
                sym = p.get("symbol")
                entry_d = p.get("entry_date", "")
                try:
                    days_held = (today - datetime.strptime(entry_d, "%Y-%m-%d").date()).days
                except Exception:
                    days_held = 0
                days_to_exit = max(60 - days_held, 0)  # 60 calendar days approx
                lp = live_pos_map.get(sym, {})
                positions.append({
                    "symbol":    sym,
                    "entry_date": entry_d,
                    "days_held": days_held,
                    "days_to_exit": days_to_exit,
                    "qty":       float(p.get("qty", 0)),
                    "avg_entry": float(lp.get("avg_entry", p.get("entry_price", 0))),
                    "price":     float(lp.get("price", 0)),
                    "market_value": float(lp.get("market_value", 0)),
                    "unrealized_pl": float(lp.get("unrealized_pl", 0)),
                })
        except Exception:
            pass

    # Recent + upcoming earnings, per universe symbol
    recent, upcoming = [], []
    cutoff_back = today - timedelta(days=21)
    cutoff_fwd  = today + timedelta(days=21)

    for sym in universe:
        # earnings_events gives PAST earnings (with surprise + AMC flag).
        # use_cache=False so we always read fresh data — the _ttl_cache on
        # pead_snapshot (30 min) limits yfinance hits anyway.
        try:
            ev = earnings_events(sym, use_cache=False)
        except Exception:
            ev = pd.DataFrame()
        if ev is not None and not ev.empty:
            # Recent: in the past 21 days
            past = ev[(ev["date"] >= pd.Timestamp(cutoff_back)) & (ev["date"] <= pd.Timestamp(today))]
            for _, e in past.iterrows():
                event_date = e["date"].date()
                # Compute reaction: close[day-after-or-of] / close[day-before]
                try:
                    px = yf.Ticker(sym).history(period="3mo", auto_adjust=True)
                    if px.index.tz is not None:
                        px.index = px.index.tz_localize(None)
                except Exception:
                    px = None
                reaction = None
                if px is not None and not px.empty:
                    pos = px.index.searchsorted(pd.Timestamp(event_date))
                    react_pos = pos + 1 if e.get("amc", False) else pos
                    if 0 < react_pos < len(px):
                        try:
                            reaction = float(px["Close"].iloc[react_pos] / px["Close"].iloc[react_pos - 1] - 1)
                        except Exception:
                            reaction = None
                surprise = float(e.get("surprise", 0))
                concordant = (surprise > 0) and (reaction is not None and reaction > 0)
                recent.append({
                    "symbol":    sym,
                    "date":      event_date.isoformat(),
                    "amc":       bool(e.get("amc", False)),
                    "surprise":  round(surprise, 2),
                    "reaction":  round(reaction * 100, 2) if reaction is not None else None,
                    "concordant": bool(concordant),
                    "miss":      surprise < 0,
                })

        # Upcoming: query yfinance for the next earnings date if any
        try:
            t = yf.Ticker(sym)
            ed = t.get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                ed = ed[~ed.index.isna()]
                # Filter to future-only
                for ts, row in ed.iterrows():
                    if pd.isna(ts):
                        continue
                    d = pd.Timestamp(ts).tz_localize(None).normalize().date() \
                        if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).normalize().date()
                    if d > today and d <= cutoff_fwd:
                        upcoming.append({
                            "symbol":  sym,
                            "date":    d.isoformat(),
                            "estimate": float(row.get("EPS Estimate", 0)) if not pd.isna(row.get("EPS Estimate", float("nan"))) else None,
                        })
                        break   # only nearest upcoming per symbol
        except Exception:
            pass

    recent.sort(key=lambda r: r["date"], reverse=True)
    upcoming.sort(key=lambda r: r["date"])
    return {
        "as_of":         today.isoformat(),
        "universe_size": len(universe),
        "positions":     positions,
        "recent":        recent,
        "upcoming":      upcoming,
    }


@app.get("/api/pead")
def api_pead():
    """Read PEAD snapshot from the precomputed file (instant). The nightly
    precompute_dashboard cron writes this file alongside the other snapshots.
    Fall back to in-process computation only if the file is missing."""
    snap_file = SNAPSHOTS / "pead.json"
    if snap_file.exists():
        try:
            data = json.loads(snap_file.read_text())
            # Augment positions with live Alpaca P&L if present
            if data.get("positions"):
                try:
                    live_pos_map = {p["symbol"]: p for p in positions_snapshot()}
                    for pos in data["positions"]:
                        lp = live_pos_map.get(pos["symbol"], {})
                        pos["avg_entry"]      = float(lp.get("avg_entry", pos.get("entry_price", 0)))
                        pos["price"]          = float(lp.get("price", 0))
                        pos["market_value"]   = float(lp.get("market_value", 0))
                        pos["unrealized_pl"]  = float(lp.get("unrealized_pl", 0))
                except Exception:
                    pass
            return JSONResponse(data)
        except Exception:
            pass
    # File missing — compute on the fly (slow first hit)
    return JSONResponse(pead_snapshot())


@app.get("/api/champions")
def api_champions():
    """Strategies the ETF sleeve is actually trading.

    The sleeve only trades symbols with a champion that cleared the Deflated-
    Sharpe gate; everything else is skipped. The dashboard needs this to show
    the ETF sleeve at all — it is not part of the adaptive weight policy, so it
    has no entry in sleeve_allocations.json.
    """
    from trading_bot.config import load_universe
    from trading_bot.learning import ChampionStore

    store = ChampionStore()
    if not store.path.exists():
        return JSONResponse({"available": False, "champions": [], "universe": 0})
    champs = store.all()
    return JSONResponse({
        "available": True,
        "universe": len(load_universe()),
        "champions": [
            {"symbol": sym, "strategy": c.strategy,
             "judge_sharpe": round(float(c.judge_sharpe), 2)}
            for sym, c in sorted(champs.items())
        ],
    })


@app.get("/api/sleeve_activity")
def api_sleeve_activity():
    """Count of filled orders per live sleeve. Used by the dashboard to clarify
    the difference between 'backtest decaying' and 'live bot hasn't traded yet'."""
    # Symbol → sleeve comes from trading_bot.ownership, the same registry the
    # executor uses. The previous version was a hand-written heuristic whose
    # final `else` swept everything unmatched into "pead" — so every fill from
    # the ETF mean-reversion sleeve (IWM, MDY, XLE, XLP, XLU, ...) was counted
    # against a sleeve that has been retired since 2026-06-26.
    from trading_bot.ownership import build_sleeves, owner_of

    registry = build_sleeves()
    keys = ("etf", "regime", "pead", "crypto", "mf", "nasdaq_router", "cash_park")
    sleeves = {k: 0 for k in keys}
    last = {k: None for k in keys}
    alias = {"managed_futures": "mf"}
    for o in _orders_full_history():
        sym = o["symbol"]
        owners = owner_of(sym, registry)
        if owners:
            sleeve = "nasdaq_router" if sym == "QQQ" else alias.get(owners[0].name, owners[0].name)
        elif "/USD" in sym or (sym.endswith("USD") and "/" not in sym):
            sleeve = "crypto"          # crypto order-symbol form (BTC/USD)
        else:
            continue                   # unknown symbol: don't invent a sleeve
        sleeves[sleeve] += 1
        if last[sleeve] is None or (o["filled"] or "") > last[sleeve]:
            last[sleeve] = o["filled"]
    return JSONResponse({"counts": sleeves, "last_fill": last})


@app.get("/api/snapshot/{name}")
def api_snapshot(name: str):
    """Read a precomputed JSON snapshot. The precompute cron writes these
    nightly so the heavy backtests don't run at request time."""
    if name not in {"team_vs_hold", "crash_perf"}:
        raise HTTPException(404)
    s = _read_snapshot(name)
    if s is None:
        return JSONResponse({"error": "snapshot missing — run scripts/precompute_dashboard.py",
                             "name": name}, status_code=503)
    return JSONResponse({**s, "production_parity": False, "promotion_eligible": False,
                         "label": "Archived legacy research; not the deployed portfolio"})


@app.get("/api/health")
def api_health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/api/reconciliation")
def api_reconciliation():
    from trading_bot.broker.history import holdings_reconciliation
    return holdings_reconciliation(_orders_full_history(), _broker().get_positions())


@app.get("/api/portfolio_lab")
def api_portfolio_lab():
    path = DATA / "research" / "portfolio_lab.json"
    result = json.loads(path.read_text()) if path.exists() else {"status": "AWAITING_FIRST_RUN"}
    for key, relative in (("pead_collection", "pead_shadow/summary.json"),
                          ("market_collection", "research/market_health.json"),
                          ("production_verification", "research/production_verification.json")):
        source = DATA / relative
        result[key] = json.loads(source.read_text()) if source.exists() else {"status": "AWAITING_FIRST_RUN"}
    # Surface missing collectors instead of displaying an old healthy badge.
    from trading_bot.sessions import last_completed_session
    result["expected_completed_session"] = str(last_completed_session())
    result["report_stale"] = result.get("as_of", "") < result["expected_completed_session"]
    stamp = result["pead_collection"].get("at")
    if stamp:
        result["pead_collection"]["stale"] = (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()>4*86400
    from trading_bot.sessions import now_et, session_bounds
    moment=now_et()
    bounds=session_bounds(moment.date())
    market=result["market_collection"]
    if bounds and bounds[0]+timedelta(minutes=7)<=moment<bounds[1]:
        stamp=market.get("at")
        market["stale"]=not stamp or (moment-datetime.fromisoformat(stamp)).total_seconds()>120
    return result


@app.get("/api/release")
def api_release():
    path = DATA / "release.json"
    return json.loads(path.read_text()) if path.exists() else {"status": "UNVERSIONED"}

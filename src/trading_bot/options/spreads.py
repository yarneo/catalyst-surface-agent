"""Turn a VRP signal into a DEFINED-RISK options structure.

Still read-only: this builds an order *specification* and computes its risk. It
never submits. Execution lives in the event repo, behind the same portfolio
risk guard as everything else.

Why defined risk, always. Selling volatility is picking up pennies in front of a
steamroller: the edge is small, positive and steady, and the tail is enormous.
A naked short strangle has unbounded loss, and a single gap can exceed every
gain the strategy has ever made. Every structure here buys a wing — it costs
part of the premium and converts "unbounded" into a number you can size against.

    SELL_VOL -> iron condor      short strangle + long wings, max loss capped
    BUY_VOL  -> long strangle    max loss is the premium paid, capped by nature

The short strikes sit at a target delta rather than a fixed percentage. Delta is
roughly the market's own probability that a strike finishes in the money, so
0.16-delta means "about a 16% chance this side is breached" regardless of
whether the underlying is a 10-vol ETF or a 60-vol single name. A fixed 5% offset
would be far out of the money on the first and nearly at the money on the second.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from scipy.stats import norm

from .iv import IVUnavailable, Quote
from .vrp import VRPSignal


_OPPOSITE = {"buy": "sell", "sell": "buy"}


@dataclass(frozen=True)
class Leg:
    symbol: str          # OCC option symbol
    side: str            # "buy" | "sell"
    ratio_qty: int = 1

    def as_mcp(self, intent: str = "open") -> dict:
        """Shape expected by the MCP server's place_option_order `legs`.

        `intent="close"` mirrors the leg: a spread is exited by submitting the
        reverse of the order that opened it. Side and intent have to move
        together — a closing order that still says `buy_to_open` re-opens the
        position instead of flattening it.
        """
        side = self.side if intent == "open" else _OPPOSITE[self.side]
        return {"symbol": self.symbol, "side": side,
                "ratio_qty": str(self.ratio_qty),
                "position_intent": f"{side}_to_{intent}"}


@dataclass(frozen=True)
class Spread:
    underlying: str
    structure: str                 # "iron_condor" | "long_strangle"
    direction: str                 # SELL_VOL | BUY_VOL
    legs: tuple[Leg, ...]
    net_credit: float              # positive = we receive, negative = we pay
    max_loss: float                # per contract, ALWAYS finite and positive
    max_profit: float
    expiry: str
    dte: int

    def __post_init__(self) -> None:
        # A structure whose worst case cannot be computed must never reach an
        # order. This is the invariant the whole module exists to guarantee.
        if not (self.max_loss > 0 and math.isfinite(self.max_loss)):
            raise ValueError(f"max_loss must be finite and positive, got {self.max_loss}")
        if len(self.legs) not in (2, 4):
            raise ValueError(f"expected 2 or 4 legs, got {len(self.legs)}")

    @property
    def risk_reward(self) -> float:
        return self.max_profit / self.max_loss if self.max_loss else float("nan")

    def contracts_for_risk(self, risk_budget_usd: float) -> int:
        """How many contracts fit a dollar risk budget.

        Sizes off MAX LOSS, not premium or notional. A credit spread that
        collects $40 and can lose $460 is a $460 risk, and sizing off the $40
        is how a book that looks small becomes an account-ending position.
        """
        per_contract = self.max_loss * 100      # options are 100x multiplier
        if per_contract <= 0:
            return 0
        return max(0, int(risk_budget_usd // per_contract))


def _delta_strike(spot: float, iv: float, T: float, target_delta: float,
                  call: bool, r: float = 0.04) -> float:
    """Strike whose Black-Scholes delta is approximately ``target_delta``."""
    if T <= 0 or iv <= 0:
        raise IVUnavailable(f"cannot solve strike: T={T} iv={iv}")
    # Invert N(d1) = delta for calls, N(-d1) = delta for puts.
    z = norm.ppf(target_delta if call else 1 - target_delta)
    return float(spot * math.exp(-z * iv * math.sqrt(T) + (r + iv * iv / 2) * T))


def nearest(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda k: abs(k - target))


def build_iron_condor(sig: VRPSignal, spot: float, strikes: list[float],
                      occ: dict[tuple[float, str], str],
                      quotes: dict[str, Quote], *,
                      short_delta: float = 0.16, wing_width: float = 0.0,
                      wing_pct: float = 0.015) -> Spread:
    """Short strangle at ~short_delta, long wings the same dollar width out.

    ``wing_width`` is a width in dollars; 0 means "pick it from ``wing_pct`` of
    spot". Narrow wings are a trap. They minimise max loss per contract, but the
    position is sized by risk, so halving the width just doubles the contract
    count — and doubles the bid-ask crossed, while the credit per contract falls
    with the width. Measured live: a $1-wide IWM condor showed 0.20 of mid credit
    and only 0.07 reachable, 34% capture. The per-leg spread is roughly fixed in
    dollars, so the only way to make it a small fraction of the credit is to
    collect a bigger credit — wider wings.

    Equal widths on the two sides are the other half of the point. Strike spacing is not uniform
    across a chain — QQQ lists $1 strikes near the money and $5 further out — so
    taking "one strike out" on each side independently produced a 5-wide call
    wing against a 1-wide put wing. Max loss is set by the WIDER side while the
    credit is collected on both, and that condor was rejected by
    `edge_after_costs` for collecting 0.37 against 4.50 of risk. Balancing the
    wings turns the same short strikes into a structure worth trading.
    """
    T = sig.dte / 365
    call_k = nearest(strikes, _delta_strike(spot, sig.implied, T, short_delta, True))
    put_k = nearest(strikes, _delta_strike(spot, sig.implied, T, short_delta, False))

    above = sorted(k for k in strikes if k > call_k)
    below = sorted((k for k in strikes if k < put_k), reverse=True)
    if not above or not below:
        # The caller fetched too narrow a strike window: the shorts landed at
        # the edge of the chain with nothing beyond them to buy. Wings are not
        # optional, so refuse rather than fall back to a naked strangle.
        raise IVUnavailable(
            f"{sig.symbol}: shorts at {put_k:.0f}/{call_k:.0f} sit at the edge of "
            f"the fetched strikes ({min(strikes):.0f}-{max(strikes):.0f}) — "
            f"widen the moneyness window so wings are available")
    # Widen to whichever side is coarser, so both wings span the same distance.
    natural = max(above[0] - call_k, put_k - below[0])
    target = wing_width if wing_width > 0 else max(natural, spot * wing_pct)
    call_wing = min(above, key=lambda k: abs((k - call_k) - target))
    put_wing = min(below, key=lambda k: abs((put_k - k) - target))

    need = [(call_k, "C"), (call_wing, "C"), (put_k, "P"), (put_wing, "P")]
    for k, t in need:
        if (k, t) not in occ:
            raise IVUnavailable(f"{sig.symbol}: missing contract {k}{t}")
    syms = {kt: occ[kt] for kt in need}
    for s in syms.values():
        q = quotes.get(s)
        if q is None or not q.usable():
            raise IVUnavailable(f"{sig.symbol}: unusable quote for {s}")

    credit = (quotes[syms[(call_k, "C")]].mid + quotes[syms[(put_k, "P")]].mid
              - quotes[syms[(call_wing, "C")]].mid - quotes[syms[(put_wing, "P")]].mid)
    # Worst case is the wider wing minus the credit already collected.
    width = max(call_wing - call_k, put_k - put_wing)
    max_loss = width - credit
    if max_loss <= 0:
        # Credit exceeding the width is an arbitrage, which in practice means a
        # stale or crossed quote. Refuse rather than book a free lunch.
        raise IVUnavailable(
            f"{sig.symbol}: credit {credit:.2f} >= width {width:.2f} — stale quotes")

    legs = (Leg(syms[(call_k, "C")], "sell"), Leg(syms[(call_wing, "C")], "buy"),
            Leg(syms[(put_k, "P")], "sell"), Leg(syms[(put_wing, "P")], "buy"))
    return Spread(sig.symbol, "iron_condor", "SELL_VOL", legs,
                  net_credit=credit, max_loss=max_loss, max_profit=credit,
                  expiry=sig.expiry, dte=sig.dte)


def build_long_strangle(sig: VRPSignal, spot: float, strikes: list[float],
                        occ: dict[tuple[float, str], str],
                        quotes: dict[str, Quote], *,
                        target_delta: float = 0.25) -> Spread:
    """Buy an OTM call and an OTM put. Max loss is the premium — capped by
    construction, which is why no wings are needed."""
    T = sig.dte / 365
    call_k = nearest(strikes, _delta_strike(spot, sig.implied, T, target_delta, True))
    put_k = nearest(strikes, _delta_strike(spot, sig.implied, T, target_delta, False))
    for k, t in ((call_k, "C"), (put_k, "P")):
        if (k, t) not in occ:
            raise IVUnavailable(f"{sig.symbol}: missing contract {k}{t}")
    cs, ps = occ[(call_k, "C")], occ[(put_k, "P")]
    for s in (cs, ps):
        q = quotes.get(s)
        if q is None or not q.usable():
            raise IVUnavailable(f"{sig.symbol}: unusable quote for {s}")

    debit = quotes[cs].mid + quotes[ps].mid
    if debit <= 0:
        raise IVUnavailable(f"{sig.symbol}: non-positive debit {debit}")
    return Spread(sig.symbol, "long_strangle", "BUY_VOL",
                  (Leg(cs, "buy"), Leg(ps, "buy")),
                  net_credit=-debit, max_loss=debit,
                  # Unbounded upside; reported as the move to double the debit,
                  # so risk_reward stays a real number rather than infinity.
                  max_profit=debit * 2, expiry=sig.expiry, dte=sig.dte)


def build_debit_vertical(*, underlying: str, long_symbol: str,
                         long_strike: float, short_symbol: str,
                         short_strike: float, right: str,
                         quotes: dict[str, Quote], expiry: str, dte: int,
                         buffer: float = 0.02) -> Spread:
    """Build a marketable call/put debit vertical with exact finite loss.

    A bullish call vertical buys the lower strike and sells the higher strike.
    A bearish put vertical buys the higher strike and sells the lower strike.
    The executable debit—not midpoint—is the maximum loss used for sizing.
    This matters in a four-day contest: a 0.20 midpoint quoted 0.10/0.30 is not
    twenty dollars of risk when the only reachable entry costs thirty.
    """
    right = right.upper()
    if right not in {"C", "P"}:
        raise ValueError("right must be C or P")
    if long_symbol == short_symbol:
        raise ValueError("vertical legs must be different contracts")
    if not (math.isfinite(long_strike) and math.isfinite(short_strike)):
        raise ValueError("strikes must be finite")
    if right == "C" and not long_strike < short_strike:
        raise ValueError("call debit vertical must buy the lower strike")
    if right == "P" and not long_strike > short_strike:
        raise ValueError("put debit vertical must buy the higher strike")
    if dte < 0:
        raise ValueError("dte cannot be negative")

    long_quote, short_quote = quotes.get(long_symbol), quotes.get(short_symbol)
    if long_quote is None or short_quote is None:
        raise IVUnavailable(f"{underlying}: missing vertical quote")
    if not long_quote.usable() or not short_quote.usable():
        raise IVUnavailable(f"{underlying}: unusable vertical quote")

    legs = (Leg(long_symbol, "buy"), Leg(short_symbol, "sell"))
    debit = marketable_limit(legs, quotes, buffer=buffer)
    width = abs(short_strike - long_strike)
    if debit <= 0:
        raise IVUnavailable(f"{underlying}: non-positive vertical debit {debit:.2f}")
    if debit >= width:
        raise IVUnavailable(
            f"{underlying}: vertical debit {debit:.2f} >= width {width:.2f}")

    return Spread(
        underlying=underlying,
        structure="call_debit_vertical" if right == "C" else "put_debit_vertical",
        direction="BULLISH" if right == "C" else "BEARISH",
        legs=legs,
        net_credit=-debit,
        max_loss=debit,
        max_profit=width - debit,
        expiry=expiry,
        dte=dte,
    )


def build_long_straddle(*, underlying: str, call_symbol: str,
                        put_symbol: str, strike: float,
                        quotes: dict[str, Quote], expiry: str, dte: int,
                        buffer: float = 0.02) -> Spread:
    """Buy an ATM call and put at an executable debit.

    The maximum loss is exactly the marketable premium paid. The payoff is
    convex in either direction, which makes this the event structure for a
    scheduled release whose magnitude may be more predictable than its sign.
    """
    if call_symbol == put_symbol:
        raise ValueError("straddle requires distinct call and put contracts")
    if not math.isfinite(strike) or strike <= 0:
        raise ValueError("strike must be finite and positive")
    if dte < 0:
        raise ValueError("dte cannot be negative")
    call_quote, put_quote = quotes.get(call_symbol), quotes.get(put_symbol)
    if call_quote is None or put_quote is None:
        raise IVUnavailable(f"{underlying}: missing straddle quote")
    if not call_quote.usable() or not put_quote.usable():
        raise IVUnavailable(f"{underlying}: unusable straddle quote")
    legs = (Leg(call_symbol, "buy"), Leg(put_symbol, "buy"))
    debit = marketable_limit(legs, quotes, buffer=buffer)
    if debit <= 0:
        raise IVUnavailable(f"{underlying}: non-positive straddle debit {debit}")
    return Spread(
        underlying=underlying, structure="long_straddle", direction="BUY_VOL",
        legs=legs, net_credit=-debit, max_loss=debit,
        # Upside is unbounded. A finite reporting proxy keeps generic ranking
        # arithmetic defined; terminal payoff code handles actual scenarios.
        max_profit=debit * 2.0, expiry=expiry, dte=dte)


def marketable_limit(legs: tuple[Leg, ...], quotes: dict[str, Quote],
                     *, intent: str = "open", buffer: float = 0.02) -> float:
    """Net limit price that will actually fill, in Alpaca's mleg convention:
    positive = net debit (we pay), negative = net credit (we receive).

    Mid-based pricing does not fill. `build_iron_condor` values every leg at the
    mid because that is the fair value the sizing and the edge test need, but a
    four-leg order has to cross four bid-ask spreads, and nobody sits on the
    other side at all four mids at once. Price each leg where we would actually
    be hit — sell at the bid, buy at the ask — and then concede `buffer` on top,
    because a limit derived from a quote is already stale by the time it lands.

    The buffer is added, never subtracted, and that single sign is correct in
    both directions: for a debit (net > 0) it raises what we will pay, and for a
    credit (net < 0) it shrinks what we insist on receiving. Both moves are
    concessions.

    Verified against live paper fills: a two-leg call credit spread priced this
    way filled at -0.07, while the same spread priced at the mid sat unfilled.
    """
    net = 0.0
    for leg in legs:
        q = quotes[leg.symbol]
        side = leg.side if intent == "open" else _OPPOSITE[leg.side]
        net += (q.ask if side == "buy" else -q.bid) * leg.ratio_qty
    return round(net + buffer, 2)


@dataclass(frozen=True)
class EdgeCheck:
    mid_credit: float          # what the structure is theoretically worth
    marketable_credit: float   # what we can actually get, crossing the spreads
    capture: float             # marketable / mid, in [0, 1] for a sane quote
    round_trip: float          # cost to open AND close at marketable prices
    ok: bool
    reason: str


def edge_after_costs(spread: Spread, quotes: dict[str, Quote], *,
                     min_capture: float = 0.50,
                     min_credit_to_loss: float = 0.10) -> EdgeCheck:
    """Does this structure still have an edge once we pay to get in?

    Every credit in `build_iron_condor` is a mid-price credit, and the mid is
    where nobody trades. Measured live on a $1-wide IWM condor: 0.20 mid credit,
    0.04 once every leg is priced where it would actually be hit. Eighty percent
    of the edge was in the spread, not in the variance risk premium. A strategy
    that books the mid and pays the market is short its own transaction costs.

    Two gates, because they fail differently:

    `capture` catches structures whose edge is mostly bid-ask — narrow wings on
    a wide-quoted chain, where the premium is real but unreachable.

    `min_credit_to_loss` catches structures that survive the first test but are
    simply not worth the risk: collecting 0.04 to risk 0.96 needs a 96% win rate
    to break even, and no VRP signal is that good.

    `round_trip` is reported rather than gated. It is the cost of opening and
    immediately closing, and it is the number that decides whether active
    management is affordable: a condor held to expiry pays it once, a condor
    rolled at 21 DTE pays it twice. Cheap to hold, expensive to manage.
    """
    mid = spread.net_credit
    mkt = -marketable_limit(spread.legs, quotes, buffer=0.0)
    exit_cost = marketable_limit(spread.legs, quotes, intent="close", buffer=0.0)
    capture = mkt / mid if mid > 0 else 0.0
    rt = exit_cost - mkt
    if mkt <= 0:
        return EdgeCheck(mid, mkt, capture, rt, False,
                         f"marketable credit {mkt:.2f} is not a credit at all")
    if capture < min_capture:
        return EdgeCheck(mid, mkt, capture, rt, False,
                         f"only {capture:.0%} of the {mid:.2f} mid credit is "
                         f"reachable; the rest is bid-ask")
    ratio = mkt / spread.max_loss
    if ratio < min_credit_to_loss:
        return EdgeCheck(mid, mkt, capture, rt, False,
                         f"collecting {mkt:.2f} to risk {spread.max_loss:.2f} "
                         f"({ratio:.0%}) needs a {1 - ratio:.0%} win rate")
    return EdgeCheck(mid, mkt, capture, rt, True,
                     f"{capture:.0%} of mid reachable, {ratio:.0%} credit/risk")


def payoff_at(legs: tuple[Leg, ...], prices) -> "np.ndarray":
    """Terminal value of a structure, per share, across underlying prices.

    Signed by side: a sold leg pays out negatively, because at expiry we owe
    its intrinsic value. This is the payoff BEFORE the premium paid or
    received — combine with the entry price to get P&L. Keeping the two
    separate is deliberate: the payoff is a property of the structure, the
    entry price is a property of the fill we got, and conflating them is how a
    structure gets evaluated at a price nobody could actually trade.
    """
    import numpy as np

    from .occ import parse

    S = np.atleast_1d(np.asarray(prices, dtype=float))
    total = np.zeros_like(S)
    for leg in legs:
        c = parse(leg.symbol)
        intrinsic = (np.maximum(S - c.strike, 0.0) if c.is_call
                     else np.maximum(c.strike - S, 0.0))
        sign = 1.0 if leg.side == "buy" else -1.0
        total += sign * intrinsic * leg.ratio_qty
    return total

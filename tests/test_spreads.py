"""Tests for defined-risk spread construction.

The invariant these protect: no structure reaches an order unless its worst case
is a finite, positive, computed number. Selling volatility has a small steady
edge and an enormous tail — a single naked position that gaps can exceed every
gain the strategy ever made.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

import pytest

from trading_bot.options.iv import IVUnavailable, Quote
from trading_bot.options.spreads import (
    Leg, Spread, build_debit_vertical, build_iron_condor, build_long_straddle,
    build_long_strangle, nearest,
)
from trading_bot.options.vrp import VRPSignal

STRIKES = [float(k) for k in range(80, 121, 1)]
SIG_SELL = VRPSignal("X", 0.30, 0.20, 0.20, 6, "2026-09-18", 30)
SIG_BUY = VRPSignal("X", 0.20, 0.30, 0.30, 6, "2026-09-18", 30)


def _occ(strikes=STRIKES):
    return {(k, t): f"X260918{t}{int(k*1000):08d}" for k in strikes for t in ("C", "P")}


def _quotes(occ, spot=100.0):
    """Prices that decay with distance from spot, as a real chain does. A flat
    fixture makes every spread net to zero credit and hides real behaviour."""
    out = {}
    for (k, t), sym in occ.items():
        moneyness = abs(k - spot)
        mid = max(0.05, 8.0 * (0.85 ** moneyness))
        out[sym] = Quote(round(mid * 0.95, 2), round(mid * 1.05, 2))
    return out


# ------------------------------------------------- the core safety invariant


def test_spread_rejects_infinite_max_loss():
    with pytest.raises(ValueError):
        Spread("X", "naked", "SELL_VOL", (Leg("a", "sell"), Leg("b", "sell")),
               net_credit=1.0, max_loss=float("inf"), max_profit=1.0,
               expiry="2026-09-18", dte=30)


def test_spread_rejects_zero_or_negative_max_loss():
    for bad in (0.0, -5.0):
        with pytest.raises(ValueError):
            Spread("X", "s", "SELL_VOL", (Leg("a", "sell"), Leg("b", "buy")),
                   net_credit=1.0, max_loss=bad, max_profit=1.0,
                   expiry="2026-09-18", dte=30)


def test_spread_rejects_odd_leg_counts():
    """2 legs (strangle) or 4 (condor). Three means a leg failed to build, and
    a partial structure is not risk-defined."""
    with pytest.raises(ValueError):
        Spread("X", "s", "SELL_VOL", (Leg("a", "sell"),), net_credit=1.0,
               max_loss=1.0, max_profit=1.0, expiry="2026-09-18", dte=30)


# ---------------------------------------------------------- iron condor


def test_iron_condor_has_four_legs_and_capped_loss():
    occ = _occ()
    s = build_iron_condor(SIG_SELL, 100.0, STRIKES, occ, _quotes(occ))
    assert s.structure == "iron_condor" and len(s.legs) == 4
    assert s.max_loss > 0 and s.max_loss < float("inf")
    assert s.net_credit > 0                      # we are paid to take the risk


def test_iron_condor_buys_wings_outside_the_shorts():
    """The wings are the entire point. Without them this is a naked strangle."""
    occ = _occ()
    s = build_iron_condor(SIG_SELL, 100.0, STRIKES, occ, _quotes(occ))
    sells = [l for l in s.legs if l.side == "sell"]
    buys = [l for l in s.legs if l.side == "buy"]
    assert len(sells) == 2 and len(buys) == 2


def test_credit_exceeding_width_is_rejected_as_stale():
    """Credit >= width is an arbitrage, which in reality means a crossed or
    stale quote. Booking it would be acting on bad data."""
    occ = _occ()
    q = _quotes(occ)
    # make the short legs absurdly rich relative to the wings
    for (k, t), s in occ.items():
        q[s] = Quote(20.0, 21.0) if k in (84.0, 116.0) else Quote(0.01, 0.02)
    with pytest.raises(IVUnavailable):
        build_iron_condor(SIG_SELL, 100.0, STRIKES, occ, q)


def test_unusable_quote_blocks_the_structure():
    occ = _occ()
    q = _quotes(occ)
    q[next(iter(occ.values()))] = Quote(0.05, 5.00)     # 197% wide
    # at least one leg is unusable -> refuse rather than price off fiction
    with pytest.raises(IVUnavailable):
        build_iron_condor(SIG_SELL, 100.0, STRIKES, occ,
                          {k: Quote(0.05, 5.00) for k in occ.values()})


def test_missing_wing_strikes_raise():
    """Shorts at the edge of the chain leave nothing to buy for protection."""
    tight = [98.0, 99.0, 100.0, 101.0, 102.0]
    occ = _occ(tight)
    with pytest.raises(IVUnavailable):
        build_iron_condor(SIG_SELL, 100.0, tight, occ, _quotes(occ))


# --------------------------------------------------------- long strangle


def test_long_strangle_is_capped_by_the_premium():
    occ = _occ()
    s = build_long_strangle(SIG_BUY, 100.0, STRIKES, occ, _quotes(occ))
    assert s.structure == "long_strangle" and len(s.legs) == 2
    assert all(l.side == "buy" for l in s.legs)
    assert s.net_credit < 0                       # we pay
    assert s.max_loss == pytest.approx(-s.net_credit)


# --------------------------------------------------------- debit verticals

def test_call_debit_vertical_sizes_from_executable_debit_not_mid():
    q = {"LC": Quote(2.00, 2.10), "SC": Quote(0.95, 1.05)}
    s = build_debit_vertical(
        underlying="AMD", long_symbol="LC", long_strike=100,
        short_symbol="SC", short_strike=105, right="C", quotes=q,
        expiry="2026-09-04", dte=4, buffer=0.02)
    # Buy at 2.10, sell at 0.95, concede 0.02 = 1.17 debit. Mid is only 1.05.
    assert s.structure == "call_debit_vertical"
    assert s.net_credit == pytest.approx(-1.17)
    assert s.max_loss == pytest.approx(1.17)
    assert s.max_profit == pytest.approx(3.83)


def test_put_debit_vertical_has_bearish_direction_and_finite_loss():
    q = {"LP": Quote(2.00, 2.10), "SP": Quote(0.95, 1.05)}
    s = build_debit_vertical(
        underlying="AMD", long_symbol="LP", long_strike=105,
        short_symbol="SP", short_strike=100, right="P", quotes=q,
        expiry="2026-09-04", dte=4)
    assert s.structure == "put_debit_vertical" and s.direction == "BEARISH"
    assert s.max_loss > 0 and s.max_loss < 5.0


def test_debit_vertical_rejects_reversed_geometry():
    q = {"A": Quote(1, 1.1), "B": Quote(0.5, 0.6)}
    with pytest.raises(ValueError, match="lower strike"):
        build_debit_vertical(
            underlying="AMD", long_symbol="A", long_strike=105,
            short_symbol="B", short_strike=100, right="C", quotes=q,
            expiry="2026-09-04", dte=4)


def test_debit_vertical_rejects_a_debit_at_or_above_width():
    q = {"A": Quote(5.0, 5.1), "B": Quote(0.10, 0.11)}
    with pytest.raises(IVUnavailable, match=">= width"):
        build_debit_vertical(
            underlying="AMD", long_symbol="A", long_strike=100,
            short_symbol="B", short_strike=105, right="C", quotes=q,
            expiry="2026-09-04", dte=4)


def test_long_straddle_max_loss_is_the_executable_premium():
    q = {"C": Quote(14.0, 14.5), "P": Quote(14.2, 14.7)}
    s = build_long_straddle(
        underlying="AVGO", call_symbol="C", put_symbol="P", strike=365,
        quotes=q, expiry="2026-09-04", dte=7, buffer=0.02)
    assert s.structure == "long_straddle" and s.direction == "BUY_VOL"
    assert s.max_loss == pytest.approx(29.22)
    assert s.net_credit == pytest.approx(-29.22)
    assert all(leg.side == "buy" for leg in s.legs)


def test_long_straddle_refuses_an_unusable_leg_quote():
    q = {"C": Quote(1.0, 2.0), "P": Quote(14.2, 14.7)}
    with pytest.raises(IVUnavailable, match="unusable"):
        build_long_straddle(
            underlying="AVGO", call_symbol="C", put_symbol="P", strike=365,
            quotes=q, expiry="2026-09-04", dte=7)


# ------------------------------------------------------------- sizing


def test_sizing_uses_max_loss_not_premium():
    """A condor collecting $0.40 that can lose $4.60 is a $460 risk. Sizing off
    the credit is how a small-looking book ends an account."""
    s = Spread("X", "iron_condor", "SELL_VOL",
               (Leg("a", "sell"), Leg("b", "buy"), Leg("c", "sell"), Leg("d", "buy")),
               net_credit=0.40, max_loss=4.60, max_profit=0.40,
               expiry="2026-09-18", dte=30)
    assert s.contracts_for_risk(1000.0) == 2      # 1000 // 460
    assert s.contracts_for_risk(100.0) == 0       # cannot afford one


def test_sizing_never_returns_negative():
    s = Spread("X", "s", "SELL_VOL", (Leg("a", "sell"), Leg("b", "buy")),
               net_credit=1.0, max_loss=10.0, max_profit=1.0,
               expiry="2026-09-18", dte=30)
    assert s.contracts_for_risk(0.0) == 0


def test_nearest_strike_picks_the_closest():
    assert nearest([95.0, 100.0, 105.0], 101.0) == 100.0
    assert nearest([95.0, 100.0, 105.0], 104.0) == 105.0


def test_mcp_leg_format_is_what_the_server_expects():
    leg = Leg("X260918C00100000", "sell").as_mcp()
    assert leg["symbol"] == "X260918C00100000"
    assert leg["ratio_qty"] == "1"               # server wants strings
    assert leg["position_intent"] == "sell_to_open"


# --- marketable pricing -------------------------------------------------
# These exist because the mid-priced order sat unfilled on a live paper
# account while the same spread priced at bid/ask filled immediately.

def test_marketable_limit_sells_at_bid_and_buys_at_ask():
    from trading_bot.options.spreads import marketable_limit
    legs = (Leg("C317", "sell"), Leg("C318", "buy"))
    q = {"C317": Quote(0.84, 0.88), "C318": Quote(0.69, 0.73)}
    # credit = 0.84 - 0.73 = 0.11, minus the 0.02 concession -> -0.09
    assert marketable_limit(legs, q, buffer=0.02) == -0.09
    # priced at the mid it would have asked for -0.15 and never filled
    assert marketable_limit(legs, q, buffer=0.02) > -0.15


def test_buffer_always_concedes_in_both_directions():
    """One added buffer must make a debit worse AND a credit worse."""
    from trading_bot.options.spreads import marketable_limit
    q = {"A": Quote(1.00, 1.10), "B": Quote(0.20, 0.30)}
    credit = (Leg("A", "sell"), Leg("B", "buy"))
    debit = (Leg("A", "buy"), Leg("B", "buy"))
    assert marketable_limit(credit, q, buffer=0.05) > marketable_limit(credit, q, buffer=0.0)
    assert marketable_limit(debit, q, buffer=0.05) > marketable_limit(debit, q, buffer=0.0)


def test_closing_reverses_side_and_intent():
    legs = (Leg("C317", "sell"), Leg("C318", "buy"))
    closed = [l.as_mcp("close") for l in legs]
    assert [c["side"] for c in closed] == ["buy", "sell"]
    assert all(c["position_intent"].endswith("_to_close") for c in closed)
    # and opening is untouched
    assert all(l.as_mcp()["position_intent"].endswith("_to_open") for l in legs)


def test_credit_spread_closes_for_a_debit():
    """Sign must flip on exit, or we submit a limit the book can never hit."""
    from trading_bot.options.spreads import marketable_limit
    legs = (Leg("C317", "sell"), Leg("C318", "buy"))
    q = {"C317": Quote(0.84, 0.88), "C318": Quote(0.69, 0.73)}
    assert marketable_limit(legs, q) < 0            # opened for a credit
    assert marketable_limit(legs, q, intent="close") > 0   # bought back at a debit


# --- edge after costs ---------------------------------------------------

# Real OCC symbols, so the fixture can be cross-checked against `payoff_at`.
# The previous version used "SC"/"LC"/"SP"/"LP", which meant nothing in this
# block could ever be verified against the code's own arithmetic — and that is
# how the capture-gate blind spot below stayed invisible.
SC, LC = "XYZ260925C00101000", "XYZ260925C00102000"
SP, LP = "XYZ260925P00099000", "XYZ260925P00098000"


def _condor(quotes):
    legs = (Leg(SC, "sell"), Leg(LC, "buy"), Leg(SP, "sell"), Leg(LP, "buy"))
    mid = (quotes[SC].mid + quotes[SP].mid - quotes[LC].mid - quotes[LP].mid)
    width = 1.0
    return Spread("X", "iron_condor", "SELL_VOL", legs, net_credit=mid,
                  max_loss=width - mid, max_profit=mid, expiry="2026-09-25", dte=30)


def test_edge_gate_rejects_a_structure_whose_credit_is_all_bid_ask():
    from trading_bot.options.spreads import edge_after_costs
    # Mid says 0.40; crossing every spread leaves 0.08 — 20% capture. The
    # marketable credit must stay POSITIVE, or the check exits at the earlier
    # not-a-credit gate and the capture gate is never exercised.
    q = {SC: Quote(0.52, 0.68), LC: Quote(0.32, 0.48),
         SP: Quote(0.52, 0.68), LP: Quote(0.32, 0.48)}
    chk = edge_after_costs(_condor(q), q)
    assert not chk.ok
    assert chk.marketable_credit < chk.mid_credit
    # Named for the CAPTURE gate, so it must reach the capture gate. The
    # previous version accepted "not a credit at all" via an `or`, which let
    # this pass down the earlier branch — and allowed both gates to be deleted
    # without any test noticing. An audit found exactly that.
    assert chk.marketable_credit > 0, \
        "fixture exits at the not-a-credit gate, so it cannot test capture"
    assert "bid-ask" in chk.reason


def test_edge_gate_accepts_a_tight_liquid_structure():
    from trading_bot.options.spreads import edge_after_costs
    q = {SC: Quote(0.99, 1.01), LC: Quote(0.59, 0.61),
         SP: Quote(0.99, 1.01), LP: Quote(0.59, 0.61)}
    chk = edge_after_costs(_condor(q), q)
    assert chk.ok, chk.reason
    assert chk.capture > 0.9


def test_round_trip_is_positive_and_reported_not_gated():
    """Opening and immediately closing always costs money; the gate must not
    silently treat that as a reason to refuse an otherwise-good trade."""
    from trading_bot.options.spreads import edge_after_costs
    q = {SC: Quote(0.99, 1.01), LC: Quote(0.59, 0.61),
         SP: Quote(0.99, 1.01), LP: Quote(0.59, 0.61)}
    chk = edge_after_costs(_condor(q), q)
    assert chk.round_trip > 0
    assert chk.ok


def test_wings_are_balanced_across_uneven_strike_spacing():
    """$1 strikes below the money and $5 above must not produce a 1-wide put
    wing against a 5-wide call wing — max loss would come from one side and the
    credit from both."""
    from trading_bot.options.spreads import build_iron_condor
    spot = 712.0
    strikes = [float(k) for k in range(660, 686)] + [745.0, 750.0, 755.0,
               760.0, 765.0, 770.0, 775.0]
    occ = {(k, r): f"X{r}{int(k)}" for k in strikes for r in ("C", "P")}
    quotes = {}
    for k in strikes:
        for r in ("C", "P"):
            d = abs(k - spot)
            px = max(0.05, 8.0 - d * 0.10)
            quotes[occ[(k, r)]] = Quote(round(px - 0.02, 2), round(px + 0.02, 2))
    sig = VRPSignal("X", 0.23, 0.18, 0.19, 8, "2026-09-25", 30)
    sp = build_iron_condor(sig, spot, strikes, occ, quotes)
    ks = [float(l.symbol[2:]) for l in sp.legs]
    call_width, put_width = abs(ks[1] - ks[0]), abs(ks[2] - ks[3])
    assert call_width == put_width, f"unbalanced wings: {call_width} vs {put_width}"

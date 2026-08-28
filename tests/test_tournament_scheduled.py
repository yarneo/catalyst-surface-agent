import datetime as dt

import pytest

from trading_bot.options.clock import ET
from trading_bot.tournament.scheduled import (
    ScheduledEventPolicy,
    evaluate_entry,
    lifecycle_action,
    surface_from_mcp,
)


POLICY = ScheduledEventPolicy()
NOW = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)


def leg(right, strike, bid, ask, *, age=5, bid_size=20, ask_size=20,
        iv=0.72):
    symbol = f"AVGO260904{right}{int(strike * 1000):08d}"
    return symbol, {
        "latestQuote": {
            "bp": bid, "ap": ask, "bs": bid_size, "as": ask_size,
            "t": (NOW - dt.timedelta(seconds=age)).isoformat(),
        },
        "impliedVolatility": iv,
    }


def payload(*, call=(14.0, 14.4), put=(14.2, 14.6), strike=365.0,
            age=5, bid_size=20, ask_size=20):
    c = leg("C", strike, *call, age=age, bid_size=bid_size, ask_size=ask_size)
    p = leg("P", strike, *put, age=age, bid_size=bid_size, ask_size=ask_size)
    return {"snapshots": {c[0]: c[1], p[0]: p[1]}, "next_page_token": None}


def surface(**changes):
    values = dict(payload=payload(), spot=366.0, observed_at=NOW, policy=POLICY)
    values.update(changes)
    return surface_from_mcp(**values)


def test_surface_selects_nearest_common_strike_not_first_contract():
    rows = payload(strike=365)["snapshots"]
    rows.update(payload(strike=370, call=(12, 12.3), put=(15, 15.3))["snapshots"])
    selected = surface_from_mcp(
        payload={"snapshots": rows}, spot=369.2, observed_at=NOW)
    assert selected.strike == 370


def test_valid_market_builds_executable_defined_loss_straddle():
    decision = evaluate_entry(now=NOW, surface=surface())
    assert decision.eligible
    assert decision.spread is not None
    assert decision.spread.structure == "long_straddle"
    # Both asks plus the frozen two-cent marketability buffer.
    assert decision.spread.max_loss == pytest.approx(29.02)


def test_premium_ratio_gate_uses_executable_debit():
    expensive = surface(payload=payload(call=(15.0, 15.5), put=(15.2, 15.7)))
    decision = evaluate_entry(now=NOW, surface=expensive)
    assert not decision.eligible
    assert "executable premium exceeds frozen spot ratio" in decision.reasons


@pytest.mark.parametrize("change,reason", [
    ({"payload": payload(call=(10.0, 12.0))}, "call quote is unusable or too wide"),
    ({"payload": payload(age=91)}, "option quote is stale"),
    ({"payload": payload(bid_size=0)}, "call displayed size is too small"),
    ({"spot": 370.0}, "nearest common strike is too far from spot"),
])
def test_each_surface_gate_has_an_explicit_no_trade_reason(change, reason):
    decision = evaluate_entry(now=NOW, surface=surface(**change))
    assert not decision.eligible
    assert reason in decision.reasons


def test_combined_width_can_fail_even_when_individual_legs_pass():
    wide = surface(payload=payload(call=(13.3, 14.4), put=(13.5, 14.6)))
    decision = evaluate_entry(now=NOW, surface=wide)
    assert not decision.eligible
    assert "combined bid/ask width exceeds frozen limit" in decision.reasons


def test_malformed_or_wrong_expiry_payload_never_creates_a_surface():
    with pytest.raises(ValueError, match="no common"):
        surface_from_mcp(
            payload={"snapshots": {"AVGO260911C00365000": {}}},
            spot=366, observed_at=NOW)


def test_lifecycle_is_predeclared_and_does_not_ask_a_model():
    assert lifecycle_action(now=dt.datetime(2026, 9, 2, 15, 19, tzinfo=ET),
                            has_position=False, entry_was_attempted=False) == "WAIT"
    assert lifecycle_action(now=POLICY.entry_start, has_position=False,
                            entry_was_attempted=False) == "ENTER"
    assert lifecycle_action(now=POLICY.event_at, has_position=True,
                            entry_was_attempted=True) == "HOLD"
    assert lifecycle_action(now=POLICY.exit_at, has_position=True,
                            entry_was_attempted=True) == "EXIT"
    assert lifecycle_action(now=POLICY.entry_end + dt.timedelta(seconds=1),
                            has_position=False, entry_was_attempted=False) == "DONE"


def test_one_attempt_means_no_chasing_after_a_nonfill():
    assert lifecycle_action(now=NOW, has_position=False,
                            entry_was_attempted=True) == "DONE"

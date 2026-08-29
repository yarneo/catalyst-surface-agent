import datetime as dt
import math

import pytest

from trading_bot.options.clock import ET
from trading_bot.options.iv import bs_price_forward
from trading_bot.tournament.event_premium import (
    EventPremiumObservation,
    EventPremiumUnavailable,
    compare_with_historical_gaps,
    decompose_event_premium,
    rank_cross_section,
    surface_from_mcp,
)


NOW = dt.datetime(2026, 8, 29, 12, 0, tzinfo=ET)


def chain(expiry: str, iv: float, *, symbol: str = "TEST", spread: float = 0.05):
    stamp = dt.datetime(2026, 8, 28, 15, 59, 59, tzinfo=ET).isoformat()
    compact_expiry = expiry[2:].replace("-", "")
    days = (dt.date.fromisoformat(expiry) - NOW.date()).days
    rows = {}
    for strike in (97.5, 100.0, 102.5):
        for right, offset in (("C", 0.002), ("P", -0.002)):
            option = f"{symbol}{compact_expiry}{right}{int(strike * 1000):08d}"
            mid = bs_price_forward(
                100.0, strike, days / 365, 0.04, iv,
                call=right == "C")
            rows[option] = {
                # The scanner must ignore provider IV and recover the value
                # from parity plus executable quotes.
                "impliedVolatility": 9.0 + offset,
                "latestQuote": {
                    "bp": mid - spread / 2,
                    "ap": mid + spread / 2,
                    "bs": 10,
                    "as": 10,
                    "t": stamp,
                },
            }
    return {"snapshots": rows}


def observation(symbol: str, variance_days: float, *, sector: str = "tech"):
    return EventPremiumObservation(
        symbol=symbol, sector=sector, event_type="earnings", spot=100,
        front_expiry="2026-09-04", back_expiry="2026-09-11",
        front_iv=0.5, back_iv=0.4, base_iv=0.3,
        implied_event_move=math.sqrt(variance_days * 0.3**2 / 252),
        standardized_jump=math.sqrt(variance_days), variance_days=variance_days,
        term_ratio=1.25, executable_front_premium_to_spot=0.06,
        front_total_spread_pct=0.03, front_pair_count=3, back_pair_count=3,
        max_quote_age_s=60,
    )


def test_extracts_paired_surface_and_executable_front_premium():
    result = surface_from_mcp(
        payload=chain("2026-09-04", 0.60), symbol="TEST",
        expiry="2026-09-04", spot=100.0, observed_at=NOW,
    )
    assert result.atm_iv == pytest.approx(0.60)
    assert result.pair_count == 3
    assert result.nearest_strike == 100.0
    atm_mid = bs_price_forward(100, 100, 6 / 365, 0.04, 0.60, call=True)
    assert result.executable_straddle_ask == pytest.approx(2 * atm_mid + 0.05)
    assert result.total_spread_pct == pytest.approx(0.10 / (2 * atm_mid))
    assert result.max_quote_age_s > 0


def test_decomposition_recovers_known_base_and_jump():
    front = surface_from_mcp(
        payload=chain("2026-09-04", 0.80), symbol="TEST",
        expiry="2026-09-04", spot=100.0, observed_at=NOW,
    )
    back = surface_from_mcp(
        payload=chain("2026-09-11", 0.60), symbol="TEST",
        expiry="2026-09-11", spot=100.0, observed_at=NOW,
    )
    result = decompose_event_premium(front, back, sector="technology")
    front_t, back_t = 6 / 365, 13 / 365
    expected_base_var = (0.60**2 * back_t - 0.80**2 * front_t) / (back_t-front_t)
    expected_jump_var = 0.80**2 * front_t - expected_base_var * front_t
    assert result.base_iv == pytest.approx(math.sqrt(expected_base_var))
    assert result.implied_event_move == pytest.approx(math.sqrt(expected_jump_var))
    assert result.variance_days == pytest.approx(expected_jump_var / (expected_base_var / 252))
    assert result.standardized_jump == pytest.approx(math.sqrt(result.variance_days))
    assert result.shadow_only is True and result.order_enabled is False


def test_decomposition_refuses_term_structure_incompatible_with_shared_jump():
    front = surface_from_mcp(
        payload=chain("2026-09-04", 0.40), symbol="TEST",
        expiry="2026-09-04", spot=100.0, observed_at=NOW,
    )
    back = surface_from_mcp(
        payload=chain("2026-09-11", 0.60), symbol="TEST",
        expiry="2026-09-11", spot=100.0, observed_at=NOW,
    )
    with pytest.raises(EventPremiumUnavailable, match="shared jump"):
        decompose_event_premium(front, back)


def test_cross_section_ranks_extremes_but_never_calls_them_validated():
    rows = [observation(f"N{i}", value) for i, value in enumerate((1, 2, 3, 4, 5, 20))]
    ranked = rank_cross_section(rows)
    by_symbol = {row.observation.symbol: row for row in ranked}
    assert by_symbol["N5"].hypothesis == "SELL_VOL_RESEARCH"
    assert by_symbol["N0"].hypothesis == "BUY_VOL_RESEARCH"
    assert all(not row.validated_edge for row in ranked)
    assert all(row.observation.order_enabled is False for row in ranked)


def test_cross_section_requires_a_real_reference_class():
    with pytest.raises(EventPremiumUnavailable, match="at least three"):
        rank_cross_section([observation("A", 1), observation("B", 2)])


def test_historical_gap_comparison_uses_next_trading_bar_and_stays_unvalidated():
    bars = [
        {"t": "2026-01-02T05:00:00Z", "o": 99, "c": 100},
        {"t": "2026-01-05T05:00:00Z", "o": 110, "c": 109},
        {"t": "2026-04-02T04:00:00Z", "o": 100, "c": 100},
        {"t": "2026-04-06T04:00:00Z", "o": 95, "c": 96},
        {"t": "2026-07-02T04:00:00Z", "o": 100, "c": 100},
        {"t": "2026-07-06T04:00:00Z", "o": 120, "c": 118},
    ]
    row = observation("TEST", 4)
    result = compare_with_historical_gaps(
        row, event_dates=("2026-01-02", "2026-04-02", "2026-07-02"),
        daily_bars=bars)
    assert result.absolute_gaps == pytest.approx((0.10, 0.05, 0.20))
    assert result.median_absolute_gap == pytest.approx(0.10)
    assert result.gap_exceeds_premium_rate == pytest.approx(2 / 3)
    assert result.intrinsic_floor_median_return == pytest.approx(0.10 / 0.06 - 1)
    assert result.validated_edge is False

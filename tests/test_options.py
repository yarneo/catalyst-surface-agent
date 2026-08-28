"""Tests for the options / variance-risk-premium agent.

The failure mode that matters here is a fabricated number reaching a position
size. Alpaca does not serve implied vol on the free feed, so we compute it — and
a solver that returns something plausible for an unsolvable input is worse than
one that raises, because the caller cannot tell.

Run: uv run python -m pytest tests/ -q
"""

from __future__ import annotations

import math

import pytest

from trading_bot.options import IVUnavailable, Quote, bs_price, implied_vol
from trading_bot.options.vrp import VRPSignal


# ------------------------------------------------------------ black-scholes


def test_round_trip_recovers_the_input_vol():
    for sig in (0.08, 0.25, 0.60, 1.20):
        p = bs_price(100, 100, 30 / 365, 0.04, sig)
        assert implied_vol(p, 100, 100, 30 / 365) == pytest.approx(sig, abs=1e-4)


def test_put_call_parity_holds():
    S, K, T, r = 100.0, 95.0, 60 / 365, 0.04
    c = bs_price(S, K, T, r, 0.3, call=True)
    p = bs_price(S, K, T, r, 0.3, call=False)
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-8)


def test_deep_itm_is_worth_at_least_intrinsic():
    assert bs_price(150, 100, 30 / 365, 0.04, 0.2) >= 150 - 100 * math.exp(-0.04 * 30 / 365)


# --------------------------------------------- refusing to invent a number


def test_price_below_intrinsic_raises():
    """No implied vol exists for a price under intrinsic. brentq handed one
    either fails obscurely or returns a boundary value that looks real."""
    with pytest.raises(IVUnavailable):
        implied_vol(0.5, 100, 90, 30 / 365)


def test_price_above_upper_bound_raises():
    with pytest.raises(IVUnavailable):
        implied_vol(120.0, 100, 90, 30 / 365)


def test_expired_contract_raises():
    with pytest.raises(IVUnavailable):
        implied_vol(5.0, 100, 100, 0.0)


def test_non_positive_price_raises():
    for bad in (0.0, -1.0):
        with pytest.raises(IVUnavailable):
            implied_vol(bad, 100, 100, 30 / 365)


def test_vol_outside_the_search_bracket_raises_not_clamps():
    """A price implying >500% vol must raise, not silently return 5.0 — a
    clamped boundary value is indistinguishable from a real measurement."""
    with pytest.raises(IVUnavailable):
        implied_vol(bs_price(100, 100, 30 / 365, 0.04, 4.99) + 20, 100, 100, 30 / 365)


# ------------------------------------------------------------- liquidity


def test_wide_spread_is_unusable():
    """A 160%-wide spread means the mid is fiction, and any IV from it too."""
    assert not Quote(0.10, 0.90).usable()
    assert Quote(1.00, 1.10).usable()


def test_crossed_or_empty_quote_is_unusable():
    assert not Quote(0.0, 1.0).usable()      # no bid
    assert not Quote(1.0, 0.5).usable()      # crossed


# ---------------------------------------------------------------- signal


def _sig(implied, forecast, n=6):
    return VRPSignal("X", implied, forecast, forecast, n, "2026-09-18", 30)


def test_ratio_not_points_drives_the_decision():
    """3 vol points on a 10-vol name is a real edge; 3 points on a 60-vol name
    is noise. Thresholding on the difference would treat them alike."""
    cheap_name = _sig(0.13, 0.10)      # +3pp, ratio 1.30
    rich_name = _sig(0.63, 0.60)       # +3pp, ratio 1.05
    assert cheap_name.direction() == "SELL_VOL"
    assert rich_name.direction() == "STAND_ASIDE"


def test_both_directions_are_tradeable():
    assert _sig(0.30, 0.20).direction() == "SELL_VOL"    # 1.50x
    assert _sig(0.20, 0.30).direction() == "BUY_VOL"     # 0.67x


def test_standing_aside_is_the_default():
    for ratio in (0.90, 0.95, 1.00, 1.05, 1.10):
        assert _sig(0.20 * ratio, 0.20).direction() == "STAND_ASIDE"


def test_a_single_strike_is_not_a_surface():
    """One strike can be stale or crossed; a median needs several. Trading off
    one quote is how a bad print becomes a position."""
    assert _sig(0.40, 0.20, n=1).direction() == "STAND_ASIDE"
    assert _sig(0.40, 0.20, n=3).direction() == "SELL_VOL"


def test_explain_states_the_reasoning():
    txt = _sig(0.30, 0.20).explain()
    assert "SELL_VOL" in txt and "1.50x" in txt


def test_nothing_here_can_place_an_order():
    """Read-only until the event's dedicated account exists. No order path
    may enter this package by accident."""
    import subprocess
    # The package DOES place orders now — that is the point of `execution.py`.
    # What must stay true is narrower and more useful: order placement happens
    # in exactly one module, so there is one place to audit and one place where
    # `live=False` has to be honoured.
    hits = subprocess.run(
        ["grep", "-rlE", r"mcp\.place_spread|mcp\.close_spread",
         "src/trading_bot/options"], capture_output=True, text=True).stdout
    files = sorted(f.split("/")[-1] for f in hits.strip().split("\n") if f
                   and "__pycache__" not in f)
    assert files == ["execution.py"], \
        f"order placement must live only in execution.py, found: {files}"


# ------------------------------------------------------- rate limiting


def test_rate_limiter_spaces_requests():
    """Alpaca's free tier allows 200 req/min. A 40-name scan is ~200 calls, so
    without spacing we hit 429s mid-scan and symbols silently produce no
    signal — a data failure that looks exactly like a trading decision."""
    import time
    from trading_bot.options.chain import RateLimiter
    rl = RateLimiter(per_minute=600)          # 0.1s apart, fast enough to test
    t0 = time.monotonic()
    for _ in range(4):
        rl.wait()
    assert time.monotonic() - t0 >= 0.25       # 3 gaps of 0.1s, minus slack


def test_client_caches_spot_within_ttl(monkeypatch):
    """Spot and contract lists barely move within one scan, and re-fetching
    them is most of the call budget."""
    from trading_bot.options.chain import ChainClient, RateLimiter
    calls = {"n": 0}

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"latestTrade": {"p": 100.0}}

    def fake_get(url, headers=None, timeout=None, params=None):
        calls["n"] += 1
        return _R()

    monkeypatch.setattr("trading_bot.options.chain.requests.get", fake_get)
    c = ChainClient("k", "s", limiter=RateLimiter(per_minute=100_000))
    assert c.spot("SPY") == 100.0
    assert c.spot("SPY") == 100.0
    assert calls["n"] == 1, "second call should have been served from cache"


def test_client_retries_once_on_429(monkeypatch):
    """A 429 is transient. Dropping the symbol would look like the agent chose
    not to trade it."""
    from trading_bot.options.chain import ChainClient, RateLimiter
    seq = {"n": 0}

    class _R:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400: raise AssertionError("should not raise")
        def json(self): return {"latestTrade": {"p": 42.0}}

    def fake_get(url, headers=None, timeout=None, params=None):
        seq["n"] += 1
        return _R(429) if seq["n"] == 1 else _R(200)

    monkeypatch.setattr("trading_bot.options.chain.requests.get", fake_get)
    monkeypatch.setattr("trading_bot.options.chain.time.sleep", lambda *_: None)
    c = ChainClient("k", "s", limiter=RateLimiter(per_minute=100_000))
    assert c.spot("X") == 42.0
    assert seq["n"] == 2

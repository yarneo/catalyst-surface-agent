"""Tests for the layer that decides WHICH STRIKES reach the builder.

An audit flagged this as the largest untested surface in the repo, and as "the
same shape as the original catastrophic defect" — a risk number computed over a
window narrower than the strikes actually being traded. Everything downstream
trusts that `contracts`, `expiries`, `signal` and `structure_inputs` return the
chain they claim to.

The HTTP layer is faked, so these exercise the real filtering, pinning and
truncation logic rather than Alpaca's availability.
"""
import datetime as dt

import pytest

from trading_bot.options.chain import ChainClient, RateLimiter
from trading_bot.options.iv import IVUnavailable, Quote, bs_price_forward
from trading_bot.options.vrp import VRPSignal, VolForecast

SPOT, TODAY = 600.0, dt.date(2026, 8, 27)


def occ(sym, expiry, right, strike):
    return f"{sym}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


class FakeHTTP:
    """Stands in for `_get`. Serves a synthetic multi-expiry chain."""

    def __init__(self, expiries, strikes, *, symbol="SPY", iv=0.20,
                 row_limit=None, no_quote_for=(), spot=SPOT,
                 put_strikes=None, leak_expiry=None):
        self.expiries = expiries
        self.strikes = strikes
        # Real chains do NOT list the same strikes for calls and puts: deep OTM
        # puts stop being quoted well before the calls do. A fake that serves
        # identical ladders makes union and intersection indistinguishable.
        self.put_strikes = put_strikes if put_strikes is not None else strikes
        # Alpaca sometimes returns a row outside the requested window. The
        # per-expiry filter is defence against that, and is untestable unless
        # the fake can actually leak one.
        self.leak_expiry = leak_expiry
        self.symbol = symbol
        self.iv = iv
        self.row_limit = row_limit          # simulate Alpaca truncating rows
        self.no_quote_for = set(no_quote_for)
        self.spot = spot
        self.calls = []

    def __call__(self, url, params=None, *, cache_key=None):
        self.calls.append((url, dict(params or {})))
        if "snapshot" in url:
            return {"latestTrade": {"p": self.spot}}
        if "options/contracts" in url:
            return {"option_contracts": self._contracts(params or {})}
        if "options/quotes" in url:
            return {"quotes": self._quotes((params or {}).get("symbols", ""))}
        raise AssertionError(f"unexpected url {url}")

    def _contracts(self, p):
        lo = dt.date.fromisoformat(p["expiration_date_gte"])
        hi = dt.date.fromisoformat(p["expiration_date_lte"])
        klo, khi = float(p["strike_price_gte"]), float(p["strike_price_lte"])
        right = "C" if p["type"] == "call" else "P"
        ladder = self.strikes if right == "C" else self.put_strikes
        rows = []
        for e in self.expiries:
            if not (lo <= e <= hi) and e != self.leak_expiry:
                continue
            for k in ladder:
                if klo <= k <= khi:
                    rows.append({"symbol": occ(self.symbol, e, right, k),
                                 "expiration_date": e.isoformat(),
                                 "strike_price": str(k)})
        rows.sort(key=lambda r: (r["expiration_date"], float(r["strike_price"])))
        n = int(p.get("limit", 100))
        if self.row_limit is not None:
            n = min(n, self.row_limit)
        return rows[:n]

    def _quotes(self, symbols):
        out = {}
        for s in symbols.split(","):
            if not s:
                continue
            if s in self.no_quote_for:
                # Alpaca returns the KEY with null bid/ask, it does not omit it.
                # A fake that omits the key entirely cannot test whether the
                # parser drops a null quote or fabricates a zero one.
                out[s] = {"bp": None, "ap": None}
                continue
            k = int(s[-8:]) / 1000.0
            e = dt.datetime.strptime(s[len(self.symbol):len(self.symbol) + 6],
                                     "%y%m%d").date()
            T = max((e - TODAY).days, 1) / 365
            px = bs_price_forward(self.spot, k, T, 0.04, self.iv, s[-9] == "C")
            if px < 0.05:
                continue
            out[s] = {"bp": round(px * 0.99, 2), "ap": round(px * 1.01, 2)}
        return out


def client(http, monkeypatch):
    c = ChainClient(key="k", secret="s", limiter=RateLimiter(per_minute=100_000))
    monkeypatch.setattr(c, "_get", http)
    monkeypatch.setattr("trading_bot.options.chain.today_et", lambda: TODAY)
    # Chain tests exercise expiry/quote mechanics, not Yahoo availability or
    # HAR-RV. Keep them deterministic and genuinely offline.
    monkeypatch.setattr("trading_bot.options.chain.forecast_vol",
                        lambda symbol: VolForecast(symbol, 0.18, 0.19))
    return c


EXPS = [dt.date(2026, 8, 28), dt.date(2026, 8, 31), dt.date(2026, 9, 3),
        dt.date(2026, 9, 25)]
STRIKES = [float(k) for k in range(540, 661, 5)]


# --- expiry pinning ------------------------------------------------------

def test_contracts_pinned_to_one_expiry_requests_only_that_expiry(monkeypatch):
    """Spanning a DTE window mixes expiries, and Alpaca truncates in ascending
    strike order — so the cut lands inside the range before any per-expiry
    filtering the caller does."""
    http = FakeHTTP(EXPS, STRIKES)
    c = client(http, monkeypatch)
    rows = c.contracts("SPY", SPOT, expiry="2026-09-03", limit=500)
    assert {r["expiration_date"] for r in rows} == {"2026-09-03"}
    _, params = http.calls[-1]
    assert params["expiration_date_gte"] == params["expiration_date_lte"] == "2026-09-03"


def test_expiries_lists_only_the_window(monkeypatch):
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    got = [e for e, _ in c.expiries("SPY", SPOT, dte_min=1, dte_max=7)]
    assert got == ["2026-08-28", "2026-08-31", "2026-09-03"]
    assert "2026-09-25" not in got


def test_expiries_are_sorted_soonest_first(monkeypatch):
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    dtes = [d for _, d in c.expiries("SPY", SPOT, dte_min=1, dte_max=40)]
    assert dtes == sorted(dtes)


def test_signal_picks_the_LATEST_expiry_in_the_window(monkeypatch):
    """The latest expiry carries the most time value while still settling
    before the caller's deadline."""
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    sig = c.signal("SPY", dte_min=1, dte_max=7)
    assert sig.expiry == "2026-09-03"


def test_signal_refuses_an_expiry_in_the_past(monkeypatch):
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    with pytest.raises(IVUnavailable, match="not in the future"):
        c.signal("SPY", expiry="2026-08-27")


def test_signal_refuses_when_the_window_holds_no_expiry(monkeypatch):
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    with pytest.raises(IVUnavailable, match="no expiry"):
        c.signal("SPY", dte_min=200, dte_max=250)


# --- the local-date bug --------------------------------------------------

def test_dte_is_measured_from_the_NEW_YORK_date(monkeypatch):
    """The machine runs seven hours ahead of New York, so from 17:00 ET its
    local date is already tomorrow. Measuring DTE against it shortened every
    evening run by a day and read implied vol 9-44% too high."""
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    sig = c.signal("SPY", expiry="2026-09-03")
    assert sig.dte == (dt.date(2026, 9, 3) - TODAY).days == 7


# --- truncation ----------------------------------------------------------

def test_structure_inputs_refuses_a_row_limited_response(monkeypatch):
    """A response that exactly fills the row limit was CUT, not completed —
    Alpaca returns ascending strikes, so the missing part is the top, and the
    short call ends up at the edge with no wing above it."""
    http = FakeHTTP(EXPS, STRIKES, row_limit=8)
    c = client(http, monkeypatch)
    sig = VRPSignal("SPY", 0.20, 0.18, 0.19, 8, "2026-09-03", 7)
    with pytest.raises(IVUnavailable, match="limit"):
        c.structure_inputs(sig, limit=8)


def test_structure_inputs_returns_the_union_of_both_rights(monkeypatch):
    """A condor needs calls above the forward and puts below it. Intersecting
    the two throws away the upper call strikes the call wing comes from."""
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    sig = VRPSignal("SPY", 0.20, 0.18, 0.19, 8, "2026-09-03", 7)
    spot, strikes, occ_map = c.structure_inputs(sig, moneyness=0.10)
    assert spot == SPOT
    assert any((k, "C") in occ_map for k in strikes)
    assert any((k, "P") in occ_map for k in strikes)
    assert strikes == sorted(set(strikes)), "strikes must be sorted and unique"


def test_structure_inputs_spans_the_requested_moneyness_band(monkeypatch):
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    sig = VRPSignal("SPY", 0.20, 0.18, 0.19, 8, "2026-09-03", 7)
    _, strikes, _ = c.structure_inputs(sig, moneyness=0.08)
    assert min(strikes) <= SPOT * 0.95
    assert max(strikes) >= SPOT * 1.05


def test_structure_inputs_pins_every_leg_to_one_expiry(monkeypatch):
    """A condor whose call wing sits in one expiry and its put wing in another
    is two unhedged verticals wearing a defined-risk label."""
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    sig = VRPSignal("SPY", 0.20, 0.18, 0.19, 8, "2026-09-03", 7)
    _, _, occ_map = c.structure_inputs(sig)
    exps = {s[3:9] for s in occ_map.values()}
    assert exps == {"260903"}, f"legs span multiple expiries: {exps}"


# --- quotes --------------------------------------------------------------

def test_quotes_are_chunked_so_the_url_stays_short(monkeypatch):
    """Symbols go in the query string; a whole chain in one request exceeds the
    URL limit and Alpaca returns a bare 400."""
    http = FakeHTTP(EXPS, STRIKES)
    c = client(http, monkeypatch)
    syms = [occ("SPY", dt.date(2026, 9, 3), "C", k) for k in STRIKES]
    c.quotes(syms, chunk=5)
    quote_calls = [p for u, p in http.calls if "quotes" in u]
    assert len(quote_calls) >= len(syms) // 5
    for _, p in [(u, p) for u, p in http.calls if "quotes" in u]:
        assert len(p["symbols"].split(",")) <= 5


def test_quotes_omit_symbols_with_no_market(monkeypatch):
    """Missing symbols are DROPPED, not defaulted. Callers must handle absence —
    a fabricated zero quote becomes a fabricated position size."""
    missing = occ("SPY", dt.date(2026, 9, 3), "C", 600.0)
    http = FakeHTTP(EXPS, STRIKES, no_quote_for=[missing])
    c = client(http, monkeypatch)
    got = c.quotes([missing, occ("SPY", dt.date(2026, 9, 3), "C", 605.0)])
    assert missing not in got
    assert len(got) == 1


def test_quotes_returns_usable_quote_objects(monkeypatch):
    c = client(FakeHTTP(EXPS, STRIKES), monkeypatch)
    got = c.quotes([occ("SPY", dt.date(2026, 9, 3), "C", 600.0)])
    q = next(iter(got.values()))
    assert isinstance(q, Quote)
    assert q.bid < q.ask


def test_an_empty_symbol_list_makes_no_request(monkeypatch):
    http = FakeHTTP(EXPS, STRIKES)
    c = client(http, monkeypatch)
    assert c.quotes([]) == {}
    assert [u for u, _ in http.calls if "quotes" in u] == []


# --- rate limiting -------------------------------------------------------

def test_rate_limiter_spaces_calls():
    import time
    rl = RateLimiter(per_minute=600)          # 0.1s apart
    t0 = time.time()
    for _ in range(3):
        rl.wait()
    assert time.time() - t0 >= 0.2


# --- gaps found by mutation testing --------------------------------------

def test_union_not_intersection_when_the_two_ladders_differ(monkeypatch):
    """Deep OTM puts stop being listed before the calls do. Intersecting throws
    away the upper call strikes the call wing has to come from."""
    calls = [float(k) for k in range(560, 681, 5)]
    puts = [float(k) for k in range(540, 621, 5)]
    http = FakeHTTP(EXPS, calls, put_strikes=puts)
    c = client(http, monkeypatch)
    sig = VRPSignal("SPY", 0.20, 0.18, 0.19, 8, "2026-09-03", 7)
    _, strikes, occ_map = c.structure_inputs(sig, moneyness=0.15)
    assert max(strikes) > max(puts), "upper call strikes were intersected away"
    assert min(strikes) < min(calls), "lower put strikes were intersected away"
    assert (max(strikes), "C") in occ_map
    assert (min(strikes), "P") in occ_map


def test_a_leaked_off_expiry_row_is_filtered_out(monkeypatch):
    """Defence in depth: even if the API returns a row outside the requested
    window, no leg may come from another expiry."""
    http = FakeHTTP(EXPS, STRIKES, leak_expiry=dt.date(2026, 9, 25))
    c = client(http, monkeypatch)
    sig = VRPSignal("SPY", 0.20, 0.18, 0.19, 8, "2026-09-03", 7)
    _, _, occ_map = c.structure_inputs(sig)
    exps = {s[3:9] for s in occ_map.values()}
    assert exps == {"260903"}, f"a leaked expiry reached the builder: {exps}"


def test_the_DEFAULT_chunk_size_keeps_the_url_short(monkeypatch):
    """The previous test passed chunk=5 explicitly, so the default — the value
    actually used in production — was untested."""
    http = FakeHTTP(EXPS, STRIKES)
    c = client(http, monkeypatch)
    syms = [occ("SPY", dt.date(2026, 9, 3), "C", k) for k in STRIKES] * 4
    c.quotes(syms)
    for u, p in http.calls:
        if "quotes" in u:
            assert len(p["symbols"]) < 2000, "URL long enough to earn a bare 400"
            assert len(p["symbols"].split(",")) <= 40


def test_a_null_quote_is_dropped_not_read_as_zero(monkeypatch):
    """Alpaca returns the key with null bid/ask. Reading that as 0.0 fabricates
    a free option, which fabricates a position size."""
    missing = occ("SPY", dt.date(2026, 9, 3), "C", 600.0)
    http = FakeHTTP(EXPS, STRIKES, no_quote_for=[missing])
    c = client(http, monkeypatch)
    got = c.quotes([missing, occ("SPY", dt.date(2026, 9, 3), "C", 605.0)])
    assert missing not in got, "a null quote was fabricated into a real one"
    assert len(got) == 1


def test_dte_is_driven_by_today_et_and_nothing_else(monkeypatch):
    """The real defect: at 02:00 Israel it is 19:00 ET the previous day, so the
    machine's date is a day ahead and every evening run computed a DTE one short
    — reading implied vol 9-44% too high.

    Checked positively, by moving `today_et` and requiring DTE to follow.
    `test_no_module_asks_the_machine_for_the_date` covers the other half by
    forbidding `date.today()` outside clock.py; patching `dt.date` itself here
    would break pandas and prove nothing.
    """
    http = FakeHTTP(EXPS, STRIKES)
    c = client(http, monkeypatch)
    assert c.signal("SPY", expiry="2026-09-03").dte == 7

    monkeypatch.setattr("trading_bot.options.chain.today_et",
                        lambda: TODAY - dt.timedelta(days=3))
    c._cache.clear()
    assert c.signal("SPY", expiry="2026-09-03").dte == 10, \
        "DTE did not follow the New York date"

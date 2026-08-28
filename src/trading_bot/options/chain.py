"""Fetch option chains and turn them into VRP signals.

Read-only. This module places no orders and holds no broker write path, so it
can run before an account exists.

Two things it refuses to do, both learned the expensive way elsewhere in this
project:

* **Never invent a number.** A contract with no quote, an unsolvable IV or a
  spread too wide to trust is dropped, not defaulted. `marketdata.py` exists
  because a NaN that survived into a comparison once planned a $30k
  liquidation.
* **Never average one strike.** ATM implied vol is taken as the median across
  several near-the-money contracts. A single strike can be stale or crossed;
  the median of five is robust to one bad quote, and disagreement across
  adjacent strikes is itself the signal that the surface is untrustworthy.
"""

from __future__ import annotations

import datetime as dt
import statistics
import threading
import time
from dataclasses import dataclass, field

import requests

from .clock import today_et
from .iv import IVUnavailable, Quote, implied_vol
from .vrp import VRPSignal, forecast_vol

TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"


class RateLimiter:
    """Token bucket for Alpaca's free tier: 200 requests/minute.

    A 40-name scan costs roughly 5 calls per symbol (spot, contracts, quote
    batches), which lands exactly on the ceiling. Hitting it returns 429s
    mid-scan, so some names silently produce no signal and the agent appears to
    have "decided" not to trade them — a data failure wearing the costume of a
    decision, which is the class of bug that has cost the most in this project.
    """

    def __init__(self, per_minute: int = 180):   # headroom under the 200 limit
        self.min_interval = 60.0 / per_minute
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


@dataclass
class ChainClient:
    key: str
    secret: str
    timeout: int = 15
    # `indicative` is the free feed. `opra` carries greeks and IV but 403s
    # without a signed OPRA agreement, which is why iv.py exists.
    feed: str = "indicative"
    limiter: RateLimiter = field(default_factory=RateLimiter)
    # Spot prices and contract lists barely move within a single scan, and
    # re-fetching them is most of the call budget.
    _cache: dict = field(default_factory=dict, repr=False)
    cache_ttl: float = 120.0

    @property
    def _h(self) -> dict:
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def _get(self, url: str, params: dict | None = None, *, cache_key: str | None = None):
        """Rate-limited GET with optional short-lived caching."""
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit and time.monotonic() - hit[0] < self.cache_ttl:
                return hit[1]
        self.limiter.wait()
        r = requests.get(url, headers=self._h, timeout=self.timeout, params=params)
        if r.status_code == 429:
            # Back off once rather than failing the symbol: a 429 is transient
            # and dropping the name would look like a trading decision.
            time.sleep(2.0)
            self.limiter.wait()
            r = requests.get(url, headers=self._h, timeout=self.timeout, params=params)
        r.raise_for_status()
        data = r.json()
        if cache_key:
            self._cache[cache_key] = (time.monotonic(), data)
        return data

    def spot(self, symbol: str) -> float:
        d = self._get(f"{DATA}/v2/stocks/{symbol}/snapshot", cache_key=f"spot:{symbol}")
        return float(d["latestTrade"]["p"])

    def contracts(self, symbol: str, spot: float, *, dte_min: int = 25,
                  dte_max: int = 45, moneyness: float = 0.08,
                  kind: str = "call", limit: int = 12,
                  expiry: str | None = None) -> list[dict]:
        # `expiry` pins the request to a single expiration. Without it the query
        # spans the whole DTE window — several expiries — and since Alpaca
        # truncates at `limit` in ascending strike order, the cut lands inside
        # the range before any per-expiry filtering the caller does. Asking for
        # one expiry is the difference between 100 strikes and 600.
        # `today_et`, never `dt.date.today()`: the machine's local date is a
        # day ahead of New York's from 17:00 ET onward, which shortened
        # every evening run's DTE by one and inflated measured implied vol
        # by 9-44% — a manufactured sell-vol signal that grew as the
        # deadline approached.
        today = today_et()
        lo = expiry or (today + dt.timedelta(days=dte_min)).isoformat()
        hi = expiry or (today + dt.timedelta(days=dte_max)).isoformat()
        # Alpaca returns strikes in ASCENDING order and truncates at `limit`, so
        # a wide window on a high-priced underlying silently loses the top of
        # the range: SPY at 764 with moneyness 0.30 asks for 535-993 but the
        # first 100 contracts only reach 783, and the call wings fall off the
        # end. Requesting a band around the money rather than a wide symmetric
        # sweep keeps both tails inside the limit.
        lo_k, hi_k = spot * (1 - moneyness), spot * (1 + moneyness)
        params = {"underlying_symbols": symbol, "status": "active", "type": kind,
                  "expiration_date_gte": lo, "expiration_date_lte": hi,
                  "strike_price_gte": str(round(lo_k)),
                  "strike_price_lte": str(round(hi_k)),
                  "limit": limit}
        d = self._get(f"{TRADING}/v2/options/contracts", params,
                      cache_key=f"contracts:{symbol}:{kind}:{moneyness}:{limit}:{lo}:{hi}")
        cs = d.get("option_contracts", [])
        # Detect the truncation rather than trusting the response: if we hit the
        # limit AND the top strike is below what we asked for, the far tail is
        # missing and any wing selection there would be wrong.
        if len(cs) >= limit and cs:
            top = max(float(c["strike_price"]) for c in cs)
            if top < hi_k * 0.995:
                self._truncated = getattr(self, "_truncated", set()) | {symbol}
        return cs

    def quotes(self, symbols: list[str], chunk: int = 40) -> dict[str, Quote]:
        """Latest quotes, batched.

        The symbols go in the query string, so a whole chain in one request
        exceeds the URL limit and Alpaca returns a bare 400. A four-leg condor
        needs both calls and puts across a wide strike range — easily 120
        symbols — so this chunks rather than assuming one request will do.
        Building a spread from a partially-failed fetch would silently price
        legs off missing data.
        """
        out: dict[str, Quote] = {}
        for i in range(0, len(symbols), chunk):
            batch = symbols[i:i + chunk]
            if not batch:
                continue
            d = self._get(f"{DATA}/v1beta1/options/quotes/latest",
                          {"symbols": ",".join(batch), "feed": self.feed})
            for sym, q in (d.get("quotes") or {}).items():
                bid, ask = q.get("bp"), q.get("ap")
                if bid is None or ask is None:
                    continue
                out[sym] = Quote(float(bid), float(ask))
        return out

    def expiries(self, symbol: str, spot: float, *, dte_min: int = 1,
                 dte_max: int = 45) -> list[tuple[str, int]]:
        """Available expiries in the window, as (date, dte), soonest first."""
        cs = self.contracts(symbol, spot, dte_min=dte_min, dte_max=dte_max,
                            moneyness=0.02, limit=500)
        today = today_et()
        seen = {c["expiration_date"] for c in cs}
        return sorted(((e, (dt.date.fromisoformat(e) - today).days) for e in seen),
                      key=lambda x: x[1])

    def signal(self, symbol: str, *, max_spread_pct: float = 0.20,
               min_strikes: int = 3, dte_min: int = 25, dte_max: int = 45,
               expiry: str | None = None, band: float = 0.05) -> VRPSignal:
        """ATM implied vol for `symbol`, paired with the HAR-RV forecast.

        The DTE window is a parameter because the horizon is a constraint, not a
        preference: an agent graded on a fixed date cannot measure volatility on
        options expiring a month later, because those contracts are priced for a
        different question. When several expiries fall inside the window the
        LATEST is chosen — it carries the most time value while still settling
        before we are graded.

        Measured across a narrow band around spot, and pinned to one expiry.
        Both matter, and both were bugs. Spanning expiries mixes two different
        volatilities into one median. And a wide band with a row limit returns
        the *lowest* strikes, because Alpaca sorts ascending — asked for a
        0-9 DTE signal on SPY it returned twelve deep-in-the-money calls
        expiring the same afternoon, every one of them unusable.
        """
        spot = self.spot(symbol)
        if expiry is None:
            avail = self.expiries(symbol, spot, dte_min=dte_min, dte_max=dte_max)
            if not avail:
                raise IVUnavailable(
                    f"{symbol}: no expiry between {dte_min} and {dte_max} days out")
            expiry, _ = avail[-1]
        days = (dt.date.fromisoformat(expiry) - today_et()).days
        if days <= 0:
            raise IVUnavailable(f"{symbol}: expiry {expiry} is not in the future")

        cs = self.contracts(symbol, spot, moneyness=band, limit=500, expiry=expiry)
        if not cs:
            raise IVUnavailable(f"{symbol}: no contracts within {band:.0%} at {expiry}")

        qs = self.quotes([c["symbol"] for c in cs])
        ivs, used = [], 0
        for c in cs:
            q = qs.get(c["symbol"])
            if q is None or not q.usable(max_spread_pct):
                continue
            K = float(c["strike_price"])
            try:
                ivs.append(implied_vol(q.mid, spot, K, days / 365, call=True))
                used += 1
            except IVUnavailable:
                continue

        if used < min_strikes:
            raise IVUnavailable(
                f"{symbol}: only {used} usable strikes at {expiry} (need "
                f"{min_strikes}) — illiquid or crossed quotes")
        dte = days

        fc = forecast_vol(symbol)
        return VRPSignal(symbol=symbol, implied=float(statistics.median(ivs)),
                         forecast=fc.forecast, realized_20d=fc.realized_20d,
                         n_strikes=used, expiry=expiry or "", dte=dte)


    def structure_inputs(self, sig: VRPSignal, *, moneyness: float = 0.15,
                         limit: int = 500) -> tuple[float, list[float], dict]:
        """Everything a spread builder needs, pinned to the signal's expiry.

        Calls and puts come from separate requests, and the DTE window normally
        spans several expiries. A condor whose call wing sits in one expiry and
        its put wing in another is not a condor — it is two unhedged verticals
        wearing a defined-risk label. Both sides are filtered to `sig.expiry`,
        the expiry the IV was measured on.

        `strikes` is the UNION of both sides, not the intersection. A condor
        needs calls above spot and puts below it, and deep OTM puts stop being
        listed well before the calls do — intersecting throws away the upper
        call strikes that the call wing has to come from.

        Returns (spot, strikes, occ) with `occ` mapping (strike, "C"|"P") to the
        OCC symbol. A missing (strike, right) is simply absent, and the builders
        already refuse when a leg they need is not there.
        """
        spot = self.spot(sig.symbol)
        by_right: dict[str, dict[float, str]] = {}
        for kind, right in (("call", "C"), ("put", "P")):
            cs = self.contracts(sig.symbol, spot, moneyness=moneyness,
                                kind=kind, limit=limit, expiry=sig.expiry)
            by_right[right] = {float(c["strike_price"]): c["symbol"]
                               for c in cs if c["expiration_date"] == sig.expiry}
        for right, ks in by_right.items():
            # A response that exactly fills `limit` was cut, not completed —
            # Alpaca returns ascending strikes, so the missing part is the top.
            if len(ks) >= limit:
                raise IVUnavailable(
                    f"{sig.symbol}: {right} chain hit the {limit}-contract limit "
                    f"at {sig.expiry}; the upper strikes are missing")
        if not by_right["C"] or not by_right["P"]:
            raise IVUnavailable(
                f"{sig.symbol}: no {'calls' if not by_right['C'] else 'puts'} "
                f"listed at {sig.expiry}")

        # `moneyness` here is deliberately wider than the 0.08 the IV scan uses.
        # That band is chosen to average implied vol near the money; this one has
        # to reach past the ~0.16-delta short strikes AND leave strikes beyond
        # them to buy as wings. At 0.08 on QQQ the band ended at 769, the short
        # call landed on 765, and the 770 wing that exists in the chain fell
        # outside the request — a structure refused for want of one strike.
        occ = {(k, r): sym for r, ks in by_right.items() for k, sym in ks.items()}
        strikes = sorted(set(by_right["C"]) | set(by_right["P"]))
        return spot, strikes, occ


def scan(client: ChainClient, universe: list[str]) -> tuple[list[VRPSignal], dict[str, str]]:
    """Signals for everything that produced one, plus why the rest were skipped.

    The skip reasons are not debug noise — an agent that explains why it is NOT
    trading two thirds of its universe is showing judgement, and that record is
    what makes the decision auditable after the fact.
    """
    sigs, skipped = [], {}
    for sym in universe:
        try:
            sigs.append(client.signal(sym))
        except Exception as exc:  # noqa: BLE001
            skipped[sym] = f"{type(exc).__name__}: {exc}"
    sigs.sort(key=lambda s: -abs(s.vrp_ratio - 1.0))
    return sigs, skipped

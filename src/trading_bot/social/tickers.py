"""Extract stock tickers from free text.

This is where naive implementations quietly break. "A", "IT", "ON", "SO", "BE"
and "DD" are all real listed tickers *and* ordinary English words, so a regex
for 1-5 capitals turns every post into a false signal — and the noise is not
random, it correlates with post length and writing style, which is exactly the
kind of structured garbage a model will happily fit.

Two mention classes are kept separate rather than merged, because they have very
different precision:

* **cashtag** — ``$AAPL``. Unambiguous, and how retail actually writes.
* **bare** — ``AAPL`` with no marker. Higher recall, lower precision; only
  accepted if the token is a known ticker AND not in the stop-list below.

Downstream code can use cashtags alone when precision matters, or both when
recall does. Merging them here would throw away that choice permanently, and
this data is being collected precisely because it cannot be re-collected later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Tokens that are valid tickers but overwhelmingly used as English or as
# finance jargon. Excluded from BARE matching only — "$IT" still counts.
STOPWORDS: frozenset[str] = frozenset("""
A I IT ON OR BE SO IS AT IF IN OF TO AS AN BY MY WE HE DO GO NO UP US ANY ALL
ARE FOR NOW OUT ONE OWN SEE TWO WHO WHY YOU CAN HAS HAD HOW ITS NEW OLD PER
THE AND BUT NOT WAS WERE WITH FROM THIS THAT THEY HAVE WILL YOUR WHAT WHEN
DD CEO CFO CTO IPO ETF EPS ROI PE PS TA FA YOLO FD FDS ATH ATL EOD EOW EOY
IMO IMHO TLDR EDIT PSA LOL WTF OMG RIP GG EV AI ML API APP CIA FBI IRS SEC
FTC DOJ GDP CPI PPI PMI FOMC FED ECB BOJ NYSE AMEX OTC OTM ITM DTE IV RSI
MACD SMA EMA VWAP HODL FOMO FUD DCA COVID USA USD EUR GBP JPY CNY UK EU US
CA NY LA SF TX FL AM PM EST PST UTC MON TUE WED THU FRI SAT SUN JAN FEB MAR
APR JUN JUL AUG SEP OCT NOV DEC Q1 Q2 Q3 Q4 YTD YOY QOQ MOM WSB OP NSFW TIL
ELI5 AMA IRL AFAIK IIRC TBH NGL FYI ASAP ETA FAQ CEO'S BIG TOP LOW HIGH NEXT
LAST MOST BEST WORST GOOD BAD BUY SELL HOLD LONG SHORT CALL PUT BULL BEAR
""".split())

CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
BARE_RE = re.compile(r"\b([A-Z]{1,5})\b")


@dataclass(frozen=True)
class Mentions:
    cashtags: dict[str, int]
    bare: dict[str, int]

    @property
    def all_symbols(self) -> set[str]:
        return set(self.cashtags) | set(self.bare)


def extract(text: str, known: frozenset[str] | None = None) -> Mentions:
    """Pull ticker mentions out of ``text``.

    ``known`` restricts BARE matches to real tickers. Without it, bare matching
    is skipped entirely rather than guessed — a wrong ticker is worse than a
    missing one, because it silently attributes attention to the wrong company.
    """
    if not text:
        return Mentions({}, {})

    cash: dict[str, int] = {}
    for m in CASHTAG_RE.finditer(text):
        sym = m.group(1).upper()
        cash[sym] = cash.get(sym, 0) + 1

    bare: dict[str, int] = {}
    if known:
        for m in BARE_RE.finditer(text):
            sym = m.group(1)
            # Single letters are never accepted bare. F (Ford), T (AT&T),
            # X (US Steel), K (Kellanova) and A (Agilent) are all real tickers
            # and all appear constantly as ordinary text — no stop-list can
            # separate them. "$F" still counts; a lone "F" never will.
            if len(sym) < 2 or sym in STOPWORDS or sym not in known:
                continue
            bare[sym] = bare.get(sym, 0) + 1

    return Mentions(cash, bare)

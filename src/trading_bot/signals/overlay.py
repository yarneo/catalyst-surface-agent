"""Fuse the quant signal with sentiment + earnings into an adjusted target.

The overlay only ever applies a multiplier in [floor, 1.0] to the quant target
— it can dampen or zero exposure, never increase it. Two effects:

* **Earnings blackout** — within N days of earnings, force the multiplier to 0
  (don't carry/initiate risk into a binary event).
* **Sentiment dampening** — negative news scales long exposure down toward the
  floor, weighted by the model's own confidence. Non-negative sentiment leaves
  the quant signal untouched (no LLM-driven leverage).
"""

from __future__ import annotations

from dataclasses import dataclass

from .earnings import days_until_earnings
from .sentiment import get_daily_sentiment


@dataclass
class OverlayConfig:
    sentiment_floor: float = 0.0      # most a position can be dampened to
    earnings_blackout_days: int = 3   # no new risk within N days of earnings
    enabled: bool = True


@dataclass
class OverlayResult:
    adjusted_weight: float
    multiplier: float
    reasons: list[str]


class SignalOverlay:
    def __init__(self, config: OverlayConfig | None = None):
        self.config = config or OverlayConfig()

    def _sentiment_multiplier(self, score: float, confidence: float) -> float:
        # Only negative sentiment dampens; weighted by confidence.
        if score >= 0:
            return 1.0
        floor = self.config.sentiment_floor
        # score -1 & confidence 1 -> floor; score 0 -> 1.0
        return 1.0 - (1.0 - floor) * (-score) * confidence

    def apply(self, symbol: str, base_weight: float) -> OverlayResult:
        if not self.config.enabled or base_weight == 0.0:
            return OverlayResult(base_weight, 1.0, [])

        reasons: list[str] = []

        dte = days_until_earnings(symbol)
        if dte is not None and 0 <= dte <= self.config.earnings_blackout_days:
            reasons.append(f"earnings in {dte}d -> blackout (no new risk)")
            return OverlayResult(0.0, 0.0, reasons)

        sent = get_daily_sentiment(symbol)
        mult = self._sentiment_multiplier(sent["score"], sent["confidence"])
        reasons.append(
            f"sentiment {sent['score']:+.2f} (conf {sent['confidence']:.2f}, "
            f"{sent['source']}, {sent['n_headlines']} headlines) -> x{mult:.2f}"
        )
        return OverlayResult(base_weight * mult, mult, reasons)

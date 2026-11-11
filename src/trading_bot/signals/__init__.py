"""Signal enrichment: alternative-data overlays on top of the quant signal.

Design stance: these overlays can only *reduce* risk, never amplify it. News
sentiment and earnings proximity dampen or veto exposure the quant strategy
already wants — they don't create new aggressive bets. That keeps an LLM (which
can be wrong or hallucinate) from levering us up, while still letting it steer us
away from bad trades. Boosting can be enabled later, once it's validated to help.
"""

from .crypto_sentiment import get_crypto_sentiment, sentiment_multiplier
from .earnings import days_until_earnings, in_earnings_blackout, next_earnings_date
from .overlay import OverlayConfig, SignalOverlay
from .sentiment import get_daily_sentiment

__all__ = [
    "get_crypto_sentiment",
    "sentiment_multiplier",
    "get_daily_sentiment",
    "next_earnings_date",
    "days_until_earnings",
    "in_earnings_blackout",
    "SignalOverlay",
    "OverlayConfig",
]

from .base import Strategy
from .breakout import DonchianBreakout, KeltnerBreakout
from .ensemble import EnsembleTrend
from .mean_reversion import (
    BollingerReversion,
    BollingerRSICombo,
    CCIReversion,
    MeanReversion,
    RSIReversion,
)
from .momentum import MACD, SMACrossover, TimeSeriesMomentum

# Registry so configs/scripts can select a strategy by name.
REGISTRY: dict[str, type[Strategy]] = {
    # trend / momentum
    "sma_crossover": SMACrossover,
    "ts_momentum": TimeSeriesMomentum,
    "macd": MACD,
    # breakout
    "donchian_breakout": DonchianBreakout,
    "keltner_breakout": KeltnerBreakout,
    # mean reversion
    "mean_reversion": MeanReversion,
    "rsi_reversion": RSIReversion,
    "bollinger_reversion": BollingerReversion,
    "cci_reversion": CCIReversion,
    "bb_rsi_combo": BollingerRSICombo,
    # ensemble (deployed for crypto)
    "ensemble_trend": EnsembleTrend,
}

__all__ = [
    "Strategy",
    "SMACrossover",
    "TimeSeriesMomentum",
    "MACD",
    "DonchianBreakout",
    "KeltnerBreakout",
    "MeanReversion",
    "RSIReversion",
    "BollingerReversion",
    "CCIReversion",
    "BollingerRSICombo",
    "EnsembleTrend",
    "REGISTRY",
]

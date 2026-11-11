"""Load shared settings from config/config.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


@lru_cache(maxsize=1)
def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def load_universe() -> list[str]:
    return list(load_config().get("universe", ["SPY", "QQQ"]))


def load_crypto_universe() -> list[str]:
    """Crypto data symbols (yfinance format, e.g. 'BTC-USD')."""
    return list(load_config().get("crypto_universe", ["BTC-USD", "ETH-USD"]))


def load_pead_universe() -> list[str]:
    """US stock tickers for the PEAD (earnings-drift) sleeve."""
    return list(load_config().get("pead_universe", ["AAPL", "MSFT"]))


def to_alpaca_symbol(data_symbol: str) -> str:
    """Map a yfinance crypto symbol to the Alpaca trade symbol: BTC-USD -> BTC/USD.

    Used for SUBMITTING crypto orders. For looking up positions, use
    to_alpaca_position_symbol() instead — Alpaca's positions API is inconsistent
    and rejects the slashed form."""
    return data_symbol.replace("-", "/")


def to_alpaca_position_symbol(data_symbol: str) -> str:
    """Map a yfinance crypto symbol to Alpaca's POSITIONS-API form: BTC-USD -> BTCUSD.

    Alpaca uses different symbol formats across endpoints:
      - submit_order            : 'BTC/USD'
      - get_open_position       : 'BTCUSD'
      - close_position          : 'BTCUSD'
    Using 'BTC/USD' for positions returns 'Not Found' even when the position
    exists. This helper strips the dash so close_position works."""
    return data_symbol.replace("-", "")

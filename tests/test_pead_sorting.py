import pandas as pd

from trading_bot.backtest import pead as module


def test_pead_normalizes_unsorted_duplicate_vendor_rows(monkeypatch):
    idx = pd.to_datetime(["2026-01-06", "2026-01-02", "2026-01-05",
                          "2026-01-05", "2026-01-07", "2026-01-08"])
    frame = pd.DataFrame({
        "open": [103, 100, 101, 102, 104, 105],
        "close": [104, 101, 102, 103, 105, 106],
    }, index=idx)
    events = pd.DataFrame([{"date": pd.Timestamp("2026-01-02"),
                                "amc": False, "surprise": 5.0}])
    monkeypatch.setattr(module, "load_ohlcv", lambda *a, **k: frame)
    monkeypatch.setattr(module, "earnings_events", lambda *a, **k: events)

    result = module.pead_backtest(["TEST"], "2026-01-01", "2026-02-01",
                                  hold_days=1)
    assert result["equity"].index.is_monotonic_increasing
    assert result["equity"].index.is_unique

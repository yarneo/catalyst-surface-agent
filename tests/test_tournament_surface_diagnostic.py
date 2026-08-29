import datetime as dt
import math

import pytest

from trading_bot.options.clock import ET
from trading_bot.tournament.surface_diagnostic import diagnose_surface


NOW = dt.datetime(2026, 9, 2, 15, 30, tzinfo=ET)


def chain(*, curvature=0.003):
    rows = {}
    spot = 100.0
    for strike in (92.5, 95.0, 97.5, 100.0, 102.5, 105.0, 107.5):
        x = 100.0 * math.log(strike / spot)
        center = 0.70 + curvature * x * x - 0.002 * x
        for right, offset in (("C", 0.01), ("P", -0.01)):
            symbol = f"AVGO260904{right}{int(strike * 1000):08d}"
            rows[symbol] = {
                "impliedVolatility": center + offset,
                "latestQuote": {
                    "bp": 2.0, "ap": 2.1, "bs": 10, "as": 10,
                    "t": (NOW - dt.timedelta(seconds=3)).isoformat(),
                },
            }
    return {"snapshots": rows}


def test_diagnostic_recovers_smile_curvature_without_changing_policy():
    result = diagnose_surface(payload=chain(), spot=100.0, observed_at=NOW)
    assert result.point_count == 7
    assert result.shape == "convex smile"
    assert result.quadratic_curvature_per_log_moneyness_pct2 == pytest.approx(0.003)
    assert result.atm_skew_per_log_moneyness_pct == pytest.approx(-0.002)
    assert result.fit_rmse < 1e-10
    assert result.diagnostic_only is True
    assert result.policy_gate_changed is False
    assert result.executable_premium_to_spot == pytest.approx(0.0422)


def test_diagnostic_requires_auditable_paired_strikes():
    payload = chain()
    snapshots = payload["snapshots"]
    payload["snapshots"] = dict(list(snapshots.items())[:8])
    with pytest.raises(ValueError, match="needs 5 paired strikes"):
        diagnose_surface(payload=payload, spot=100.0, observed_at=NOW)

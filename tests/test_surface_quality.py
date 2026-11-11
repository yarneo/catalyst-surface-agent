from trading_bot.options.surface_quality import (
    static_arbitrage_report,
    tp2_rectangle_report,
)


def _row(bid, ask):
    return {"latestQuote": {"bp": bid, "ap": ask}}


def test_static_surface_reports_crossable_call_monotonicity_violation():
    payload = {"snapshots": {
        "XYZ260904C00100000": _row(5.0, 5.1),
        "XYZ260904C00105000": _row(6.0, 6.1),
        "XYZ260904C00110000": _row(3.0, 3.1),
    }}
    result = static_arbitrage_report(
        payload, underlying="XYZ", expiry="2026-09-04")
    assert result["status"] == "WARN"
    assert result["crossable_violations"] >= 1
    assert any(row["type"] == "monotonicity" for row in result["violations"])


def test_static_surface_ignores_non_crossable_mid_noise():
    payload = {"snapshots": {
        "XYZ260904C00100000": _row(9.9, 10.1),
        "XYZ260904C00105000": _row(6.9, 7.1),
        "XYZ260904C00110000": _row(4.9, 5.1),
        "XYZ260904P00100000": _row(4.9, 5.1),
        "XYZ260904P00105000": _row(6.9, 7.1),
        "XYZ260904P00110000": _row(9.9, 10.1),
    }}
    result = static_arbitrage_report(
        payload, underlying="XYZ", expiry="2026-09-04")
    assert result["status"] == "CLEAN"
    assert result["crossable_violations"] == 0


def test_tp2_rectangle_uses_asks_against_bids():
    points = [
        {"tenor": .1, "moneyness": .9, "bid": .9, "ask": 1.0},
        {"tenor": .1, "moneyness": 1.1, "bid": 2.0, "ask": 2.1},
        {"tenor": .2, "moneyness": .9, "bid": 2.0, "ask": 2.1},
        {"tenor": .2, "moneyness": 1.1, "bid": .9, "ask": 1.0},
    ]
    result = tp2_rectangle_report(points)
    assert result["rectangles_tested"] == 1
    assert result["status"] == "WARN"
    assert result["crossable_violations"] == 1

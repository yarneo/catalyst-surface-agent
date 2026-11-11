import pytest

from trading_bot.live.managed_futures_executor import idle_router_targets


@pytest.mark.parametrize(
    ("rule", "bull", "qld", "targets", "wrong"),
    [
        ("mf_always", True, 0, {"DBMF": 500, "KMLM": 500}, ["QQQ"]),
        ("regime_qqq", True, 0, {"QQQ": 1000}, ["DBMF", "KMLM"]),
        ("split", True, 0, {"QQQ": 500, "DBMF": 250, "KMLM": 250}, []),
        ("split", False, 0, {"DBMF": 500, "KMLM": 500}, ["QQQ"]),
        ("regime_qqq", True, 20_000, {"DBMF": 500, "KMLM": 500}, ["QQQ"]),
        ("split", True, 20_000, {"DBMF": 500, "KMLM": 500}, ["QQQ"]),
    ],
)
def test_idle_router_policy_targets(rule, bull, qld, targets, wrong):
    actual, actual_wrong, _ = idle_router_targets(
        1000, bull=bull, regime_nasdaq=qld, idle_rule=rule, ma_window=150)
    assert actual == targets
    assert actual_wrong == wrong


def test_idle_router_rejects_unknown_policy():
    with pytest.raises(ValueError, match="unsupported"):
        idle_router_targets(
            1000, bull=True, regime_nasdaq=0, idle_rule="lever_everything")

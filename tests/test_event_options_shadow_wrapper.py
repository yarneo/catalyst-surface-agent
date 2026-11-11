import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "run_event_options_shadow_under_test",
        ROOT / "scripts" / "run_event_options_shadow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wrapper_constructs_only_zero_authority_shadow_command(monkeypatch):
    module = load_wrapper()
    seen = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        module.subprocess, "run",
        lambda command, **kwargs: seen.append((command, kwargs)) or Result())
    monkeypatch.setattr(sys, "argv", ["run_event_options_shadow"])

    assert module.main() == 0
    command, kwargs = seen[0]
    assert "--production-shadow" in command
    assert "--enable-orders" not in command
    assert "event_options_shadow" in " ".join(command)
    assert kwargs["check"] is False

"""The credential scanner is only useful if it still bites.

A scanner that has quietly stopped matching is worse than no scanner, because
it reports CLEAN and everyone believes it. These tests pin the two properties
that matter: real secrets are caught, and placeholders are not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_no_secrets", ROOT / "scripts" / "check_no_secrets.py")
check_no_secrets = importlib.util.module_from_spec(_spec)
sys.modules["check_no_secrets"] = check_no_secrets
_spec.loader.exec_module(check_no_secrets)


def test_selftest_passes():
    assert check_no_secrets.selftest() == 0


@pytest.mark.parametrize("line", [
    "ALPACA_API_KEY=PKA1B2C3D4E5F6G7H8I9",
    "ALPACA_SECRET_KEY=aB3dEf6hIj9lMnO2qRsT5vWx8zA1cD4fG7hJ",
    'FEATHERLESS_API_KEY = "rc_9aB3dEf6hIj9lMnO2qRsT"',
    "client = Anthropic(api_key='sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx')",
    "-----BEGIN RSA PRIVATE KEY-----",
])
def test_real_secrets_are_caught(line):
    assert check_no_secrets.scan_text(line, "f") != []


@pytest.mark.parametrize("line", [
    "ALPACA_API_KEY=replace_me",
    "ALPACA_SECRET_KEY=your_secret_here",
    "FEATHERLESS_API_KEY=<paste-yours>",
    "ALPACA_API_KEY=${ALPACA_API_KEY}",
    "# set ALPACA_SECRET_KEY before running",
    "ALPACA_INITIAL_EQUITY=100000",
])
def test_placeholders_and_prose_are_not_flagged(line):
    assert check_no_secrets.scan_text(line, "f") == []


def test_shipped_env_example_is_clean():
    """The one committed env file must stay placeholder-only."""
    example = (ROOT / ".env.example").read_text()
    assert check_no_secrets.scan_text(example, ".env.example") == []


def test_findings_never_include_the_secret_itself():
    """Output of a failing run must be safe to paste anywhere."""
    secret = "PKA1B2C3D4E5F6G7H8I9"
    findings = check_no_secrets.scan_text(f"ALPACA_API_KEY={secret}", "f")
    assert findings, "sanity: this line should be flagged"
    assert all(secret not in finding for finding in findings)

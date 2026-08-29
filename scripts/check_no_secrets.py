"""Fail loudly if anything publishable looks like a credential.

Run this before making a repository public, before publishing an evidence
export, and before recording a demo. It scans three places, because a clean
working tree says nothing about what is already in the history:

    1. every file in the working tree that Git is not ignoring
    2. every blob ever committed, on every branch
    3. any extra paths named on the command line

Matches are reported as ``path:line rule`` only. The matched text is never
printed, so the output of a failing run is itself safe to paste into an issue,
a chat, or a screen recording.

    python scripts/check_no_secrets.py            # tree + history
    python scripts/check_no_secrets.py --selftest # prove the rules still bite
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# A credential is a *value*, so most rules require the value's shape as well as
# its name. Naming a variable is not leaking it; assigning a real one is.
ASSIGNED = r"""\s*[=:]\s*["']?"""

RULES: dict[str, re.Pattern[str]] = {
    "alpaca-key-id": re.compile(r"\b[PA]K[A-Z0-9]{16,20}\b"),
    "alpaca-assignment": re.compile(
        rf"(?:ALPACA|APCA)[A-Z_]*(?:KEY|SECRET)[A-Z_]*{ASSIGNED}[A-Za-z0-9/+_-]{{16,}}",
        re.IGNORECASE),
    "featherless-assignment": re.compile(
        rf"FEATHERLESS[A-Z_]*KEY{ASSIGNED}[A-Za-z0-9_-]{{16,}}", re.IGNORECASE),
    "openai-or-anthropic": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer-token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{24,}"),
}

# Values that exist to be replaced. A placeholder in .env.example is the point
# of .env.example, and flagging it trains people to ignore this script.
PLACEHOLDER = re.compile(
    r"replace[_-]?me|your[_-]|<[^>]*>|\$\{|xxx+|example|changeme|dummy|fake|"
    r"placeholder|redacted|\bnot[_-]?a[_-]?real\b",
    re.IGNORECASE)

SKIP_SUFFIXES = {".lock", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico",
                 ".woff", ".woff2", ".mp4", ".mov", ".zip", ".parquet"}
SKIP_NAMES = {"check_no_secrets.py", "test_check_no_secrets.py"}


def scan_text(text: str, origin: str) -> list[str]:
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if PLACEHOLDER.search(line):
            continue
        for rule, pattern in RULES.items():
            if pattern.search(line):
                findings.append(f"{origin}:{lineno} {rule}")
    return findings


def _readable(path: Path) -> str | None:
    if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _git(*args: str) -> str:
    return subprocess.run(("git", *args), capture_output=True, text=True,
                          check=False).stdout


def scan_worktree(root: Path) -> list[str]:
    listed = _git("-C", str(root), "ls-files", "--cached", "--others",
                  "--exclude-standard")
    findings: list[str] = []
    for name in filter(None, listed.splitlines()):
        text = _readable(root / name)
        if text is not None:
            findings += scan_text(text, name)
    return findings


def scan_history(root: Path) -> list[str]:
    listing = _git("-C", str(root), "rev-list", "--objects", "--all")
    findings: list[str] = []
    seen: set[str] = set()
    for entry in filter(None, listing.splitlines()):
        sha, _, path = entry.partition(" ")
        if not path or sha in seen:
            continue
        seen.add(sha)
        if Path(path).suffix.lower() in SKIP_SUFFIXES or Path(path).name in SKIP_NAMES:
            continue
        blob = subprocess.run(("git", "-C", str(root), "cat-file", "blob", sha),
                              capture_output=True, check=False)
        try:
            text = blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings += scan_text(text, f"history:{path}")
    return findings


def selftest() -> int:
    """Prove each rule still catches a synthetic secret and spares a placeholder.

    Without this, a regex that silently stops matching turns this script into a
    green light that checks nothing.
    """
    samples = {
        "alpaca-key-id": "PKA1B2C3D4E5F6G7H8I9",
        "alpaca-assignment": "ALPACA_SECRET_KEY=aB3dEf6hIj9lMnO2qRsT5vWx8zA1cD4fG7hJ",
        "featherless-assignment": "FEATHERLESS_API_KEY=rc_9aB3dEf6hIj9lMnO2qRsT",
        "openai-or-anthropic": "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "github-token": "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        "aws-access-key": "AKIA3FKLMNOPQRSTUVWX",
        "private-key-block": "-----BEGIN RSA PRIVATE KEY-----",
        "bearer-token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
    }
    failures = 0
    for rule, sample in samples.items():
        if not RULES[rule].search(sample):
            print(f"  FAIL  {rule} no longer matches its own sample")
            failures += 1
    missing = set(RULES) - set(samples)
    for rule in sorted(missing):
        print(f"  FAIL  {rule} has no self-test sample")
        failures += 1
    for placeholder in ("ALPACA_API_KEY=replace_me",
                        "ALPACA_SECRET_KEY=your_secret_here",
                        "FEATHERLESS_API_KEY=<paste-yours>"):
        if not PLACEHOLDER.search(placeholder):
            print(f"  FAIL  placeholder would be reported: {placeholder}")
            failures += 1
    if failures:
        print(f"selftest: {failures} problem(s)")
        return 1
    print(f"selftest: {len(RULES)} rules and 3 placeholders behave correctly")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path,
                        help="extra files to scan, e.g. an evidence export")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--no-history", action="store_true",
                        help="skip the history scan (much faster)")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    findings = scan_worktree(args.root)
    scanned = "working tree"
    if not args.no_history:
        findings += scan_history(args.root)
        scanned += " + full git history"
    for extra in args.paths:
        text = _readable(extra)
        if text is None:
            print(f"cannot read {extra}", file=sys.stderr)
            return 2
        findings += scan_text(text, str(extra))
        scanned += f" + {extra}"

    if findings:
        print(f"REFUSED — {len(findings)} possible credential(s) in {scanned}:")
        for finding in sorted(set(findings)):
            print(f"  {finding}")
        print("\nMatched text is deliberately not shown. Open each location.")
        return 1

    print(f"CLEAN — no credential-shaped values in {scanned}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

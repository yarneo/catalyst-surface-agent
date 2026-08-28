"""Plain-English narration of decisions already made. Nothing more.

The boundary matters and is worth stating twice. Everything in this module runs
AFTER the cycle's decisions are executed, reads only the structured facts the
agent logged, and writes only prose for the dashboard. No output of the model
feeds back into scanning, sizing, or execution — the trading path is
deterministic arithmetic and stays that way. If the inference API is down, the
journal shows the raw facts instead; nothing else notices.

Why an LLM here at all, given the strategy's whole differentiation is NOT using
one to trade: a judge clicking the demo gets each cycle explained in one
paragraph — why NFLX was excluded, what the AMD strikes mean, what the worst
case is — without reading a log. Narration is the one place a language model is
the right tool in this system, and the one place it can do no damage.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import requests

from .clock import now_et

MODEL = "Qwen/Qwen2.5-72B-Instruct"          # ungated on Featherless
URL = "https://api.featherless.ai/v1/chat/completions"

SYSTEM = (
    "You narrate decisions a rules-based options trading agent has ALREADY "
    "made. Write 3-5 plain sentences for a dashboard. Facts only from the JSON "
    "given: what was scanned, what was skipped and why, what was opened or "
    "closed, the credit collected and the exact worst case. Never advise, "
    "never predict, never editorialise. Dollar figures verbatim from the data."
)


@dataclass(frozen=True)
class Entry:
    when: str
    text: str
    generated: bool          # False = fallback rendering, no model involved


def narrate(cycle_facts: dict, *, api_key: str | None = None,
            timeout_s: float = 30.0) -> Entry:
    """One journal paragraph from one cycle's structured facts.

    Degrades to a mechanical rendering on ANY failure. The dashboard must never
    be down because a narration sponsor is.
    """
    key = api_key or os.getenv("FEATHERLESS_API_KEY", "")
    stamp = now_et().isoformat(timespec="seconds")
    if key:
        try:
            r = requests.post(
                URL, headers={"Authorization": f"Bearer {key}"},
                json={"model": MODEL,
                      "messages": [
                          {"role": "system", "content": SYSTEM},
                          {"role": "user",
                           "content": json.dumps(cycle_facts, default=str)}],
                      "max_tokens": 250, "temperature": 0.3},
                timeout=timeout_s)
            if r.ok:
                text = r.json()["choices"][0]["message"]["content"].strip()
                if text:
                    return Entry(stamp, text, True)
        except Exception:  # noqa: BLE001 — narration must never break anything
            pass
    return Entry(stamp, _fallback(cycle_facts), False)


def _fallback(f: dict) -> str:
    bits = []
    sk = f.get("skipped") or []
    if sk:
        named = "; ".join(f"{x['symbol']} ({x['reason']})" for x in sk[:3])
        more = f" and {len(sk) - 3} more" if len(sk) > 3 else ""
        bits.append(f"Skipped {len(sk)}: {named}{more}.")
    for o in f.get("opened", []):
        bits.append(f"Opened {o['symbol']} x{o['qty']} for ${o['credit']:,.0f} "
                    f"credit, worst case ${o['risk']:,.0f}.")
    for c in f.get("closed", []):
        bits.append(f"Closed {c['symbol']} x{c['qty']} ({c['reason']}).")
    if not bits:
        bits.append("No action this cycle: " + f.get("summary", "holding."))
    return " ".join(bits)


def append(path: str | Path, entry: Entry) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps({"when": entry.when, "text": entry.text,
                             "generated": entry.generated}) + "\n")


def read_all(path: str | Path, limit: int = 50) -> list[Entry]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().strip().split("\n"):
        if not line:
            continue
        try:
            d = json.loads(line)
            out.append(Entry(d["when"], d["text"], d.get("generated", False)))
        except Exception:  # noqa: BLE001 — a corrupt line loses one entry, not the page
            continue
    return out[-limit:]

"""The champion store: the strategy config currently deployed for each symbol.

This is the single source of truth the live executor reads — so "self-improving"
means the retraining loop *edits this file* when (and only when) it has proof.
Persisted as JSON so it survives restarts and is easy to inspect by eye.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CHAMPION_PATH = Path(__file__).resolve().parents[3] / "data" / "champion.json"


@dataclass
class Champion:
    strategy: str                 # registry key, e.g. "sma_crossover"
    params: dict                  # the deployed parameters
    judge_sharpe: float           # OOS Sharpe on the judge window when promoted
    promoted_at: str              # ISO timestamp
    promotions: int = 1           # how many times this symbol has been promoted
    notes: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChampionStore:
    path: Path = CHAMPION_PATH
    _data: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def get(self, symbol: str) -> Champion | None:
        d = self._data.get(symbol)
        return Champion(**d) if d else None

    def set(self, symbol: str, champion: Champion) -> None:
        self._data[symbol] = champion.as_dict()
        self._save()

    def remove(self, symbol: str) -> None:
        """Retire a champion (e.g. it no longer clears the confidence gate)."""
        if symbol in self._data:
            del self._data[symbol]
            self._save()

    def all(self) -> dict[str, Champion]:
        return {s: Champion(**d) for s, d in self._data.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from ..runtime_state import atomic_json
        atomic_json(self.path, self._data)

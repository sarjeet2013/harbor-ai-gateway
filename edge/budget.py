from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from .config import GatewayConfig


Action = Literal["allow", "force_cheap", "reject"]


class DayBudget:
    """In-memory day spend with optional JSON persistence across restarts."""

    def __init__(self, cfg: GatewayConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._day: str = self._today()
        self._spent: float = 0.0
        self._path = self._resolve_path(cfg.budget_state_path)
        self._load()

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _resolve_path(self, p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            from .config import repo_root

            path = repo_root() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            if data.get("day") == self._today():
                self._spent = float(data.get("spent_usd", 0.0))
                self._day = data["day"]
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    def _save_unlocked(self) -> None:
        payload = {
            "day": self._day,
            "spent_usd": round(self._spent, 6),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._path.write_text(json.dumps(payload, indent=2) + "\n")

    def _rollover_unlocked(self) -> None:
        today = self._today()
        if self._day != today:
            self._day = today
            self._spent = 0.0
            self._save_unlocked()

    def check(self) -> tuple[Action, float, float]:
        """Return (action, spent, remaining)."""
        with self._lock:
            self._rollover_unlocked()
            remaining = self._cfg.daily_budget_usd - self._spent
            if remaining > 0:
                return "allow", self._spent, remaining
            if self._cfg.on_budget_exceeded == "reject":
                return "reject", self._spent, remaining
            return "force_cheap", self._spent, remaining

    def record(self, cost_usd: float) -> float:
        with self._lock:
            self._rollover_unlocked()
            self._spent += max(0.0, cost_usd)
            self._save_unlocked()
            return self._spent

    def snapshot(self) -> dict:
        with self._lock:
            self._rollover_unlocked()
            return {
                "day": self._day,
                "spent_usd": round(self._spent, 6),
                "daily_budget_usd": self._cfg.daily_budget_usd,
                "remaining_usd": round(self._cfg.daily_budget_usd - self._spent, 6),
                "on_budget_exceeded": self._cfg.on_budget_exceeded,
            }

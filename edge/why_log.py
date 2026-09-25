from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WhyLog:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.is_absolute():
            from .config import repo_root

            self._path = repo_root() / self._path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._lock:
            with self._path.open("a") as f:
                f.write(line)

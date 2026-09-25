"""Exact response cache (disk). Semantic mode is planned — see docs/features/cache.md."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CacheConfig, repo_root


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_for_hash(
    body: dict[str, Any],
    *,
    model: str,
    pool: str,
    surface: str,
) -> dict[str, Any]:
    """Fields that define an exact-cache key (stable across equivalent requests)."""
    payload: dict[str, Any] = {
        "surface": surface,
        "model": model,
        "pool": pool,
    }
    if "messages" in body:
        payload["messages"] = body.get("messages")
    if "input" in body:
        payload["input"] = body.get("input")
    if "system" in body:
        payload["system"] = body.get("system")
    # Sampling / tools that change the completion
    for key in (
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "tools",
        "tool_choice",
        "response_format",
        "stop",
    ):
        if key in body:
            payload[key] = body[key]
    return payload


def cache_key_hash(normalized: dict[str, Any]) -> str:
    raw = _stable_json(normalized).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def extract_usage_tokens(result: dict[str, Any]) -> tuple[int, int, int]:
    """Return (prompt, completion, total) token estimates from a response body."""
    usage = result.get("usage") or {}
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    try:
        prompt_i = int(prompt or 0)
    except (TypeError, ValueError):
        prompt_i = 0
    try:
        completion_i = int(completion or 0)
    except (TypeError, ValueError):
        completion_i = 0
    total = usage.get("total_tokens")
    try:
        total_i = int(total) if total is not None else prompt_i + completion_i
    except (TypeError, ValueError):
        total_i = prompt_i + completion_i
    return prompt_i, completion_i, total_i


def should_bypass_cache(body: dict[str, Any], headers: dict[str, str] | None) -> bool:
    """Skip cache for streaming or explicit bypass."""
    if body.get("stream") is True:
        return True
    if headers:
        for k, v in headers.items():
            lk = k.lower()
            if lk == "x-cache-bypass" and str(v).lower() in ("1", "true", "yes"):
                return True
            if lk == "cache-control" and "no-cache" in str(v).lower():
                return True
            if lk == "cache-control" and "no-store" in str(v).lower():
                return True
    # OpenAI-style cache_control on body (if present)
    cc = body.get("cache_control")
    if isinstance(cc, str) and "no-cache" in cc.lower():
        return True
    if cc is False:
        return True
    return False


@dataclass
class CacheHit:
    body: dict[str, Any]
    kind: str  # exact | semantic (semantic not shipped)
    tokens_saved_prompt: int
    tokens_saved_completion: int
    tokens_saved: int
    key: str


class ResponseCache:
    """Disk-backed exact cache with TTL + max_entries (FIFO eviction)."""

    def __init__(self, cfg: CacheConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._path = self._resolve_path(cfg.path)
        self._index_path = self._path / "index.json"
        self._path.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict[str, Any]] = {}
        self._load_index()

    def _resolve_path(self, p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            path = repo_root() / path
        return path

    def _load_index(self) -> None:
        if not self._index_path.exists():
            return
        try:
            data = json.loads(self._index_path.read_text())
            if isinstance(data, dict):
                self._index = data
        except (json.JSONDecodeError, OSError, TypeError):
            self._index = {}

    def _save_index_unlocked(self) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index, indent=2) + "\n")
        tmp.replace(self._index_path)

    def _entry_path(self, key: str) -> Path:
        return self._path / f"{key}.json"

    def _purge_expired_unlocked(self, now: float) -> None:
        ttl = self._cfg.ttl_seconds
        dead: list[str] = []
        for key, meta in self._index.items():
            created = float(meta.get("created_at", 0))
            if ttl > 0 and (now - created) > ttl:
                dead.append(key)
        for key in dead:
            self._drop_unlocked(key)

    def _evict_if_needed_unlocked(self) -> None:
        max_e = self._cfg.max_entries
        if max_e <= 0:
            return
        while len(self._index) >= max_e:
            # FIFO: oldest created_at
            oldest_key = min(
                self._index.items(),
                key=lambda kv: float(kv[1].get("created_at", 0)),
            )[0]
            self._drop_unlocked(oldest_key)

    def _drop_unlocked(self, key: str) -> None:
        self._index.pop(key, None)
        ep = self._entry_path(key)
        try:
            ep.unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    @property
    def mode(self) -> str:
        return self._cfg.mode

    def get(
        self,
        body: dict[str, Any],
        *,
        model: str,
        pool: str,
        surface: str,
    ) -> CacheHit | None:
        if not self.enabled:
            return None
        # Semantic not implemented — only exact
        if self.mode not in ("exact", "semantic"):
            return None
        # semantic mode currently falls back to exact hashing only
        key = cache_key_hash(
            normalize_for_hash(body, model=model, pool=pool, surface=surface)
        )
        with self._lock:
            now = time.time()
            self._purge_expired_unlocked(now)
            meta = self._index.get(key)
            if not meta:
                return None
            created = float(meta.get("created_at", 0))
            if self._cfg.ttl_seconds > 0 and (now - created) > self._cfg.ttl_seconds:
                self._drop_unlocked(key)
                self._save_index_unlocked()
                return None
            ep = self._entry_path(key)
            if not ep.exists():
                self._drop_unlocked(key)
                self._save_index_unlocked()
                return None
            try:
                entry = json.loads(ep.read_text())
            except (json.JSONDecodeError, OSError):
                self._drop_unlocked(key)
                self._save_index_unlocked()
                return None
            resp = entry.get("response")
            if not isinstance(resp, dict):
                return None
            # Touch for bookkeeping (keep created_at for FIFO; optional last_access)
            meta["last_access"] = now
            self._index[key] = meta
            self._save_index_unlocked()
            tp = int(entry.get("tokens_prompt") or 0)
            tc = int(entry.get("tokens_completion") or 0)
            tt = int(entry.get("tokens_total") or (tp + tc))
            kind = "exact"  # semantic not shipped
            return CacheHit(
                body=resp,
                kind=kind,
                tokens_saved_prompt=tp,
                tokens_saved_completion=tc,
                tokens_saved=tt,
                key=key,
            )

    def set(
        self,
        body: dict[str, Any],
        *,
        model: str,
        pool: str,
        surface: str,
        response: dict[str, Any],
    ) -> str | None:
        if not self.enabled:
            return None
        if self.mode not in ("exact", "semantic"):
            return None
        key = cache_key_hash(
            normalize_for_hash(body, model=model, pool=pool, surface=surface)
        )
        prompt_t, completion_t, total_t = extract_usage_tokens(response)
        now = time.time()
        entry = {
            "key": key,
            "created_at": now,
            "model": model,
            "pool": pool,
            "surface": surface,
            "tokens_prompt": prompt_t,
            "tokens_completion": completion_t,
            "tokens_total": total_t,
            "response": response,
        }
        with self._lock:
            self._purge_expired_unlocked(now)
            self._evict_if_needed_unlocked()
            ep = self._entry_path(key)
            tmp = ep.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry, ensure_ascii=False) + "\n")
            tmp.replace(ep)
            self._index[key] = {
                "created_at": now,
                "last_access": now,
                "model": model,
                "pool": pool,
                "surface": surface,
                "tokens_total": total_t,
            }
            self._save_index_unlocked()
        return key

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._purge_expired_unlocked(now)
            return {
                "enabled": self.enabled,
                "mode": self.mode,
                "entries": len(self._index),
                "max_entries": self._cfg.max_entries,
                "ttl_seconds": self._cfg.ttl_seconds,
                "path": str(self._path),
            }

"""Exact + semantic response cache (disk). Semantic uses Ollama embeddings + cosine similarity."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import CacheConfig, repo_root

logger = logging.getLogger(__name__)

# Sampling / tool fields that affect completion — part of exact hash and semantic candidate filter.
_SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "tools",
    "tool_choice",
    "response_format",
    "stop",
)

_DEFAULT_EMBED_BASE = "http://127.0.0.1:11434"
_EMBED_TIMEOUT_S = 10.0


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
    for key in _SAMPLING_KEYS:
        if key in body:
            payload[key] = body[key]
    return payload


def cache_key_hash(normalized: dict[str, Any]) -> str:
    raw = _stable_json(normalized).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sampling_fingerprint(body: dict[str, Any]) -> dict[str, Any]:
    """Sampling/tool keys used to filter semantic candidates."""
    return {k: body[k] for k in _SAMPLING_KEYS if k in body}


def _content_to_text(content: Any) -> str:
    """Flatten message content (str or list of parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    parts.append(str(part["text"]))
                elif "text" in part:
                    parts.append(str(part["text"]))
                elif "content" in part:
                    parts.append(_content_to_text(part["content"]))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"])
        if "content" in content:
            return _content_to_text(content["content"])
    return str(content)


def cacheable_text(body: dict[str, Any]) -> str:
    """Textual parts for embedding — messages/system/input, not full tool JSON."""
    chunks: list[str] = []
    system = body.get("system")
    if system is not None:
        if isinstance(system, list):
            # Anthropic-style system blocks
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    chunks.append(block)
                else:
                    chunks.append(_content_to_text(block))
        else:
            chunks.append(_content_to_text(system))
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            text = _content_to_text(msg.get("content"))
            if text.strip():
                chunks.append(f"{role}: {text}" if role else text)
    inp = body.get("input")
    if inp is not None:
        if isinstance(inp, str):
            chunks.append(inp)
        elif isinstance(inp, list):
            for item in inp:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    role = str(item.get("role") or "")
                    text = _content_to_text(item.get("content") or item.get("text"))
                    if text.strip():
                        chunks.append(f"{role}: {text}" if role else text)
                else:
                    chunks.append(_content_to_text(item))
        else:
            chunks.append(_content_to_text(inp))
    return "\n".join(c for c in chunks if c and str(c).strip())


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity; 0.0 on length mismatch or zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        xf = float(x)
        yf = float(y)
        dot += xf * yf
        na += xf * xf
        nb += yf * yf
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


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
    """Skip cache for explicit bypass headers/body flags.

    ``stream: true`` is *not* a bypass — stream and non-stream share the same
    cache key (``stream`` is omitted from ``normalize_for_hash``). Hits replay
    as synthesized SSE; misses tee-store the completed body.
    """
    if headers:
        for k, v in headers.items():
            lk = k.lower()
            if lk == "x-cache-bypass" and str(v).lower() in ("1", "true", "yes"):
                return True
            if lk == "cache-control" and "no-cache" in str(v).lower():
                return True
            if lk == "cache-control" and "no-store" in str(v).lower():
                return True
    cc = body.get("cache_control")
    if isinstance(cc, str) and "no-cache" in cc.lower():
        return True
    if cc is False:
        return True
    return False


@dataclass
class CacheHit:
    body: dict[str, Any]
    kind: str  # exact | semantic
    tokens_saved_prompt: int
    tokens_saved_completion: int
    tokens_saved: int
    key: str


class ResponseCache:
    """Disk-backed exact (+ optional semantic) cache with TTL + max_entries (FIFO eviction)."""

    def __init__(
        self,
        cfg: CacheConfig,
        *,
        default_embedding_api_base: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._path = self._resolve_path(cfg.path)
        self._index_path = self._path / "index.json"
        self._path.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict[str, Any]] = {}
        self._load_index()
        self._default_embedding_api_base = default_embedding_api_base
        self._http = http_client  # optional inject for tests; else per-call Client
        self._embed_fail_warned = False

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

    @property
    def similarity_threshold(self) -> float:
        thr = self._cfg.similarity_threshold
        if thr is None:
            return 0.92
        return float(thr)

    def embedding_api_base(self) -> str:
        base = self._cfg.embedding_api_base or self._default_embedding_api_base
        if not base:
            base = _DEFAULT_EMBED_BASE
        return str(base).rstrip("/")

    def embedding_model(self) -> str | None:
        return self._cfg.embedding_model

    def _warn_embed_once(self, msg: str) -> None:
        if self._embed_fail_warned:
            return
        self._embed_fail_warned = True
        logger.warning("%s", msg)

    def _embed(self, text: str) -> list[float] | None:
        """Call Ollama embeddings API. Soft-fail → None (never raises to caller)."""
        model = self.embedding_model()
        if not model:
            self._warn_embed_once(
                "semantic cache: embedding_model unset; treating as miss / store without embedding"
            )
            return None
        if not text.strip():
            return None
        base = self.embedding_api_base()
        payload = {"model": model, "prompt": text}
        try:
            if self._http is not None:
                # Injected client (tests): try /api/embeddings then /api/embed
                r = self._http.post(f"{base}/api/embeddings", json=payload)
                if r.status_code == 404:
                    r = self._http.post(
                        f"{base}/api/embed",
                        json={"model": model, "input": text},
                    )
            else:
                with httpx.Client(timeout=_EMBED_TIMEOUT_S) as client:
                    r = client.post(f"{base}/api/embeddings", json=payload)
                    if r.status_code == 404:
                        r = client.post(
                            f"{base}/api/embed",
                            json={"model": model, "input": text},
                        )
            if r.status_code >= 400:
                self._warn_embed_once(
                    f"semantic cache: embedding provider HTTP {r.status_code} "
                    f"at {base}; soft-fail to miss/store-without-embedding"
                )
                return None
            data = r.json()
        except Exception as exc:  # noqa: BLE001 — soft-fail any transport/parse error
            self._warn_embed_once(
                f"semantic cache: embedding provider failed ({exc!s}); "
                "soft-fail to miss/store-without-embedding"
            )
            return None
        return _parse_embedding_response(data)

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
        if self.mode not in ("exact", "semantic"):
            return None

        normalized = normalize_for_hash(body, model=model, pool=pool, surface=surface)
        key = cache_key_hash(normalized)

        with self._lock:
            now = time.time()
            self._purge_expired_unlocked(now)
            exact = self._get_exact_unlocked(key, now)
            if exact is not None:
                return exact

            if self.mode != "semantic":
                return None

            # Exact miss → semantic search (embed outside? keep lock short: embed outside lock)
            # Release lock for HTTP embed, then re-acquire for scan.
            pass

        # Embed outside the lock so we don't block other requests on Ollama.
        query_vec = self._embed(cacheable_text(body))
        if query_vec is None:
            return None

        samp = sampling_fingerprint(body)
        threshold = self.similarity_threshold
        embed_model = self.embedding_model()

        with self._lock:
            now = time.time()
            self._purge_expired_unlocked(now)
            # Re-check exact in case a concurrent set landed the same key
            exact = self._get_exact_unlocked(key, now)
            if exact is not None:
                return exact
            return self._semantic_search_unlocked(
                query_vec=query_vec,
                model=model,
                pool=pool,
                surface=surface,
                sampling=samp,
                threshold=threshold,
                embed_model=embed_model,
                exclude_key=key,
                now=now,
            )

    def _get_exact_unlocked(self, key: str, now: float) -> CacheHit | None:
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
        meta["last_access"] = now
        self._index[key] = meta
        self._save_index_unlocked()
        tp = int(entry.get("tokens_prompt") or 0)
        tc = int(entry.get("tokens_completion") or 0)
        tt = int(entry.get("tokens_total") or (tp + tc))
        return CacheHit(
            body=resp,
            kind="exact",
            tokens_saved_prompt=tp,
            tokens_saved_completion=tc,
            tokens_saved=tt,
            key=key,
        )

    def _semantic_search_unlocked(
        self,
        *,
        query_vec: list[float],
        model: str,
        pool: str,
        surface: str,
        sampling: dict[str, Any],
        threshold: float,
        embed_model: str | None,
        exclude_key: str,
        now: float,
    ) -> CacheHit | None:
        best_key: str | None = None
        best_sim = -1.0
        best_entry: dict[str, Any] | None = None

        for cand_key, meta in list(self._index.items()):
            if cand_key == exclude_key:
                continue
            if meta.get("model") != model or meta.get("pool") != pool:
                continue
            if meta.get("surface") != surface:
                continue
            if not meta.get("has_embedding"):
                continue
            if embed_model and meta.get("embed_model") and meta.get("embed_model") != embed_model:
                continue
            stored_samp = meta.get("sampling")
            if isinstance(stored_samp, dict):
                if stored_samp != sampling:
                    continue
            elif sampling:
                # Candidate has no sampling meta but request has sampling keys — skip
                continue

            ep = self._entry_path(cand_key)
            if not ep.exists():
                continue
            try:
                entry = json.loads(ep.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            emb = entry.get("embedding")
            if not isinstance(emb, list) or not emb:
                continue
            try:
                vec = [float(x) for x in emb]
            except (TypeError, ValueError):
                continue
            sim = cosine_similarity(query_vec, vec)
            if sim > best_sim:
                best_sim = sim
                best_key = cand_key
                best_entry = entry

        if best_key is None or best_entry is None or best_sim < threshold:
            return None

        resp = best_entry.get("response")
        if not isinstance(resp, dict):
            return None
        meta = self._index.get(best_key)
        if meta is not None:
            meta["last_access"] = now
            self._index[best_key] = meta
            self._save_index_unlocked()
        tp = int(best_entry.get("tokens_prompt") or 0)
        tc = int(best_entry.get("tokens_completion") or 0)
        tt = int(best_entry.get("tokens_total") or (tp + tc))
        return CacheHit(
            body=resp,
            kind="semantic",
            tokens_saved_prompt=tp,
            tokens_saved_completion=tc,
            tokens_saved=tt,
            key=best_key,
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
        text = cacheable_text(body)
        fp = text_fingerprint(text) if text else None
        samp = sampling_fingerprint(body)

        embedding: list[float] | None = None
        embed_model: str | None = None
        if self.mode == "semantic":
            embedding = self._embed(text)
            if embedding is not None:
                embed_model = self.embedding_model()

        entry: dict[str, Any] = {
            "key": key,
            "created_at": now,
            "model": model,
            "pool": pool,
            "surface": surface,
            "tokens_prompt": prompt_t,
            "tokens_completion": completion_t,
            "tokens_total": total_t,
            "response": response,
            "sampling": samp,
        }
        if fp:
            entry["text_fingerprint"] = fp
        if embedding is not None:
            entry["embedding"] = embedding
            entry["embed_model"] = embed_model

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
                "has_embedding": embedding is not None,
                "embed_model": embed_model,
                "sampling": samp,
                "text_fingerprint": fp,
            }
            self._save_index_unlocked()
        return key

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._purge_expired_unlocked(now)
            with_embed = sum(1 for m in self._index.values() if m.get("has_embedding"))
            return {
                "enabled": self.enabled,
                "mode": self.mode,
                "entries": len(self._index),
                "entries_with_embedding": with_embed,
                "max_entries": self._cfg.max_entries,
                "ttl_seconds": self._cfg.ttl_seconds,
                "path": str(self._path),
                "similarity_threshold": self.similarity_threshold
                if self.mode == "semantic"
                else None,
                "embedding_model": self.embedding_model()
                if self.mode == "semantic"
                else None,
            }


def _parse_embedding_response(data: Any) -> list[float] | None:
    """Accept Ollama /api/embeddings and /api/embed response shapes."""
    if not isinstance(data, dict):
        return None
    emb = data.get("embedding")
    if isinstance(emb, list) and emb:
        try:
            return [float(x) for x in emb]
        except (TypeError, ValueError):
            return None
    embs = data.get("embeddings")
    if isinstance(embs, list) and embs:
        first = embs[0]
        if isinstance(first, list) and first:
            try:
                return [float(x) for x in first]
            except (TypeError, ValueError):
                return None
    return None


def cheap_pool_embedding_api_base(models_cheap: list[Any] | None) -> str | None:
    """First enabled cheap-pool api_base (for Ollama embed default)."""
    if not models_cheap:
        return None
    for spec in models_cheap:
        enabled = getattr(spec, "enabled", True)
        api_base = getattr(spec, "api_base", None)
        if enabled and api_base:
            return str(api_base)
    return None

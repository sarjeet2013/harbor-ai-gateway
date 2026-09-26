"""Mini Admin UI — localhost-guarded settings API + static /admin pages.

YAML remains source of truth. Mutating admin API routes refuse non-loopback
clients by default (see docs/features/admin-ui.md). Secrets go to repo ``.env``
only — never into gateway.yaml.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import GatewayConfig, default_config_path, load_config, repo_root, save_raw_config
from .litellm_gen import write_litellm_config
from .models_yaml import load_raw_yaml

ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "admin_static"

_LOOPBACK_HOSTS = frozenset(
    {
        "127.0.0.1",
        "::1",
        "localhost",
        "0:0:0:0:0:0:0:1",
        "::ffff:127.0.0.1",
    }
)

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FRONTIER_KEYS = ("OPENAI_API_KEY", "XAI_API_KEY")

_RESTART_NOTE = (
    "Apply saves config and automatically restarts edge + LiteLLM "
    "(brief downtime while services come back)."
)
_RESTART_SCHEDULED_MSG = (
    "Settings saved. Harbor edge and LiteLLM are restarting to apply changes "
    "(brief downtime)."
)
_RESTART_FAILED_MSG = (
    "Settings saved, but auto-restart failed to schedule. "
    "Run: python -m edge restart"
)


def is_loopback_host(host: str | None) -> bool:
    """True if the peer address is loopback (IPv4/IPv6)."""
    if not host:
        return False
    h = host.strip().lower()
    if h in _LOOPBACK_HOSTS:
        return True
    # Some stacks report IPv4-mapped form without the canonical key above
    if h.startswith("::ffff:") and h.endswith("127.0.0.1"):
        return True
    return False


def require_localhost(request: Request) -> None:
    """Refuse mutating admin calls from non-loopback clients."""
    peer = request.client.host if request.client else None
    if not is_loopback_host(peer):
        raise HTTPException(
            status_code=403,
            detail=(
                "Admin mutations are localhost-only "
                f"(client host {peer!r} is not loopback). "
                "Open http://127.0.0.1:<port>/admin from this machine."
            ),
        )


def default_dotenv_path() -> Path:
    return repo_root() / ".env"


def env_key_is_set(key: str, *, dotenv_path: Path | None = None) -> bool:
    """True if key is non-empty in process env or repo .env (no value returned)."""
    val = os.environ.get(key)
    if val:
        return True
    path = dotenv_path or default_dotenv_path()
    if not path.is_file():
        return False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        return bool(v)
    return False


def update_dotenv_keys(
    updates: dict[str, str],
    *,
    path: Path | None = None,
) -> list[str]:
    """Create/update KEY=value lines in ``.env``. Never logs values.

    Only keys present in ``updates`` with non-empty values are written.
    Returns the list of keys that were set/updated (names only).
    """
    env_path = path or default_dotenv_path()
    if not updates:
        return []
    for key in updates:
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"invalid env key: {key!r}")
        if not updates[key]:
            raise ValueError(f"refusing empty value for {key}")

    existing_lines: list[str] = []
    if env_path.is_file():
        existing_lines = env_path.read_text().splitlines()

    found: set[str] = set()
    out_lines: list[str] = []
    for raw in existing_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(raw)
            continue
        k, _, _rest = stripped.partition("=")
        key = k.strip()
        if key in updates:
            out_lines.append(f"{key}={updates[key]}")
            found.add(key)
        else:
            out_lines.append(raw)

    for key, value in updates.items():
        if key not in found:
            if out_lines and out_lines[-1].strip():
                out_lines.append("")
            out_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(out_lines)
    if text and not text.endswith("\n"):
        text += "\n"
    env_path.write_text(text)
    # Reflect into current process so subsequent reads see set=true
    for key, value in updates.items():
        os.environ[key] = value
    return list(updates.keys())


class ModelRowIn(BaseModel):
    name: str
    litellm_model: str | None = None
    api_base: str | None = None
    enabled: bool = True
    think_false: bool = False


class CacheSettingsIn(BaseModel):
    enabled: bool = True
    mode: str = "exact"
    similarity_threshold: float = 0.92
    embedding_model: str | None = None
    embedding_api_base: str | None = None
    semantic_scope: str = "full"


class JudgeSettingsIn(BaseModel):
    enabled: bool = True
    litellm_model: str = "ollama_chat/qwen3:8b"
    api_base: str = "http://127.0.0.1:11434"
    timeout_ms: int = 400
    min_confidence: float = 0.55


class RouterSettingsIn(BaseModel):
    strategy: str = "judge_then_heuristic"
    judge: JudgeSettingsIn = Field(default_factory=JudgeSettingsIn)


class SecretsIn(BaseModel):
    """Optional frontier keys. Empty / omitted = leave unchanged. Never echoed back."""

    OPENAI_API_KEY: str | None = None
    XAI_API_KEY: str | None = None


class ApplySettingsIn(BaseModel):
    models: dict[str, list[ModelRowIn]] | None = None
    cache: CacheSettingsIn | None = None
    router: RouterSettingsIn | None = None
    secrets: SecretsIn | None = None


def _model_row_out(spec_dict: dict[str, Any]) -> dict[str, Any]:
    extra = spec_dict.get("extra_litellm_params") or {}
    think_false = bool(isinstance(extra, dict) and extra.get("think") is False)
    return {
        "name": spec_dict.get("name"),
        "litellm_model": spec_dict.get("litellm_model"),
        "api_base": spec_dict.get("api_base"),
        "enabled": spec_dict.get("enabled", True) is not False,
        "think_false": think_false,
    }


def _model_row_to_yaml(row: ModelRowIn) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": row.name.strip()}
    if row.litellm_model:
        entry["litellm_model"] = row.litellm_model.strip()
    if row.api_base:
        entry["api_base"] = row.api_base.strip().rstrip("/")
    if not row.enabled:
        entry["enabled"] = False
    if row.think_false:
        entry["extra_litellm_params"] = {"think": False}
    return entry


def build_settings_payload(
    cfg: GatewayConfig,
    *,
    config_path: Path | None = None,
    dotenv_path: Path | None = None,
    budget_snapshot: dict[str, Any] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-oriented admin payload (no secret values)."""
    path = config_path or default_config_path()
    raw = load_raw_yaml(path) if path.exists() else {}
    models_raw = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    cheap_raw = models_raw.get("cheap") or []
    expensive_raw = models_raw.get("expensive") or []

    # Prefer raw YAML rows so the form preserves api_base / extras exactly.
    def _rows(pool_raw: list[Any], fallback_specs: list[Any]) -> list[dict[str, Any]]:
        if pool_raw:
            out: list[dict[str, Any]] = []
            for item in pool_raw:
                if isinstance(item, str):
                    out.append(
                        {
                            "name": item,
                            "litellm_model": None,
                            "api_base": None,
                            "enabled": True,
                            "think_false": False,
                        }
                    )
                elif isinstance(item, dict):
                    out.append(_model_row_out(item))
            return out
        return [
            {
                "name": s.name,
                "litellm_model": s.litellm_model,
                "api_base": s.api_base,
                "enabled": s.enabled,
                "think_false": bool(
                    s.extra_litellm_params
                    and s.extra_litellm_params.get("think") is False
                ),
            }
            for s in fallback_specs
        ]

    cache_cfg = cfg.cache
    judge = cfg.router.judge
    return {
        "status": {
            "upstream_mode": cfg.upstream_mode,
            "budget": budget_snapshot or {},
            "cache": cache_stats
            or {
                "enabled": cache_cfg.enabled,
                "mode": cache_cfg.mode,
                "entries": None,
            },
            "router_strategy": cfg.router.strategy,
        },
        "models": {
            "cheap": _rows(cheap_raw if isinstance(cheap_raw, list) else [], cfg.models.cheap),
            "expensive": _rows(
                expensive_raw if isinstance(expensive_raw, list) else [],
                cfg.models.expensive,
            ),
        },
        "cache": {
            "enabled": cache_cfg.enabled,
            "mode": cache_cfg.mode,
            "similarity_threshold": cache_cfg.similarity_threshold,
            "embedding_model": cache_cfg.embedding_model,
            "embedding_api_base": cache_cfg.embedding_api_base,
            "semantic_scope": cache_cfg.semantic_scope,
        },
        "router": {
            "strategy": cfg.router.strategy,
            "judge": {
                "enabled": judge.enabled,
                "litellm_model": judge.litellm_model,
                "api_base": judge.api_base,
                "timeout_ms": judge.timeout_ms,
                "min_confidence": judge.min_confidence,
            },
        },
        "secrets": {
            key: env_key_is_set(key, dotenv_path=dotenv_path) for key in _FRONTIER_KEYS
        },
        "config_path": str(path),
        "restart_note": _RESTART_NOTE,
    }


def apply_settings(
    body: ApplySettingsIn,
    *,
    config_path: Path | None = None,
    dotenv_path: Path | None = None,
    regenerate_litellm: bool = True,
) -> dict[str, Any]:
    """Merge settings into gateway.yaml (+ optional .env keys). Returns result meta."""
    path = config_path or default_config_path()
    raw = load_raw_yaml(path) if path.exists() else {}

    changed: list[str] = []

    if body.models is not None:
        models = raw.setdefault("models", {})
        if not isinstance(models, dict):
            models = {}
            raw["models"] = models
        for pool in ("cheap", "expensive"):
            rows = body.models.get(pool)
            if rows is None:
                continue
            models[pool] = [_model_row_to_yaml(r) for r in rows]
            changed.append(f"models.{pool}")

    if body.cache is not None:
        cache = raw.setdefault("cache", {})
        if not isinstance(cache, dict):
            cache = {}
            raw["cache"] = cache
        c = body.cache
        cache["enabled"] = bool(c.enabled)
        cache["mode"] = c.mode
        cache["similarity_threshold"] = float(c.similarity_threshold)
        cache["semantic_scope"] = c.semantic_scope
        if c.embedding_model:
            cache["embedding_model"] = c.embedding_model.strip()
        else:
            cache.pop("embedding_model", None)
        if c.embedding_api_base:
            cache["embedding_api_base"] = c.embedding_api_base.strip().rstrip("/")
        else:
            cache.pop("embedding_api_base", None)
        # Preserve ttl_seconds / max_entries / path if already present
        changed.append("cache")

    if body.router is not None:
        router = raw.setdefault("router", {})
        if not isinstance(router, dict):
            router = {}
            raw["router"] = router
        router["strategy"] = body.router.strategy
        judge = router.setdefault("judge", {})
        if not isinstance(judge, dict):
            judge = {}
            router["judge"] = judge
        j = body.router.judge
        judge["enabled"] = bool(j.enabled)
        judge["litellm_model"] = j.litellm_model.strip()
        judge["api_base"] = j.api_base.strip().rstrip("/")
        judge["timeout_ms"] = int(j.timeout_ms)
        judge["min_confidence"] = float(j.min_confidence)
        # Preserve expensive_keywords / coder_keywords / etc.
        changed.append("router")

    save_raw_config(raw, path)

    secrets_updated: list[str] = []
    if body.secrets is not None:
        updates: dict[str, str] = {}
        for key in _FRONTIER_KEYS:
            val = getattr(body.secrets, key, None)
            if val is None:
                continue
            val = val.strip()
            if not val:
                continue  # empty = leave unchanged
            updates[key] = val
        if updates:
            secrets_updated = update_dotenv_keys(updates, path=dotenv_path)
            changed.extend(f"env.{k}" for k in secrets_updated)

    litellm_path: str | None = None
    if regenerate_litellm:
        # Reload validated config from the path we just wrote
        new_cfg = load_config(path)
        out = write_litellm_config(new_cfg)
        litellm_path = str(out)

    return {
        "ok": True,
        "changed": changed,
        "config_path": str(path),
        "litellm_config": litellm_path,
        "secrets_updated": secrets_updated,  # key names only
        "message": "Settings saved.",
        "secrets": {
            key: env_key_is_set(key, dotenv_path=dotenv_path) for key in _FRONTIER_KEYS
        },
    }


def schedule_edge_restart(*, delay_seconds: float = 0.5) -> dict[str, Any]:
    """Spawn a detached ``python -m edge restart`` so Apply can return first.

    The helper sleeps ``delay_seconds``, then stops and starts edge + LiteLLM
    (detached). Logs go to ``logs/restart.log``. Returns response meta; never
    raises — scheduling failure is returned as ``restart_scheduled: false``.
    """
    try:
        root = repo_root()
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "restart.log"
        log_f = log_path.open("a")
        cmd = [
            sys.executable,
            "-m",
            "edge",
            "restart",
            "--delay",
            str(delay_seconds),
            "--detach",
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=dict(os.environ),
        )
        return {
            "restart_scheduled": True,
            "restart_helper_pid": proc.pid,
            "message": _RESTART_SCHEDULED_MSG,
        }
    except Exception as e:
        return {
            "restart_scheduled": False,
            "message": f"{_RESTART_FAILED_MSG} ({e})",
        }


def create_admin_router(app: FastAPI) -> APIRouter:
    """Build admin routes bound to the live FastAPI app state."""
    router = APIRouter(tags=["admin"])

    @router.get("/admin/api/settings")
    async def get_settings(request: Request) -> JSONResponse:
        cfg: GatewayConfig = request.app.state.config
        budget = getattr(request.app.state, "budget", None)
        cache = getattr(request.app.state, "cache", None)
        payload = build_settings_payload(
            cfg,
            budget_snapshot=budget.snapshot() if budget is not None else None,
            cache_stats=cache.stats() if cache is not None else None,
        )
        return JSONResponse(payload)

    @router.post("/admin/api/settings")
    async def post_settings(request: Request) -> JSONResponse:
        require_localhost(request)
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body") from None
        try:
            body = ApplySettingsIn.model_validate(data)
        except Exception as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        try:
            result = apply_settings(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"save failed: {e}") from e
        # Refresh in-memory config for any GETs before the scheduled restart lands.
        try:
            request.app.state.config = load_config()
        except Exception:
            pass
        # Detached helper sleeps briefly, then stop+start — must not kill us mid-response.
        result.update(schedule_edge_restart(delay_seconds=0.5))
        return JSONResponse(result)

    @router.get("/admin", include_in_schema=False)
    @router.get("/admin/", include_in_schema=False)
    async def admin_index() -> FileResponse:
        index = ADMIN_STATIC_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=500, detail="admin UI assets missing")
        return FileResponse(index, media_type="text/html; charset=utf-8")

    return router


def mount_admin(app: FastAPI) -> None:
    """Attach /admin UI + /admin/api/* to the edge FastAPI app."""
    if ADMIN_STATIC_DIR.is_dir():
        app.mount(
            "/admin/static",
            StaticFiles(directory=str(ADMIN_STATIC_DIR)),
            name="admin_static",
        )
    app.include_router(create_admin_router(app))


__all__ = [
    "ADMIN_STATIC_DIR",
    "ApplySettingsIn",
    "apply_settings",
    "build_settings_payload",
    "create_admin_router",
    "env_key_is_set",
    "is_loopback_host",
    "mount_admin",
    "require_localhost",
    "schedule_edge_restart",
    "update_dotenv_keys",
]

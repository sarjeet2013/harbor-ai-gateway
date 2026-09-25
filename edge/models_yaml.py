"""YAML helpers for `python -m edge models` — edit cheap/expensive pools in gateway.yaml.

Round-trips with PyYAML (comments are not preserved). Prefer hand-edits or
``config/gateway.yaml.example`` when you need the annotated template.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .config import GatewayConfig, ModelSpec, load_config, repo_root

POOLS = ("cheap", "expensive")

_REWRITE_HEADER = (
    "# Canonical config — source of truth for Harbor.\n"
    "# Note: `python -m edge models set` rewrites this file with PyYAML "
    "(inline comments are not preserved).\n"
    "# Annotated template: config/gateway.yaml.example\n"
    "# Hybrid: cheap = Ollama (litellm_model + api_base), expensive = cloud or Ollama.\n"
)


def default_gateway_path() -> Path:
    import os

    cfg_path = Path(
        os.environ.get("GATEWAY_CONFIG", repo_root() / "config" / "gateway.yaml")
    )
    if not cfg_path.is_absolute():
        cfg_path = repo_root() / cfg_path
    return cfg_path


def load_raw_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"gateway config must be a mapping, got {type(raw)}")
    return raw


def dump_raw_yaml(path: Path, raw: dict[str, Any]) -> None:
    """Write gateway YAML (PyYAML round-trip; comments not preserved)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(raw, default_flow_style=False, sort_keys=False, allow_unicode=True)
    path.write_text(_REWRITE_HEADER + body)


def normalize_litellm_model(
    model: str,
    *,
    pool: str,
    api_base: str | None,
) -> str:
    """Map a short model id to a LiteLLM model string.

    Already-prefixed ids (``ollama_chat/…``, ``openai/…``, ``xai/…``) pass through.
    With ``api_base`` → ``ollama_chat/{model}``. Expensive without api_base → ``openai/{model}``.
    Cheap without api_base → ``ollama_chat/{model}``.
    """
    model = (model or "").strip()
    if not model:
        raise ValueError("model id must be non-empty")
    if "/" in model:
        return model
    if api_base:
        return f"ollama_chat/{model}"
    if pool == "expensive":
        return f"openai/{model}"
    return f"ollama_chat/{model}"


def _default_alias(pool: str, litellm_model: str | None, api_base: str | None) -> str:
    if api_base or (litellm_model and litellm_model.startswith(("ollama/", "ollama_chat/"))):
        return f"ollama-{pool}"
    if litellm_model and litellm_model.startswith("xai/"):
        return f"grok-{pool}"
    if litellm_model and litellm_model.startswith("openai/"):
        return f"openai-{pool}"
    return f"model-{pool}"


def _coerce_entry(entry: Any) -> dict[str, Any]:
    if isinstance(entry, str):
        return {"name": entry}
    if isinstance(entry, dict):
        return dict(entry)
    raise TypeError(f"unsupported model entry type: {type(entry)}")


def pool_primary_entry(raw: dict[str, Any], pool: str) -> dict[str, Any]:
    """Return the first models.<pool> entry dict (creates list/entry if missing)."""
    if pool not in POOLS:
        raise ValueError(f"pool must be one of {POOLS}, got {pool!r}")
    models = raw.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError("models: must be a mapping of pools")
    entries = models.get(pool)
    if entries is None:
        entries = []
        models[pool] = entries
    if not isinstance(entries, list):
        raise ValueError(f"models.{pool} must be a list")
    if not entries:
        entries.append({"name": f"model-{pool}"})
    entries[0] = _coerce_entry(entries[0])
    return entries[0]


def apply_pool_set(
    raw: dict[str, Any],
    pool: str,
    *,
    model: str | None = None,
    api_base: str | None | object = ...,
    enabled: bool | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Mutate raw config dict for models set. api_base=... means leave unchanged;
    api_base=None clears it.
    """
    entry = pool_primary_entry(raw, pool)
    if name:
        entry["name"] = name.strip()

    clear_api_base = api_base is None
    set_api_base = api_base is not ... and api_base is not None
    next_api_base: str | None
    if clear_api_base:
        next_api_base = None
    elif set_api_base:
        next_api_base = str(api_base).rstrip("/")
    else:
        next_api_base = entry.get("api_base")

    if model is not None:
        entry["litellm_model"] = normalize_litellm_model(
            model, pool=pool, api_base=next_api_base
        )

    if clear_api_base:
        entry.pop("api_base", None)
    elif set_api_base:
        entry["api_base"] = next_api_base

    if enabled is not None:
        if enabled:
            entry.pop("enabled", None)  # default True — omit for clean YAML
        else:
            entry["enabled"] = False

    if not entry.get("name"):
        entry["name"] = _default_alias(
            pool, entry.get("litellm_model"), entry.get("api_base")
        )
    return entry


def list_pool_rows(cfg: GatewayConfig | None = None) -> list[dict[str, Any]]:
    """Rows for `models` / `models list` (cheap + expensive only)."""
    config = cfg or load_config()
    rows: list[dict[str, Any]] = []
    for pool_name, specs in (
        ("cheap", config.models.cheap),
        ("expensive", config.models.expensive),
    ):
        for spec in specs:
            rows.append(
                {
                    "pool": pool_name,
                    "alias": spec.name,
                    "litellm_model": spec.litellm_model,
                    "api_base": spec.api_base,
                    "enabled": spec.enabled,
                }
            )
    return rows


def is_ollama_api_base(litellm_model: str | None, api_base: str | None) -> bool:
    if not api_base:
        return False
    if litellm_model and (
        litellm_model.startswith("ollama/") or litellm_model.startswith("ollama_chat/")
    ):
        return True
    # Heuristic: host:11434 is almost always Ollama
    try:
        port = urlparse(api_base).port
    except Exception:
        port = None
    return port == 11434


def collect_ollama_bases(cfg: GatewayConfig | None = None) -> list[dict[str, str]]:
    """Unique Ollama api_base targets from cheap/expensive pools (for doctor)."""
    config = cfg or load_config()
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for pool_name, specs in (
        ("cheap", config.models.cheap),
        ("expensive", config.models.expensive),
    ):
        for spec in specs:
            if not is_ollama_api_base(spec.litellm_model, spec.api_base):
                continue
            base = (spec.api_base or "").rstrip("/")
            if base in seen:
                continue
            seen.add(base)
            out.append({"pool": pool_name, "alias": spec.name, "api_base": base})
    return out


def probe_ollama_tags(
    api_base: str,
    *,
    timeout: float = 2.0,
) -> tuple[bool, list[str], str | None]:
    """GET ``{api_base}/api/tags``. Returns (ok, model_names, error_message)."""
    import httpx

    url = api_base.rstrip("/") + "/api/tags"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return False, [], str(e)
    names: list[str] = []
    for m in data.get("models") or []:
        if isinstance(m, dict):
            n = m.get("name") or m.get("model")
            if n:
                names.append(str(n))
        elif isinstance(m, str):
            names.append(m)
    return True, names, None


# Re-export ModelSpec for type checkers / tests
__all__ = [
    "POOLS",
    "ModelSpec",
    "apply_pool_set",
    "collect_ollama_bases",
    "default_gateway_path",
    "dump_raw_yaml",
    "is_ollama_api_base",
    "list_pool_rows",
    "load_raw_yaml",
    "normalize_litellm_model",
    "pool_primary_entry",
    "probe_ollama_tags",
]

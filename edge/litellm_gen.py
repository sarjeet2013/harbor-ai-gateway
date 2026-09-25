"""Generate LiteLLM proxy config from the unified gateway.yaml models block."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .config import GatewayConfig, ModelSpec, load_config, repo_root

GENERATED_REL = Path("config") / ".generated_litellm.yaml"


def _params_for(spec: ModelSpec) -> dict[str, Any] | None:
    """Build litellm_params for one model. Returns None if not generatable."""
    if not spec.litellm_model:
        return None
    params: dict[str, Any] = {"model": spec.litellm_model}
    if spec.api_base:
        params["api_base"] = spec.api_base
    if spec.extra_litellm_params:
        params.update(spec.extra_litellm_params)
    return params


def build_litellm_config(cfg: GatewayConfig) -> dict[str, Any]:
    """Return a LiteLLM config dict from gateway model pools."""
    model_list: list[dict[str, Any]] = []
    skipped: list[str] = []
    for spec in cfg.models.all_specs():
        params = _params_for(spec)
        if params is None:
            skipped.append(spec.name)
            continue
        model_list.append({"model_name": spec.name, "litellm_params": params})
    return {
        "model_list": model_list,
        "litellm_settings": {"drop_params": True},
        "_meta": {
            "generated_from": "config/gateway.yaml",
            "skipped_without_litellm_model": skipped,
        },
    }


def default_output_path() -> Path:
    override = os.environ.get("GATEWAY_LITELLM_CONFIG")
    if override:
        p = Path(override)
        return p if p.is_absolute() else repo_root() / p
    return repo_root() / GENERATED_REL


def write_litellm_config(
    cfg: GatewayConfig | None = None,
    *,
    path: Path | None = None,
) -> Path:
    """Write LiteLLM YAML. Uses GATEWAY_LITELLM_CONFIG if set, else generated path."""
    config = cfg or load_config()
    out = path or default_output_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    data = build_litellm_config(config)
    # Strip internal meta before writing (LiteLLM doesn't need it)
    meta = data.pop("_meta", {})
    header = (
        "# AUTO-GENERATED from config/gateway.yaml — do not hand-edit.\n"
        "# Regenerate via: python -m edge start  (or edge.litellm_gen)\n"
        f"# skipped (no litellm_model): {meta.get('skipped_without_litellm_model')}\n"
    )
    with out.open("w") as f:
        f.write(header)
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    return out


def resolved_aliases(cfg: GatewayConfig | None = None) -> list[dict[str, Any]]:
    """Human-readable alias → litellm model mapping for config-check."""
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
                    "will_send_to_litellm": bool(spec.litellm_model),
                }
            )
    return rows


if __name__ == "__main__":
    p = write_litellm_config()
    print(f"wrote {p}")

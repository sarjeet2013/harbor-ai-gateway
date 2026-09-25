from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class RouterConfig(BaseModel):
    strategy: str = "heuristic_cost_first"
    expensive_keywords: list[str] = Field(default_factory=list)
    expensive_min_chars: int = 2000


class ModelSpec(BaseModel):
    """One model in a cheap/expensive pool (unified gateway.yaml shape)."""

    name: str
    litellm_model: str | None = None
    api_base: str | None = None
    # Extra litellm_params merged into generated LiteLLM config
    extra_litellm_params: dict[str, Any] = Field(default_factory=dict)


class ModelsConfig(BaseModel):
    """Pools of models. Accepts unified ModelSpec dicts or legacy bare name strings."""

    cheap: list[ModelSpec] = Field(
        default_factory=lambda: [ModelSpec(name="mock-cheap")]
    )
    expensive: list[ModelSpec] = Field(
        default_factory=lambda: [ModelSpec(name="mock-expensive")]
    )

    @field_validator("cheap", "expensive", mode="before")
    @classmethod
    def _coerce_entries(cls, v: Any) -> list[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise TypeError("models.cheap / models.expensive must be a list")
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append({"name": item})
            elif isinstance(item, dict):
                out.append(item)
            elif isinstance(item, ModelSpec):
                out.append(item)
            else:
                raise TypeError(f"unsupported model entry type: {type(item)}")
        return out

    def names(self, pool: str) -> list[str]:
        specs = self.cheap if pool == "cheap" else self.expensive
        return [m.name for m in specs]

    @property
    def cheap_names(self) -> list[str]:
        return self.names("cheap")

    @property
    def expensive_names(self) -> list[str]:
        return self.names("expensive")

    def all_specs(self) -> list[ModelSpec]:
        return list(self.cheap) + list(self.expensive)


class CostEstimates(BaseModel):
    cheap_per_request: float = 0.001
    expensive_per_request: float = 0.02



class CacheConfig(BaseModel):
    """Exact response cache (semantic mode planned — see docs/features/cache.md)."""

    enabled: bool = True
    mode: str = "exact"  # exact | semantic (semantic planned; falls back to exact hashing)
    ttl_seconds: int = 86400
    max_entries: int = 1000
    path: str = "data/cache"
    # Planned (not used yet):
    embedding_model: str | None = None
    similarity_threshold: float | None = None


class GatewayConfig(BaseModel):
    listen: str = "0.0.0.0:4000"
    litellm_host: str = "127.0.0.1"
    litellm_port: int = 4001
    # Legacy single URL; if set, overrides host/port for the edge proxy target
    litellm_base_url: str | None = None
    upstream_mode: str = "mock"  # mock | litellm
    daily_budget_usd: float = 10.0
    on_budget_exceeded: str = "force_cheap"  # force_cheap | reject
    budget_state_path: str = "data/budget.json"
    ledger_path: str = "logs/ledger.jsonl"  # was why_log_path / logs/why.jsonl
    cost_estimates_usd: CostEstimates = Field(default_factory=CostEstimates)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    @model_validator(mode="before")
    @classmethod
    def _legacy_why_log_path(cls, data: Any) -> Any:
        """Accept deprecated why_log_path as an alias for ledger_path."""
        if isinstance(data, dict) and "ledger_path" not in data and "why_log_path" in data:
            data = dict(data)
            data["ledger_path"] = data.pop("why_log_path")
        elif isinstance(data, dict) and "why_log_path" in data:
            data = dict(data)
            data.pop("why_log_path", None)
        return data

    @model_validator(mode="after")
    def _resolve_litellm_base_url(self) -> GatewayConfig:
        if not self.litellm_base_url:
            object.__setattr__(
                self,
                "litellm_base_url",
                f"http://{self.litellm_host}:{self.litellm_port}",
            )
        return self

    @property
    def host(self) -> str:
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> GatewayConfig:
    cfg_path = Path(path) if path else Path(
        os.environ.get("GATEWAY_CONFIG", repo_root() / "config" / "gateway.yaml")
    )
    if not cfg_path.is_absolute():
        cfg_path = repo_root() / cfg_path
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open() as f:
            raw = yaml.safe_load(f) or {}
    return GatewayConfig.model_validate(raw)

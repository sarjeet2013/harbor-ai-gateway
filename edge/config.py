from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RouterConfig(BaseModel):
    strategy: str = "heuristic_cost_first"
    expensive_keywords: list[str] = Field(default_factory=list)
    expensive_min_chars: int = 2000


class ModelsConfig(BaseModel):
    cheap: list[str] = Field(default_factory=lambda: ["mock-cheap"])
    expensive: list[str] = Field(default_factory=lambda: ["mock-expensive"])


class CostEstimates(BaseModel):
    cheap_per_request: float = 0.001
    expensive_per_request: float = 0.02


class GatewayConfig(BaseModel):
    listen: str = "0.0.0.0:4000"
    litellm_base_url: str = "http://127.0.0.1:4001"
    upstream_mode: str = "mock"  # mock | litellm
    daily_budget_usd: float = 10.0
    on_budget_exceeded: str = "force_cheap"  # force_cheap | reject
    budget_state_path: str = "data/budget.json"
    why_log_path: str = "logs/why.jsonl"
    cost_estimates_usd: CostEstimates = Field(default_factory=CostEstimates)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)

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

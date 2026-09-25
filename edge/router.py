from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig


@dataclass
class RouteDecision:
    model: str
    pool: str  # cheap | expensive
    reason: str


def _extract_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    # OpenAI chat
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    parts.append(block)
    # Responses API
    inp = body.get("input")
    if isinstance(inp, str):
        parts.append(inp)
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                c = item.get("content")
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, list):
                    for block in c:
                        if isinstance(block, dict) and "text" in block:
                            parts.append(str(block["text"]))
    # Anthropic sometimes uses system
    sys = body.get("system")
    if isinstance(sys, str):
        parts.append(sys)
    elif isinstance(sys, list):
        for block in sys:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
    return "\n".join(parts)


def classify(body: dict[str, Any], cfg: GatewayConfig) -> tuple[str, str]:
    """Return (wanted_pool, heuristic_reason)."""
    text = _extract_text(body)
    lower = text.lower()
    for kw in cfg.router.expensive_keywords:
        if kw.lower() in lower:
            return "expensive", f"keyword:{kw}"
    if len(text) >= cfg.router.expensive_min_chars:
        return "expensive", f"long_prompt:{len(text)}chars"
    return "cheap", "default_cheap"


def pick_model(
    body: dict[str, Any],
    cfg: GatewayConfig,
    *,
    force_cheap: bool = False,
    client_model: str | None = None,
) -> RouteDecision:
    wanted, heur = classify(body, cfg)
    if force_cheap:
        pool = "cheap"
        reason = f"budget_force_cheap;would_have={wanted}/{heur}"
    else:
        pool = wanted
        reason = f"heuristic:{heur}"

    models = cfg.models.names(pool)
    if not models:
        models = cfg.models.cheap_names or ["mock-cheap"]
        pool = "cheap"
        reason += ";fallback_empty_pool"

    # If client named a model that sits in the chosen pool, keep it
    if client_model and client_model in models:
        return RouteDecision(model=client_model, pool=pool, reason=reason + ";client_model")

    return RouteDecision(model=models[0], pool=pool, reason=reason)

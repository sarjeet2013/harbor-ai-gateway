from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig


@dataclass
class RouteDecision:
    model: str
    pool: str  # cheap | expensive
    reason: str


def _extract_text(body: dict[str, Any], *, include_system: bool = False) -> str:
    """Text used for heuristic routing.

    Default skips Anthropic/OpenAI system prompts: Claude Code (and similar
    agents) inject large system instructions that contain words like
    "refactor" / "architecture" and would falsely force the expensive pool.
    """
    parts: list[str] = []
    # OpenAI chat / Anthropic messages — user (+ assistant) turns only by default
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").lower()
        if role == "system" and not include_system:
            continue
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
    if include_system:
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

    # Explicit Harbor alias always wins (Claude Code ANTHROPIC_MODEL, Cursor custom models).
    cheap_names = list(cfg.models.cheap_names or [])
    expensive_names = list(cfg.models.expensive_names or [])
    if client_model and client_model in cheap_names:
        return RouteDecision(
            model=client_model,
            pool="cheap",
            reason=reason + ";client_model",
        )
    if client_model and client_model in expensive_names and not force_cheap:
        return RouteDecision(
            model=client_model,
            pool="expensive",
            reason=reason + ";client_model",
        )

    models = cfg.models.names(pool)
    if not models:
        models = cheap_names or ["mock-cheap"]
        pool = "cheap"
        reason += ";fallback_empty_pool"

    return RouteDecision(model=models[0], pool=pool, reason=reason)

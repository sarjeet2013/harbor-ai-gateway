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


def _keyword_hit(lower: str, keywords: list[str]) -> bool:
    for kw in keywords:
        if kw and kw.lower() in lower:
            return True
    return False


def classify_pool(body: dict[str, Any], cfg: GatewayConfig) -> tuple[str, str]:
    """Return (wanted_pool, heuristic_reason) for expensive vs cheap."""
    text = _extract_text(body)
    lower = text.lower()
    if _keyword_hit(lower, cfg.router.expensive_keywords):
        return "expensive", "heuristic:expensive_keyword"
    if len(text) >= cfg.router.expensive_min_chars:
        return "expensive", "heuristic:expensive_min_chars"
    return "cheap", "heuristic:default_cheap"


def classify(body: dict[str, Any], cfg: GatewayConfig) -> tuple[str, str]:
    """Backward-compatible: (wanted_pool, reason_without_heuristic_prefix)."""
    pool, reason = classify_pool(body, cfg)
    # Strip "heuristic:" for callers that prepend it (legacy tests / callers)
    if reason.startswith("heuristic:"):
        return pool, reason[len("heuristic:") :]
    return pool, reason


def _resolve_cheap_by_name(cfg: GatewayConfig, name: str) -> str | None:
    """Return name if it is an enabled cheap-pool alias, else None."""
    if name and name in cfg.models.cheap_names:
        return name
    return None


def pick_cheap_model(body: dict[str, Any], cfg: GatewayConfig) -> tuple[str, str]:
    """Select a cheap-pool alias by task-class keywords (config-driven).

    Priority: coder_keywords → reason_keywords → default_cheap_model (by name).
    Falls back to the first enabled cheap entry if the named alias is missing.
    """
    text = _extract_text(body)
    lower = text.lower()
    cheap_names = list(cfg.models.cheap_names or [])

    if _keyword_hit(lower, cfg.router.coder_keywords):
        hit = _resolve_cheap_by_name(cfg, cfg.router.coder_model)
        if hit:
            return hit, "heuristic:coder_keyword"

    if _keyword_hit(lower, cfg.router.reason_keywords):
        hit = _resolve_cheap_by_name(cfg, cfg.router.reason_model)
        if hit:
            return hit, "heuristic:reason_keyword"

    default = _resolve_cheap_by_name(cfg, cfg.router.default_cheap_model)
    if default:
        return default, "heuristic:default_cheap"
    if cheap_names:
        return cheap_names[0], "heuristic:default_cheap"
    return "mock-cheap", "heuristic:default_cheap"


def pick_model(
    body: dict[str, Any],
    cfg: GatewayConfig,
    *,
    force_cheap: bool = False,
    client_model: str | None = None,
) -> RouteDecision:
    cheap_names = list(cfg.models.cheap_names or [])
    expensive_names = list(cfg.models.expensive_names or [])

    # Explicit Harbor alias always wins (Claude Code ANTHROPIC_MODEL, Cursor custom models).
    if client_model and client_model in cheap_names:
        return RouteDecision(
            model=client_model,
            pool="cheap",
            reason="client:model",
        )
    if client_model and client_model in expensive_names and not force_cheap:
        return RouteDecision(
            model=client_model,
            pool="expensive",
            reason="client:model",
        )

    wanted, pool_reason = classify_pool(body, cfg)
    if force_cheap:
        model, task_reason = pick_cheap_model(body, cfg)
        reason = f"budget_force_cheap;would_have={wanted}/{pool_reason};{task_reason}"
        return RouteDecision(model=model, pool="cheap", reason=reason)

    if wanted == "expensive":
        models = expensive_names
        if not models:
            model, _ = pick_cheap_model(body, cfg)
            return RouteDecision(
                model=model,
                pool="cheap",
                reason=pool_reason + ";fallback_empty_pool",
            )
        return RouteDecision(model=models[0], pool="expensive", reason=pool_reason)

    model, task_reason = pick_cheap_model(body, cfg)
    return RouteDecision(model=model, pool="cheap", reason=task_reason)

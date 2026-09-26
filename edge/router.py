from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import GatewayConfig
from .judge import JudgeResult, last_user_text, run_judge

_JUDGE_STRATEGIES = frozenset({"judge_then_heuristic", "judge"})


@dataclass
class RouteDecision:
    model: str
    pool: str  # cheap | expensive
    reason: str
    judge_ms: float | None = None
    judge_label: str | None = None
    judge_confidence: float | None = None

    def ledger_extras(self) -> dict[str, Any]:
        """Optional judge fields for the JSONL ledger (schemaless)."""
        out: dict[str, Any] = {}
        if self.judge_ms is not None:
            out["judge_ms"] = round(self.judge_ms, 1)
        if self.judge_label is not None:
            out["judge_label"] = self.judge_label
        if self.judge_confidence is not None:
            out["judge_confidence"] = self.judge_confidence
        return out


def _extract_text(body: dict[str, Any], *, include_system: bool = False) -> str:
    """Text used for heuristic routing: **last user turn only**.

    Matches the judge (`last_user_text`): prior user/assistant history and
    system prompts must not drive expensive_min_chars or task-class keywords
    (Claude Code multi-turn sessions otherwise grow into false expensive/coder).

    ``include_system`` is accepted for API compatibility but ignored — system
    is never scored for heuristics.
    """
    _ = include_system  # intentionally unused
    return last_user_text(body)


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


def _heuristic_decision(
    body: dict[str, Any],
    cfg: GatewayConfig,
    *,
    force_cheap: bool,
) -> RouteDecision:
    """Existing keyword / length rails (also used as judge fallback)."""
    cheap_names = list(cfg.models.cheap_names or [])
    expensive_names = list(cfg.models.expensive_names or [])

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


def _map_judge_label(
    label: str,
    cfg: GatewayConfig,
    *,
    force_cheap: bool,
) -> tuple[str, str, str] | None:
    """Map closed label → (model, pool, reason_suffix). None if unmapped."""
    cheap_names = list(cfg.models.cheap_names or [])
    expensive_names = list(cfg.models.expensive_names or [])

    if label == "chat":
        model = _resolve_cheap_by_name(cfg, cfg.router.default_cheap_model)
        if not model and cheap_names:
            model = cheap_names[0]
        if model:
            return model, "cheap", "judge:chat"
        return None

    if label == "coder":
        model = _resolve_cheap_by_name(cfg, cfg.router.coder_model)
        if model:
            return model, "cheap", "judge:coder"
        # Missing coder alias → default cheap still counts as judge:coder intent miss
        model = _resolve_cheap_by_name(cfg, cfg.router.default_cheap_model)
        if model:
            return model, "cheap", "judge:coder"
        if cheap_names:
            return cheap_names[0], "cheap", "judge:coder"
        return None

    if label == "reason":
        model = _resolve_cheap_by_name(cfg, cfg.router.reason_model)
        if model:
            return model, "cheap", "judge:reason"
        model = _resolve_cheap_by_name(cfg, cfg.router.default_cheap_model)
        if model:
            return model, "cheap", "judge:reason"
        if cheap_names:
            return cheap_names[0], "cheap", "judge:reason"
        return None

    if label == "expensive":
        if force_cheap:
            return None  # caller falls back within cheap
        if expensive_names:
            return expensive_names[0], "expensive", "judge:expensive"
        model = _resolve_cheap_by_name(cfg, cfg.router.default_cheap_model)
        if model:
            return model, "cheap", "judge:expensive;fallback_empty_pool"
        if cheap_names:
            return cheap_names[0], "cheap", "judge:expensive;fallback_empty_pool"
        return None

    return None


def _with_judge_meta(
    decision: RouteDecision,
    result: JudgeResult,
) -> RouteDecision:
    decision.judge_ms = result.ms
    decision.judge_label = result.label
    decision.judge_confidence = result.confidence
    return decision


def _judge_then_heuristic(
    body: dict[str, Any],
    cfg: GatewayConfig,
    *,
    force_cheap: bool,
    http_client: httpx.Client | None,
) -> RouteDecision:
    judge_cfg = cfg.router.judge
    # enabled defaults true for judge_then_heuristic; honor explicit false
    if not judge_cfg.enabled:
        d = _heuristic_decision(body, cfg, force_cheap=force_cheap)
        d.reason = f"judge_fallback:{d.reason}"
        return d

    result = run_judge(body, judge_cfg, http_client=http_client)
    min_conf = float(judge_cfg.min_confidence)

    use_fallback = (
        not result.ok
        or result.label is None
        or result.confidence is None
        or result.confidence < min_conf
    )

    if not use_fallback and result.label == "expensive" and force_cheap:
        # Budget forces cheap: keep judge meta, pick within cheap via heuristic
        model, task_reason = pick_cheap_model(body, cfg)
        d = RouteDecision(
            model=model,
            pool="cheap",
            reason=(
                f"budget_force_cheap;judge:expensive;"
                f"would_have=expensive/judge:expensive;{task_reason}"
            ),
        )
        return _with_judge_meta(d, result)

    if use_fallback:
        inner = _heuristic_decision(body, cfg, force_cheap=force_cheap)
        # Prefix existing heuristic reason
        d = RouteDecision(
            model=inner.model,
            pool=inner.pool,
            reason=f"judge_fallback:{inner.reason}",
            judge_ms=result.ms,
            judge_label=result.label,
            judge_confidence=result.confidence,
        )
        return d

    assert result.label is not None
    mapped = _map_judge_label(result.label, cfg, force_cheap=force_cheap)
    if mapped is None:
        inner = _heuristic_decision(body, cfg, force_cheap=force_cheap)
        return _with_judge_meta(
            RouteDecision(
                model=inner.model,
                pool=inner.pool,
                reason=f"judge_fallback:{inner.reason}",
            ),
            result,
        )

    model, pool, reason = mapped
    if force_cheap and pool == "expensive":
        # Should not happen (handled above); belt-and-suspenders
        model, task_reason = pick_cheap_model(body, cfg)
        d = RouteDecision(
            model=model,
            pool="cheap",
            reason=f"budget_force_cheap;{reason};{task_reason}",
        )
        return _with_judge_meta(d, result)

    return _with_judge_meta(
        RouteDecision(model=model, pool=pool, reason=reason),
        result,
    )


def pick_model(
    body: dict[str, Any],
    cfg: GatewayConfig,
    *,
    force_cheap: bool = False,
    client_model: str | None = None,
    http_client: httpx.Client | None = None,
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

    strategy = (cfg.router.strategy or "").strip().lower()
    if strategy in _JUDGE_STRATEGIES:
        return _judge_then_heuristic(
            body,
            cfg,
            force_cheap=force_cheap,
            http_client=http_client,
        )

    # heuristic_cost_first (and unknown strategies → heuristic)
    return _heuristic_decision(body, cfg, force_cheap=force_cheap)

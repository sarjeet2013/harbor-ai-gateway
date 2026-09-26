"""LLM judge for Harbor routing — closed-label classify via local Ollama.

Used when ``router.strategy`` is ``judge_then_heuristic``. Soft-fails to the
caller on timeout / HTTP / parse errors so the router can fall back to
heuristic keyword rails.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import JudgeConfig

logger = logging.getLogger(__name__)

LABELS = frozenset({"chat", "coder", "reason", "expensive"})

_SYSTEM_PROMPT = """\
Classify the user request into exactly one label. Ignore system/agent chrome.
Labels only: chat | coder | reason | expensive
- chat: greetings, short Q&A, casual, summaries without hard reasoning
- coder: write/fix/implement/review code, tests, stack traces, PRs
- reason: step-by-step math/logic/proofs, careful multi-step reasoning
- expensive: large refactors, system design, deep architecture, long complex analysis
Reply with JSON only: {"label":"<one>","confidence":0.0-1.0}
"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


@dataclass
class JudgeResult:
    """Outcome of one judge call (success or soft-failure)."""

    ok: bool
    label: str | None = None
    confidence: float | None = None
    ms: float = 0.0
    error: str | None = None


def ollama_model_name(litellm_model: str) -> str:
    """Strip LiteLLM provider prefixes for Ollama ``/api/chat``."""
    name = (litellm_model or "").strip()
    for prefix in ("ollama_chat/", "ollama/", "openai/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def last_user_text(body: dict[str, Any]) -> str:
    """Last user turn only (messages / Responses input). Skips system."""
    messages = body.get("messages")
    if isinstance(messages, list):
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            if (m.get("role") or "").lower() != "user":
                continue
            return _content_to_text(m.get("content"))
    inp = body.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        for item in reversed(inp):
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                role = (item.get("role") or "").lower()
                if role and role != "user":
                    continue
                text = _content_to_text(item.get("content") or item.get("text"))
                if text.strip():
                    return text
    return ""


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" or "text" in block:
                    parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return ""


def tools_present(body: dict[str, Any]) -> bool:
    tools = body.get("tools")
    if isinstance(tools, list) and len(tools) > 0:
        return True
    if body.get("tool_choice") not in (None, "none", "auto"):
        # explicit tool_choice other than none/auto still signals tools context
        if body.get("tool_choice"):
            return True
    return False


def build_user_payload(body: dict[str, Any], *, max_chars: int = 1500) -> str:
    text = last_user_text(body).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    approx = len(last_user_text(body))
    tools = "yes" if tools_present(body) else "no"
    return f"chars≈{approx} tools={tools}\n{text or '(empty)'}"


def parse_judge_json(raw: str) -> tuple[str, float] | None:
    """Extract closed label + confidence from model text. None on failure."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    candidates.append(text)
    for obj_m in _JSON_OBJ_RE.finditer(text):
        candidates.append(obj_m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        label = str(data.get("label") or "").strip().lower()
        if label not in LABELS:
            continue
        try:
            conf = float(data.get("confidence"))
        except (TypeError, ValueError):
            continue
        if conf < 0.0:
            conf = 0.0
        if conf > 1.0:
            conf = 1.0
        return label, conf
    return None


def run_judge(
    body: dict[str, Any],
    cfg: JudgeConfig,
    *,
    http_client: httpx.Client | None = None,
) -> JudgeResult:
    """Call Ollama chat with think disabled; return structured result (never raises)."""
    if not cfg.enabled:
        return JudgeResult(ok=False, error="judge_disabled", ms=0.0)

    model = ollama_model_name(cfg.litellm_model)
    base = (cfg.api_base or "").rstrip("/")
    if not base or not model:
        return JudgeResult(ok=False, error="judge_misconfigured", ms=0.0)

    timeout_s = max(cfg.timeout_ms, 1) / 1000.0
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "think": False,
        "options": {
            "temperature": 0,
            "num_predict": 48,
        },
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": build_user_payload(body)},
        ],
    }
    url = f"{base}/api/chat"
    t0 = time.perf_counter()
    try:
        if http_client is not None:
            resp = http_client.post(url, json=payload, timeout=timeout_s)
        else:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(url, json=payload)
        ms = (time.perf_counter() - t0) * 1000.0
        if resp.status_code >= 400:
            return JudgeResult(
                ok=False,
                ms=ms,
                error=f"http_{resp.status_code}",
            )
        try:
            data = resp.json()
        except Exception:
            return JudgeResult(ok=False, ms=ms, error="invalid_json_body")
        content = ""
        msg = data.get("message") if isinstance(data, dict) else None
        if isinstance(msg, dict):
            content = str(msg.get("content") or "")
        elif isinstance(data, dict) and "response" in data:
            content = str(data.get("response") or "")
        parsed = parse_judge_json(content)
        if parsed is None:
            return JudgeResult(ok=False, ms=ms, error="parse_failed")
        label, conf = parsed
        return JudgeResult(ok=True, label=label, confidence=conf, ms=ms)
    except httpx.TimeoutException:
        ms = (time.perf_counter() - t0) * 1000.0
        return JudgeResult(ok=False, ms=ms, error="timeout")
    except Exception as exc:  # noqa: BLE001 — soft-fail to heuristic
        ms = (time.perf_counter() - t0) * 1000.0
        logger.debug("judge failed: %s", exc)
        return JudgeResult(ok=False, ms=ms, error=f"error:{type(exc).__name__}")

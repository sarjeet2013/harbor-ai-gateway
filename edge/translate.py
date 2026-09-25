"""Translate Anthropic Messages + OpenAI Responses <-> OpenAI Chat Completions.

LiteLLM + Ollama reliably support /v1/chat/completions. Bridging /v1/messages
and /v1/responses through LiteLLM varies by LiteLLM version and provider
prefix (e.g. openai/ may passthrough /v1/responses and 404). The edge owns
these client surfaces and always forwards chat/completions upstream in
litellm mode, then reshapes the response.
"""
from __future__ import annotations

import time
import uuid
from typing import Any


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for block in content:
            if isinstance(block, str):
                bits.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "input_text", "output_text"):
                    bits.append(str(block.get("text", "")))
                elif "text" in block:
                    bits.append(str(block["text"]))
        return "".join(bits)
    return str(content)


def anthropic_to_chat(body: dict[str, Any], model: str) -> dict[str, Any]:
    """Anthropic /v1/messages body -> OpenAI chat.completions body."""
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = _content_to_text(system)
        if text.strip():
            messages.append({"role": "system", "content": text})

    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        messages.append({"role": role, "content": _content_to_text(m.get("content"))})

    if not messages:
        messages = [{"role": "user", "content": ""}]

    out: dict[str, Any] = {"model": model, "messages": messages}
    # Anthropic requires max_tokens; default so LiteLLM never TypeErrors
    out["max_tokens"] = int(body.get("max_tokens") or 1024)
    for key in ("temperature", "top_p", "stream", "stop", "user"):
        if key in body and body[key] is not None:
            out[key] = body[key]
    return out


def chat_to_anthropic(chat: dict[str, Any], model: str) -> dict[str, Any]:
    """OpenAI chat.completions response -> Anthropic message response."""
    text = ""
    stop_reason = "end_turn"
    choices = chat.get("choices") or []
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        text = _content_to_text(msg.get("content"))
        fr = choices[0].get("finish_reason")
        if fr == "length":
            stop_reason = "max_tokens"
        elif fr == "stop":
            stop_reason = "end_turn"
    usage = chat.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or usage.get("output_tokens") or 0,
        },
    }


def responses_to_chat(body: dict[str, Any], model: str) -> dict[str, Any]:
    """OpenAI /v1/responses body -> OpenAI chat.completions body."""
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = item.get("role") or "user"
                if "content" in item:
                    messages.append({"role": role, "content": _content_to_text(item.get("content"))})
                elif item.get("type") == "message":
                    messages.append(
                        {
                            "role": item.get("role") or "user",
                            "content": _content_to_text(item.get("content")),
                        }
                    )
                elif "text" in item:
                    messages.append({"role": "user", "content": str(item["text"])})

    if not messages:
        messages = [{"role": "user", "content": ""}]

    out: dict[str, Any] = {"model": model, "messages": messages}
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    elif body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p", "stream", "user"):
        if key in body and body[key] is not None:
            out[key] = body[key]
    return out


def chat_to_responses(chat: dict[str, Any], model: str) -> dict[str, Any]:
    """OpenAI chat.completions response -> OpenAI Responses API object."""
    text = ""
    choices = chat.get("choices") or []
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        text = _content_to_text(msg.get("content"))
    usage = chat.get("usage") or {}
    rid = f"resp_{uuid.uuid4().hex[:12]}"
    return {
        "id": rid,
        "object": "response",
        "created_at": int(chat.get("created") or time.time()),
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex[:8]}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
                "status": "completed",
            }
        ],
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or usage.get("output_tokens") or 0,
            "total_tokens": usage.get("total_tokens")
            or (
                (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
            ),
        },
    }

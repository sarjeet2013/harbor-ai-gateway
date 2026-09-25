"""Translate Anthropic Messages + OpenAI Responses <-> OpenAI Chat Completions.

LiteLLM + Ollama reliably support /v1/chat/completions. Bridging /v1/messages
and /v1/responses through LiteLLM varies by LiteLLM version and provider
prefix (e.g. openai/ may passthrough /v1/responses and 404). The edge owns
these client surfaces and always forwards chat/completions upstream in
litellm mode, then reshapes the response.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
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


def format_anthropic_sse(event: str, data: dict[str, Any]) -> bytes:
    """One Anthropic Messages SSE event (event: + data: + blank line)."""
    return (
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


def _parse_openai_chat_sse_objects(raw: bytes) -> list[dict[str, Any]]:
    """Parse OpenAI chat.completion.chunk JSON objects from an SSE byte buffer."""
    objs: list[dict[str, Any]] = []
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def openai_chat_chunks_to_anthropic_sse(
    chat_objs: list[dict[str, Any]], *, model: str
) -> list[bytes]:
    """Build Anthropic Messages SSE events from OpenAI chat chunk objects."""
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"
    text_parts: list[str] = []

    for obj in chat_objs:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            input_tokens = int(
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or input_tokens
                or 0
            )
            output_tokens = int(
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or output_tokens
                or 0
            )
        choices = obj.get("choices") or []
        if not choices:
            continue
        ch0 = choices[0] or {}
        fr = ch0.get("finish_reason")
        if fr == "length":
            stop_reason = "max_tokens"
        elif fr == "stop":
            stop_reason = "end_turn"
        delta = ch0.get("delta") or {}
        piece = delta.get("content")
        if piece:
            text_parts.append(str(piece))

    events: list[bytes] = [
        format_anthropic_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": 0,
                    },
                },
            },
        ),
        format_anthropic_sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    # Emit text in one or more deltas (chunk by ~40 chars for friendlier UX)
    full = "".join(text_parts)
    if not full:
        full = ""
    step = 48
    for i in range(0, max(len(full), 1), step):
        piece = full[i : i + step]
        if not piece and i > 0:
            break
        events.append(
            format_anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        )
    events.append(
        format_anthropic_sse(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        )
    )
    events.append(
        format_anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {
                    "output_tokens": output_tokens or max(1, len(full) // 4),
                    "input_tokens": input_tokens,
                },
            },
        )
    )
    events.append(format_anthropic_sse("message_stop", {"type": "message_stop"}))
    return events


def openai_chat_sse_to_anthropic_sse_sync(
    chunks: Iterator[bytes], *, model: str
) -> Iterator[bytes]:
    """Sync: collect OpenAI chat SSE, emit Anthropic Messages SSE."""
    collected = bytearray()
    for chunk in chunks:
        collected.extend(chunk)
    yield from openai_chat_chunks_to_anthropic_sse(
        _parse_openai_chat_sse_objects(bytes(collected)), model=model
    )


async def openai_chat_sse_to_anthropic_sse(
    chunks: AsyncIterator[bytes], *, model: str
) -> AsyncIterator[bytes]:
    """Async: collect OpenAI chat SSE, emit Anthropic Messages SSE.

    Collects the upstream stream then remaps. True token-at-a-time remap can
    follow; Claude Code needs correct Anthropic SSE event types more than
    mid-token latency for Harbor v1.
    """
    collected = bytearray()
    async for chunk in chunks:
        collected.extend(chunk)
    for event in openai_chat_chunks_to_anthropic_sse(
        _parse_openai_chat_sse_objects(bytes(collected)), model=model
    ):
        yield event

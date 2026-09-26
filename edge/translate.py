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
    # Anthropic requires max_tokens; Claude Code often sends 32k+. Clamp so
    # OpenAI gpt-4o-mini (16k completion cap) and similar don't 400.
    _MAX_COMPLETION_TOKENS = 16384
    requested = int(body.get("max_tokens") or 1024)
    out["max_tokens"] = max(1, min(requested, _MAX_COMPLETION_TOKENS))
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


def openai_chat_sse_to_completion(raw: bytes) -> dict[str, Any] | None:
    """Rebuild a chat.completion JSON body from OpenAI chat SSE bytes.

    Returns None if the stream looks incomplete (no ``[DONE]``) or has no
    usable chunks — callers must not cache in that case.
    """
    text = raw.decode("utf-8", errors="replace")
    if "data: [DONE]" not in text and "data:[DONE]" not in text:
        return None
    objs = _parse_openai_chat_sse_objects(raw)
    if not objs:
        return None

    cid = ""
    created = int(time.time())
    model = ""
    role = "assistant"
    content_parts: list[str] = []
    finish_reason = "stop"
    usage: dict[str, Any] = {}

    for obj in objs:
        if not cid and obj.get("id"):
            cid = str(obj["id"])
        if obj.get("created") is not None:
            try:
                created = int(obj["created"])
            except (TypeError, ValueError):
                pass
        if obj.get("model"):
            model = str(obj["model"])
        u = obj.get("usage")
        if isinstance(u, dict) and u:
            usage = u
        choices = obj.get("choices") or []
        if not choices:
            continue
        ch0 = choices[0] or {}
        fr = ch0.get("finish_reason")
        if fr:
            finish_reason = str(fr)
        delta = ch0.get("delta") or {}
        if delta.get("role"):
            role = str(delta["role"])
        piece = delta.get("content")
        if piece:
            content_parts.append(str(piece))
        # Some providers put a full message on non-delta chunks
        msg = ch0.get("message") or {}
        if isinstance(msg, dict) and msg.get("content") and not content_parts:
            content_parts.append(_content_to_text(msg.get("content")))

    if not cid:
        cid = f"chatcmpl_{uuid.uuid4().hex[:12]}"
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": role, "content": "".join(content_parts)},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _parse_anthropic_sse_events(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse Anthropic ``event:`` / ``data:`` SSE into (event_name, data_dict)."""
    events: list[tuple[str, dict[str, Any]]] = []
    text = raw.decode("utf-8", errors="replace")
    event_name = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                event_name = ""
                continue
            if isinstance(obj, dict):
                name = event_name or str(obj.get("type") or "")
                events.append((name, obj))
            event_name = ""
            continue
        if line.strip() == "":
            event_name = ""
    return events


def anthropic_sse_to_message(raw: bytes) -> dict[str, Any] | None:
    """Rebuild an Anthropic Messages JSON body from Anthropic SSE bytes.

    Returns None if ``message_stop`` is missing (incomplete stream).
    """
    events = _parse_anthropic_sse_events(raw)
    if not any(name == "message_stop" for name, _ in events):
        return None

    msg: dict[str, Any] = {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": ""}],
        "model": "",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    text_parts: list[str] = []

    for name, data in events:
        if name == "message_start":
            m = data.get("message") if isinstance(data.get("message"), dict) else data
            if isinstance(m, dict):
                if m.get("id"):
                    msg["id"] = str(m["id"])
                if m.get("model"):
                    msg["model"] = str(m["model"])
                if m.get("role"):
                    msg["role"] = str(m["role"])
                u = m.get("usage")
                if isinstance(u, dict):
                    msg["usage"] = {
                        "input_tokens": int(u.get("input_tokens") or 0),
                        "output_tokens": int(u.get("output_tokens") or 0),
                    }
        elif name == "content_block_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text_parts.append(str(delta.get("text") or ""))
            elif isinstance(delta, dict) and "text" in delta:
                text_parts.append(str(delta["text"]))
        elif name == "message_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict) and delta.get("stop_reason"):
                msg["stop_reason"] = str(delta["stop_reason"])
            u = data.get("usage")
            if isinstance(u, dict):
                if u.get("input_tokens") is not None:
                    msg["usage"]["input_tokens"] = int(u.get("input_tokens") or 0)
                if u.get("output_tokens") is not None:
                    msg["usage"]["output_tokens"] = int(u.get("output_tokens") or 0)

    full = "".join(text_parts)
    msg["content"] = [{"type": "text", "text": full}]
    return msg


def iter_openai_sse_from_cached_chat(body: dict[str, Any]) -> Iterator[bytes]:
    """Synthesize OpenAI chat.completion.chunk SSE from a cached chat.completion body."""
    cid = str(body.get("id") or f"chatcmpl_{uuid.uuid4().hex[:12]}")
    created = int(body.get("created") or time.time())
    model = str(body.get("model") or "")
    text = ""
    finish_reason = "stop"
    choices = body.get("choices") or []
    if choices:
        ch0 = choices[0] or {}
        msg = ch0.get("message") or {}
        text = _content_to_text(msg.get("content"))
        if ch0.get("finish_reason"):
            finish_reason = str(ch0["finish_reason"])
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}

    def _line(obj: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")

    # Emit content in modest chunks for friendlier client UX
    step = 48
    first = True
    if not text:
        yield _line(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
        )
        first = False
    else:
        for i in range(0, len(text), step):
            piece = text[i : i + step]
            delta: dict[str, Any] = {"content": piece}
            if first:
                delta["role"] = "assistant"
                first = False
            yield _line(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None}
                    ],
                }
            )

    final: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": finish_reason}
        ],
    }
    if usage:
        final["usage"] = usage
    yield _line(final)
    yield b"data: [DONE]\n\n"


def iter_anthropic_sse_from_cached_message(body: dict[str, Any]) -> Iterator[bytes]:
    """Synthesize Anthropic Messages SSE from a cached Anthropic message body."""
    msg_id = str(body.get("id") or f"msg_{uuid.uuid4().hex[:12]}")
    model = str(body.get("model") or "")
    text = ""
    content = body.get("content")
    if isinstance(content, list):
        text = _content_to_text(content)
    elif isinstance(content, str):
        text = content
    stop_reason = str(body.get("stop_reason") or "end_turn")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or max(1, len(text) // 4) if text else 0)

    yield format_anthropic_sse(
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
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        },
    )
    yield format_anthropic_sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    step = 48
    if not text:
        yield format_anthropic_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": ""},
            },
        )
    else:
        for i in range(0, len(text), step):
            piece = text[i : i + step]
            yield format_anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
    yield format_anthropic_sse(
        "content_block_stop", {"type": "content_block_stop", "index": 0}
    )
    yield format_anthropic_sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {
                "output_tokens": output_tokens,
                "input_tokens": input_tokens,
            },
        },
    )
    yield format_anthropic_sse("message_stop", {"type": "message_stop"})


def iter_anthropic_sse_from_cached_chat(body: dict[str, Any], *, model: str) -> Iterator[bytes]:
    """Synthesize Anthropic SSE from a cached OpenAI chat.completion body."""
    yield from iter_anthropic_sse_from_cached_message(chat_to_anthropic(body, model))


def format_responses_sse(event: str, data: dict[str, Any]) -> bytes:
    """One OpenAI Responses SSE event (event: + data: + blank line)."""
    payload = dict(data)
    payload.setdefault("type", event)
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


def _responses_usage_from_chat_objs(chat_objs: list[dict[str, Any]]) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    for obj in chat_objs:
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
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
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def openai_chat_chunks_to_responses_sse(
    chat_objs: list[dict[str, Any]], *, model: str
) -> list[bytes]:
    """Build OpenAI Responses SSE events from OpenAI chat chunk objects."""
    rid = f"resp_{uuid.uuid4().hex[:12]}"
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    created_at = int(time.time())
    text_parts: list[str] = []
    out_model = model

    for obj in chat_objs:
        if obj.get("model"):
            out_model = str(obj["model"])
        if obj.get("created") is not None:
            try:
                created_at = int(obj["created"])
            except (TypeError, ValueError):
                pass
        choices = obj.get("choices") or []
        if not choices:
            continue
        ch0 = choices[0] or {}
        delta = ch0.get("delta") or {}
        piece = delta.get("content")
        if piece:
            text_parts.append(str(piece))
        msg = ch0.get("message") or {}
        if isinstance(msg, dict) and msg.get("content") and not text_parts:
            text_parts.append(_content_to_text(msg.get("content")))

    full = "".join(text_parts)
    usage = _responses_usage_from_chat_objs(chat_objs)
    if usage["output_tokens"] == 0 and full:
        usage["output_tokens"] = max(1, len(full) // 4)
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

    partial = {
        "id": rid,
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "model": out_model,
        "output": [],
        "usage": None,
    }
    completed_item = {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": full}],
        "status": "completed",
    }
    completed = {
        "id": rid,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": out_model,
        "output": [completed_item],
        "usage": usage,
    }

    seq = 0

    def _next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq

    events: list[bytes] = [
        format_responses_sse(
            "response.created",
            {"type": "response.created", "response": partial, "sequence_number": _next_seq()},
        ),
        format_responses_sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": msg_id,
                    "status": "in_progress",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                },
                "sequence_number": _next_seq(),
            },
        ),
        format_responses_sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
                "sequence_number": _next_seq(),
            },
        ),
    ]

    step = 48
    if not full:
        events.append(
            format_responses_sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": msg_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "",
                    "sequence_number": _next_seq(),
                    "logprobs": [],
                },
            )
        )
    else:
        for i in range(0, len(full), step):
            piece = full[i : i + step]
            events.append(
                format_responses_sse(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": msg_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": piece,
                        "sequence_number": _next_seq(),
                        "logprobs": [],
                    },
                )
            )

    events.append(
        format_responses_sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "text": full,
                "sequence_number": _next_seq(),
                "logprobs": [],
            },
        )
    )
    events.append(
        format_responses_sse(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": full, "annotations": []},
                "sequence_number": _next_seq(),
            },
        )
    )
    events.append(
        format_responses_sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": completed_item,
                "sequence_number": _next_seq(),
            },
        )
    )
    events.append(
        format_responses_sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": completed,
                "sequence_number": _next_seq(),
            },
        )
    )
    return events


def openai_chat_sse_to_responses_sse_sync(
    chunks: Iterator[bytes], *, model: str
) -> Iterator[bytes]:
    """Sync: collect OpenAI chat SSE, emit OpenAI Responses SSE."""
    collected = bytearray()
    for chunk in chunks:
        collected.extend(chunk)
    yield from openai_chat_chunks_to_responses_sse(
        _parse_openai_chat_sse_objects(bytes(collected)), model=model
    )


async def openai_chat_sse_to_responses_sse(
    chunks: AsyncIterator[bytes], *, model: str
) -> AsyncIterator[bytes]:
    """Async: collect OpenAI chat SSE, emit OpenAI Responses SSE.

    Collects the upstream stream then remaps (same collect-then-remap approach
    as Anthropic messages). Token-at-a-time Responses remap can follow.
    """
    collected = bytearray()
    async for chunk in chunks:
        collected.extend(chunk)
    for event in openai_chat_chunks_to_responses_sse(
        _parse_openai_chat_sse_objects(bytes(collected)), model=model
    ):
        yield event


def _parse_responses_sse_events(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse Responses ``event:`` / ``data:`` SSE into (event_name, data_dict)."""
    events: list[tuple[str, dict[str, Any]]] = []
    text = raw.decode("utf-8", errors="replace")
    event_name = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                event_name = ""
                continue
            if isinstance(obj, dict):
                name = event_name or str(obj.get("type") or "")
                events.append((name, obj))
            event_name = ""
            continue
        if line.strip() == "":
            event_name = ""
    return events


def responses_sse_to_response(raw: bytes) -> dict[str, Any] | None:
    """Rebuild an OpenAI Responses JSON body from Responses SSE bytes.

    Returns None if ``response.completed`` is missing (incomplete stream).
    """
    events = _parse_responses_sse_events(raw)
    completed: dict[str, Any] | None = None
    for name, data in events:
        if name == "response.completed":
            resp = data.get("response")
            if isinstance(resp, dict):
                completed = resp
    if completed is None:
        return None
    # Ensure minimal shape for cache / non-stream consumers
    if "object" not in completed:
        completed["object"] = "response"
    if "status" not in completed:
        completed["status"] = "completed"
    return completed


def iter_responses_sse_from_cached_response(body: dict[str, Any]) -> Iterator[bytes]:
    """Synthesize OpenAI Responses SSE from a cached Responses body."""
    model = str(body.get("model") or "")
    created_at = int(body.get("created_at") or body.get("created") or time.time())
    text = ""
    output = body.get("output")
    if isinstance(output, list) and output:
        item0 = output[0] or {}
        if isinstance(item0, dict):
            text = _content_to_text(item0.get("content"))
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or (max(1, len(text) // 4) if text else 0)
    )
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))

    chat_like = [
        {
            "id": f"chatcmpl_{uuid.uuid4().hex[:8]}",
            "object": "chat.completion.chunk",
            "created": created_at,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": f"chatcmpl_{uuid.uuid4().hex[:8]}",
            "object": "chat.completion.chunk",
            "created": created_at,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
        },
    ]
    yield from openai_chat_chunks_to_responses_sse(chat_like, model=model)



def iter_responses_sse_from_cached_chat(body: dict[str, Any], *, model: str) -> Iterator[bytes]:
    """Synthesize Responses SSE from a cached OpenAI chat.completion body."""
    yield from iter_responses_sse_from_cached_response(chat_to_responses(body, model))

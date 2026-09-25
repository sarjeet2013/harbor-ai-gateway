from __future__ import annotations

import time
import uuid
from typing import Any


def _echo_text(body: dict[str, Any], model: str) -> str:
    # Prefer last user message content
    msgs = body.get("messages") or []
    last = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                last = c
            elif isinstance(c, list):
                bits = []
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        bits.append(str(b.get("text", "")))
                    elif isinstance(b, str):
                        bits.append(b)
                last = " ".join(bits)
            break
    if not last:
        inp = body.get("input")
        if isinstance(inp, str):
            last = inp
        elif isinstance(inp, list) and inp:
            last = str(inp[0])
    snippet = (last or "(empty)").strip().replace("\n", " ")[:200]
    return f"[mock:{model}] echo: {snippet}"


def mock_chat_completion(body: dict[str, Any], model: str) -> dict[str, Any]:
    text = _echo_text(body, model)
    prompt_chars = len(str(body.get("messages", "")))
    completion_chars = len(text)
    return {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, prompt_chars // 4),
            "completion_tokens": max(1, completion_chars // 4),
            "total_tokens": max(2, (prompt_chars + completion_chars) // 4),
        },
    }


def mock_anthropic_message(body: dict[str, Any], model: str) -> dict[str, Any]:
    text = _echo_text(body, model)
    prompt_chars = len(str(body.get("messages", ""))) + len(str(body.get("system", "")))
    return {
        "id": f"msg_mock_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(1, prompt_chars // 4),
            "output_tokens": max(1, len(text) // 4),
        },
    }


def mock_responses(body: dict[str, Any], model: str) -> dict[str, Any]:
    text = _echo_text(body, model)
    rid = f"resp_mock_{uuid.uuid4().hex[:12]}"
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
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
            "input_tokens": max(1, len(str(body.get("input", ""))) // 4),
            "output_tokens": max(1, len(text) // 4),
            "total_tokens": max(2, (len(str(body.get("input", ""))) + len(text)) // 4),
        },
    }

"""Unit tests for Anthropic/Responses <-> chat translation."""
from edge.translate import (
    anthropic_to_chat,
    chat_to_anthropic,
    chat_to_responses,
    openai_chat_chunks_to_responses_sse,
    responses_sse_to_response,
    responses_to_chat,
)


def test_anthropic_roundtrip():
    body = {
        "model": "ollama-cheap",
        "max_tokens": 64,
        "system": "be brief",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    chat_req = anthropic_to_chat(body, "ollama-cheap")
    assert chat_req["model"] == "ollama-cheap"
    assert chat_req["max_tokens"] == 64
    assert chat_req["messages"][0] == {"role": "system", "content": "be brief"}
    assert chat_req["messages"][1]["content"] == "hi"

    chat_resp = {
        "id": "chatcmpl-x",
        "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    out = chat_to_anthropic(chat_resp, "ollama-cheap")
    assert out["type"] == "message"
    assert out["content"][0]["text"] == "hello"
    assert out["usage"]["input_tokens"] == 3


def test_anthropic_default_max_tokens():
    chat_req = anthropic_to_chat(
        {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "m"
    )
    assert chat_req["max_tokens"] == 1024


def test_responses_roundtrip():
    body = {"model": "ollama-cheap", "input": "hi there"}
    chat_req = responses_to_chat(body, "ollama-cheap")
    assert chat_req["messages"] == [{"role": "user", "content": "hi there"}]

    chat_resp = {
        "id": "chatcmpl-y",
        "created": 1,
        "choices": [{"message": {"role": "assistant", "content": "yo"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    out = chat_to_responses(chat_resp, "ollama-cheap")
    assert out["object"] == "response"
    assert out["status"] == "completed"
    assert out["output"][0]["content"][0]["text"] == "yo"


def test_anthropic_clamps_max_tokens():
    chat_req = anthropic_to_chat(
        {
            "model": "m",
            "max_tokens": 32000,
            "messages": [{"role": "user", "content": "hi"}],
        },
        "m",
    )
    assert chat_req["max_tokens"] == 16384


def test_responses_sse_roundtrip_from_chat_chunks():
    chunks = [
        {
            "id": "chatcmpl-z",
            "object": "chat.completion.chunk",
            "created": 10,
            "model": "m",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-z",
            "object": "chat.completion.chunk",
            "created": 10,
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        },
    ]
    events = openai_chat_chunks_to_responses_sse(chunks, model="m")
    raw = b"".join(events)
    assert b"event: response.created" in raw
    assert b"event: response.completed" in raw
    assert b"response.output_text.delta" in raw
    rebuilt = responses_sse_to_response(raw)
    assert rebuilt is not None
    assert rebuilt["object"] == "response"
    assert rebuilt["status"] == "completed"
    assert rebuilt["output"][0]["content"][0]["text"] == "hi"
    assert rebuilt["usage"]["input_tokens"] == 2

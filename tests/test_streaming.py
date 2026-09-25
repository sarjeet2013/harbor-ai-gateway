"""Chat completions SSE streaming (v1)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from edge.cache import should_bypass_cache
from edge.config import CacheConfig, GatewayConfig, ModelsConfig, ModelSpec
from edge.main import create_app


def _mock_cfg(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(
        listen="127.0.0.1:0",
        upstream_mode="mock",
        ledger_path=str(tmp_path / "ledger.jsonl"),
        budget_state_path=str(tmp_path / "budget.json"),
        models=ModelsConfig(
            cheap=[ModelSpec(name="mock-cheap")],
            expensive=[ModelSpec(name="mock-expensive")],
        ),
        cache=CacheConfig(
            enabled=True,
            mode="exact",
            ttl_seconds=86400,
            max_entries=100,
            path=str(tmp_path / "cache"),
        ),
    )


def test_stream_chat_completions_sse(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "stream": True,
        "messages": [{"role": "user", "content": "hello stream"}],
    }
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 200
    ctype = r.headers.get("content-type", "")
    assert "text/event-stream" in ctype
    body = r.text
    assert "data:" in body
    assert "[DONE]" in body
    assert "mock:" in body
    assert "chat.completion.chunk" in body


def test_stream_writes_ledger(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "stream": True,
        "messages": [{"role": "user", "content": "ledger after stream"}],
    }
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 200
    # Drain body so the generator finally block runs
    _ = r.text
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert lines, "expected a ledger line after stream"
    row = json.loads(lines[-1])
    assert row["status"] == "ok"
    assert row["surface"] == "openai.chat.completions"
    assert row.get("stream") is True
    assert row["cache_hit"] is False
    assert row["model"] == "mock-cheap"
    # Mock SSE includes usage on the final chunk
    tokens = row.get("tokens") or {}
    assert tokens.get("prompt") or tokens.get("completion") or tokens.get("total")


def test_non_stream_chat_still_works(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "messages": [{"role": "user", "content": "hello non-stream"}],
    }
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/json")
    data = r.json()
    assert data["object"] == "chat.completion"
    assert "mock:" in data["choices"][0]["message"]["content"]


def test_stream_bypasses_cache(tmp_path: Path):
    assert should_bypass_cache({"stream": True}, None) is True
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "stream": True,
        "messages": [{"role": "user", "content": "same stream twice"}],
    }
    r1 = client.post("/v1/chat/completions", json=payload)
    r2 = client.post("/v1/chat/completions", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    # Distinct completion ids across streams (not a cached JSON body)
    assert r1.text != r2.text or "chatcmpl-mock-" in r1.text
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) >= 2
    for line in lines[-2:]:
        row = json.loads(line)
        assert row["cache_hit"] is False
        assert row.get("stream") is True


def test_messages_stream_anthropic_sse(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    r = client.post(
        "/v1/messages",
        json={
            "model": "mock-cheap",
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "hello messages stream"}],
        },
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    body = r.text
    assert "event: message_start" in body
    assert "event: content_block_delta" in body
    assert "event: message_stop" in body
    assert "text_delta" in body
    _ = body  # drain
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert lines
    row = json.loads(lines[-1])
    assert row["surface"] == "anthropic.messages"
    assert row.get("stream") is True
    assert row["status"] == "ok"


def test_responses_stream_returns_501(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    r = client.post(
        "/v1/responses",
        json={
            "model": "mock-cheap",
            "stream": True,
            "input": "no stream yet",
        },
    )
    assert r.status_code == 501

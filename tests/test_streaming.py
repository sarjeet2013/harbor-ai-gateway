"""Chat completions SSE streaming (v1)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import httpx

from edge.cache import ResponseCache, should_bypass_cache
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


def test_stream_alone_not_bypass():
    assert should_bypass_cache({"stream": True}, None) is False
    assert should_bypass_cache({"stream": True}, {"X-Cache-Bypass": "1"}) is True


def test_stream_exact_hit_returns_sse_no_upstream(tmp_path: Path):
    """stream=true exact hit synthesizes SSE and does not call upstream (mock still fine)."""
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    # Seed via non-stream so we know the stored body id
    seed = {
        "model": "mock-cheap",
        "messages": [{"role": "user", "content": "stream exact hit seed"}],
    }
    r0 = client.post("/v1/chat/completions", json=seed)
    assert r0.status_code == 200
    seeded_id = r0.json()["id"]
    seeded_content = r0.json()["choices"][0]["message"]["content"]

    # Stream request with same prompt → exact hit → SSE replay
    payload = {**seed, "stream": True}
    r1 = client.post("/v1/chat/completions", json=payload)
    assert r1.status_code == 200
    assert "text/event-stream" in r1.headers.get("content-type", "")
    body = r1.text
    assert "data:" in body
    assert "[DONE]" in body
    assert seeded_id in body
    assert "chat.completion.chunk" in body
    # Content from cache appears in deltas
    assert "stream exact hit seed" in body or seeded_content.split("echo:")[-1].strip()[:20] in body

    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    hit = json.loads(lines[-1])
    assert hit["cache_hit"] is True
    assert hit["cache_kind"] == "exact"
    assert hit.get("stream") is True
    assert hit["cost_usd"] == 0.0
    assert hit["tokens_saved"] > 0


def test_stream_miss_stores_usable_by_nonstream_and_stream(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "stream": True,
        "messages": [{"role": "user", "content": "stream miss then hit"}],
    }
    r1 = client.post("/v1/chat/completions", json=payload)
    assert r1.status_code == 200
    _ = r1.text
    miss = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert miss["cache_hit"] is False
    assert miss.get("stream") is True

    # Non-stream same prompt → exact hit from stream-stored entry
    r2 = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-cheap",
            "messages": [{"role": "user", "content": "stream miss then hit"}],
        },
    )
    assert r2.status_code == 200
    assert r2.headers.get("content-type", "").startswith("application/json")
    hit_ns = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert hit_ns["cache_hit"] is True
    assert hit_ns["cache_kind"] == "exact"

    # Stream again → SSE hit
    r3 = client.post("/v1/chat/completions", json=payload)
    assert r3.status_code == 200
    assert "[DONE]" in r3.text
    hit_s = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert hit_s["cache_hit"] is True
    assert hit_s.get("stream") is True


def test_stream_still_bypasses_with_header(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "stream": True,
        "messages": [{"role": "user", "content": "bypass stream store"}],
    }
    client.post("/v1/chat/completions", json=payload)
    # Force miss via bypass even though entry may exist from first call…
    # First call without bypass stored; second with bypass must miss.
    r2 = client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"X-Cache-Bypass": "1"},
    )
    assert r2.status_code == 200
    _ = r2.text
    row = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert row["cache_hit"] is False
    assert row.get("stream") is True


def test_stream_semantic_hit(tmp_path: Path):
    """stream=true semantic hit when mode=semantic and embeddings match."""
    shared = [0.8, 0.2, 0.1]

    class _FakeEmbedTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/embeddings"):
                return httpx.Response(200, json={"embedding": shared})
            return httpx.Response(404, json={"error": "not found"})

    cfg = _mock_cfg(tmp_path)
    cfg.cache = CacheConfig(
        enabled=True,
        mode="semantic",
        ttl_seconds=86400,
        max_entries=100,
        path=str(tmp_path / "cache"),
        embedding_model="nomic-embed-text",
        embedding_api_base="http://embed.test",
        similarity_threshold=0.92,
    )
    # Build app then inject fake embed client into cache
    app = create_app(cfg)
    app.state.cache._http = httpx.Client(transport=_FakeEmbedTransport())

    client = TestClient(app)
    # Store via non-stream
    r0 = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-cheap",
            "messages": [{"role": "user", "content": "how do I reset my password"}],
        },
    )
    assert r0.status_code == 200
    seeded_id = r0.json()["id"]

    # Paraphrase + stream → semantic hit SSE
    r1 = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock-cheap",
            "stream": True,
            "messages": [
                {"role": "user", "content": "what's the way to reset a password"}
            ],
        },
    )
    assert r1.status_code == 200
    assert "text/event-stream" in r1.headers.get("content-type", "")
    assert seeded_id in r1.text
    assert "[DONE]" in r1.text
    hit = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert hit["cache_hit"] is True
    assert hit["cache_kind"] == "semantic"
    assert hit.get("stream") is True


def test_failed_incomplete_stream_does_not_poison_cache(tmp_path: Path):
    """Incomplete OpenAI SSE (no [DONE]) must not be stored."""
    from edge.translate import openai_chat_sse_to_completion, anthropic_sse_to_message

    incomplete = b'data: {"id":"x","object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    assert openai_chat_sse_to_completion(incomplete) is None

    incomplete_anth = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"m1","model":"m","role":"assistant","content":[],"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    )
    assert anthropic_sse_to_message(incomplete_anth) is None

    # Also: bypass header on stream miss path must not leave a hit for next call
    # when we only ever called with bypass (nothing stored).
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "stream": True,
        "messages": [{"role": "user", "content": "never store me"}],
    }
    r1 = client.post(
        "/v1/chat/completions", json=payload, headers={"X-Cache-Bypass": "1"}
    )
    assert r1.status_code == 200
    _ = r1.text
    # Without bypass — should be a miss (nothing was stored under bypass)
    r2 = client.post("/v1/chat/completions", json=payload)
    assert r2.status_code == 200
    _ = r2.text
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    # First was bypass miss; second is miss-then-store (still cache_hit false)
    assert json.loads(lines[-1])["cache_hit"] is False
    # Third call should hit
    r3 = client.post("/v1/chat/completions", json=payload)
    _ = r3.text
    assert json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])[
        "cache_hit"
    ] is True


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


def test_responses_stream_sse(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    r = client.post(
        "/v1/responses",
        json={
            "model": "mock-cheap",
            "stream": True,
            "input": "hello responses stream",
        },
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    body = r.text
    assert "event: response.created" in body
    assert "event: response.output_text.delta" in body
    assert "event: response.completed" in body
    assert "hello responses stream" in body or "output_text" in body
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert lines
    row = json.loads(lines[-1])
    assert row["surface"] == "openai.responses"
    assert row.get("stream") is True
    assert row["status"] == "ok"
    assert row["cache_hit"] is False


def test_responses_stream_exact_hit(tmp_path: Path):
    """Non-stream /v1/responses store → stream hit returns Responses SSE."""
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    seed = {
        "model": "mock-cheap",
        "input": "responses stream cache seed",
    }
    r0 = client.post("/v1/responses", json=seed)
    assert r0.status_code == 200
    seeded_text = r0.json()["output"][0]["content"][0]["text"]

    r1 = client.post("/v1/responses", json={**seed, "stream": True})
    assert r1.status_code == 200
    assert "text/event-stream" in r1.headers.get("content-type", "")
    body = r1.text
    assert "event: response.created" in body
    assert "event: response.completed" in body
    assert "response.output_text.delta" in body
    assert "responses stream cache seed" in body or seeded_text[:30] in body
    hit = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert hit["cache_hit"] is True
    assert hit["cache_kind"] == "exact"
    assert hit["surface"] == "openai.responses"
    assert hit.get("stream") is True


def test_responses_stream_miss_stores_for_nonstream(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "stream": True,
        "input": "responses stream miss then hit",
    }
    r0 = client.post("/v1/responses", json=payload)
    assert r0.status_code == 200
    _ = r0.text
    miss = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert miss["cache_hit"] is False
    assert miss.get("stream") is True

    r1 = client.post(
        "/v1/responses",
        json={
            "model": "mock-cheap",
            "input": "responses stream miss then hit",
        },
    )
    assert r1.status_code == 200
    assert r1.json()["object"] == "response"
    assert r1.json()["status"] == "completed"
    hit = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert hit["cache_hit"] is True
    assert hit["cache_kind"] == "exact"


def test_messages_stream_exact_hit(tmp_path: Path):
    """Non-stream /v1/messages store → stream hit returns Anthropic SSE."""
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    seed = {
        "model": "mock-cheap",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "messages stream cache seed"}],
    }
    r0 = client.post("/v1/messages", json=seed)
    assert r0.status_code == 200
    seeded_text = r0.json()["content"][0]["text"]

    r1 = client.post("/v1/messages", json={**seed, "stream": True})
    assert r1.status_code == 200
    assert "text/event-stream" in r1.headers.get("content-type", "")
    body = r1.text
    assert "event: message_start" in body
    assert "event: message_stop" in body
    assert "text_delta" in body
    # Cached assistant text appears in deltas
    assert "messages stream cache seed" in body or seeded_text[:30] in body
    hit = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert hit["cache_hit"] is True
    assert hit["cache_kind"] == "exact"
    assert hit["surface"] == "anthropic.messages"
    assert hit.get("stream") is True

"""Exact cache: hash stability + hit path."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from fastapi.testclient import TestClient

from edge.cache import (
    ResponseCache,
    cache_key_hash,
    cacheable_text,
    cosine_similarity,
    normalize_for_hash,
    system_fingerprint,
    should_bypass_cache,
)
from edge.config import CacheConfig, GatewayConfig, ModelsConfig, ModelSpec
from edge.main import create_app


def test_hash_stability_order_independent_messages_wrapper():
    a = normalize_for_hash(
        {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
        model="mock-cheap",
        pool="cheap",
        surface="openai.chat.completions",
    )
    b = normalize_for_hash(
        {"temperature": 0.2, "messages": [{"role": "user", "content": "hi"}]},
        model="mock-cheap",
        pool="cheap",
        surface="openai.chat.completions",
    )
    assert cache_key_hash(a) == cache_key_hash(b)


def test_hash_differs_on_content_or_model():
    base = normalize_for_hash(
        {"messages": [{"role": "user", "content": "hi"}]},
        model="mock-cheap",
        pool="cheap",
        surface="openai.chat.completions",
    )
    other_msg = normalize_for_hash(
        {"messages": [{"role": "user", "content": "bye"}]},
        model="mock-cheap",
        pool="cheap",
        surface="openai.chat.completions",
    )
    other_model = normalize_for_hash(
        {"messages": [{"role": "user", "content": "hi"}]},
        model="mock-expensive",
        pool="expensive",
        surface="openai.chat.completions",
    )
    assert cache_key_hash(base) != cache_key_hash(other_msg)
    assert cache_key_hash(base) != cache_key_hash(other_model)


def test_disk_get_set_and_ttl(tmp_path: Path):
    cfg = CacheConfig(enabled=True, mode="exact", ttl_seconds=3600, max_entries=10, path=str(tmp_path))
    cache = ResponseCache(cfg)
    body = {"messages": [{"role": "user", "content": "ping"}], "model": "mock-cheap"}
    resp = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    assert cache.get(body, model="mock-cheap", pool="cheap", surface="chat") is None
    cache.set(body, model="mock-cheap", pool="cheap", surface="chat", response=resp)
    hit = cache.get(body, model="mock-cheap", pool="cheap", surface="chat")
    assert hit is not None
    assert hit.kind == "exact"
    assert hit.tokens_saved == 8
    assert hit.tokens_saved_prompt == 5
    assert hit.body["choices"][0]["message"]["content"] == "pong"


def test_should_bypass_stream_and_headers():
    # stream alone is NOT a bypass — stream shares the non-stream cache key
    assert should_bypass_cache({"stream": True}, None) is False
    assert should_bypass_cache({}, {"X-Cache-Bypass": "1"}) is True
    assert should_bypass_cache({}, {"Cache-Control": "no-cache"}) is True
    assert should_bypass_cache({"messages": []}, None) is False


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


def test_hit_path_via_app(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "messages": [{"role": "user", "content": "hello cache path"}],
    }
    r1 = client.post("/v1/chat/completions", json=payload)
    assert r1.status_code == 200
    body1 = r1.json()
    r2 = client.post("/v1/chat/completions", json=payload)
    assert r2.status_code == 200
    body2 = r2.json()
    # Same assistant content (mock includes uuid in id — content is deterministic echo)
    assert body1["choices"][0]["message"]["content"] == body2["choices"][0]["message"]["content"]
    assert body1["id"] == body2["id"]  # cached body reused

    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) >= 2
    miss = json.loads(lines[-2])
    hit = json.loads(lines[-1])
    assert miss["cache_hit"] is False
    assert miss["cache_kind"] == "none"
    assert miss["tokens_saved"] == 0
    assert hit["cache_hit"] is True
    assert hit["cache_kind"] == "exact"
    assert hit["tokens_saved"] > 0
    assert hit["cost_usd"] == 0.0


def test_bypass_header_skips_hit(tmp_path: Path):
    cfg = _mock_cfg(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    payload = {
        "model": "mock-cheap",
        "messages": [{"role": "user", "content": "bypass me"}],
    }
    client.post("/v1/chat/completions", json=payload)
    r2 = client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"X-Cache-Bypass": "1"},
    )
    assert r2.status_code == 200
    hit_line = json.loads((tmp_path / "ledger.jsonl").read_text().strip().splitlines()[-1])
    assert hit_line["cache_hit"] is False


# --- Semantic cache (v2) ---


def test_cosine_similarity_identical_and_orthogonal():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0
    assert cosine_similarity([], [1.0]) == 0.0


def test_cacheable_text_skips_tools_noise():
    body = {
        "messages": [{"role": "user", "content": "hello world"}],
        "system": "be brief",
        "tools": [{"type": "function", "function": {"name": "x", "parameters": {"a": 1}}}],
        "temperature": 0.1,
    }
    text = cacheable_text(body)
    assert "hello world" in text
    assert "be brief" in text
    assert "function" not in text
    assert "parameters" not in text


def test_cacheable_text_last_user_strips_prior_turns():
    body = {
        "system": "sys",
        "messages": [
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": "reply one"},
            {"role": "user", "content": "turn two paraphrase"},
        ],
    }
    full = cacheable_text(body, scope="full")
    last = cacheable_text(body, scope="last_user")
    assert "turn one" in full and "reply one" in full
    assert "turn one" not in last and "reply one" not in last
    assert "turn two paraphrase" in last
    assert "sys" in last
    # Exact keys still differ when history grows
    from edge.cache import cache_key_hash, normalize_for_hash

    b1 = {"messages": [{"role": "user", "content": "turn one"}], "system": "sys"}
    b2 = body
    assert cache_key_hash(normalize_for_hash(b1, model="m", pool="p", surface="s")) != cache_key_hash(
        normalize_for_hash(b2, model="m", pool="p", surface="s")
    )
    assert system_fingerprint(b1) == system_fingerprint(b2)


class _FakeEmbedTransport(httpx.BaseTransport):
    """Minimal httpx transport: /api/embeddings returns a vector keyed by prompt text."""

    def __init__(self, vectors: dict[str, list[float]], *, fail: bool = False):
        self.vectors = vectors
        self.fail = fail
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if self.fail:
            return httpx.Response(503, json={"error": "down"})
        if request.url.path.endswith("/api/embeddings"):
            payload = json.loads(request.content.decode())
            prompt = payload.get("prompt") or ""
            # Map unknown prompts to a near-orthogonal default
            vec = self.vectors.get(prompt)
            if vec is None:
                # Deterministic tiny hash → mostly dissimilar
                h = sum(ord(c) for c in prompt) % 97
                vec = [float(h), 1.0, 0.0]
            return httpx.Response(200, json={"embedding": vec})
        return httpx.Response(404, json={"error": "not found"})


def _semantic_cfg(tmp_path: Path, **kwargs) -> CacheConfig:
    base = dict(
        enabled=True,
        mode="semantic",
        ttl_seconds=3600,
        max_entries=50,
        path=str(tmp_path),
        embedding_model="nomic-embed-text",
        embedding_api_base="http://embed.test",
        similarity_threshold=0.92,
    )
    base.update(kwargs)
    return CacheConfig(**base)


def test_semantic_hit_when_vectors_similar(tmp_path: Path):
    # Same vector for paraphrases → cosine 1.0 ≥ 0.92
    shared = [0.9, 0.1, 0.2]
    transport = _FakeEmbedTransport(
        {
            "user: how do I reset my password": shared,
            "user: what's the way to reset a password": shared,
        }
    )
    client = httpx.Client(transport=transport)
    cache = ResponseCache(_semantic_cfg(tmp_path), http_client=client)
    body1 = {"messages": [{"role": "user", "content": "how do I reset my password"}]}
    body2 = {"messages": [{"role": "user", "content": "what's the way to reset a password"}]}
    resp = {
        "id": "cached-1",
        "choices": [{"message": {"role": "assistant", "content": "Use the reset link."}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    cache.set(body1, model="mock-cheap", pool="cheap", surface="chat", response=resp)
    hit = cache.get(body2, model="mock-cheap", pool="cheap", surface="chat")
    assert hit is not None
    assert hit.kind == "semantic"
    assert hit.body["id"] == "cached-1"
    assert hit.tokens_saved == 15
    assert transport.calls  # embed was invoked


def test_exact_preferred_over_semantic(tmp_path: Path):
    shared = [1.0, 0.0, 0.0]
    transport = _FakeEmbedTransport({"user: same prompt": shared})
    client = httpx.Client(transport=transport)
    cache = ResponseCache(_semantic_cfg(tmp_path), http_client=client)
    body = {"messages": [{"role": "user", "content": "same prompt"}]}
    resp = {
        "id": "exact-body",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    cache.set(body, model="mock-cheap", pool="cheap", surface="chat", response=resp)
    calls_before = len(transport.calls)
    hit = cache.get(body, model="mock-cheap", pool="cheap", surface="chat")
    assert hit is not None
    assert hit.kind == "exact"
    # Exact hit must not call embed
    assert len(transport.calls) == calls_before


def test_semantic_threshold_miss(tmp_path: Path):
    transport = _FakeEmbedTransport(
        {
            "user: cats": [1.0, 0.0, 0.0],
            "user: quantum chromodynamics proofs": [0.0, 1.0, 0.0],
        }
    )
    client = httpx.Client(transport=transport)
    cache = ResponseCache(
        _semantic_cfg(tmp_path, similarity_threshold=0.92),
        http_client=client,
    )
    body1 = {"messages": [{"role": "user", "content": "cats"}]}
    body2 = {"messages": [{"role": "user", "content": "quantum chromodynamics proofs"}]}
    resp = {
        "id": "a",
        "choices": [{"message": {"role": "assistant", "content": "meow"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    cache.set(body1, model="mock-cheap", pool="cheap", surface="chat", response=resp)
    hit = cache.get(body2, model="mock-cheap", pool="cheap", surface="chat")
    assert hit is None


def test_embed_failure_soft_miss_and_store_without_embedding(tmp_path: Path):
    transport = _FakeEmbedTransport({}, fail=True)
    client = httpx.Client(transport=transport)
    cache = ResponseCache(_semantic_cfg(tmp_path), http_client=client)
    body = {"messages": [{"role": "user", "content": "soft fail please"}]}
    resp = {
        "id": "stored",
        "choices": [{"message": {"role": "assistant", "content": "x"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    key = cache.set(body, model="mock-cheap", pool="cheap", surface="chat", response=resp)
    assert key is not None
    entry = json.loads((tmp_path / f"{key}.json").read_text())
    assert "embedding" not in entry
    # get: exact would hit; use different text so only semantic could match → soft miss
    body2 = {"messages": [{"role": "user", "content": "different text entirely"}]}
    assert cache.get(body2, model="mock-cheap", pool="cheap", surface="chat") is None


def test_mode_exact_never_calls_embed(tmp_path: Path):
    transport = _FakeEmbedTransport({"user: hi": [1.0, 0.0]})
    client = httpx.Client(transport=transport)
    cfg = CacheConfig(
        enabled=True,
        mode="exact",
        ttl_seconds=3600,
        max_entries=10,
        path=str(tmp_path),
        embedding_model="nomic-embed-text",
        embedding_api_base="http://embed.test",
    )
    cache = ResponseCache(cfg, http_client=client)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    resp = {
        "id": "e",
        "choices": [{"message": {"role": "assistant", "content": "yo"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    cache.set(body, model="mock-cheap", pool="cheap", surface="chat", response=resp)
    # Different text → exact miss; semantic would embed, but mode=exact must not
    other = {"messages": [{"role": "user", "content": "other"}]}
    assert cache.get(other, model="mock-cheap", pool="cheap", surface="chat") is None
    assert transport.calls == []


def test_semantic_skips_different_sampling(tmp_path: Path):
    shared = [0.5, 0.5, 0.0]
    transport = _FakeEmbedTransport(
        {
            "user: same question": shared,
        }
    )
    # Make both prompts map to same vector via defaulting — use identical contentable text
    # but different temperature so sampling filter skips.
    client = httpx.Client(transport=transport)
    cache = ResponseCache(_semantic_cfg(tmp_path), http_client=client)
    body1 = {
        "messages": [{"role": "user", "content": "same question"}],
        "temperature": 0.0,
    }
    body2 = {
        "messages": [{"role": "user", "content": "same question"}],
        "temperature": 0.9,
    }
    # Different temperature → different exact keys; same cacheable text → would be semantic
    # hit without sampling filter.
    resp = {
        "id": "t0",
        "choices": [{"message": {"role": "assistant", "content": "a"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    cache.set(body1, model="mock-cheap", pool="cheap", surface="chat", response=resp)
    hit = cache.get(body2, model="mock-cheap", pool="cheap", surface="chat")
    assert hit is None

def test_semantic_last_user_hits_across_growing_history(tmp_path: Path):
    """Claude Code shape: same system/tools, growing messages — last_user can semantic-hit."""
    shared = [0.8, 0.2, 0.1]
    # Embed text under last_user scope
    embed1 = "system: agent-sys\nuser: explain caching"
    embed2 = "system: agent-sys\nuser: explain caching please"  # paraphrase, history grown
    transport = _FakeEmbedTransport({embed1: shared, embed2: shared})
    client = httpx.Client(transport=transport)
    cache = ResponseCache(
        _semantic_cfg(tmp_path, semantic_scope="last_user"),
        http_client=client,
    )
    tools = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]
    body1 = {
        "system": "agent-sys",
        "messages": [{"role": "user", "content": "explain caching"}],
        "max_tokens": 100,
        "tools": tools,
    }
    body2 = {
        "system": "agent-sys",
        "messages": [
            {"role": "user", "content": "explain caching"},
            {"role": "assistant", "content": "Cache stores answers."},
            {"role": "user", "content": "explain caching please"},
        ],
        "max_tokens": 100,
        "tools": tools,
    }
    resp = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Cache stores answers."}],
        "usage": {"input_tokens": 40, "output_tokens": 10},
    }
    cache.set(body1, model="ollama-cheap", pool="cheap", surface="anthropic.messages", response=resp)
    # Exact must miss (history differs)
    from edge.cache import cache_key_hash, normalize_for_hash

    assert cache_key_hash(normalize_for_hash(body1, model="ollama-cheap", pool="cheap", surface="anthropic.messages")) != cache_key_hash(
        normalize_for_hash(body2, model="ollama-cheap", pool="cheap", surface="anthropic.messages")
    )
    hit = cache.get(body2, model="ollama-cheap", pool="cheap", surface="anthropic.messages")
    assert hit is not None
    assert hit.kind == "semantic"
    assert hit.body["id"] == "msg-1"


def test_semantic_last_user_rejects_different_system(tmp_path: Path):
    shared = [1.0, 0.0, 0.0]
    transport = _FakeEmbedTransport(
        {
            "system: sys-a\nuser: same question": shared,
            "system: sys-b\nuser: same question": shared,
        }
    )
    client = httpx.Client(transport=transport)
    cache = ResponseCache(
        _semantic_cfg(tmp_path, semantic_scope="last_user"),
        http_client=client,
    )
    body1 = {"system": "sys-a", "messages": [{"role": "user", "content": "same question"}]}
    body2 = {"system": "sys-b", "messages": [{"role": "user", "content": "same question"}]}
    resp = {
        "id": "a",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    cache.set(body1, model="m", pool="cheap", surface="chat", response=resp)
    hit = cache.get(body2, model="m", pool="cheap", surface="chat")
    assert hit is None

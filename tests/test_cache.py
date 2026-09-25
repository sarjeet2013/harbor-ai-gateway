"""Exact cache: hash stability + hit path."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from edge.cache import (
    ResponseCache,
    cache_key_hash,
    normalize_for_hash,
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
    assert should_bypass_cache({"stream": True}, None) is True
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

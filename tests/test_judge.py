"""Judge module: parse, label mapping, HTTP timeout/fallback (mocked)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from edge.config import GatewayConfig, JudgeConfig, ModelsConfig, ModelSpec, RouterConfig
from edge.judge import (
    build_user_payload,
    last_user_text,
    ollama_model_name,
    parse_judge_json,
    run_judge,
)
from edge.router import pick_model


def _cfg(*, strategy: str = "judge_then_heuristic", judge: JudgeConfig | None = None) -> GatewayConfig:
    return GatewayConfig(
        listen="127.0.0.1:0",
        upstream_mode="mock",
        models=ModelsConfig(
            cheap=[
                ModelSpec(name="ollama-cheap"),
                ModelSpec(name="ollama-chat"),
                ModelSpec(name="ollama-coder"),
                ModelSpec(name="ollama-reason"),
            ],
            expensive=[ModelSpec(name="openai-expensive")],
        ),
        router=RouterConfig(
            strategy=strategy,
            expensive_keywords=["refactor", "complex", "architecture"],
            expensive_min_chars=2000,
            coder_keywords=["write a function", "implement", "unit test", "fix the bug"],
            reason_keywords=["step by step", "reason about", "prove that", "math problem"],
            default_cheap_model="ollama-cheap",
            coder_model="ollama-coder",
            reason_model="ollama-reason",
            judge=judge
            or JudgeConfig(
                enabled=True,
                litellm_model="ollama_chat/qwen3:8b",
                api_base="http://judge.test",
                timeout_ms=400,
                min_confidence=0.55,
                on_failure="heuristic",
            ),
        ),
    )


class _FakeResp:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Minimal httpx.Client stand-in for run_judge / pick_model."""

    def __init__(self, handler):
        self._handler = handler

    def post(self, url: str, json: dict | None = None, timeout: float | None = None):
        return self._handler(url, json, timeout)


def test_ollama_model_name_strips_prefix():
    assert ollama_model_name("ollama_chat/qwen3:8b") == "qwen3:8b"
    assert ollama_model_name("qwen3:8b") == "qwen3:8b"


def test_parse_judge_json_variants():
    assert parse_judge_json('{"label":"coder","confidence":0.9}') == ("coder", 0.9)
    assert parse_judge_json('```json\n{"label":"chat","confidence":0.8}\n```') == ("chat", 0.8)
    assert parse_judge_json("noise {\"label\":\"reason\",\"confidence\":0.7} tail") == (
        "reason",
        0.7,
    )
    assert parse_judge_json('{"label":"nope","confidence":0.9}') is None
    assert parse_judge_json("not json") is None


def test_last_user_and_payload_signals():
    body = {
        "messages": [
            {"role": "system", "content": "ignore refactor architecture"},
            {"role": "user", "content": "hi there"},
        ],
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }
    assert last_user_text(body) == "hi there"
    payload = build_user_payload(body)
    assert "tools=yes" in payload
    assert "hi there" in payload
    assert "refactor" not in payload


def test_run_judge_success():
    def handler(url, json_body, timeout):
        assert "qwen3:8b" in json_body["model"]
        assert json_body.get("think") is False
        assert json_body.get("format") == "json"
        return _FakeResp(
            200,
            {"message": {"content": json.dumps({"label": "coder", "confidence": 0.91})}},
        )

    result = run_judge(
        {"messages": [{"role": "user", "content": "implement binary search"}]},
        _cfg().router.judge,
        http_client=_FakeClient(handler),
    )
    assert result.ok
    assert result.label == "coder"
    assert result.confidence == 0.91


def test_run_judge_timeout():
    def handler(url, json_body, timeout):
        raise httpx.TimeoutException("slow", request=None)

    result = run_judge(
        {"messages": [{"role": "user", "content": "hi"}]},
        _cfg().router.judge,
        http_client=_FakeClient(handler),
    )
    assert not result.ok
    assert result.error == "timeout"


def _judge_client(label: str, confidence: float = 0.9) -> _FakeClient:
    def handler(url, json_body, timeout):
        return _FakeResp(
            200,
            {
                "message": {
                    "content": json.dumps({"label": label, "confidence": confidence})
                }
            },
        )

    return _FakeClient(handler)


def test_judge_maps_labels():
    cfg = _cfg()
    cases = [
        ("chat", "ollama-cheap", "cheap", "judge:chat"),
        ("coder", "ollama-coder", "cheap", "judge:coder"),
        ("reason", "ollama-reason", "cheap", "judge:reason"),
        ("expensive", "openai-expensive", "expensive", "judge:expensive"),
    ]
    for label, model, pool, reason in cases:
        body = {"messages": [{"role": "user", "content": f"prompt for {label}"}]}
        d = pick_model(body, cfg, http_client=_judge_client(label))
        assert d.model == model
        assert d.pool == pool
        assert d.reason == reason
        assert d.judge_label == label
        assert d.judge_confidence == 0.9


def test_judge_low_confidence_falls_back_heuristic():
    cfg = _cfg()
    body = {
        "messages": [{"role": "user", "content": "Please write a function to parse JSON."}],
    }
    d = pick_model(body, cfg, http_client=_judge_client("chat", confidence=0.2))
    assert d.model == "ollama-coder"
    assert d.reason == "judge_fallback:heuristic:coder_keyword"
    assert d.judge_confidence == 0.2


def test_judge_timeout_falls_back_heuristic():
    def handler(url, json_body, timeout):
        raise httpx.TimeoutException("slow", request=None)

    cfg = _cfg()
    body = {"messages": [{"role": "user", "content": "Say hello in one word."}]}
    d = pick_model(body, cfg, http_client=_FakeClient(handler))
    assert d.model == "ollama-cheap"
    assert d.reason == "judge_fallback:heuristic:default_cheap"


def test_client_alias_skips_judge():
    called = {"n": 0}

    def handler(url, json_body, timeout):
        called["n"] += 1
        return _FakeResp(200, {"message": {"content": '{"label":"coder","confidence":0.99}'}})

    cfg = _cfg()
    body = {"messages": [{"role": "user", "content": "implement anything"}]}
    d = pick_model(
        body, cfg, client_model="ollama-cheap", http_client=_FakeClient(handler)
    )
    assert d.reason == "client:model"
    assert d.model == "ollama-cheap"
    assert called["n"] == 0


def test_force_cheap_blocks_expensive_label():
    cfg = _cfg()
    body = {"messages": [{"role": "user", "content": "design a complex architecture"}]}
    d = pick_model(
        body, cfg, force_cheap=True, http_client=_judge_client("expensive")
    )
    assert d.pool == "cheap"
    assert d.model == "ollama-cheap"  # no coder/reason keyword → default cheap
    assert "budget_force_cheap" in d.reason
    assert "judge:expensive" in d.reason
    assert d.judge_label == "expensive"


def test_force_cheap_allows_coder_within_cheap():
    cfg = _cfg()
    body = {"messages": [{"role": "user", "content": "implement foo"}]}
    d = pick_model(body, cfg, force_cheap=True, http_client=_judge_client("coder"))
    assert d.pool == "cheap"
    assert d.model == "ollama-coder"
    assert d.reason == "judge:coder"


def test_heuristic_strategy_unchanged():
    cfg = _cfg(strategy="heuristic_cost_first")
    body = {
        "messages": [{"role": "user", "content": "Please write a function to parse JSON."}],
    }
    # No http client — must not call network
    d = pick_model(body, cfg)
    assert d.model == "ollama-coder"
    assert d.reason == "heuristic:coder_keyword"


# Light eval fixtures — expected labels for Sarjeet-style example prompts
EVAL_FIXTURES = [
    ("Say hello in one word.", "chat"),
    ("Please write a function to parse JSON.", "coder"),
    ("Solve this math problem step by step: 17*19.", "reason"),
    ("Please refactor this module's architecture carefully.", "expensive"),
]


def test_eval_fixture_labels_via_mock_judge():
    """Fixtures map when the judge returns the expected label (contract check)."""
    cfg = _cfg()
    for prompt, label in EVAL_FIXTURES:
        body = {"messages": [{"role": "user", "content": prompt}]}
        d = pick_model(body, cfg, http_client=_judge_client(label))
        assert d.reason == f"judge:{label}"

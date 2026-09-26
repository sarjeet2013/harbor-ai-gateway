"""Unit tests for models YAML helpers + Ollama doctor probe (mocked)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from edge.config import GatewayConfig, ModelSpec, ModelsConfig, load_config
from edge.models_yaml import (
    apply_pool_set,
    collect_ollama_bases,
    dump_raw_yaml,
    is_ollama_api_base,
    list_pool_rows,
    load_raw_yaml,
    normalize_litellm_model,
    probe_ollama_tags,
)


def test_normalize_litellm_model_prefixes():
    assert (
        normalize_litellm_model(
            "qwen3:8b", pool="cheap", api_base="http://127.0.0.1:11434"
        )
        == "ollama_chat/qwen3:8b"
    )
    assert (
        normalize_litellm_model("gpt-4o-mini", pool="expensive", api_base=None)
        == "openai/gpt-4o-mini"
    )
    assert (
        normalize_litellm_model(
            "openai/gpt-4o", pool="expensive", api_base=None
        )
        == "openai/gpt-4o"
    )
    assert (
        normalize_litellm_model(
            "ollama_chat/llama3.2:3b", pool="cheap", api_base="http://x"
        )
        == "ollama_chat/llama3.2:3b"
    )


def test_apply_pool_set_updates_temp_yaml(tmp_path: Path):
    path = tmp_path / "gateway.yaml"
    raw = {
        "listen": "0.0.0.0:4000",
        "upstream_mode": "litellm",
        "models": {
            "cheap": [
                {
                    "name": "ollama-cheap",
                    "litellm_model": "ollama_chat/old:1b",
                    "api_base": "http://127.0.0.1:11434",
                }
            ],
            "expensive": [
                {"name": "openai-expensive", "litellm_model": "openai/gpt-4o-mini"}
            ],
        },
    }
    dump_raw_yaml(path, raw)

    loaded = load_raw_yaml(path)
    entry = apply_pool_set(
        loaded,
        "cheap",
        model="qwen3:8b",
        api_base="http://127.0.0.1:11434",
    )
    assert entry["litellm_model"] == "ollama_chat/qwen3:8b"
    assert entry["api_base"] == "http://127.0.0.1:11434"
    dump_raw_yaml(path, loaded)

    again = yaml.safe_load(path.read_text())
    cheap0 = again["models"]["cheap"][0]
    assert cheap0["litellm_model"] == "ollama_chat/qwen3:8b"
    assert cheap0["api_base"] == "http://127.0.0.1:11434"

    # expensive cloud bump + clear any api_base
    apply_pool_set(
        again,
        "expensive",
        model="gpt-4o",
        api_base=None,
    )
    dump_raw_yaml(path, again)
    exp = yaml.safe_load(path.read_text())["models"]["expensive"][0]
    assert exp["litellm_model"] == "openai/gpt-4o"
    assert "api_base" not in exp


def test_apply_pool_set_disable_enable(tmp_path: Path):
    raw: dict = {
        "models": {
            "cheap": [{"name": "ollama-cheap", "litellm_model": "ollama_chat/qwen3:8b"}]
        }
    }
    apply_pool_set(raw, "cheap", enabled=False)
    assert raw["models"]["cheap"][0]["enabled"] is False
    apply_pool_set(raw, "cheap", enabled=True)
    assert "enabled" not in raw["models"]["cheap"][0]


def test_list_pool_rows_and_enabled_filter():
    cfg = GatewayConfig(
        models=ModelsConfig(
            cheap=[
                ModelSpec(
                    name="c1",
                    litellm_model="ollama_chat/qwen3:8b",
                    api_base="http://127.0.0.1:11434",
                )
            ],
            expensive=[
                ModelSpec(
                    name="e1",
                    litellm_model="openai/gpt-4o-mini",
                    enabled=False,
                )
            ],
        )
    )
    rows = list_pool_rows(cfg)
    assert len(rows) == 2
    assert rows[0]["enabled"] is True
    assert rows[1]["enabled"] is False
    assert cfg.models.names("expensive") == []
    assert cfg.models.names("cheap") == ["c1"]


def test_collect_ollama_bases():
    cfg = GatewayConfig(
        models=ModelsConfig(
            cheap=[
                ModelSpec(
                    name="c1",
                    litellm_model="ollama_chat/qwen3:8b",
                    api_base="http://127.0.0.1:11434",
                )
            ],
            expensive=[
                ModelSpec(name="e1", litellm_model="openai/gpt-4o-mini"),
                ModelSpec(
                    name="e2",
                    litellm_model="ollama_chat/llama3.2:3b",
                    api_base="http://127.0.0.1:11434",  # dedupe
                ),
            ],
        )
    )
    bases = collect_ollama_bases(cfg)
    assert len(bases) == 1
    assert bases[0]["api_base"] == "http://127.0.0.1:11434"


def test_is_ollama_api_base():
    assert is_ollama_api_base("ollama_chat/x", "http://h:11434")
    assert is_ollama_api_base(None, "http://h:11434")
    assert not is_ollama_api_base("openai/gpt-4o-mini", None)


def test_probe_ollama_tags_ok():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "models": [{"name": "qwen3:8b"}, {"name": "llama3.2:3b"}]
    }
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client.get.return_value = mock_resp
    with patch("httpx.Client", return_value=mock_client):
        ok, names, err = probe_ollama_tags("http://127.0.0.1:11434")
    assert ok is True
    assert names == ["qwen3:8b", "llama3.2:3b"]
    assert err is None


def test_probe_ollama_tags_unreachable():
    with patch("httpx.Client", side_effect=OSError("connection refused")):
        ok, names, err = probe_ollama_tags("http://127.0.0.1:9")
    assert ok is False
    assert names == []
    assert err and "connection refused" in err


def test_cli_models_list_and_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "gateway.yaml"
    dump_raw_yaml(
        path,
        {
            "listen": "0.0.0.0:4000",
            "upstream_mode": "mock",
            "models": {
                "cheap": [
                    {
                        "name": "ollama-cheap",
                        "litellm_model": "ollama_chat/qwen3:8b",
                        "api_base": "http://127.0.0.1:11434",
                    }
                ],
                "expensive": [
                    {"name": "openai-expensive", "litellm_model": "openai/gpt-4o-mini"}
                ],
            },
        },
    )
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))
    from edge.cli import main

    assert main(["models"]) == 0
    assert main(["models", "list"]) == 0
    before = path.read_text()
    assert (
        main(
            [
                "models",
                "set",
                "--pool",
                "cheap",
                "--model",
                "llama3.2:3b",
                "--api-base",
                "http://127.0.0.1:11434",
                "--dry-run",
            ]
        )
        == 0
    )
    assert path.read_text() == before  # dry-run must not write

"""Admin UI: localhost guard, settings shape, cache roundtrip, .env updater."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from edge.admin import (
    ApplySettingsIn,
    CacheSettingsIn,
    apply_settings,
    build_settings_payload,
    env_key_is_set,
    is_loopback_host,
    require_localhost,
    schedule_edge_restart,
    update_dotenv_keys,
)
from edge.config import (
    CacheConfig,
    GatewayConfig,
    ModelsConfig,
    ModelSpec,
    load_config,
)
from edge.main import create_app


def test_is_loopback_host():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::ffff:127.0.0.1")
    assert not is_loopback_host("192.168.1.5")
    assert not is_loopback_host("10.0.0.1")
    assert not is_loopback_host("testclient")
    assert not is_loopback_host(None)
    assert not is_loopback_host("")


def _cfg(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(
        listen="127.0.0.1:4000",
        upstream_mode="mock",
        ledger_path=str(tmp_path / "ledger.jsonl"),
        budget_state_path=str(tmp_path / "budget.json"),
        models=ModelsConfig(
            cheap=[
                ModelSpec(
                    name="ollama-cheap",
                    litellm_model="ollama_chat/qwen3:8b",
                    api_base="http://127.0.0.1:11434",
                    extra_litellm_params={"think": False},
                )
            ],
            expensive=[
                ModelSpec(name="openai-expensive", litellm_model="openai/gpt-4o-mini")
            ],
        ),
        cache=CacheConfig(
            enabled=True,
            mode="exact",
            path=str(tmp_path / "cache"),
            embedding_model="nomic-embed-text",
            similarity_threshold=0.92,
            semantic_scope="last_user",
        ),
    )


def test_get_settings_shape(tmp_path: Path):
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    res = client.get("/admin/api/settings")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "upstream_mode" in data["status"]
    assert "budget" in data["status"]
    assert "cache" in data["status"]
    assert "router_strategy" in data["status"]
    assert "models" in data
    assert "cheap" in data["models"] and "expensive" in data["models"]
    assert "cache" in data
    assert set(data["cache"]) >= {
        "enabled",
        "mode",
        "similarity_threshold",
        "embedding_model",
        "embedding_api_base",
        "semantic_scope",
    }
    assert "router" in data and "judge" in data["router"]
    assert "secrets" in data
    assert "OPENAI_API_KEY" in data["secrets"]
    assert "XAI_API_KEY" in data["secrets"]
    assert isinstance(data["secrets"]["OPENAI_API_KEY"], bool)
    # Never leak key material
    blob = res.text
    assert "sk-" not in blob


def test_admin_html_served(tmp_path: Path):
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    for path in ("/admin", "/admin/"):
        res = client.get(path)
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")
        assert "Harbor Admin" in res.text
    css = client.get("/admin/static/style.css")
    assert css.status_code == 200
    js = client.get("/admin/static/app.js")
    assert js.status_code == 200


def test_mutate_rejects_non_local(tmp_path: Path):
    """TestClient peer host is 'testclient' → not loopback → 403."""
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    res = client.post(
        "/admin/api/settings",
        json={"cache": {"enabled": True, "mode": "exact", "similarity_threshold": 0.9, "semantic_scope": "full"}},
    )
    assert res.status_code == 403
    assert "localhost" in res.json()["detail"].lower() or "loopback" in res.json()["detail"].lower()


def test_mutate_allows_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "gateway.yaml"
    dotenv = tmp_path / ".env"
    raw = {
        "listen": "127.0.0.1:4000",
        "upstream_mode": "mock",
        "models": {
            "cheap": [
                {
                    "name": "ollama-cheap",
                    "litellm_model": "ollama_chat/qwen3:8b",
                    "api_base": "http://10.0.0.9:11434",
                    "extra_litellm_params": {"think": False},
                }
            ],
            "expensive": [
                {"name": "openai-expensive", "litellm_model": "openai/gpt-4o-mini"}
            ],
        },
        "cache": {
            "enabled": True,
            "mode": "exact",
            "ttl_seconds": 86400,
            "max_entries": 100,
            "path": str(tmp_path / "cache"),
            "similarity_threshold": 0.92,
            "semantic_scope": "full",
        },
        "router": {
            "strategy": "heuristic_cost_first",
            "judge": {
                "enabled": True,
                "litellm_model": "ollama_chat/qwen3:8b",
                "api_base": "http://10.0.0.9:11434",
                "timeout_ms": 400,
                "min_confidence": 0.55,
            },
            "expensive_keywords": ["refactor"],
        },
    }
    cfg_path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("GATEWAY_CONFIG", str(cfg_path))
    monkeypatch.setenv("GATEWAY_LITELLM_CONFIG", str(tmp_path / "generated_litellm.yaml"))

    app = create_app(load_config(cfg_path))
    client = TestClient(app)

    with patch("edge.admin.is_loopback_host", return_value=True):
        with patch("edge.admin.default_dotenv_path", return_value=dotenv):
            with patch("edge.admin.default_config_path", return_value=cfg_path):
                with patch(
                    "edge.admin.schedule_edge_restart",
                    return_value={
                        "restart_scheduled": True,
                        "restart_helper_pid": 99999,
                        "message": (
                            "Settings saved. Harbor edge and LiteLLM are "
                            "restarting to apply changes (brief downtime)."
                        ),
                    },
                ) as sched:
                    res = client.post(
                        "/admin/api/settings",
                        json={
                            "cache": {
                                "enabled": True,
                                "mode": "semantic",
                                "similarity_threshold": 0.88,
                                "embedding_model": "nomic-embed-text",
                                "embedding_api_base": "http://10.0.0.9:11434",
                                "semantic_scope": "last_user",
                            }
                        },
                    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["restart_scheduled"] is True
    assert "restart" in body["message"].lower()
    sched.assert_called_once()

    saved = yaml.safe_load(cfg_path.read_text())
    assert saved["cache"]["mode"] == "semantic"
    assert saved["cache"]["similarity_threshold"] == 0.88
    assert saved["cache"]["semantic_scope"] == "last_user"
    # Preserve unrelated fields
    assert saved["cache"]["ttl_seconds"] == 86400
    assert saved["router"]["expensive_keywords"] == ["refactor"]
    # Preserve existing api_base on models (not in this POST body)
    assert saved["models"]["cheap"][0]["api_base"] == "http://10.0.0.9:11434"


def test_apply_cache_mode_roundtrip(tmp_path: Path):
    cfg_path = tmp_path / "gateway.yaml"
    raw = {
        "upstream_mode": "mock",
        "models": {
            "cheap": [
                {
                    "name": "ollama-cheap",
                    "litellm_model": "ollama_chat/qwen3:8b",
                    "api_base": "http://192.168.1.50:11434",
                }
            ],
            "expensive": [
                {"name": "openai-expensive", "litellm_model": "openai/gpt-4o-mini"}
            ],
        },
        "cache": {
            "enabled": True,
            "mode": "exact",
            "ttl_seconds": 3600,
            "path": str(tmp_path / "cache"),
            "similarity_threshold": 0.9,
            "semantic_scope": "full",
        },
        "router": {"strategy": "judge_then_heuristic", "judge": {"enabled": True}},
    }
    cfg_path.write_text(yaml.safe_dump(raw))

    # Point generated LiteLLM YAML at tmp (do not touch repo config/.generated_litellm.yaml)
    import os
    os.environ["GATEWAY_LITELLM_CONFIG"] = str(tmp_path / "generated_litellm.yaml")
    result = apply_settings(
        ApplySettingsIn(
            cache=CacheSettingsIn(
                enabled=True,
                mode="semantic",
                similarity_threshold=0.95,
                embedding_model="nomic-embed-text",
                embedding_api_base="http://192.168.1.50:11434",
                semantic_scope="last_user",
            )
        ),
        config_path=cfg_path,
        dotenv_path=tmp_path / ".env",
        regenerate_litellm=True,
    )
    assert result["ok"] is True
    again = yaml.safe_load(cfg_path.read_text())
    assert again["cache"]["mode"] == "semantic"
    assert again["cache"]["similarity_threshold"] == 0.95
    assert again["cache"]["ttl_seconds"] == 3600  # preserved
    assert again["models"]["cheap"][0]["api_base"] == "http://192.168.1.50:11434"
    cfg = load_config(cfg_path)
    payload = build_settings_payload(cfg, config_path=cfg_path, dotenv_path=tmp_path / ".env")
    assert payload["cache"]["mode"] == "semantic"
    assert payload["models"]["cheap"][0]["api_base"] == "http://192.168.1.50:11434"


def test_update_dotenv_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\nOPENAI_API_KEY=old\nOTHER=keep\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    updated = update_dotenv_keys({"OPENAI_API_KEY": "new-secret-value"}, path=env_path)
    assert updated == ["OPENAI_API_KEY"]
    text = env_path.read_text()
    assert "OPENAI_API_KEY=new-secret-value" in text
    assert "OTHER=keep" in text
    assert "# comment" in text
    assert env_key_is_set("OPENAI_API_KEY", dotenv_path=env_path)
    # Add missing key
    update_dotenv_keys({"XAI_API_KEY": "xai-secret"}, path=env_path)
    assert "XAI_API_KEY=xai-secret" in env_path.read_text()
    assert env_key_is_set("XAI_API_KEY", dotenv_path=env_path)


def test_require_localhost_raises():
    class _C:
        host = "8.8.8.8"

    class _R:
        client = _C()

    with pytest.raises(Exception) as ei:
        require_localhost(_R())  # type: ignore[arg-type]
    assert getattr(ei.value, "status_code", None) == 403


def test_schedule_edge_restart_spawns_detached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """schedule_edge_restart must Popen a detached restart helper (not kill in-process)."""
    monkeypatch.setattr("edge.admin.repo_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir(parents=True)

    class _Proc:
        pid = 4242

    calls: list[dict] = []

    def _fake_popen(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return _Proc()

    with patch("edge.admin.subprocess.Popen", side_effect=_fake_popen):
        meta = schedule_edge_restart(delay_seconds=0.5)

    assert meta["restart_scheduled"] is True
    assert meta["restart_helper_pid"] == 4242
    assert "restarting" in meta["message"].lower()
    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    assert cmd[1:4] == ["-m", "edge", "restart"]
    assert "--delay" in cmd and "0.5" in cmd
    assert "--detach" in cmd
    assert calls[0]["start_new_session"] is True
    assert calls[0]["cwd"] == str(tmp_path)


def test_schedule_edge_restart_failure_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("edge.admin.repo_root", lambda: tmp_path)
    (tmp_path / "logs").mkdir(parents=True)

    with patch("edge.admin.subprocess.Popen", side_effect=OSError("boom")):
        meta = schedule_edge_restart()

    assert meta["restart_scheduled"] is False
    assert "auto-restart failed" in meta["message"].lower()
    assert "python -m edge restart" in meta["message"]


def test_get_settings_restart_note_mentions_auto(tmp_path: Path):
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    data = client.get("/admin/api/settings").json()
    note = (data.get("restart_note") or "").lower()
    assert "restart" in note
    assert "automatically" in note or "auto" in note

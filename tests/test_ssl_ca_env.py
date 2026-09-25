"""SSL CA env helper for LiteLLM/aiohttp on macOS."""

from __future__ import annotations

import os

import certifi

from edge.cli import _ensure_ssl_ca_env, _load_dotenv


def test_ensure_ssl_ca_env_sets_certifi(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    env: dict[str, str] = {}
    ca = _ensure_ssl_ca_env(env)
    assert ca == certifi.where()
    assert env["SSL_CERT_FILE"] == ca
    assert env["REQUESTS_CA_BUNDLE"] == ca


def test_ensure_ssl_ca_env_respects_existing(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/custom.pem")
    env = {"SSL_CERT_FILE": "/tmp/custom.pem"}
    assert _ensure_ssl_ca_env(env) == "/tmp/custom.pem"
    assert "REQUESTS_CA_BUNDLE" not in env


def test_load_dotenv_skips_blank_then_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = tmp_path / ".env"
    p.write_text("OPENAI_API_KEY=\nOPENAI_API_KEY=sk-test-real\n")
    loaded = _load_dotenv(p)
    assert loaded == ["OPENAI_API_KEY"]
    assert os.environ["OPENAI_API_KEY"] == "sk-test-real"

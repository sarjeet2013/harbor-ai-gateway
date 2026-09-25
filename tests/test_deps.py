"""Dependency preflight: mapping + missing-module messaging + litellm mode."""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from edge.deps import (
    REQUIRED_IMPORTS,
    check_deps,
    ensure_deps,
    ensure_litellm,
    ensure_upstream_deps,
    format_litellm_missing_message,
    format_missing_message,
    litellm_importable,
    litellm_ready,
    missing_modules,
    preflight_ok_message,
    resolve_litellm_cmd,
)


def test_required_imports_are_importable_when_requirements_installed():
    """With requirements.txt installed, every required import succeeds."""
    assert check_deps() == []
    assert missing_modules() == []


def test_required_imports_mapping_covers_known_packages():
    # import name → pip name (yaml is the classic mismatch)
    assert REQUIRED_IMPORTS["yaml"] == "PyYAML"
    for name in ("pydantic", "fastapi", "httpx", "uvicorn"):
        assert name in REQUIRED_IMPORTS
    # litellm stays out of core required set (mock mode must work without it)
    assert "litellm" not in REQUIRED_IMPORTS


def test_missing_modules_with_fake_mapping():
    fake = {"this_module_definitely_does_not_exist_xyz": "fake-pkg"}
    missing = missing_modules(fake)
    assert missing == [("this_module_definitely_does_not_exist_xyz", "fake-pkg")]


def test_format_missing_message_mentions_pip_install():
    msg = format_missing_message([("yaml", "PyYAML")])
    assert "yaml" in msg
    assert "PyYAML" in msg
    assert "pip install -r requirements.txt" in msg
    assert "requirements-litellm.txt" in msg
    assert "venv" in msg.lower() or "Activate" in msg


def test_ensure_deps_exits_without_traceback_on_missing():
    fake = {"this_module_definitely_does_not_exist_xyz": "fake-pkg"}
    buf = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        ensure_deps(fake, stream=buf)
    assert exc.value.code == 1
    out = buf.getvalue()
    assert "this_module_definitely_does_not_exist_xyz" in out
    assert "pip install -r requirements.txt" in out
    # No traceback dump
    assert "Traceback" not in out


def test_preflight_ok_message():
    assert "OK" in preflight_ok_message()
    assert "LiteLLM" in preflight_ok_message(litellm_checked=True)


def test_ensure_upstream_deps_skips_litellm_in_mock_mode():
    """mock mode must not require litellm even if import is broken."""
    with patch("edge.deps.litellm_importable", return_value=False):
        with patch("edge.deps.resolve_litellm_cmd", return_value=None):
            ensure_upstream_deps("mock")  # must not raise


def test_ensure_upstream_deps_requires_litellm_in_litellm_mode():
    buf = io.StringIO()
    with patch("edge.deps.litellm_importable", return_value=False):
        with patch("edge.deps.resolve_litellm_cmd", return_value=None):
            with pytest.raises(SystemExit) as exc:
                ensure_upstream_deps("litellm", stream=buf)
    assert exc.value.code == 1
    out = buf.getvalue()
    assert "litellm" in out.lower()
    assert "pip install" in out
    assert "litellm[proxy]" in out
    assert "Traceback" not in out


def test_ensure_litellm_ok_when_import_and_cli_present():
    with patch("edge.deps.litellm_importable", return_value=True):
        with patch(
            "edge.deps.resolve_litellm_cmd",
            return_value=[sys.executable, "-m", "litellm"],
        ):
            cmd = ensure_litellm()
    assert cmd == [sys.executable, "-m", "litellm"]


def test_resolve_litellm_cmd_none_when_missing():
    with patch("edge.deps.litellm_importable", return_value=False):
        assert resolve_litellm_cmd() is None


def test_resolve_litellm_cmd_uses_this_interpreter(monkeypatch):
    """Prefer python -m edge.litellm_cli over a (possibly stale) console script."""
    with patch("edge.deps.litellm_importable", return_value=True):
        with patch("edge.deps.litellm_proxy_importable", return_value=True):
            assert resolve_litellm_cmd() == [sys.executable, "-m", "edge.litellm_cli"]


def test_resolve_litellm_cmd_none_when_proxy_cli_missing():
    with patch("edge.deps.litellm_importable", return_value=True):
        with patch("edge.deps.litellm_proxy_importable", return_value=False):
            assert resolve_litellm_cmd() is None


def test_format_litellm_missing_message_has_install_line():
    with patch("edge.deps.litellm_importable", return_value=False):
        with patch("edge.deps.resolve_litellm_cmd", return_value=None):
            msg = format_litellm_missing_message()
    assert "MISSING" in msg
    assert "litellm[proxy]" in msg
    assert "requirements-litellm.txt" in msg


# --- doctor / start integration (config-aware) ---


def test_doctor_ok_in_mock_mode_without_litellm(tmp_path, monkeypatch):
    """python -m edge doctor must succeed in mock mode even if litellm is absent."""
    cfg = tmp_path / "gateway.yaml"
    cfg.write_text("upstream_mode: mock\nlisten: \"127.0.0.1:4000\"\n")
    monkeypatch.setenv("GATEWAY_CONFIG", str(cfg))

    with patch("edge.deps.litellm_importable", return_value=False):
        with patch("edge.deps.resolve_litellm_cmd", return_value=None):
            with patch("edge.cli.litellm_ready", return_value=False):
                from edge.cli import cmd_doctor

                rc = cmd_doctor(None)
    assert rc == 0


def test_doctor_fails_in_litellm_mode_when_litellm_missing(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "gateway.yaml"
    cfg.write_text("upstream_mode: litellm\nlisten: \"127.0.0.1:4000\"\n")
    monkeypatch.setenv("GATEWAY_CONFIG", str(cfg))

    with patch("edge.cli.litellm_ready", return_value=False):
        from edge.cli import cmd_doctor

        rc = cmd_doctor(None)
    assert rc == 1
    err = capsys.readouterr().err
    assert "litellm" in err.lower()
    assert "pip install" in err


def test_start_preflight_fails_before_config_gen_when_litellm_missing(
    tmp_path, monkeypatch
):
    """start must refuse before write_litellm_config when litellm is missing."""
    cfg = tmp_path / "gateway.yaml"
    cfg.write_text(
        "upstream_mode: litellm\n"
        "listen: \"127.0.0.1:4000\"\n"
        "litellm_host: \"127.0.0.1\"\n"
        "litellm_port: 4001\n"
        "models:\n"
        "  cheap:\n"
        "    - name: ollama-cheap\n"
        "      litellm_model: ollama_chat/llama3.2:1b\n"
        "  expensive:\n"
        "    - name: ollama-expensive\n"
        "      litellm_model: ollama_chat/llama3.2:3b\n"
    )
    monkeypatch.setenv("GATEWAY_CONFIG", str(cfg))

    wrote = {"called": False}

    def _fake_write(_cfg):
        wrote["called"] = True
        return tmp_path / "generated.yaml"

    import argparse

    from edge.cli import cmd_start

    buf = io.StringIO()
    with patch("edge.deps.litellm_importable", return_value=False):
        with patch("edge.deps.resolve_litellm_cmd", return_value=None):
            with patch("edge.cli.write_litellm_config", side_effect=_fake_write):
                with pytest.raises(SystemExit) as exc:
                    # ensure_upstream_deps prints to our stream via ensure_litellm;
                    # cmd_start calls ensure_upstream_deps without stream → stderr.
                    # Patch ensure_upstream_deps path by making litellm missing.
                    with patch("edge.cli.ensure_upstream_deps") as mock_ensure:
                        # Call the real ensure so we test the wiring, but redirect:
                        from edge.deps import ensure_upstream_deps as real_ensure

                        def _wrapped(mode, stream=None):
                            return real_ensure(mode, stream=buf)

                        mock_ensure.side_effect = _wrapped
                        args = argparse.Namespace(detach=True)
                        cmd_start(args)
    assert exc.value.code == 1
    assert wrote["called"] is False
    assert "litellm" in buf.getvalue().lower()

"""First-run seed of config/gateway.yaml from the example template."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_ensure_gateway_yaml_seeds_from_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from edge import cli

    monkeypatch.delenv("GATEWAY_CONFIG", raising=False)
    monkeypatch.delenv("HARBOR_DOCKER", raising=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    example = config_dir / "gateway.yaml.example"
    example.write_text("listen: \"0.0.0.0:4000\"\nupstream_mode: mock\n")

    with patch.object(cli, "repo_root", return_value=tmp_path):
        seeded = cli._ensure_gateway_yaml()

    dest = tmp_path / "config" / "gateway.yaml"
    assert seeded == dest
    assert dest.is_file()
    assert "upstream_mode: mock" in dest.read_text()

    # Second call is a no-op
    with patch.object(cli, "repo_root", return_value=tmp_path):
        assert cli._ensure_gateway_yaml() is None


def test_ensure_gateway_yaml_prefers_docker_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from edge import cli

    monkeypatch.delenv("GATEWAY_CONFIG", raising=False)
    monkeypatch.setenv("HARBOR_DOCKER", "1")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "gateway.yaml.example").write_text("listen: native\n")
    (config_dir / "gateway.docker.yaml.example").write_text(
        "listen: docker\napi_base: http://host.docker.internal:11434\n"
    )

    with patch.object(cli, "repo_root", return_value=tmp_path):
        seeded = cli._ensure_gateway_yaml()

    assert seeded is not None
    body = seeded.read_text()
    assert "listen: docker" in body
    assert "host.docker.internal" in body


def test_ensure_gateway_yaml_skips_when_gateway_config_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from edge import cli

    monkeypatch.setenv("GATEWAY_CONFIG", str(tmp_path / "custom.yaml"))
    with patch.object(cli, "repo_root", return_value=tmp_path):
        assert cli._ensure_gateway_yaml() is None

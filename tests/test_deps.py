"""Dependency preflight: mapping + missing-module messaging."""
from __future__ import annotations

import io
import sys

import pytest

from edge.deps import (
    REQUIRED_IMPORTS,
    check_deps,
    ensure_deps,
    format_missing_message,
    missing_modules,
    preflight_ok_message,
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


def test_missing_modules_with_fake_mapping():
    fake = {"this_module_definitely_does_not_exist_xyz": "fake-pkg"}
    missing = missing_modules(fake)
    assert missing == [("this_module_definitely_does_not_exist_xyz", "fake-pkg")]


def test_format_missing_message_mentions_pip_install():
    msg = format_missing_message([("yaml", "PyYAML")])
    assert "yaml" in msg
    assert "PyYAML" in msg
    assert "pip install -r requirements.txt" in msg
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

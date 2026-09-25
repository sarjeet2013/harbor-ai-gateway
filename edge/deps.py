"""Required third-party dependency preflight for the Harbor edge CLI.

Keep REQUIRED_IMPORTS in sync with requirements.txt whenever you add a new
runtime dependency under edge/. Import name → pip package name (for messages).

litellm is intentionally NOT listed here: it is optional and only needed when
upstream_mode is litellm (mock mode works without it). Install separately:
  pip install 'litellm[proxy]'
"""

from __future__ import annotations

import importlib
import sys
from typing import Iterable, Mapping

# import_name -> pip install name (as shown to the user)
REQUIRED_IMPORTS: dict[str, str] = {
    "yaml": "PyYAML",
    "pydantic": "pydantic",
    "fastapi": "fastapi",
    "httpx": "httpx",
    "uvicorn": "uvicorn",
}

INSTALL_HINT = (
    "Activate your venv, then install dependencies:\n"
    "  pip install -r requirements.txt\n"
    "Optional (real providers / Ollama via LiteLLM):\n"
    "  pip install 'litellm[proxy]'"
)


def missing_modules(
    required: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Return [(import_name, pip_name), ...] for modules that fail to import."""
    mapping = REQUIRED_IMPORTS if required is None else required
    missing: list[tuple[str, str]] = []
    for import_name, pip_name in mapping.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append((import_name, pip_name))
    return missing


def format_missing_message(missing: Iterable[tuple[str, str]]) -> str:
    missing = list(missing)
    modules = ", ".join(f"{imp} (pip: {pip})" for imp, pip in missing)
    return (
        f"Missing required dependencies: {modules}\n"
        f"{INSTALL_HINT}"
    )


def check_deps(required: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Return missing (import_name, pip_name) pairs; empty list means OK."""
    return missing_modules(required)


def ensure_deps(
    required: Mapping[str, str] | None = None,
    *,
    stream=None,
) -> None:
    """Exit process with a short message if any required module is missing.

    Does not dump a traceback — only the missing modules and install hint.
    """
    missing = missing_modules(required)
    if not missing:
        return
    out = sys.stderr if stream is None else stream
    print(format_missing_message(missing), file=out)
    raise SystemExit(1)


def preflight_ok_message() -> str:
    return "OK: all required edge dependencies are installed."

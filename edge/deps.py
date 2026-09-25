"""Required third-party dependency preflight for the Harbor edge CLI.

Keep REQUIRED_IMPORTS in sync with requirements.txt whenever you add a new
runtime dependency under edge/. Import name → pip package name (for messages).

litellm is intentionally NOT in REQUIRED_IMPORTS: it is only needed when
upstream_mode is litellm (mock mode works without it). Install via:
  pip install -r requirements-litellm.txt
  # or: pip install 'litellm[proxy]'
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

LITELLM_IMPORT = "litellm"
LITELLM_PIP = "litellm[proxy]"
LITELLM_REQUIREMENTS = "requirements-litellm.txt"

INSTALL_HINT = (
    "Activate your venv, then install dependencies:\n"
    "  pip install -r requirements.txt\n"
    "For Ollama / real providers (upstream_mode: litellm), also install:\n"
    f"  pip install -r {LITELLM_REQUIREMENTS}\n"
    f"  # or: pip install '{LITELLM_PIP}'"
)

LITELLM_INSTALL_HINT = (
    f"Install: pip install '{LITELLM_PIP}'\n"
    f"  # or: pip install -r {LITELLM_REQUIREMENTS}"
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


def litellm_importable() -> bool:
    """True if `import litellm` succeeds."""
    try:
        importlib.import_module(LITELLM_IMPORT)
        return True
    except ImportError:
        return False


def litellm_proxy_importable() -> bool:
    """True if LiteLLM's proxy CLI entry (run_server) can be imported."""
    try:
        importlib.import_module("litellm.proxy.proxy_cli")
        return True
    except ImportError:
        return False


def resolve_litellm_cmd() -> list[str] | None:
    """Return argv prefix to launch LiteLLM, or None if not resolvable.

    Always launches via *this* interpreter (``python -m edge.litellm_cli``) so a
    stale ``.venv/bin/litellm`` console-script shebang cannot break ``start``
    while ``import litellm`` still succeeds for ``doctor``.
    """
    if not litellm_importable() or not litellm_proxy_importable():
        return None
    return [sys.executable, "-m", "edge.litellm_cli"]


def litellm_ready() -> bool:
    """True when litellm imports and a CLI launch command is resolvable."""
    return litellm_importable() and resolve_litellm_cmd() is not None


def format_litellm_missing_message() -> str:
    return (
        "LiteLLM is required for upstream_mode: litellm "
        "(default Ollama gateway.yaml), but it is not available.\n"
        f"  - import litellm:              {'OK' if litellm_importable() else 'MISSING'}\n"
        f"  - import litellm.proxy.proxy_cli: "
        f"{'OK' if litellm_proxy_importable() else 'MISSING'}\n"
        f"  - launch cmd (python -m edge.litellm_cli): "
        f"{'OK' if resolve_litellm_cmd() else 'MISSING'}\n"
        f"{LITELLM_INSTALL_HINT}"
    )


def ensure_litellm(*, stream=None) -> list[str]:
    """Require litellm import + resolvable CLI; exit 1 with install hint if not.

    Returns the resolved CLI argv prefix when OK (for callers that spawn).
    """
    cmd = resolve_litellm_cmd()
    if litellm_importable() and cmd is not None:
        return cmd
    out = sys.stderr if stream is None else stream
    print(format_litellm_missing_message(), file=out)
    raise SystemExit(1)


def ensure_upstream_deps(upstream_mode: str, *, stream=None) -> None:
    """After core deps + config load: require litellm when mode will launch it."""
    if (upstream_mode or "").strip().lower() == "litellm":
        ensure_litellm(stream=stream)


def preflight_ok_message(*, litellm_checked: bool = False) -> str:
    if litellm_checked:
        return "OK: required edge dependencies and LiteLLM are installed."
    return "OK: all required edge dependencies are installed."

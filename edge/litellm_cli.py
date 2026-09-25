"""Same-interpreter LiteLLM proxy launcher.

Avoids relying on the ``litellm`` console script under ``.venv/bin``, which
often breaks after a venv recreate or Python upgrade (stale shebang →
FileNotFoundError) even while ``import litellm`` still works in this process.
"""

from __future__ import annotations


def main() -> None:
    from litellm.proxy.proxy_cli import run_server

    run_server()


if __name__ == "__main__":
    main()

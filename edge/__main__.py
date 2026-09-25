"""Allow `python -m edge` → CLI.

Preflight runs *before* importing the CLI (which pulls in PyYAML / pydantic / …)
so a bare `python -m edge start` without deps prints a short install hint
instead of ModuleNotFoundError + traceback.
"""

from .deps import ensure_deps

ensure_deps()

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Allow `python -m edge` → CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

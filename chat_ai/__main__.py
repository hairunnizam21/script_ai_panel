"""CLI entry point for ``python3 -m chat_ai``."""

from __future__ import annotations

import sys

from .agent import run


def main() -> int:
    try:
        return run(sys.argv[1:])
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point: ``python -m typing_simulator``."""

from __future__ import annotations

import sys


def main() -> int:
    from typing_simulator.application import run

    return run()


if __name__ == "__main__":
    sys.exit(main())

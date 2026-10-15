"""``python -m vericto dbt check ...``: the same command as ``vericto-dbt``."""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if argv and argv[0] == "dbt":
        from .dbt import main as dbt_main

        return dbt_main(argv[1:])
    sys.stderr.write("usage: python -m vericto dbt check [options]  (see: python -m vericto dbt --help)\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python
"""JSON view of every strategy and operational module in the repository.

Used by the console's Strategies and Watchdogs pages so they report what is
actually installed and running, rather than a hardcoded list.

    python quant_backtester/ops_cli.py inventory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_backtester.src.ops.inventory import build_inventory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inventory"], nargs="?", default="inventory")
    parser.parse_args()
    try:
        print(json.dumps(build_inventory(), default=str))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

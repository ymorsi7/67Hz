"""GridSafe evaluation entrypoint (expanded in Phases 2-7)."""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="GridSafe evaluation harness")
    parser.add_argument("--mode", choices=["dev", "final"], default="dev")
    args = parser.parse_args()
    print(f"[eval:{args.mode}] Scaffold ready. No gold labels or eval suites registered yet (Phase 1+).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

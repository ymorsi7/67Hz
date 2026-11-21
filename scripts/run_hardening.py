#!/usr/bin/env python3
"""Start the long background hardening run (gridsafe-hardener / gemini-4-carbon)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.harden import run_hardening_loop


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profiles", nargs="+", default=["lipa", "southern_dev"])
    p.add_argument("--max-cycles", type=int, default=50)
    p.add_argument("--seed", type=int, default=20261121)
    args = p.parse_args()
    try:
        run_hardening_loop(profile_ids=args.profiles, max_cycles=args.max_cycles, seed=args.seed)
    except KeyboardInterrupt:
        print("[harden] interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

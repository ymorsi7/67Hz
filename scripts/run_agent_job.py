#!/usr/bin/env python3
"""Run one GridSafe managed-agent job for a utility profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from agents.job import run_job


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default="lipa")
    p.add_argument("--seed", type=int, default=303)
    p.add_argument("--budget-s", type=float, default=75.0)
    p.add_argument("--attack-rounds", type=int, default=2)
    p.add_argument("--no-agents", action="store_true", help="Local evolve only (enable_agents=false path)")
    args = p.parse_args()
    cert = run_job(
        args.profile,
        seed=args.seed,
        budget_s=args.budget_s,
        attack_rounds=args.attack_rounds,
        use_agents=False if args.no_agents else None,
    )
    print(json.dumps({k: cert[k] for k in cert if k != "controller_code"}, indent=2, default=str))
    print(f"certificate -> {cert.get('certificate_path')}")
    return 0 if cert.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

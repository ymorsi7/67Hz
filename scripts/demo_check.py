"""Pre-demo verification script for `make demo-check`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def main() -> int:
    failures: list[str] = []

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[FAIL] GEMINI_API_KEY is not set in .env")
        return 1
    print("[OK]   GEMINI_API_KEY is set")

    with (REPO_ROOT / "config" / "models.yaml").open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    client = genai.Client(http_options={"api_version": "v1beta"})

    for role in ("reader", "designer", "hardener"):
        model_id = cfg[role]["model"]
        try:
            r = client.interactions.create(
                model=model_id,
                input="Reply OK.",
                generation_config={"thinking_level": "low"},
            )
            print(f"[OK]   {role} ({model_id}): {r.output_text.strip()!r}")
        except Exception as exc:
            msg = f"{role} ({model_id}) failed: {exc}"
            print(f"[FAIL] {msg}")
            failures.append(msg)

    verifier_model = cfg["verifier"]["model"]
    try:
        d = client.decisions.create(
            model=verifier_model,
            input="Maximum ramp rate is 5 MW/min.",
            questions={
                "check": {
                    "type": "noul",
                    "instructions": "Does the text state a 5 MW/min ramp rate limit?",
                }
            },
        )
        ans = d.answers["check"]
        print(f"[OK]   verifier ({verifier_model}): noul={ans.noul:.2f}, confidence={ans.confidence:.2f}")
    except Exception as exc:
        msg = f"verifier ({verifier_model}) failed: {exc}"
        print(f"[FAIL] {msg}")
        failures.append(msg)

    try:
        agents = client.agents.list()
        print(f"[OK]   managed agents endpoint reachable (saved agents: {len(agents.agents or [])})")
    except Exception as exc:
        msg = f"managed agents endpoint failed: {exc}"
        print(f"[FAIL] {msg}")
        failures.append(msg)

    frontend_index = REPO_ROOT / "frontend" / "dist" / "index.html"
    if frontend_index.exists():
        print("[OK]   frontend/dist/index.html built")
    else:
        print("[WARN] frontend/dist/index.html not built yet (run `make build-ui`)")

    if failures:
        print(f"\nDemo check failed with {len(failures)} error(s).")
        return 1
    print("\nAll demo checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

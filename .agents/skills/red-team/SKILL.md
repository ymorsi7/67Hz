---
name: red-team
description: Attack a candidate controller with adversarial workloads and return structured counterexamples.
---

# Red Team

Given a candidate controller and verified constraints, search for break workloads.
Prefer:
- resonant periods near IEEE FBM modes (~15–48 Hz) for frequency-band rules
- abrupt job start / checkpoint / stop edges for ramp and step rules
- idle-to-full cold starts that stress slew limits

Return structured JSON only:
```json
{
  "status": "break_found" | "no_break_within_budget",
  "counterexamples": [
    {
      "workload": {"family":"fast|slow", "period_s":0.05, "depth":0.7, "...": "..."},
      "failed_constraint_id": "fluctuation_band_25",
      "measured": 10.5,
      "limit": 10.0,
      "margin_pct": -5.0
    }
  ],
  "notes": "short"
}
```
Do not claim a break unless you ran the checker on that workload. The host will re-verify every counterexample.

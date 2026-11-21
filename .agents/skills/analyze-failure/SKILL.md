---
name: analyze-failure
description: Explain a verified controller failure and propose a concrete fix hint for the engineer.
---

# Analyze Failure

Input: controller source, failed constraint, measured vs limit, workload params.
Output structured JSON:
```json
{
  "failure_mode": "spectral_leak|ramp_overshoot|step_spike|soc_starvation|other",
  "root_cause": "one sentence",
  "fix_hint": "concrete controller change (tau, slew, preload, notch, ...)",
  "suggested_tests": ["workload description"]
}
```
Stay within pure-numpy controller constraints. Never invent margins; use the measured values provided.

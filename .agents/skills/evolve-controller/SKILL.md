---
name: evolve-controller
description: Evolve a pure-numpy controller that passes the deterministic checker, seeded by B3.
---

# Evolve Controller

Produce `def control(obs) -> action` under 60 lines, numpy-only:
actions = `gpu_floor_mw`, `gpu_cap_mw`, `battery_setpoint_mw`.

Starting point and safety ladder (host evolve loop enforces this):
1. Tuned B3 (low-pass + battery) as seed and second fallback.
2. Parallel Hydrogen mutations against the checker.
3. If evolved score is worse than B3 on the same cases, keep B3.
4. Else conservative fixed floor, else explicit refusal.

Banned in controller code: file I/O, network, dynamic imports, `os`, `subprocess`, `eval`, `exec`.
Feed failing constraint id + margin back into the next mutation.

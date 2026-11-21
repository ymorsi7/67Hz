---
name: certify
description: Emit a compliance certificate only after backend re-check of controller and attacks.
---

# Certify

Call backend verification (or wait for host re-check) before claiming compliance.
Certificate fields:
- each rule, page citation, measured margin
- attacks survived (from hardener rounds)
- which model did which job
- outcome: `evolved_compliant` | `fallback_b3` | `fallback_conservative_floor` | `refused`

Never certify on agent self-report alone. If any checkable rule fails or is unevaluable across the suite, refuse.

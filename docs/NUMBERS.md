# Twelve numbers a presenter can quote

Every value comes from a saved run file. Command shown is how to regenerate or re-print it.

| # | Number | Meaning | File | Command |
| ---: | --- | --- | --- | --- |
| 1 | **42.32 s** | `make eval-final` wall-clock (`SEED=20261121`) | `eval/results/eval_final_timing.json` (log: `eval/results/eval_final_run.log`) | `make eval-final SEED=20261121` |
| 2 | **33/33** (95% WI **[89.6%, 100%]** ) | GridSafe preset seed-run pass rate across 11 profiles × 3 seeds | `eval/results/final_20261121.json` → `method_summary.gridsafe` | `make eval-final SEED=20261121` then `make report` |
| 3 | **33/33** (same WI) | B3 (no-AI matched-budget search) preset pass rate — **matches GridSafe on presets** | `eval/results/final_20261121.json` → `method_summary.b3` | same |
| 4 | **9/11** random-suite all-pass | GridSafe profiles that pass all 20 unseen workloads (fails: `lipa` 19/20, `lipa_pseg` 19/20) | `eval/results/final_20261121.json` → `method_summary.gridsafe.random_suite_*` | `make eval-final SEED=20261121` |
| 5 | **11/11** random-suite all-pass | B0 (single Neon shot) — stronger than GridSafe on this particular random seed | `eval/results/final_20261121.json` → `method_summary.b0` | same |
| 6 | **4.05% OH / 11.6% TP** | GridSafe mean energy overhead / throughput loss on passing preset runs | `eval/results/final_20261121.json` → `method_summary.gridsafe.mean_*` | same |
| 7 | **4.09% OH / 15.8% TP** | B3 mean OH / TP on passing presets — similar OH, **worse** TP than GridSafe | `eval/results/final_20261121.json` → `method_summary.b3` | same |
| 8 | **P=0.679 / R=1.000 / H=0.321 / A=0.034** | Extraction vs 6 human gold files (9 false positives) | `eval/results/final_20261121.json` → `extraction` | `make eval-final SEED=20261121` (uses `eval/cache/extractions` + `eval/gold`) |
| 9 | **Severity 4 → abstain** (`page_readable_p=0.1314`) | Verifier abstains on unreadable page instead of guessing | `eval/cache/dev_calibration.json` → `degradation_sweep[severity=4]` | `python scripts/calibrate_dev_verifier.py` (cached) |
| 10 | **0/11 B2 passes** in **296.6 s** (8‑min budget) | Stock Antigravity, no skills/hooks/checker — all 11 profiles host-scored **FAIL** | `eval/results/b2_budget_20261121.json` | `make b2-budget` / `python scripts/run_b2_budget.py --budget-s 480` |
| 11 | **2 breaks / 2 fixes**; **14** hardener timeout files | Hardening tally + LIPA break-and-fix; repeated 90 s client timeouts | `eval/attacks/hardening_tally.jsonl`, `eval/results/final_20261121.json` → `hardening` | `make harden` (pause/resume: `make harden-pause` / `make harden-resume`) |
| 12 | **0.0143%** max IEEE FBM mode error; eval **~20 ms** mean | Shaft modes vs published Hz; one evaluation ≪ 0.5 s | `eval/results/final_20261121.json` → `phase1` | `make eval-final` / `make eval` |

## Bonus live-judge command

`make eval-live SEED=<n>` re-scores cached controllers on 20 fresh workloads for that seed and prints a pass table (**41.8 s** for `SEED=20261121` → `eval/results/eval_live_20261121.json`).

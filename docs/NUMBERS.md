# Twelve numbers a presenter can quote

Every value comes from a saved run file. Command shown is how to regenerate or re-print it.

| # | Number | Meaning | File | Command |
| ---: | --- | --- | --- | --- |
| 1 | **42.32 s** | `make eval-final` wall-clock | `eval/results/eval_final_timing.json` | `make eval-final SEED=20261121` |
| 2 | **33/33** (WI [89.6%, 100.0%]) | GridSafe preset pass rate | `eval/results/final_20261121.json` | `make eval-final` / `make report` |
| 3 | **33/33** | B3 preset pass — matches GridSafe | same → `method_summary.b3` | same |
| 4 | **9/11** | GridSafe random-suite all-pass | same | same |
| 5 | **11/11** | B0 random-suite all-pass | same | same |
| 6 | **4.051% OH / 11.59% TP** | GridSafe mean cost on passing presets | same | same |
| 7 | **P=0.6785714285714286 / R=1.0** | Extraction vs 6 gold | same + `eval/results/fp_breakdown.json` | `python scripts/classify_fps.py` |
| 8 | **1 invented/wrong · 8 quotes/dupes** | FP split | `eval/results/fp_breakdown.json` | same |
| 9 | **B2 run1 0/11** (mostly harness) | Thin-prompt B2 | `eval/results/b2_budget_20261121_run1_harness.json` | archived run |
| 10 | **B2 run2 6/10** in 455.997 s | Fair-prompt B2 | `eval/results/b2_budget_20261121_run2_fair_prompt.json` | `make b2-budget` |
| 11 | **2 breaks / 2 fixes · 20 timeouts / 19 rounds** | Hardening tally | `eval/attacks/hardening_tally.jsonl` | `make harden` |
| 12 | **SEED=4242 before→after** (see RESULTS) | Hardener holdout | `eval/results/harden_fix_4242.json` | `python scripts/run_harden_fix_eval.py` |

See also `RESULTS.md` § “Before and after the hardener agents” and § “B2 stock Antigravity”.

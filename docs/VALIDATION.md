# GridSafe Physics & Checker Validation

Generated from `scripts/generate_validation_doc.py` on 2026-11-21 17:58 UTC.
All numbers below come directly from deterministic runs of `sim/shaft.py`, `sim/plant.py`, `workload/synth.py`, and `rules/checker.py` (pure Python + `numpy`, no `scipy`).

## 1. IEEE First Benchmark Model Torsional Mode Validation

Source parameters: `config/fbm.yaml` (IEEE SSR Task Force, *First benchmark model for computer simulation of subsynchronous resonance*, IEEE Trans. PAS-96, no. 5, pp. 1565–1572, 1977).

- Base MVA: `892.4` MVA
- Base frequency: `60.0` Hz (`w0 = 2 * pi * 60` rad/s)
- Masses (`6`): `HP, IP, LPA, LPB, GEN, EXC`
- Shaft sections (`5`): `HP-IP, IP-LPA, LPA-LPB, LPB-GEN, GEN-EXC`
- Generalized eigenproblem: `M = diag(2 H / w0)`, symmetric transform `A = M^{-1/2} K M^{-1/2}`, solved with `numpy.linalg.eigh`.
- Rigid-body mode frequency: `0.000000` Hz

| Mode | Computed (Hz) | Published (Hz) | Error (Hz) | Error (%) | Dominant Section | `phi_GEN` | Gate (<= 2.0%) |
| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |
| Mode 1 | 15.7122 | 15.71 | +0.0022 | 0.0140% | `LPA-LPB` (55.2% strain E) | -8.3548 | **PASS** |
| Mode 2 | 20.2113 | 20.21 | +0.0013 | 0.0066% | `GEN-EXC` (91.5% strain E) | -2.6045 | **PASS** |
| Mode 3 | 25.5472 | 25.55 | -0.0028 | 0.0111% | `IP-LPA` (43.8% strain E) | +5.2197 | **PASS** |
| Mode 4 | 32.2846 | 32.28 | +0.0046 | 0.0143% | `LPB-GEN` (56.4% strain E) | -6.9324 | **PASS** |
| Mode 5 | 47.4563 | 47.46 | -0.0037 | 0.0078% | `HP-IP` (58.2% strain E) | -0.1292 | **PASS** |

**Max relative error:** `0.0143%` (gate: `<= 2.0%`) -> **PASS**.

## 2. Plant Simulation, Energy Conservation & Latency Gates

- Evaluation latency gate (`< 500 ms` per full synth + time-stepped plant + frequency-domain shaft + checker evaluation):
  - Mean: `19.5 ms`
  - Max: `42.2 ms` -> **PASS**
- Energy conservation ledger relative residual (`|E_grid - (E_final - E_init) - (E_gpu + E_batt_loss)| / E_throughput`):
  - Max relative residual across all fast (20 s) + slow (30 min) presets: `6.727e-15` (gate `< 1e-12`; abs max `3.553e-14 MWh`) -> **PASS**

### Preset Workload Benchmarks (100 MW cluster, fast 20 s @ 2 ms & slow 30 min @ 0.5 s)

| Preset | Eval Time (ms) | Uncontrolled Peak Torque (pu) | B1 (90% Floor) Peak Torque (pu) | B1 Energy Overhead (%) |
| --- | ---: | ---: | ---: | ---: |
| `standard_training` | 42.2 | 0.0866 | 0.0177 | 15.9% |
| `ssr_mode1_attack` | 22.2 | 1.6504 | 0.3220 | 32.0% |
| `ssr_mode2_attack` | 23.5 | 0.1509 | 0.0258 | 32.7% |
| `abrupt_checkpoint_drop` | 19.7 | 0.0904 | 0.0182 | 29.9% |
| `slow_training_run` | 9.5 | 0.0458 | 0.0039 | 30.2% |
| `slow_heavy_checkpoints` | 8.7 | 0.0362 | 0.0049 | 54.3% |
| `slow_step_job_cycle` | 10.8 | 0.0407 | 0.0040 | 43.3% |

## 3. Modeling Simplifications & Assumptions (Disclosed)

- **Modal damping**: Linear modal damping ratio `zeta = 0.005` is assumed uniformly across torsional modes in the frequency-domain transfer function `H_m(w)`. Real turbine-generator units exhibit mode-dependent steam-damping and electrical network negative damping near series-compensated lines.
- **Electrical coupling**: Net cluster load perturbation `dP_grid(t) / S_base` is applied directly as an electrical torque perturbation at the generator bus (`coupling_factor = 1.0` default) without full AC network impedance or sub-transient flux dynamics.
- **Battery model**: Idealized power-limited, SOC-limited energy storage with symmetric charge/discharge efficiency (`sqrt(0.90)` each way for 90% round-trip) and a C-rate-weighted Equivalent Full Cycles (EFC) wear proxy rather than an electrochemical degradation model.
- **Pure Python + `numpy` sandbox compatibility**: `workload/`, `sim/`, and `rules/` have zero `scipy` or `PyMuPDF` imports (verified by automated AST unit test) so the managed agent sandbox runs the exact same physics and checker code as the backend.

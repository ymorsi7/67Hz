"""Multi-mass turbine-generator shaft dynamics (IEEE First Benchmark Model).

Pure Python + numpy (no scipy). Loads inertia constants H, shaft stiffnesses K,
and published torsional mode frequencies from config/fbm.yaml (never hardcoded
or invented). Computes frequency-domain torsional shaft torque and angular
twist responses via modal decomposition and FFT.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repo_paths import find_repo_root
from typing import Any

import numpy as np
import yaml

DEFAULT_FBM_PATH = find_repo_root() / "config" / "fbm.yaml"
DEFAULT_MODAL_DAMPING_RATIO = 0.005  # Light mechanical/electrical damping assumption


@dataclass
class ShaftResponse:
    """Frequency-domain and inverse-FFT time-domain turbine-generator shaft response."""

    freqs_hz: np.ndarray
    power_spectrum_mw: np.ndarray  # One-sided FFT amplitude spectrum of net grid power (MW)
    section_names: list[str]
    section_torque_pu: np.ndarray  # Shape (n_sections, N), time-domain torque perturbation (pu)
    section_torque_spectrum_pu: np.ndarray  # Shape (n_sections, n_freqs), amplitude spectrum (pu)
    mass_twist_rad: np.ndarray  # Shape (n_masses, N), relative torsional angle (elec. rad)
    peak_torque_per_section_pu: dict[str, float]
    max_torque_pu: float
    dominant_section: str
    dominant_freq_hz: float
    modal_frequencies_hz: list[float]
    illustrative: bool = False


class ShaftModel:
    """6-mass (or arbitrary N-mass) torsional turbine-generator shaft model."""

    def __init__(
        self,
        *,
        base_mva: float,
        base_frequency_hz: float,
        masses: list[str],
        inertia_H_s: np.ndarray,
        stiffness_K_pu_per_rad: np.ndarray,
        published_modes_hz: list[float] | None = None,
        modal_damping_ratio: float = DEFAULT_MODAL_DAMPING_RATIO,
        generator_mass_name: str = "GEN",
        illustrative: bool = False,
        source_path: str | None = None,
    ) -> None:
        self.base_mva = float(base_mva)
        self.base_frequency_hz = float(base_frequency_hz)
        self.w0 = 2.0 * np.pi * self.base_frequency_hz
        self.masses = list(masses)
        self.inertia_H_s = np.asarray(inertia_H_s, dtype=np.float64)
        self.stiffness_K = np.asarray(stiffness_K_pu_per_rad, dtype=np.float64)
        self.published_modes_hz = list(published_modes_hz) if published_modes_hz is not None else []
        self.modal_damping_ratio = float(modal_damping_ratio)
        self.illustrative = bool(illustrative)
        self.source_path = source_path

        n = len(self.masses)
        if len(self.inertia_H_s) != n:
            raise ValueError("masses and inertia_H_s must have the same length")
        if len(self.stiffness_K) != n - 1:
            raise ValueError("stiffness_K must have length len(masses) - 1")

        self.section_names = [f"{self.masses[i]}-{self.masses[i + 1]}" for i in range(n - 1)]
        if generator_mass_name in self.masses:
            self.gen_index = self.masses.index(generator_mass_name)
        else:
            self.gen_index = max(0, n - 1)

        # Mass matrix diagonal M_i = 2 * H_i / w0  [pu * s^2 / elec. rad]
        self.M_diag = (2.0 * self.inertia_H_s) / self.w0

        # Tridiagonal stiffness matrix K [pu / elec. rad]
        K_mat = np.zeros((n, n), dtype=np.float64)
        for i, ki in enumerate(self.stiffness_K):
            K_mat[i, i] += ki
            K_mat[i + 1, i + 1] += ki
            K_mat[i, i + 1] -= ki
            K_mat[i + 1, i] -= ki
        self.K_mat = K_mat

        # Solve symmetric generalized eigenproblem K phi = omega^2 M phi
        # via A = M^{-1/2} K M^{-1/2} using pure numpy eigh.
        inv_sqrt_m = 1.0 / np.sqrt(self.M_diag)
        A = (inv_sqrt_m[:, None] * K_mat) * inv_sqrt_m[None, :]
        eigvals, U = np.linalg.eigh(A)
        eigvals = np.maximum(eigvals, 0.0)

        # Mass-normalized mode shapes Phi = M^{-1/2} U, so Phi^T M Phi = I
        self.omega_all_rad_s = np.sqrt(eigvals)
        self.freqs_all_hz = self.omega_all_rad_s / (2.0 * np.pi)
        self.phi_all = inv_sqrt_m[:, None] * U  # shape (n_masses, n_modes)

        # Drop rigid-body mode (index 0, omega == 0) for torsional twist calculation
        self.torsional_omega_rad_s = self.omega_all_rad_s[1:]
        self.computed_modes_hz = [float(f) for f in self.freqs_all_hz[1:]]
        self.phi_torsional = self.phi_all[:, 1:]  # shape (n_masses, n_torsional)

        # Relative mode-shape difference across each shaft section: dphi[s, m] = phi[s+1, m] - phi[s, m]
        self.dphi_sections = self.phi_torsional[1:, :] - self.phi_torsional[:-1, :]
        self.phi_gen = self.phi_torsional[self.gen_index, :]  # shape (n_torsional,)

    def modal_dominant_sections(self) -> list[tuple[str, float]]:
        """Return (section_name, strain_energy_share_pct) per torsional mode.

        Uses modal elastic strain energy 0.5 * K_s * (dphi_{s,m})^2 so stiff
        sections do not bias mode identification away from where the modal
        elastic deformation actually concentrates.
        """
        out: list[tuple[str, float]] = []
        for m in range(self.dphi_sections.shape[1]):
            strain_e = 0.5 * self.stiffness_K * (self.dphi_sections[:, m] ** 2)
            dom_idx = int(np.argmax(strain_e))
            total_e = float(np.sum(strain_e))
            share_pct = float(strain_e[dom_idx] / total_e * 100.0) if total_e > 0 else 0.0
            out.append((self.section_names[dom_idx], share_pct))
        return out

    def validate_modes(self, max_error_pct: float = 2.0) -> dict[str, Any]:
        """Compare computed torsional mode frequencies against published values."""
        if not self.published_modes_hz:
            return {
                "passed": False,
                "max_error_pct": None,
                "threshold_pct": max_error_pct,
                "modes": [],
                "illustrative": self.illustrative,
                "reason": "No published mode frequencies supplied.",
            }
        if len(self.computed_modes_hz) != len(self.published_modes_hz):
            raise ValueError(
                f"Mode count mismatch: computed {len(self.computed_modes_hz)} vs published {len(self.published_modes_hz)}"
            )

        rows: list[dict[str, float | int | bool]] = []
        max_err = 0.0
        for idx, (comp, pub) in enumerate(zip(self.computed_modes_hz, self.published_modes_hz), start=1):
            err_hz = comp - pub
            err_pct = abs(err_hz) / pub * 100.0
            max_err = max(max_err, err_pct)
            rows.append(
                {
                    "mode": idx,
                    "computed_hz": comp,
                    "published_hz": pub,
                    "error_hz": err_hz,
                    "error_pct": err_pct,
                    "within_gate": err_pct <= max_error_pct,
                }
            )
        return {
            "passed": max_err <= max_error_pct,
            "max_error_pct": max_err,
            "threshold_pct": max_error_pct,
            "modes": rows,
            "illustrative": self.illustrative,
        }

    def compute_shaft_response(
        self,
        p_grid_mw: np.ndarray,
        dt_s: float,
        coupling_factor: float = 1.0,
    ) -> ShaftResponse:
        """Compute frequency-domain shaft torques and mass twists from net grid power (MW)."""
        p = np.asarray(p_grid_mw, dtype=np.float64)
        n_samples = len(p)
        if n_samples < 2:
            raise ValueError("p_grid_mw must contain at least 2 samples")

        # Detrend DC operating point (steady-state electrical torque balances mechanical input)
        dp_mw = p - float(np.mean(p))
        freqs_hz = np.fft.rfftfreq(n_samples, d=dt_s)
        w = 2.0 * np.pi * freqs_hz  # shape (n_freqs,)

        # One-sided FFT amplitude spectrum of grid power fluctuation (MW)
        dp_fft = np.fft.rfft(dp_mw)
        scale = np.full(len(freqs_hz), 2.0 / n_samples, dtype=np.float64)
        scale[0] = 1.0 / n_samples
        if n_samples % 2 == 0:
            scale[-1] = 1.0 / n_samples
        power_spectrum_mw = np.abs(dp_fft) * scale

        # Electrical torque perturbation at generator bus (pu)
        d_te_fft = (dp_fft / self.base_mva) * float(coupling_factor)

        # Modal transfer functions H_m(w) = 1 / ((w_m^2 - w^2) + j * 2 * zeta * w_m * w)
        wm = self.torsional_omega_rad_s[:, None]  # (n_modes, 1)
        w_row = w[None, :]  # (1, n_freqs)
        denom = (wm**2 - w_row**2) + 1j * (2.0 * self.modal_damping_ratio * wm * w_row)
        H_modal = 1.0 / denom  # (n_modes, n_freqs)

        # Modal responses Q_m(w) = H_m(w) * phi_gen_m * (-dTe(w))
        Q_modal = H_modal * (self.phi_gen[:, None] * (-d_te_fft[None, :]))

        # Mass angular deflections Theta(w) = Phi @ Q(w)  [n_masses, n_freqs]
        theta_fft = self.phi_torsional @ Q_modal
        mass_twist_rad = np.fft.irfft(theta_fft, n=n_samples, axis=1)

        # Section torques T_s(w) = k_s * (dPhi @ Q(w))  [n_sections, n_freqs]
        torque_fft = (self.stiffness_K[:, None]) * (self.dphi_sections @ Q_modal)
        section_torque_pu = np.fft.irfft(torque_fft, n=n_samples, axis=1)
        section_torque_spectrum_pu = np.abs(torque_fft) * scale[None, :]

        peak_per_section: dict[str, float] = {}
        for idx, name in enumerate(self.section_names):
            peak_per_section[name] = float(np.max(np.abs(section_torque_pu[idx])))

        dominant_idx = int(np.argmax([peak_per_section[s] for s in self.section_names]))
        dominant_section = self.section_names[dominant_idx]
        max_torque_pu = peak_per_section[dominant_section]

        # Dominant frequency of the highest-stress section (excluding DC bin 0)
        dom_spec = section_torque_spectrum_pu[dominant_idx]
        if len(dom_spec) > 1:
            dom_f_idx = int(np.argmax(dom_spec[1:])) + 1
            dominant_freq_hz = float(freqs_hz[dom_f_idx])
        else:
            dominant_freq_hz = 0.0

        return ShaftResponse(
            freqs_hz=freqs_hz,
            power_spectrum_mw=power_spectrum_mw,
            section_names=list(self.section_names),
            section_torque_pu=section_torque_pu,
            section_torque_spectrum_pu=section_torque_spectrum_pu,
            mass_twist_rad=mass_twist_rad,
            peak_torque_per_section_pu=peak_per_section,
            max_torque_pu=max_torque_pu,
            dominant_section=dominant_section,
            dominant_freq_hz=dominant_freq_hz,
            modal_frequencies_hz=list(self.computed_modes_hz),
            illustrative=self.illustrative,
        )


def load_shaft_model(
    path: str | Path | None = None,
    modal_damping_ratio: float = DEFAULT_MODAL_DAMPING_RATIO,
) -> ShaftModel:
    """Load IEEE First Benchmark Model shaft parameters from YAML."""
    cfg_path = Path(path) if path is not None else DEFAULT_FBM_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"IEEE FBM config not found at {cfg_path}. Supply config/fbm.yaml or explicitly call create_illustrative_shaft()."
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    return ShaftModel(
        base_mva=float(cfg["base_mva"]),
        base_frequency_hz=float(cfg["base_frequency_hz"]),
        masses=list(cfg["masses"]),
        inertia_H_s=np.asarray(cfg["inertia_constant_H_seconds"], dtype=np.float64),
        stiffness_K_pu_per_rad=np.asarray(cfg["shaft_stiffness_K_pu_per_rad"], dtype=np.float64),
        published_modes_hz=[float(x) for x in cfg.get("published_torsional_modes_hz", [])],
        modal_damping_ratio=modal_damping_ratio,
        illustrative=False,
        source_path=str(cfg_path),
    )


def create_illustrative_shaft(
    resonant_freq_hz: float = 16.0,
    base_mva: float = 500.0,
    modal_damping_ratio: float = 0.01,
) -> ShaftModel:
    """Explicit single-mode fallback shaft clearly labeled as illustrative."""
    w0 = 2.0 * np.pi * 60.0
    h1, h2 = 1.0, 1.0
    m1 = 2.0 * h1 / w0
    m2 = 2.0 * h2 / w0
    m_eff = (m1 * m2) / (m1 + m2)
    k = ((2.0 * np.pi * resonant_freq_hz) ** 2) * m_eff
    return ShaftModel(
        base_mva=base_mva,
        base_frequency_hz=60.0,
        masses=["TURB", "GEN"],
        inertia_H_s=np.array([h1, h2], dtype=np.float64),
        stiffness_K_pu_per_rad=np.array([k], dtype=np.float64),
        published_modes_hz=[resonant_freq_hz],
        modal_damping_ratio=modal_damping_ratio,
        illustrative=True,
        source_path=None,
    )

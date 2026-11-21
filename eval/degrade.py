"""Deterministic page image degradation at 4 severities for robustness evaluation.

Applies rotation, Gaussian blur, low-resolution downscale/upscale, affine skew,
partial crop, and sensor noise using numpy + scipy.ndimage + PyMuPDF (backend-only).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pymupdf
from scipy.ndimage import affine_transform, gaussian_filter, rotate, zoom


@dataclass(frozen=True)
class DegradationSpec:
    """Parameters defining one severity level of physical document/camera degradation."""

    severity: int  # 1 (mild) .. 4 (extreme / unreadable)
    rotation_deg: float
    blur_sigma: float
    res_scale: float  # Downscale factor in (0, 1]
    skew_shear: float  # Horizontal shear tangent
    crop_fraction: float  # Fraction trimmed from margins in [0, 0.4]
    noise_std: float  # Additive pixel noise std in [0, 255]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SEVERITY_PRESETS: dict[int, DegradationSpec] = {
    1: DegradationSpec(
        severity=1,
        rotation_deg=2.5,
        blur_sigma=0.6,
        res_scale=0.80,
        skew_shear=0.02,
        crop_fraction=0.02,
        noise_std=4.0,
    ),
    2: DegradationSpec(
        severity=2,
        rotation_deg=6.0,
        blur_sigma=1.4,
        res_scale=0.55,
        skew_shear=0.05,
        crop_fraction=0.06,
        noise_std=10.0,
    ),
    3: DegradationSpec(
        severity=3,
        rotation_deg=11.0,
        blur_sigma=2.5,
        res_scale=0.35,
        skew_shear=0.09,
        crop_fraction=0.12,
        noise_std=18.0,
    ),
    4: DegradationSpec(
        severity=4,
        rotation_deg=18.0,
        blur_sigma=5.0,
        res_scale=0.16,
        skew_shear=0.14,
        crop_fraction=0.22,
        noise_std=32.0,
    ),
}


def png_bytes_to_rgb_array(png_bytes: bytes) -> np.ndarray:
    """Decode PNG/JPEG bytes to uint8 RGB numpy array (H, W, 3) via PyMuPDF."""
    pix = pymupdf.Pixmap(png_bytes)
    if pix.n != 3 or pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()
    return arr


def rgb_array_to_png_bytes(arr: np.ndarray) -> bytes:
    """Encode uint8 RGB numpy array (H, W, 3) to PNG bytes via PyMuPDF."""
    clipped = np.clip(np.rint(arr), 0, 255).astype(np.uint8)
    h, w, c = clipped.shape
    if c != 3:
        raise ValueError(f"Expected RGB array with 3 channels, got shape {clipped.shape}")
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, clipped.tobytes(), False)
    return pix.tobytes("png")


def degrade_image(
    image_bytes: bytes,
    severity: int,
    *,
    seed: int = 42,
    spec: DegradationSpec | None = None,
) -> tuple[bytes, DegradationSpec]:
    """Produce a deterministic degraded copy of a page image at severity 1..4."""
    if spec is None:
        if severity not in SEVERITY_PRESETS:
            raise ValueError(f"severity must be in 1..4, got {severity}")
        spec = SEVERITY_PRESETS[severity]

    rng = np.random.default_rng(seed + spec.severity * 1009)
    arr = png_bytes_to_rgb_array(image_bytes).astype(np.float32)
    h, w, _ = arr.shape

    # 1. Partial crop (asymmetric margins simulating off-center camera framing)
    if spec.crop_fraction > 0.0:
        f = spec.crop_fraction
        top = int(round(h * f * rng.uniform(0.3, 0.8)))
        bottom = max(top + 32, h - int(round(h * f * rng.uniform(0.3, 0.8))))
        left = int(round(w * f * rng.uniform(0.3, 0.8)))
        right = max(left + 32, w - int(round(w * f * rng.uniform(0.3, 0.8))))
        arr = arr[top:bottom, left:right, :]

    # 2. Rotation (signed randomly with deterministic seed)
    if spec.rotation_deg > 0.0:
        sign = 1.0 if rng.random() >= 0.5 else -1.0
        angle = sign * spec.rotation_deg
        arr = rotate(arr, angle=angle, axes=(0, 1), reshape=False, order=1, mode="constant", cval=245.0)

    # 3. Perspective-like horizontal skew (affine shear)
    if spec.skew_shear > 0.0:
        sign = 1.0 if rng.random() >= 0.5 else -1.0
        shear = sign * spec.skew_shear
        mat = np.array([[1.0, shear], [0.0, 1.0]], dtype=np.float64)
        offset = np.array([-0.5 * shear * arr.shape[1], 0.0], dtype=np.float64)
        sheared = np.empty_like(arr)
        for ch in range(3):
            sheared[:, :, ch] = affine_transform(
                arr[:, :, ch], matrix=mat, offset=offset, order=1, mode="constant", cval=245.0
            )
        arr = sheared

    # 4. Low resolution (downsample then upsample back)
    if 0.0 < spec.res_scale < 1.0:
        small = zoom(arr, zoom=(spec.res_scale, spec.res_scale, 1.0), order=1)
        up_h = arr.shape[0] / max(1, small.shape[0])
        up_w = arr.shape[1] / max(1, small.shape[1])
        arr = zoom(small, zoom=(up_h, up_w, 1.0), order=1)

    # 5. Optical defocus / motion blur
    if spec.blur_sigma > 0.0:
        for ch in range(3):
            arr[:, :, ch] = gaussian_filter(arr[:, :, ch], sigma=spec.blur_sigma)

    # 6. Camera lighting noise
    if spec.noise_std > 0.0:
        arr = arr + rng.normal(0.0, spec.noise_std, size=arr.shape).astype(np.float32)

    return rgb_array_to_png_bytes(arr), spec


def generate_degraded_suite(
    image_bytes: bytes,
    severities: tuple[int, ...] = (1, 2, 3, 4),
    seed: int = 42,
) -> dict[int, tuple[bytes, DegradationSpec]]:
    """Return degraded PNG bytes across all 4 severities."""
    return {sev: degrade_image(image_bytes, severity=sev, seed=seed) for sev in severities}

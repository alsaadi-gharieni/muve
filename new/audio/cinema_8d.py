"""8D cinema spatial processing for Satori L/R speakers and body transducers."""

from __future__ import annotations

import numpy as np


def apply_8d_stereo_pan(
    stereo: np.ndarray,
    sample_rate: int,
    depth: float = 0.72,
    primary_rate_hz: float = 0.12,
) -> np.ndarray:
    """Auto-pan stereo speakers for circular 8D-style movement on L/R."""
    stereo = stereo.astype(np.float32)
    n = len(stereo)
    if n == 0:
        return stereo

    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    # Layered LFOs: slow orbit + faster wobble (typical 8D feel).
    pan = depth * (
        0.55 * np.sin(2.0 * np.pi * primary_rate_hz * t)
        + 0.30 * np.sin(2.0 * np.pi * primary_rate_hz * 2.17 * t + 0.9)
        + 0.15 * np.sin(2.0 * np.pi * primary_rate_hz * 0.47 * t + 2.1)
    )
    pan = np.clip(pan, -1.0, 1.0)

    # Equal-power panning on mono sum while preserving some stereo width.
    mono = (stereo[:, 0] + stereo[:, 1]) * 0.5
    side = (stereo[:, 0] - stereo[:, 1]) * 0.5
    left_gain = np.sqrt(0.5 * (1.0 - pan))
    right_gain = np.sqrt(0.5 * (1.0 + pan))
    width = 0.35

    left = mono * left_gain + side * width
    right = mono * right_gain - side * width
    return np.column_stack([left, right]).astype(np.float32)


def apply_8d_vibration_motion(
    legs: np.ndarray,
    mid: np.ndarray,
    upper_mid: np.ndarray,
    head: np.ndarray,
    sample_rate: int,
    drum_impacts: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sweep vibration across Satori zones: LFE/C vertical + Ls/Rs horizontal roll."""
    n = len(legs)
    if n == 0:
        return legs, mid, upper_mid, head

    t = np.arange(n, dtype=np.float32) / float(sample_rate)

    # Horizontal roll: upper body transducers (Ls/Rs) alternate L <-> R.
    h_orbit = np.sin(2.0 * np.pi * 0.11 * t) + 0.4 * np.sin(2.0 * np.pi * 0.27 * t + 1.4)
    h_orbit = h_orbit / (np.max(np.abs(h_orbit)) + 1e-7)
    ls_motion = np.clip(0.55 + 0.45 * h_orbit, 0.2, 1.0)
    rs_motion = np.clip(0.55 - 0.45 * h_orbit, 0.2, 1.0)

    # Vertical wave: sub (LFE) and center (C) pulse in opposition.
    v_wave = np.sin(2.0 * np.pi * 0.06 * t + 0.5) + 0.35 * np.sin(2.0 * np.pi * 0.14 * t)
    v_wave = v_wave / (np.max(np.abs(v_wave)) + 1e-7)
    lfe_motion = np.clip(0.75 + 0.35 * v_wave, 0.45, 1.25)
    c_motion = np.clip(0.75 - 0.30 * v_wave, 0.45, 1.15)

    legs_out = legs * lfe_motion.astype(np.float32)
    mid_out = mid * c_motion.astype(np.float32)
    upper_out = upper_mid * ls_motion.astype(np.float32)
    head_out = head * rs_motion.astype(np.float32)

    # Cinema impacts: drum transients punch the sub (LFE).
    if drum_impacts is not None and len(drum_impacts) >= n:
        impact = drum_impacts[:n].astype(np.float32)
        impact = impact / (np.max(impact) + 1e-7)
        legs_out = legs_out + impact * legs * 0.45

    return (
        np.clip(legs_out, -1.0, 1.0).astype(np.float32),
        np.clip(mid_out, -1.0, 1.0).astype(np.float32),
        np.clip(upper_out, -1.0, 1.0).astype(np.float32),
        np.clip(head_out, -1.0, 1.0).astype(np.float32),
    )


def drum_impact_envelope(drums_mono: np.ndarray, sample_rate: int) -> np.ndarray:
    """Extract transient envelope from drums for LFE impact accents."""
    diff = np.abs(np.diff(np.pad(drums_mono.astype(np.float32), (1, 0))))
    window = max(int(sample_rate * 0.02), 64)
    kernel = np.ones(window, dtype=np.float32) / float(window)
    return np.convolve(diff**2, kernel, mode="same")

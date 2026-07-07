"""Synthetic vibration generator types."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SyntheticType:
    id: str
    label: str
    description: str


SYNTHETIC_TYPES: list[SyntheticType] = [
    SyntheticType(
        "sine",
        "Sine (smooth)",
        "Clean sine wave; frequency and amplitude follow music energy.",
    ),
    SyntheticType(
        "pulse",
        "Pulse (rhythmic)",
        "Rounded pulses synced to energy; sharper, more percussive feel.",
    ),
    SyntheticType(
        "dual",
        "Dual Tone (beating)",
        "Two close frequencies for a slow beating tactile sensation.",
    ),
    SyntheticType(
        "sweep",
        "Sweep (moving)",
        "Slow frequency sweep combined with energy-linked pitch.",
    ),
    SyntheticType(
        "soft",
        "Soft (healing)",
        "Gentle sine with smoothed amplitude for ambient/healing tracks.",
    ),
    SyntheticType(
        "rumble",
        "Rumble (harmonic)",
        "Sine plus harmonics for a richer, cinema-style sub rumble.",
    ),
]


def get_synthetic_type(type_id: str) -> SyntheticType:
    for synth in SYNTHETIC_TYPES:
        if synth.id == type_id:
            return synth
    return SYNTHETIC_TYPES[0]


def _energy_norm(energy: np.ndarray, reference_peak: float | None = None) -> np.ndarray:
    peak = float(reference_peak) if reference_peak is not None else float(np.max(energy))
    return np.clip(energy / (peak + 1e-7), 0.0, 1.0).astype(np.float32)


def _smooth_track(signal: np.ndarray, window: int) -> np.ndarray:
    window = max(int(window), 1)
    kernel = np.ones(window, dtype=np.float32) / float(window)
    return np.convolve(signal.astype(np.float32), kernel, mode="same")


def _prepare_energy(
    energy: np.ndarray,
    sample_rate: int,
    gentle: bool,
    energy_reference_peak: float | None = None,
) -> np.ndarray:
    """Normalize and smooth energy so synthetic vibration follows musical swells, not noise."""
    norm = _energy_norm(energy, reference_peak=energy_reference_peak)
    smooth_window = int(sample_rate * (0.14 if gentle else 0.05))
    norm = _smooth_track(norm, max(smooth_window, 2048 if gentle else 512))
    if gentle:
        # Fade out quiet sections; soften peaks for ambient/healing content.
        norm = np.clip((norm - 0.10) / 0.90, 0.0, 1.0)
        norm = norm**1.5
    else:
        # Gate low energy so loopback noise does not sustain a constant sine tone.
        norm = np.clip((norm - 0.14) / 0.86, 0.0, 1.0)
        norm = norm**1.25
    return norm.astype(np.float32)


def _smooth_freq(freq: np.ndarray, sample_rate: int, gentle: bool) -> np.ndarray:
    window = int(sample_rate * (0.10 if gentle else 0.04))
    return _smooth_track(freq, max(window, 2048 if gentle else 512)).astype(np.float32)


def _freq_from_energy(
    norm: np.ndarray,
    sample_rate: int,
    freq_range: tuple[float, float],
    gentle: bool = False,
) -> np.ndarray:
    f_min, f_max = freq_range
    freq = (f_min + (f_max - f_min) * norm).astype(np.float32)
    return _smooth_freq(freq, sample_rate, gentle)


def _phase_from_freq(freq: np.ndarray, sample_rate: int) -> np.ndarray:
    phase_increment = (2.0 * np.pi * freq) / float(sample_rate)
    return np.cumsum(phase_increment).astype(np.float32)


def _amp_from_norm(norm: np.ndarray, sample_rate: int, peak: float, gentle: bool) -> np.ndarray:
    amp = np.clip(norm * peak, 0.0, 1.0).astype(np.float32)
    if gentle:
        window = max(int(sample_rate * 0.10), 4096)
        amp = _smooth_track(amp, window)
    return amp


def generate_synthetic_wave(
    energy: np.ndarray,
    sample_rate: int,
    synth_type_id: str,
    freq_range: tuple[float, float] | None = None,
    gentle: bool = False,
    energy_reference_peak: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (waveform, dominant_freq_track)."""
    f_min, f_max = freq_range or (35.0, 50.0)
    norm = _prepare_energy(energy, sample_rate, gentle, energy_reference_peak=energy_reference_peak)
    n = len(energy)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    peak = 0.52 if gentle else 0.82

    if synth_type_id == "pulse":
        freq = _freq_from_energy(norm, sample_rate, (f_min, f_max), gentle)
        phase = _phase_from_freq(freq, sample_rate)
        carrier = np.sin(phase)
        amp = _amp_from_norm(norm, sample_rate, peak * 0.95, gentle)
        wave = np.sign(carrier) * np.sqrt(np.abs(carrier)) * amp
        return wave.astype(np.float32), freq

    if synth_type_id == "dual":
        freq = _freq_from_energy(norm, sample_rate, (f_min, f_max), gentle)
        freq_b = freq * 1.06
        phase_a = _phase_from_freq(freq, sample_rate)
        phase_b = _phase_from_freq(freq_b, sample_rate)
        amp = _amp_from_norm(norm, sample_rate, peak * 0.9, gentle)
        wave = amp * (0.65 * np.sin(phase_a) + 0.35 * np.sin(phase_b))
        return wave.astype(np.float32), freq

    if synth_type_id == "sweep":
        slow = 0.04 * np.sin(2.0 * np.pi * 0.07 * t)
        freq = _freq_from_energy(norm, sample_rate, (f_min, f_max), gentle)
        freq = np.clip(freq * (1.0 + slow), f_min, f_max * 1.15).astype(np.float32)
        freq = _smooth_freq(freq, sample_rate, gentle)
        phase = _phase_from_freq(freq, sample_rate)
        amp = _amp_from_norm(norm, sample_rate, peak, gentle)
        wave = np.sin(phase) * amp
        return wave.astype(np.float32), freq

    if synth_type_id == "soft":
        soft_range = (f_min, min(f_max, f_min + (8.0 if gentle else 12.0)))
        freq = _freq_from_energy(norm, sample_rate, soft_range, gentle=True)
        phase = _phase_from_freq(freq, sample_rate)
        amp = _amp_from_norm(norm, sample_rate, 0.48 if gentle else 0.72, gentle=True)
        wave = np.sin(phase) * amp
        return wave.astype(np.float32), freq

    if synth_type_id == "rumble":
        freq = _freq_from_energy(norm, sample_rate, (f_min, f_max), gentle)
        phase = _phase_from_freq(freq, sample_rate)
        amp = _amp_from_norm(norm, sample_rate, peak * 0.85, gentle)
        fundamental = np.sin(phase)
        harmonic = 0.22 * np.sin(phase * 2.0)
        sub = 0.12 * np.sin(phase * 0.5)
        wave = amp * (fundamental + harmonic + sub)
        return wave.astype(np.float32), freq

    # Default: sine
    freq = _freq_from_energy(norm, sample_rate, (f_min, f_max), gentle)
    phase = _phase_from_freq(freq, sample_rate)
    amp = _amp_from_norm(norm, sample_rate, peak, gentle)
    wave = np.sin(phase) * amp
    return wave.astype(np.float32), freq

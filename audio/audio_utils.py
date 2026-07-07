"""Shared audio helpers."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from scipy.signal import resample_poly

from audio.cinema_8d import drum_impact_envelope

TARGET_SR = 44100
# ~20 minutes at 44.1 kHz keeps memory reasonable on Raspberry Pi 4.
MAX_SAMPLES = TARGET_SR * 60 * 20


def ensure_stereo(audio: np.ndarray) -> np.ndarray:
    """Convert mono (1-D or Nx1) audio to stereo Nx2."""
    if audio.ndim == 1:
        return np.column_stack([audio, audio]).astype(np.float32)
    if audio.shape[1] == 1:
        return np.repeat(audio, 2, axis=1).astype(np.float32)
    if audio.shape[1] > 2:
        return audio[:, :2].astype(np.float32)
    return audio.astype(np.float32)


def to_mono(audio: np.ndarray) -> np.ndarray:
    if audio.ndim == 1:
        return audio.astype(np.float32)
    return audio.mean(axis=1).astype(np.float32)


def resample_if_needed(data: np.ndarray, src_sr: int, target_sr: int) -> np.ndarray:
    if src_sr == target_sr:
        return data.astype(np.float32)
    gcd = np.gcd(src_sr, target_sr)
    up = target_sr // gcd
    down = src_sr // gcd
    return resample_poly(data, up, down, axis=0).astype(np.float32)


def normalize_prepared_mix(mix) -> object:
    """Trim stems to a common length and cache mono / drum-impact arrays for rebuilds."""
    if (
        mix.bass_mono is not None
        and mix.drums_mono is not None
        and mix.other_mono is not None
        and mix.drum_impacts is not None
        and len(mix.bass) == len(mix.bass_mono)
    ):
        return mix

    bass = ensure_stereo(mix.bass)
    drums = ensure_stereo(mix.drums)
    other = ensure_stereo(mix.other)
    original = ensure_stereo(mix.original)
    min_len = min(len(bass), len(drums), len(other), len(original))
    bass = bass[:min_len].astype(np.float32, copy=False)
    drums = drums[:min_len].astype(np.float32, copy=False)
    other = other[:min_len].astype(np.float32, copy=False)
    original = original[:min_len].astype(np.float32, copy=False)
    drums_mono = to_mono(drums)
    return replace(
        mix,
        bass=bass,
        drums=drums,
        other=other,
        original=original,
        bass_mono=to_mono(bass),
        drums_mono=drums_mono,
        other_mono=to_mono(other),
        drum_impacts=drum_impact_envelope(drums_mono, mix.sample_rate),
    )

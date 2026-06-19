"""Stem loading and fallback logic (Demucs -> Spleeter)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

import numpy as np
import soundfile as sf

from audio.audio_utils import MAX_SAMPLES, TARGET_SR, ensure_stereo, resample_if_needed
from processors.demucs_processor import DemucsError, demucs_is_available, run_demucs
from processors.spleeter_processor import SpleeterError, run_spleeter, spleeter_is_available


class StemSeparationError(RuntimeError):
    """Raised when neither backend can provide valid stems."""


def _load_audio(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return data, int(sr)


def _pad_or_trim(stems: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    min_len = min(len(stem) for stem in stems.values())
    if min_len > MAX_SAMPLES:
        min_len = MAX_SAMPLES
    return {name: stem[:min_len].astype(np.float32) for name, stem in stems.items()}


def _read_stems_from_dir(stem_dir: Path) -> Dict[str, np.ndarray]:
    expected = ["vocals", "bass", "drums", "other"]
    loaded: Dict[str, np.ndarray] = {}

    for stem in expected:
        stem_path = stem_dir / f"{stem}.wav"
        if not stem_path.exists():
            raise StemSeparationError(f"Missing stem: {stem_path}")
        audio, sr = _load_audio(stem_path)
        loaded[stem] = ensure_stereo(resample_if_needed(audio, sr, TARGET_SR))

    loaded = _pad_or_trim({k: loaded[k] for k in expected})
    loaded["sample_rate"] = TARGET_SR
    return loaded


def _load_source_original(source: Path, target_length: int) -> np.ndarray:
    """Load the untouched source file for speaker channels 1-2."""
    source_audio, src_sr = _load_audio(source)
    source_audio = ensure_stereo(resample_if_needed(source_audio, src_sr, TARGET_SR))
    if len(source_audio) > target_length:
        source_audio = source_audio[:target_length]
    elif len(source_audio) < target_length:
        pad = np.zeros((target_length - len(source_audio), 2), dtype=np.float32)
        source_audio = np.vstack([source_audio, pad])
    return source_audio.astype(np.float32)


def separate_stems(source_path: str, workspace_dir: str) -> Dict[str, np.ndarray]:
    """Run Demucs first, then fallback to Spleeter, returning aligned stems."""
    source = Path(source_path)
    if not source.exists():
        raise StemSeparationError(f"Source file not found: {source_path}")

    attempts = []

    if demucs_is_available():
        try:
            stem_dir = run_demucs(source_path, os.path.join(workspace_dir, "demucs"))
            stems = _read_stems_from_dir(stem_dir)
            stems["original"] = _load_source_original(source, len(stems["bass"]))
            stems["quick_mode"] = False
            return stems
        except (DemucsError, StemSeparationError) as exc:
            attempts.append(f"Demucs error: {exc}")
    else:
        attempts.append("Demucs not installed.")

    if spleeter_is_available():
        try:
            stem_dir = run_spleeter(source_path, os.path.join(workspace_dir, "spleeter"))
            stems = _read_stems_from_dir(stem_dir)
            stems["original"] = _load_source_original(source, len(stems["bass"]))
            stems["quick_mode"] = False
            return stems
        except (SpleeterError, StemSeparationError) as exc:
            attempts.append(f"Spleeter error: {exc}")
    else:
        attempts.append("Spleeter not installed.")

    raise StemSeparationError("No separator available. " + " | ".join(attempts))

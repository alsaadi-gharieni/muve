"""Fast pseudo-stem generation without ML separation."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

from audio.audio_utils import MAX_SAMPLES, TARGET_SR, ensure_stereo, resample_if_needed, to_mono


def _load_source(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return data, int(sr)


def load_quick_stems(source_path: str) -> Dict[str, np.ndarray]:
    """Build approximate stems instantly so playback can start before Demucs finishes."""
    source = Path(source_path)
    audio, sr = _load_source(source)
    audio = ensure_stereo(resample_if_needed(audio, sr, TARGET_SR))

    if len(audio) > MAX_SAMPLES:
        audio = audio[:MAX_SAMPLES]

    mono = to_mono(audio)
    low_sos = butter(4, 180, btype="low", fs=TARGET_SR, output="sos")
    high_sos = butter(4, 180, btype="high", fs=TARGET_SR, output="sos")
    mid_sos = butter(4, [80, 4000], btype="bandpass", fs=TARGET_SR, output="sos")

    bass_mono = sosfilt(low_sos, mono)
    other_mono = sosfilt(high_sos, mono)
    drums_mono = sosfilt(mid_sos, np.abs(np.diff(np.pad(mono, (1, 0)))))

    return {
        "sample_rate": TARGET_SR,
        "bass": ensure_stereo(bass_mono),
        "drums": ensure_stereo(drums_mono),
        "other": ensure_stereo(other_mono),
        "original": audio.astype(np.float32),
        "quick_mode": True,
    }

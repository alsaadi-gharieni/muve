"""Fast pseudo-stem generation without ML separation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import numpy as np
import soundfile as sf

from audio.audio_utils import MAX_SAMPLES, TARGET_SR, ensure_stereo, resample_if_needed, to_mono
from audio.pseudo_stems import pseudo_stems_to_stereo, split_pseudo_stems_batch


def _load_source(path: Path) -> tuple:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return data, int(sr)


def load_quick_stems(source_path: str) -> Dict[str, Any]:
    """Build approximate stems instantly so playback can start before Demucs finishes."""
    source = Path(source_path)
    audio, sr = _load_source(source)
    audio = ensure_stereo(resample_if_needed(audio, sr, TARGET_SR))

    if len(audio) > MAX_SAMPLES:
        audio = audio[:MAX_SAMPLES]

    mono = to_mono(audio)
    bass_mono, drums_mono, other_mono = split_pseudo_stems_batch(mono, TARGET_SR)
    bass, drums, other = pseudo_stems_to_stereo(bass_mono, drums_mono, other_mono)

    return {
        "sample_rate": TARGET_SR,
        "bass": bass,
        "drums": drums,
        "other": other,
        "original": audio.astype(np.float32),
        "quick_mode": True,
    }

"""Fast pseudo-stem splitting — shared by file preview and live streaming."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from audio.audio_utils import TARGET_SR, ensure_stereo, to_mono

STEM_CROSSOVER_HZ = 180.0
DRUMS_BAND_HZ = (80.0, 4000.0)


def _drum_onset_mono(mono: np.ndarray, prev_sample: float) -> tuple[np.ndarray, float]:
    """Percussive onset signal — same method as quick_stems (diff of waveform)."""
    padded = np.concatenate([[prev_sample], mono.astype(np.float32)])
    onset = np.abs(np.diff(padded))
    new_prev = float(mono[-1]) if len(mono) else prev_sample
    return onset, new_prev


def split_pseudo_stems_batch(mono: np.ndarray, sample_rate: int = TARGET_SR) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split one mono buffer into bass / drums / other stems (offline)."""
    low_sos = butter(4, STEM_CROSSOVER_HZ, btype="low", fs=sample_rate, output="sos")
    high_sos = butter(4, STEM_CROSSOVER_HZ, btype="high", fs=sample_rate, output="sos")
    drums_sos = butter(4, DRUMS_BAND_HZ, btype="bandpass", fs=sample_rate, output="sos")

    bass_mono = sosfilt(low_sos, mono)
    other_mono = sosfilt(high_sos, mono)
    onset, _ = _drum_onset_mono(mono, 0.0)
    drums_mono = sosfilt(drums_sos, onset)
    return bass_mono.astype(np.float32), drums_mono.astype(np.float32), other_mono.astype(np.float32)


def pseudo_stems_to_stereo(
    bass_mono: np.ndarray,
    drums_mono: np.ndarray,
    other_mono: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (
        ensure_stereo(bass_mono),
        ensure_stereo(drums_mono),
        ensure_stereo(other_mono),
    )


class StreamingPseudoStems:
    """Stateful pseudo-stem splitter for real-time blocks."""

    def __init__(self, sample_rate: int = TARGET_SR) -> None:
        self.sample_rate = sample_rate
        self.low_sos = butter(4, STEM_CROSSOVER_HZ, btype="low", fs=sample_rate, output="sos")
        self.high_sos = butter(4, STEM_CROSSOVER_HZ, btype="high", fs=sample_rate, output="sos")
        self.drums_sos = butter(4, DRUMS_BAND_HZ, btype="bandpass", fs=sample_rate, output="sos")
        self.reset()

    def reset(self) -> None:
        self.zi_low = sosfilt_zi(self.low_sos)
        self.zi_high = sosfilt_zi(self.high_sos)
        self.zi_drums = sosfilt_zi(self.drums_sos)
        self._prev_mono = 0.0

    def process_stereo(self, stereo: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        mono = to_mono(stereo)
        bass_mono, self.zi_low = sosfilt(self.low_sos, mono, zi=self.zi_low)
        other_mono, self.zi_high = sosfilt(self.high_sos, mono, zi=self.zi_high)
        onset, self._prev_mono = _drum_onset_mono(mono, self._prev_mono)
        drums_mono, self.zi_drums = sosfilt(self.drums_sos, onset, zi=self.zi_drums)
        return pseudo_stems_to_stereo(bass_mono, drums_mono, other_mono)

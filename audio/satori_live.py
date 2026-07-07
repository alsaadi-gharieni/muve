"""Clean Satori live vibration — slow envelopes from L/R, noise-suppressed."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from audio.audio_utils import ensure_stereo, to_mono
from audio.synthetic_types import generate_synthetic_wave
from audio.vibration_presets import get_frequency_profile, get_segmentation_preset

# Tactile path: keep only bass/mid body content from loopback (drops hiss/hash).
INPUT_HIGHPASS_HZ = 28.0
INPUT_LOWPASS_HZ = 155.0
# Slow envelopes — transducers need amplitude swells, not waveform buzz.
ENVELOPE_LP_HZ = 6.0
OUTPUT_ZONE_LP_HZ = 45.0
# Music must be present in 25–110 Hz before zones open.
MUSIC_BAND_HZ = (25.0, 110.0)
MUSIC_GATE_FLOOR = 0.008
MUSIC_GATE_RANGE = 0.055
ZONE_MAKEUP_GAIN = 10.0
SYNTH_GATE_FLOOR = 0.028
SYNTH_GATE_RANGE = 0.065


def _clamp_band(low: float, high: float, sample_rate: int) -> tuple[float, float]:
    nyquist = sample_rate * 0.5
    return low, min(high, max(nyquist * 0.95, low + 1.0))


def _gate_envelope(signal: np.ndarray, threshold: float, ratio: float = 5.0) -> np.ndarray:
    """Zero sub-threshold hash; compress peaks above threshold."""
    sig = signal.astype(np.float32)
    abs_sig = np.abs(sig)
    out = np.zeros_like(sig)
    over = abs_sig > threshold
    if not np.any(over):
        return out
    out[over] = threshold + (abs_sig[over] - threshold) / ratio
    return out.astype(np.float32)


class _ZoneChannel:
    """Band energy envelope for one Satori zone — output is smooth amplitude only."""

    def __init__(self, sample_rate: int, band: tuple[float, float]) -> None:
        low, high = _clamp_band(*band, sample_rate)
        self.bp_sos = butter(4, (low, high), btype="bandpass", fs=sample_rate, output="sos")
        self.env_sos = butter(2, ENVELOPE_LP_HZ, btype="low", fs=sample_rate, output="sos")
        self.out_sos = butter(2, OUTPUT_ZONE_LP_HZ, btype="low", fs=sample_rate, output="sos")
        self._threshold = 0.004
        self._noise_floor = 0.005
        self.reset()

    def reset(self) -> None:
        self.zi_bp = sosfilt_zi(self.bp_sos)
        self.zi_env = sosfilt_zi(self.env_sos)
        self.zi_out = sosfilt_zi(self.out_sos)
        self._noise_floor = 0.005

    def process(self, mono: np.ndarray, presence: float) -> np.ndarray:
        band, self.zi_bp = sosfilt(self.bp_sos, mono.astype(np.float32), zi=self.zi_bp)
        rect = np.abs(band.astype(np.float32))
        env_sq, self.zi_env = sosfilt(self.env_sos, rect**2, zi=self.zi_env)
        envelope = np.sqrt(np.maximum(env_sq, 0.0)).astype(np.float32)

        block_peak = float(np.max(envelope))
        if presence < 0.15:
            self._noise_floor = min(self._noise_floor * 0.992 + block_peak * 0.008, 0.06)
        envelope = np.maximum(envelope - self._noise_floor * 1.1, 0.0).astype(np.float32)

        gated = _gate_envelope(envelope, self._threshold, ratio=4.0)
        smoothed, self.zi_out = sosfilt(self.out_sos, gated, zi=self.zi_out)
        return (smoothed * presence * ZONE_MAKEUP_GAIN).astype(np.float32)


class _MusicPresence:
    """Detect bass/mid body energy in L+R — opens zones only when music is playing."""

    def __init__(self, sample_rate: int) -> None:
        low, high = _clamp_band(*MUSIC_BAND_HZ, sample_rate)
        self.bp_sos = butter(4, (low, high), btype="bandpass", fs=sample_rate, output="sos")
        self.env_sos = butter(2, 5.0, btype="low", fs=sample_rate, output="sos")
        self.reset()

    def reset(self) -> None:
        self.zi_bp = sosfilt_zi(self.bp_sos)
        self.zi_env = sosfilt_zi(self.env_sos)
        self._level = 0.0

    def process(self, mono: np.ndarray) -> float:
        band, self.zi_bp = sosfilt(self.bp_sos, mono.astype(np.float32), zi=self.zi_bp)
        env_sq, self.zi_env = sosfilt(self.env_sos, band.astype(np.float32) ** 2, zi=self.zi_env)
        level = float(np.sqrt(max(float(np.mean(env_sq)), 0.0)))
        if level > self._level:
            self._level = level
        else:
            self._level = max(level, self._level * 0.90)
        return float(np.clip((self._level - MUSIC_GATE_FLOOR) / MUSIC_GATE_RANGE, 0.0, 1.0) ** 1.2)


class SatoriLiveProcessor:
    """Live Satori zones from L/R — slow envelopes, adaptive noise floor, music gate."""

    def __init__(
        self,
        sample_rate: int,
        frequency_profile_id: str = "satori",
        segmentation_id: str = "default",
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        self.sample_rate = sample_rate
        self.synthetic_vibro = synthetic_vibro
        self.synthetic_type_id = synthetic_type_id
        self._frequency_profile_id = frequency_profile_id
        self._segmentation_id = segmentation_id
        self._profile = get_frequency_profile(frequency_profile_id)
        self._segmentation = get_segmentation_preset(segmentation_id)
        self._input_hp_sos = butter(2, INPUT_HIGHPASS_HZ, btype="highpass", fs=sample_rate, output="sos")
        self._input_lp_sos = butter(4, INPUT_LOWPASS_HZ, btype="low", fs=sample_rate, output="sos")
        self._music = _MusicPresence(sample_rate)
        self._legs = _ZoneChannel(sample_rate, self._profile.legs_band)
        self._mid = _ZoneChannel(sample_rate, self._profile.mid_band)
        self._upper = _ZoneChannel(sample_rate, self._profile.upper_mid_band)
        self._head = _ZoneChannel(sample_rate, self._profile.head_band)
        self._synth_energy_peak = 1e-4
        self.reset()

    def reset(self) -> None:
        self.zi_input_hp = sosfilt_zi(self._input_hp_sos)
        self.zi_input_lp = sosfilt_zi(self._input_lp_sos)
        self._music.reset()
        self._legs.reset()
        self._mid.reset()
        self._upper.reset()
        self._head.reset()
        self._synth_energy_peak = 1e-4

    def update_presets(
        self,
        frequency_profile_id: str,
        segmentation_id: str,
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        changed = (
            frequency_profile_id != self._frequency_profile_id
            or segmentation_id != self._segmentation_id
            or synthetic_vibro != self.synthetic_vibro
        )
        self.synthetic_vibro = synthetic_vibro
        self.synthetic_type_id = synthetic_type_id
        if frequency_profile_id != self._frequency_profile_id:
            self._frequency_profile_id = frequency_profile_id
            self._profile = get_frequency_profile(frequency_profile_id)
            self._legs = _ZoneChannel(self.sample_rate, self._profile.legs_band)
            self._mid = _ZoneChannel(self.sample_rate, self._profile.mid_band)
            self._upper = _ZoneChannel(self.sample_rate, self._profile.upper_mid_band)
            self._head = _ZoneChannel(self.sample_rate, self._profile.head_band)
        if segmentation_id != self._segmentation_id:
            self._segmentation_id = segmentation_id
            self._segmentation = get_segmentation_preset(segmentation_id)
        if changed:
            self.reset()

    def _input_mono(self, stereo: np.ndarray) -> np.ndarray:
        mono = to_mono(stereo)
        mono, self.zi_input_hp = sosfilt(self._input_hp_sos, mono, zi=self.zi_input_hp)
        mono, self.zi_input_lp = sosfilt(self._input_lp_sos, mono, zi=self.zi_input_lp)
        return mono.astype(np.float32)

    def _synthetic_layers(
        self,
        legs: np.ndarray,
        mid: np.ndarray,
        upper: np.ndarray,
        head: np.ndarray,
        presence: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
        seg = self._segmentation
        energy = legs + 0.35 * mid + 0.15 * upper
        block_peak = float(np.max(energy))
        if block_peak > self._synth_energy_peak:
            self._synth_energy_peak = block_peak
        else:
            self._synth_energy_peak = max(block_peak, self._synth_energy_peak * 0.995)
        energy_peak = max(self._synth_energy_peak, 1e-5)

        generated, generated_freq = generate_synthetic_wave(
            energy,
            sample_rate=self.sample_rate,
            synth_type_id=self.synthetic_type_id,
            freq_range=seg.synthetic_freq_range,
            gentle=seg.gentle_dynamics,
            energy_reference_peak=energy_peak,
        )
        generated = generated * presence
        freq_hz = float(np.mean(generated_freq)) if len(generated_freq) else 0.0

        if seg.gentle_dynamics:
            return generated, 0.55 * generated, 0.38 * generated, 0.25 * generated, freq_hz
        return generated, 0.75 * generated, 0.58 * generated, 0.42 * generated, freq_hz

    def process_block(self, stereo: np.ndarray) -> dict[str, Any]:
        stereo = ensure_stereo(stereo)
        mono = self._input_mono(stereo)
        presence = self._music.process(mono)

        legs = self._legs.process(mono, presence)
        mid = self._mid.process(mono, presence)
        upper = self._upper.process(mono, presence)
        head = self._head.process(mono, presence)

        generated_freq_hz = 0.0
        if self.synthetic_vibro:
            legs, mid, upper, head, generated_freq_hz = self._synthetic_layers(
                legs, mid, upper, head, presence
            )

        def soften(x: np.ndarray) -> np.ndarray:
            return np.tanh(x.astype(np.float32) * 0.75).astype(np.float32)

        legs = soften(legs)
        mid = soften(mid)
        upper = soften(upper)
        head = soften(head)

        return {
            "speakers": stereo,
            "legs": np.clip(legs, -1.0, 1.0).astype(np.float32),
            "mid": np.clip(mid, -1.0, 1.0).astype(np.float32),
            "upper_mid": np.clip(upper, -1.0, 1.0).astype(np.float32),
            "head": np.clip(head, -1.0, 1.0).astype(np.float32),
            "rms_legs": float(np.sqrt(np.mean(legs**2) + 1e-10)),
            "rms_mid": float(np.sqrt(np.mean(mid**2) + 1e-10)),
            "rms_upper_mid": float(np.sqrt(np.mean(upper**2) + 1e-10)),
            "rms_head": float(np.sqrt(np.mean(head**2) + 1e-10)),
            "bass_energy": float(np.sqrt(np.mean(legs**2) + 1e-10)),
            "generated_freq_hz": generated_freq_hz,
        }

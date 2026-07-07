"""Signal processing for Satori body-zone vibration channels."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from audio.synthetic_types import generate_synthetic_wave
from audio.vibration_presets import (
    FrequencyProfile,
    SegmentationPreset,
    get_frequency_profile,
    get_segmentation_preset,
)


class VibrationEngine:
    """Build legs / mid / upper-mid / head tactile layers from stem content."""

    def __init__(
        self,
        sample_rate: int,
        frequency_profile: FrequencyProfile | None = None,
        segmentation: SegmentationPreset | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        profile = frequency_profile or get_frequency_profile("satori")
        self.frequency_profile = profile

        self.legs_sos = butter(4, profile.legs_band, btype="bandpass", fs=sample_rate, output="sos")
        self.mid_sos = butter(4, profile.mid_band, btype="bandpass", fs=sample_rate, output="sos")
        self.upper_mid_sos = butter(4, profile.upper_mid_band, btype="bandpass", fs=sample_rate, output="sos")
        self.head_sos = butter(4, profile.head_band, btype="bandpass", fs=sample_rate, output="sos")
        self.transient_sos = butter(2, 150, btype="highpass", fs=sample_rate, output="sos")
        self.segmentation = segmentation or get_segmentation_preset("default")
        self._streaming = False
        self._synth_energy_peak = 1e-4
        self._reset_streaming_state()

    def _reset_streaming_state(self) -> None:
        self.zi_legs = sosfilt_zi(self.legs_sos)
        self.zi_mid = sosfilt_zi(self.mid_sos)
        self.zi_upper_mid = sosfilt_zi(self.upper_mid_sos)
        self.zi_head = sosfilt_zi(self.head_sos)
        self.zi_transient = sosfilt_zi(self.transient_sos)
        self._prev_drums_mono = 0.0
        self._energy_history = np.zeros(0, dtype=np.float32)
        self._envelope_peak = 1e-4

    def _streaming_rms_energy(self, energy_source: np.ndarray, window: int) -> np.ndarray:
        """RMS energy with history across blocks — avoids per-block discontinuities."""
        self._energy_history = np.concatenate(
            [self._energy_history, energy_source.astype(np.float32)]
        )
        max_hist = max(window * 4, 8192)
        if len(self._energy_history) > max_hist:
            self._energy_history = self._energy_history[-max_hist:]
        kernel = np.ones(window, dtype=np.float32) / float(window)
        squared = self._energy_history.astype(np.float32) ** 2
        rms_full = np.sqrt(np.convolve(squared, kernel, mode="same"))
        return rms_full[-len(energy_source) :].astype(np.float32)

    def set_streaming(self, enabled: bool) -> None:
        self._streaming = enabled
        if enabled:
            self._reset_streaming_state()

    def _zone_filter(self, sos, source: np.ndarray, zi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self._streaming:
            return sosfilt(sos, source, zi=zi)
        return sosfilt(sos, source), zi

    @staticmethod
    def _to_mono(stem: np.ndarray) -> np.ndarray:
        if stem.ndim == 1:
            return stem
        return stem.mean(axis=1)

    @staticmethod
    def _compress(signal: np.ndarray, threshold: float, ratio: float, makeup: float) -> np.ndarray:
        abs_sig = np.abs(signal)
        over = np.maximum(abs_sig - threshold, 0.0)
        compressed = np.sign(signal) * (threshold + over / ratio)
        below = abs_sig <= threshold
        compressed[below] = signal[below]
        return compressed * makeup

    def _mix_stems(
        self,
        weights: tuple[float, float, float],
        bass_mono: np.ndarray,
        drums_mono: np.ndarray,
        other_mono: np.ndarray,
    ) -> np.ndarray:
        w_bass, w_drums, w_other = weights
        return w_bass * bass_mono + w_drums * drums_mono + w_other * other_mono

    @staticmethod
    def _boost_quiet(signal: np.ndarray, target_peak: float = 0.35, max_gain: float = 10.0) -> np.ndarray:
        peak = float(np.max(np.abs(signal)))
        if peak < 1e-6:
            return signal
        gain = min(target_peak / peak, max_gain)
        return signal * gain

    def _smooth_envelope(self, signal: np.ndarray, window: int) -> np.ndarray:
        kernel = np.ones(window, dtype=np.float32) / float(window)
        envelope = np.sqrt(np.convolve(signal.astype(np.float32) ** 2, kernel, mode="same"))
        if self._streaming:
            block_peak = float(np.max(envelope))
            if block_peak > self._envelope_peak:
                self._envelope_peak = block_peak
            else:
                self._envelope_peak = max(block_peak, self._envelope_peak * 0.995)
            peak = self._envelope_peak
        else:
            peak = float(np.max(envelope)) + 1e-7
        norm = envelope / (peak + 1e-7)
        return signal * (0.35 + 0.65 * norm)

    def _apply_dynamics(
        self,
        legs: np.ndarray,
        mid: np.ndarray,
        upper_mid: np.ndarray,
        head: np.ndarray,
        seg: SegmentationPreset,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        if seg.gentle_dynamics:
            legs = self._compress(legs, threshold=0.04, ratio=2.5, makeup=1.6)
            mid = self._compress(mid, threshold=0.04, ratio=2.5, makeup=1.4)
            upper_mid = self._compress(upper_mid, threshold=0.05, ratio=2.2, makeup=1.2)
            head = self._compress(head, threshold=0.05, ratio=2.0, makeup=1.1)
            legs = self._smooth_envelope(legs, seg.energy_window)
            mid = self._smooth_envelope(mid, max(seg.energy_window // 2, 512))
            upper_mid = self._smooth_envelope(upper_mid, max(seg.energy_window // 2, 512))
            head = self._smooth_envelope(head, max(seg.energy_window // 2, 512))
        else:
            legs = self._compress(legs, threshold=0.08, ratio=6.0, makeup=1.8)
            mid = self._compress(mid, threshold=0.09, ratio=3.5, makeup=1.3)
            upper_mid = self._compress(upper_mid, threshold=0.10, ratio=2.5, makeup=1.1)
            head = self._compress(head, threshold=0.10, ratio=2.0, makeup=1.0)

        if seg.amplify_quiet:
            legs = self._boost_quiet(legs, target_peak=0.22, max_gain=4.0)
            mid = self._boost_quiet(mid, target_peak=0.18, max_gain=3.5)
            upper_mid = self._boost_quiet(upper_mid, target_peak=0.15, max_gain=3.0)
            head = self._boost_quiet(head, target_peak=0.12, max_gain=2.5)

        return legs, mid, upper_mid, head

    def update_presets(
        self,
        frequency_profile: FrequencyProfile | None = None,
        segmentation: SegmentationPreset | None = None,
    ) -> None:
        if frequency_profile is not None:
            self.frequency_profile = frequency_profile
            profile = frequency_profile
            self.legs_sos = butter(4, profile.legs_band, btype="bandpass", fs=self.sample_rate, output="sos")
            self.mid_sos = butter(4, profile.mid_band, btype="bandpass", fs=self.sample_rate, output="sos")
            self.upper_mid_sos = butter(
                4, profile.upper_mid_band, btype="bandpass", fs=self.sample_rate, output="sos"
            )
            self.head_sos = butter(4, profile.head_band, btype="bandpass", fs=self.sample_rate, output="sos")
        if segmentation is not None:
            self.segmentation = segmentation
        if self._streaming:
            self._reset_streaming_state()

    def _update_synth_energy_peak(self, energy_track: np.ndarray) -> float:
        block_peak = float(np.max(energy_track))
        if block_peak > self._synth_energy_peak:
            self._synth_energy_peak = block_peak
        else:
            self._synth_energy_peak = max(block_peak, self._synth_energy_peak * 0.995)
        return max(self._synth_energy_peak, 1e-5)

    def build_vibration_layers(
        self,
        bass: np.ndarray,
        drums: np.ndarray,
        other: np.ndarray,
        synthetic_vibro: bool,
        synthetic_type_id: str = "sine",
    ) -> dict[str, np.ndarray]:
        bass_mono = self._to_mono(bass)
        drums_mono = self._to_mono(drums)
        other_mono = self._to_mono(other)
        seg = self.segmentation

        legs_source = self._mix_stems(seg.legs_weights, bass_mono, drums_mono, other_mono)
        mid_source = self._mix_stems(seg.mid_weights, bass_mono, drums_mono, other_mono)
        upper_mid_source = self._mix_stems(seg.upper_mid_weights, bass_mono, drums_mono, other_mono)
        head_source = self._mix_stems(seg.head_weights, bass_mono, drums_mono, other_mono)

        if seg.use_transient_on_head:
            if self._streaming:
                padded = np.concatenate([[self._prev_drums_mono], drums_mono])
                transient = np.abs(np.diff(padded))
                self._prev_drums_mono = float(drums_mono[-1]) if len(drums_mono) else self._prev_drums_mono
                transient, self.zi_transient = sosfilt(self.transient_sos, transient, zi=self.zi_transient)
            else:
                transient = np.abs(np.diff(np.pad(drums_mono, (1, 0))))
                transient = sosfilt(self.transient_sos, transient)
            head_source = head_source + 0.3 * transient

        legs, self.zi_legs = self._zone_filter(self.legs_sos, legs_source, self.zi_legs)
        mid, self.zi_mid = self._zone_filter(self.mid_sos, mid_source, self.zi_mid)
        upper_mid, self.zi_upper_mid = self._zone_filter(self.upper_mid_sos, upper_mid_source, self.zi_upper_mid)
        head, self.zi_head = self._zone_filter(self.head_sos, head_source, self.zi_head)

        legs, mid, upper_mid, head = self._apply_dynamics(legs, mid, upper_mid, head, seg)

        energy_source = legs_source + 0.35 * mid_source + 0.15 * upper_mid_source
        window = max(seg.energy_window, 512)
        if synthetic_vibro and seg.gentle_dynamics:
            window = max(window * 2, 8192)
        if self._streaming:
            energy_track = self._streaming_rms_energy(energy_source, window)
        else:
            kernel = np.ones(window, dtype=np.float32) / float(window)
            energy_track = np.sqrt(np.convolve(energy_source**2, kernel, mode="same"))
        generated_freq = np.zeros_like(legs, dtype=np.float32)

        if synthetic_vibro:
            energy_peak = (
                self._update_synth_energy_peak(energy_track) if self._streaming else None
            )
            generated, generated_freq = generate_synthetic_wave(
                energy_track,
                sample_rate=self.sample_rate,
                synth_type_id=synthetic_type_id,
                freq_range=seg.synthetic_freq_range,
                gentle=seg.gentle_dynamics,
                energy_reference_peak=energy_peak,
            )
            if seg.gentle_dynamics:
                legs = generated
                mid = 0.55 * generated
                upper_mid = 0.38 * generated
                head = 0.25 * generated
            else:
                legs = generated
                mid = 0.75 * generated
                upper_mid = 0.58 * generated
                head = 0.42 * generated

        return {
            "legs": np.clip(legs, -1.0, 1.0).astype(np.float32),
            "mid": np.clip(mid, -1.0, 1.0).astype(np.float32),
            "upper_mid": np.clip(upper_mid, -1.0, 1.0).astype(np.float32),
            "head": np.clip(head, -1.0, 1.0).astype(np.float32),
            "bass_energy": energy_track.astype(np.float32),
            "generated_freq": generated_freq.astype(np.float32),
        }

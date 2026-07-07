"""6-channel Satori playback engine using sounddevice callbacks."""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd

from audio.audio_utils import ensure_stereo, to_mono
from audio.cinema_8d import (
    apply_8d_stereo_pan,
    apply_8d_vibration_motion,
    drum_impact_envelope,
)
from audio.vibration_engine import VibrationEngine
from audio.vibration_presets import (
    SATORI_OUTPUT_CHANNELS,
    get_frequency_profile,
    get_segmentation_preset,
)


# Center frequencies (Hz) for short zone test pulses.
ZONE_TEST_FREQUENCIES: dict[str, float] = {
    "legs": 35.0,
    "mid": 59.0,
    "upper": 90.0,
    "head": 125.0,
}
# Physical output channel index per logical zone.
ZONE_TEST_CHANNELS: dict[str, int] = {
    "head": 2,    # C
    "upper": 3,   # LFE
    "legs": 4,    # Ls
    "mid": 5,     # Rs
}
ZONE_TEST_DURATION_SEC = 0.65
ZONE_TEST_LEVEL = 0.55


class GigaportOutput:
    """Manages synchronized 6-channel Satori playback (L, R, C, LFE, Ls, Rs)."""

    def __init__(self) -> None:
        self.device_index = None
        self.stream: sd.OutputStream | None = None
        self.speaker_program: np.ndarray | None = None
        self.vib_legs: np.ndarray | None = None
        self.vib_mid: np.ndarray | None = None
        self.vib_upper_mid: np.ndarray | None = None
        self.vib_head: np.ndarray | None = None
        self.total_frames = 0
        self.sample_rate = 44100
        self.playhead = 0
        self.is_playing = False
        self.volume = 0.85
        self.intensity_mid = 1.0
        self.intensity_legs = 1.0
        self.intensity_upper = 1.0
        self.intensity_head = 1.0
        self.cinema_8d_enabled = False
        self.speaker_program_raw: np.ndarray | None = None
        self.generated_freq_track: np.ndarray | None = None
        self.test_zone: str | None = None
        self.test_frames_left = 0
        self.test_phase = 0.0

        self.lock = threading.Lock()
        self.stats: dict[str, float] = {
            "rms_legs": 0.0,
            "rms_mid": 0.0,
            "rms_upper_mid": 0.0,
            "rms_head": 0.0,
            "bass_energy": 0.0,
            "generated_freq_hz": 0.0,
            "cpu_usage_estimate": 0.0,
            "progress_fraction": 0.0,
        }

    def list_output_devices(self, min_channels: int = SATORI_OUTPUT_CHANNELS) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        hostapis = sd.query_hostapis()
        for idx, dev in enumerate(sd.query_devices()):
            out_ch = int(dev["max_output_channels"])
            if out_ch <= 0:
                continue
            name = str(dev["name"])
            is_gigaport = "gigaport" in name.lower()
            if out_ch < min_channels and not is_gigaport:
                continue
            api_name = str(hostapis[int(dev["hostapi"])]["name"])
            devices.append(
                {
                    "index": idx,
                    "name": name,
                    "channels": out_ch,
                    "hostapi": api_name,
                    "is_gigaport": is_gigaport,
                }
            )
        devices.sort(
            key=lambda d: (
                not d.get("is_gigaport", False),
                "asio" not in d.get("hostapi", "").lower(),
                -int(d.get("channels", 0)),
                d["name"].lower(),
            )
        )
        return devices

    def set_device(self, device_index: int) -> None:
        self.device_index = device_index
        self._close_stream()

    def set_volume(self, volume: float) -> None:
        self.volume = float(np.clip(volume, 0.0, 1.2))

    def set_zone_intensities(
        self,
        mid: float | None = None,
        legs: float | None = None,
        upper: float | None = None,
        head: float | None = None,
    ) -> None:
        """Scale each Satori vibration zone independently (C, LFE, Ls, Rs)."""
        if mid is not None:
            self.intensity_mid = float(np.clip(mid, 0.0, 3.0))
        if legs is not None:
            self.intensity_legs = float(np.clip(legs, 0.0, 3.0))
        if upper is not None:
            self.intensity_upper = float(np.clip(upper, 0.0, 3.0))
        if head is not None:
            self.intensity_head = float(np.clip(head, 0.0, 3.0))

    def trigger_zone_test(self, zone: str) -> None:
        """Play a short sine pulse on one vibration zone (works without loaded audio)."""
        if zone not in ZONE_TEST_FREQUENCIES:
            raise ValueError(f"Unknown zone: {zone}")
        with self.lock:
            self.test_zone = zone
            self.test_frames_left = int(self.sample_rate * ZONE_TEST_DURATION_SEC)
            self.test_phase = 0.0
        self._ensure_stream()

    def _cancel_zone_test(self) -> None:
        self.test_zone = None
        self.test_frames_left = 0
        self.test_phase = 0.0

    def _render_zone_test_block(self, frames: int) -> np.ndarray:
        """Return a 6-channel block containing only the active zone test tone."""
        output = np.zeros((frames, SATORI_OUTPUT_CHANNELS), dtype=np.float32)
        if self.test_frames_left <= 0 or self.test_zone is None:
            return output

        zone = self.test_zone
        channel = ZONE_TEST_CHANNELS[zone]
        freq = ZONE_TEST_FREQUENCIES[zone]
        total_test_frames = int(self.sample_rate * ZONE_TEST_DURATION_SEC)
        n = min(frames, self.test_frames_left)

        t_global = np.arange(
            total_test_frames - self.test_frames_left,
            total_test_frames - self.test_frames_left + n,
            dtype=np.float32,
        )
        envelope = np.ones(n, dtype=np.float32)
        fade = max(int(self.sample_rate * 0.04), 1)
        attack = np.minimum(t_global + 1.0, float(fade)) / float(fade)
        release_start = max(total_test_frames - fade, 0)
        release = np.minimum(total_test_frames - t_global, float(fade)) / float(fade)
        envelope = np.minimum(attack, release)

        phase = self.test_phase + (2.0 * np.pi * freq) * t_global / float(self.sample_rate)
        tone = (ZONE_TEST_LEVEL * envelope * np.sin(phase)).astype(np.float32)
        output[:n, channel] = tone

        self.test_phase = float(phase[-1]) if n > 0 else self.test_phase
        self.test_frames_left -= n
        if self.test_frames_left <= 0:
            self._cancel_zone_test()

        return output

    def _set_speaker_program(self, original: np.ndarray, cinema_8d: bool) -> None:
        self.speaker_program_raw = ensure_stereo(original).astype(np.float32)
        if cinema_8d:
            self.speaker_program = apply_8d_stereo_pan(self.speaker_program_raw, self.sample_rate)
        else:
            self.speaker_program = self.speaker_program_raw.copy()

    def _build_vibration_layers(
        self,
        bass: np.ndarray,
        drums: np.ndarray,
        other: np.ndarray,
        synthetic_vibro: bool,
        segmentation_id: str,
        frequency_profile_id: str,
        cinema_8d: bool,
        synthetic_type_id: str = "sine",
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        frequency_profile = get_frequency_profile(frequency_profile_id)
        segmentation = get_segmentation_preset(segmentation_id)

        vib = VibrationEngine(
            self.sample_rate,
            frequency_profile=frequency_profile,
            segmentation=segmentation,
        )
        layers = vib.build_vibration_layers(
            bass=bass,
            drums=drums,
            other=other,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )

        legs = layers["legs"]
        mid = layers["mid"]
        upper_mid = layers["upper_mid"]
        head = layers["head"]

        if cinema_8d:
            drums_mono = to_mono(drums)
            impacts = drum_impact_envelope(drums_mono, self.sample_rate)
            legs, mid, upper_mid, head = apply_8d_vibration_motion(
                legs, mid, upper_mid, head, self.sample_rate, drum_impacts=impacts
            )

        # Return logical body zones (legs, mid, upper_mid, head).
        return (
            legs,
            mid,
            upper_mid,
            head,
            layers["bass_energy"],
            layers["generated_freq"],
        )

    def _set_vibration_layers(
        self,
        legs: np.ndarray,
        mid: np.ndarray,
        upper_mid: np.ndarray,
        head: np.ndarray,
        bass_energy: np.ndarray,
        generated_freq: np.ndarray,
    ) -> None:
        self.vib_legs = legs.astype(np.float32)
        self.vib_mid = mid.astype(np.float32)
        self.vib_upper_mid = upper_mid.astype(np.float32)
        self.vib_head = head.astype(np.float32)
        self.bass_energy_track = bass_energy.astype(np.float32)
        self.generated_freq_track = generated_freq.astype(np.float32)
        if self.speaker_program is not None:
            self.total_frames = min(
                len(self.speaker_program),
                len(self.vib_legs),
                len(self.vib_mid),
                len(self.vib_upper_mid),
                len(self.vib_head),
            )

    def prepare_program(
        self,
        sample_rate: int,
        bass: np.ndarray,
        drums: np.ndarray,
        other: np.ndarray,
        original: np.ndarray,
        synthetic_vibro: bool,
        segmentation_id: str = "default",
        frequency_profile_id: str = "satori",
        cinema_8d: bool = False,
        synthetic_type_id: str = "sine",
        reset_playhead: bool = True,
    ) -> None:
        bass = ensure_stereo(bass)
        drums = ensure_stereo(drums)
        other = ensure_stereo(other)
        original = ensure_stereo(original)

        min_len = min(len(bass), len(drums), len(other), len(original))
        bass = bass[:min_len]
        drums = drums[:min_len]
        other = other[:min_len]
        original = original[:min_len]

        with self.lock:
            self.sample_rate = sample_rate
            self.cinema_8d_enabled = cinema_8d
            self._set_speaker_program(original, cinema_8d=cinema_8d)
            legs, mid, upper_mid, head, bass_energy, generated_freq = self._build_vibration_layers(
                bass=bass,
                drums=drums,
                other=other,
                synthetic_vibro=synthetic_vibro,
                segmentation_id=segmentation_id,
                frequency_profile_id=frequency_profile_id,
                cinema_8d=cinema_8d,
                synthetic_type_id=synthetic_type_id,
            )
            self._set_vibration_layers(legs, mid, upper_mid, head, bass_energy, generated_freq)
            if reset_playhead:
                self.playhead = 0
                self.stats["progress_fraction"] = 0.0

        self._close_stream()

    def update_vibration_only(
        self,
        bass: np.ndarray,
        drums: np.ndarray,
        other: np.ndarray,
        synthetic_vibro: bool,
        segmentation_id: str = "default",
        frequency_profile_id: str = "satori",
        cinema_8d: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        if self.speaker_program is None:
            raise RuntimeError("Load a file before changing vibration presets")

        bass = ensure_stereo(bass)
        drums = ensure_stereo(drums)
        other = ensure_stereo(other)

        min_len = min(len(bass), len(drums), len(other), len(self.speaker_program))
        bass = bass[:min_len]
        drums = drums[:min_len]
        other = other[:min_len]

        with self.lock:
            saved_playhead = self.playhead
            saved_progress = self.stats["progress_fraction"]

            self.cinema_8d_enabled = cinema_8d
            if cinema_8d and self.speaker_program_raw is not None:
                self.speaker_program = apply_8d_stereo_pan(self.speaker_program_raw, self.sample_rate)
            elif self.speaker_program_raw is not None:
                self.speaker_program = self.speaker_program_raw.copy()

            legs, mid, upper_mid, head, bass_energy, generated_freq = self._build_vibration_layers(
                bass=bass,
                drums=drums,
                other=other,
                synthetic_vibro=synthetic_vibro,
                segmentation_id=segmentation_id,
                frequency_profile_id=frequency_profile_id,
                cinema_8d=cinema_8d,
                synthetic_type_id=synthetic_type_id,
            )
            self._set_vibration_layers(legs, mid, upper_mid, head, bass_energy, generated_freq)
            self.playhead = min(saved_playhead, max(self.total_frames - 1, 0))
            self.stats["progress_fraction"] = saved_progress

    def play(self) -> None:
        if self.speaker_program is None:
            raise RuntimeError("No audio loaded")
        self._ensure_stream()
        self.is_playing = True

    def pause(self) -> None:
        self.is_playing = False

    def stop(self) -> None:
        self.is_playing = False
        with self.lock:
            self.playhead = 0
            self.stats["progress_fraction"] = 0.0
            self._cancel_zone_test()

    def release_output_device(self) -> None:
        """Close the ASIO/output stream so another engine can open Gigaport."""
        self.is_playing = False
        self._close_stream()

    def set_position_fraction(self, fraction: float) -> None:
        with self.lock:
            if self.total_frames <= 0:
                return
            self.playhead = int(np.clip(fraction, 0.0, 1.0) * max(self.total_frames - 1, 1))

    def get_runtime_stats(self) -> dict[str, float]:
        with self.lock:
            return dict(self.stats)

    def _ensure_stream(self) -> None:
        if self.stream is not None:
            return

        self.stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=SATORI_OUTPUT_CHANNELS,
            dtype="float32",
            device=self.device_index,
            blocksize=1024,
            callback=self._callback,
            latency="low",
        )
        self.stream.start()

    def _close_stream(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            self.stream = None
            self._cancel_zone_test()

    def _callback(self, outdata, frames, _time_info, status) -> None:
        start_t = time.perf_counter()
        if status:
            pass

        with self.lock:
            testing = self.test_frames_left > 0
            can_play = (
                self.is_playing
                and self.speaker_program is not None
                and self.vib_legs is not None
                and self.vib_mid is not None
                and self.vib_upper_mid is not None
                and self.vib_head is not None
            )

            if not testing and not can_play:
                outdata.fill(0)
                return

            block_len = frames
            output = np.zeros((block_len, SATORI_OUTPUT_CHANNELS), dtype=np.float32)

            if can_play:
                start = self.playhead
                end = start + frames
                total = self.total_frames

                if start >= total:
                    outdata.fill(0)
                    self.is_playing = False
                    if testing:
                        output += self._render_zone_test_block(frames)
                        outdata[:] = np.clip(output, -1.0, 1.0)
                    else:
                        outdata.fill(0)
                    return

                sl = slice(start, min(end, total))
                speakers = self.speaker_program[sl]
                legs = self.vib_legs[sl]
                mid = self.vib_mid[sl]
                upper_mid = self.vib_upper_mid[sl]
                head = self.vib_head[sl]
                n = len(speakers)

                output[:n, 0] = speakers[:, 0] * self.volume
                output[:n, 1] = speakers[:, 1] * self.volume
                output[:n, 2] = head * self.intensity_head
                output[:n, 3] = upper_mid * self.intensity_upper
                output[:n, 4] = legs * self.intensity_legs
                output[:n, 5] = mid * self.intensity_mid

                if block_len < frames:
                    output[block_len:].fill(0)
                    self.is_playing = False
                else:
                    self.playhead = min(end, total)

                self.stats["progress_fraction"] = self.playhead / max(total, 1)
                self.stats["rms_legs"] = float(np.sqrt(np.mean(legs**2) + 1e-10))
                self.stats["rms_mid"] = float(np.sqrt(np.mean(mid**2) + 1e-10))
                self.stats["rms_upper_mid"] = float(np.sqrt(np.mean(upper_mid**2) + 1e-10))
                self.stats["rms_head"] = float(np.sqrt(np.mean(head**2) + 1e-10))

                if self.bass_energy_track is not None:
                    be_chunk = self.bass_energy_track[sl]
                    if len(be_chunk) > 0:
                        self.stats["bass_energy"] = float(np.mean(be_chunk))

                if self.generated_freq_track is not None:
                    gf_chunk = self.generated_freq_track[sl]
                    if len(gf_chunk) > 0:
                        self.stats["generated_freq_hz"] = float(np.mean(gf_chunk))

            if testing:
                output += self._render_zone_test_block(frames)

            outdata[:] = np.clip(output, -1.0, 1.0)

        elapsed = time.perf_counter() - start_t
        block_duration = frames / float(self.sample_rate)
        cpu_est = (elapsed / max(block_duration, 1e-6)) * 100.0
        with self.lock:
            self.stats["cpu_usage_estimate"] = float(cpu_est)

"""Real-time audio capture for Bluetooth / live input on Windows and other platforms."""

from __future__ import annotations

import collections
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import sounddevice as sd

from audio.audio_utils import TARGET_SR, ensure_stereo, fit_frames, resample_if_needed
from audio.satori_live import SatoriLiveProcessor
from audio.soundcard_loopback import (
    SoundcardLoopbackThread,
    get_default_playback_speaker_id,
    list_speaker_loopback_devices,
    soundcard_available,
)
from audio.vibration_presets import (
    SATORI_OUTPUT_CHANNELS,
)

# Vibration transducers map to Gigaport channels 2-5 (C, LFE, Ls, Rs).
GIGAPORT_VIBRATION_CHANNELS = (2, 3, 4, 5)
BLOCKSIZE = 2048
RING_MAX_BLOCKS = 6
PROCESSING_SR = TARGET_SR
# Extra gain on live vibration zones (file playback uses full stem energy).
LIVE_VIB_GAIN = 1.5


def _looks_like_bluetooth(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("bluetooth", "bt ", "hands-free", "hands free", "a2dp"))


def _is_hands_free(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in (
            "hands-free",
            "hands free",
            "hands-free hf",
            "hf audio",
            "ag audio",
            "bthhfenum",
        )
    )


def _is_stereo_bluetooth(name: str) -> bool:
    lowered = name.lower()
    if _is_hands_free(name):
        return False
    return _looks_like_bluetooth(name) or any(
        token in lowered for token in ("stereo", "a2dp", "avrcp", "bthhfenum.sys,#2")
    )


def _is_loopback_name(name: str) -> bool:
    return "loopback" in name.lower()


def _wasapi_hostapi_index() -> int | None:
    for idx, api in enumerate(sd.query_hostapis()):
        if "wasapi" in str(api["name"]).lower():
            return idx
    return None


def _find_loopback_input_index(output_name: str) -> int | None:
    """Match a WASAPI output name to its [Loopback] input sibling, if PortAudio exposes one."""
    base = output_name.strip().lower()
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) <= 0:
            continue
        name = str(dev["name"])
        if not _is_loopback_name(name):
            continue
        stem = name.lower().replace("[loopback]", "").replace("(loopback)", "").strip()
        if base in stem or stem in base:
            return idx
    return None


def list_capture_devices() -> list[dict[str, Any]]:
    """Return capture devices: stereo inputs and speaker loopback (not Hands-Free)."""
    devices: list[dict[str, Any]] = []
    seen_indices: set[int] = set()

    for idx, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) <= 0:
            continue
        name = str(dev["name"])
        if _is_hands_free(name):
            continue
        seen_indices.add(idx)
        devices.append(
            {
                "backend": "sounddevice",
                "index": idx,
                "name": name,
                "loopback": _is_loopback_name(name),
                "channels": min(2, int(dev["max_input_channels"])),
                "is_bluetooth": _looks_like_bluetooth(name),
                "is_hands_free": False,
                "is_stereo_bluetooth": _is_stereo_bluetooth(name),
                "hostapi": int(dev["hostapi"]),
            }
        )

    if sys.platform == "win32":
        wasapi_idx = _wasapi_hostapi_index()
        if wasapi_idx is not None:
            for idx, dev in enumerate(sd.query_devices()):
                if int(dev["hostapi"]) != wasapi_idx:
                    continue
                if int(dev["max_output_channels"]) <= 0:
                    continue
                name = str(dev["name"])
                if _is_loopback_name(name):
                    continue
                loopback_idx = _find_loopback_input_index(name)
                if loopback_idx is not None and loopback_idx not in seen_indices:
                    seen_indices.add(loopback_idx)
                    devices.append(
                        {
                            "backend": "sounddevice",
                            "index": loopback_idx,
                            "name": f"{name} [Loopback]",
                            "loopback": True,
                            "channels": min(2, int(sd.query_devices(loopback_idx)["max_input_channels"])),
                            "is_bluetooth": _looks_like_bluetooth(name),
                            "is_hands_free": False,
                            "is_stereo_bluetooth": _looks_like_bluetooth(name),
                            "hostapi": wasapi_idx,
                        }
                    )

        for sc_dev in list_speaker_loopback_devices():
            sc_name = sc_dev["name"].lower()
            if any(d.get("backend") == "soundcard" and d["name"].lower() == sc_name for d in devices):
                continue
            devices.append(
                {
                    **sc_dev,
                    "is_bluetooth": _looks_like_bluetooth(sc_dev["name"]),
                    "is_hands_free": False,
                    "is_stereo_bluetooth": _looks_like_bluetooth(sc_dev["name"]),
                }
            )

    devices.sort(
        key=lambda d: (
            not d.get("is_default_playback", False),
            not d.get("is_bluetooth", False),
            not d.get("loopback", False),
            d["name"].lower(),
        )
    )
    return devices


def pick_default_capture_device() -> dict[str, Any] | None:
    """Best default: Windows default playback speaker loopback."""
    devices = list_capture_devices()
    default_id = get_default_playback_speaker_id()
    if default_id:
        for dev in devices:
            if dev.get("soundcard_id") == default_id:
                return dev
            if default_id in str(dev.get("name", "")):
                return dev
    for dev in devices:
        if dev.get("is_default_playback"):
            return dev
    for dev in devices:
        if dev.get("is_bluetooth") and dev.get("backend") == "soundcard":
            return dev
    for dev in devices:
        if dev.get("loopback"):
            return dev
    return devices[0] if devices else None


def find_gigaport_loopback_device() -> dict[str, Any] | None:
    """Find speaker loopback for Gigaport (for AudioPlaybackConnector overlay capture)."""
    for dev in list_capture_devices():
        if dev.get("is_gigaport"):
            return dev
    for dev in list_capture_devices():
        name = str(dev.get("name", "")).lower()
        if "gigaport" in name and dev.get("loopback"):
            return dev
    return None


@dataclass(frozen=True)
class InputStreamConfig:
    sample_rate: int
    channels: int
    extra_settings: Any | None
    latency: str


def _wasapi_auto_convert() -> Any | None:
    if sys.platform != "win32":
        return None
    try:
        return sd.WasapiSettings(auto_convert=True)
    except Exception:
        return None


def _enumerate_input_configs(device_index: int) -> list[InputStreamConfig]:
    """Build ordered list of input open attempts for a capture device."""
    dev = sd.query_devices(device_index)
    name = str(dev["name"])
    max_ch = max(1, int(dev["max_input_channels"]))
    is_hfp = _is_hands_free(name)
    on_wasapi = _wasapi_hostapi_index() == int(dev["hostapi"])

    if is_hfp:
        channel_candidates = [1, 2] if max_ch >= 2 else [1]
        rate_candidates = [16000, 8000, 44100, 48000]
        latency_candidates = ["high", "medium", "low"]
    else:
        channel_candidates = [min(2, max_ch)]
        if max_ch >= 2:
            channel_candidates.append(1)
        rate_candidates = [PROCESSING_SR, 48000, 16000, 96000, 8000]
        default_sr = int(dev.get("default_samplerate") or 0)
        if default_sr and default_sr not in rate_candidates:
            rate_candidates.insert(1, default_sr)
        latency_candidates = ["low", "medium", "high"]

    extra_candidates: list[Any | None] = [None]
    wasapi_extra = _wasapi_auto_convert()
    if on_wasapi and wasapi_extra is not None:
        extra_candidates.insert(0, wasapi_extra)

    configs: list[InputStreamConfig] = []
    seen: set[tuple[int, int, str, bool]] = set()
    for extra in extra_candidates:
        for channels in channel_candidates:
            for sample_rate in rate_candidates:
                for latency in latency_candidates:
                    key = (sample_rate, channels, latency, extra is not None)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        kwargs: dict[str, Any] = {
                            "device": device_index,
                            "channels": channels,
                            "samplerate": sample_rate,
                            "dtype": "float32",
                        }
                        if extra is not None:
                            kwargs["extra_settings"] = extra
                        sd.check_input_settings(**kwargs)
                    except Exception:
                        continue
                    configs.append(
                        InputStreamConfig(
                            sample_rate=sample_rate,
                            channels=channels,
                            extra_settings=extra,
                            latency=latency,
                        )
                    )

    if not configs:
        fallback_ch = 1 if is_hfp else min(2, max_ch)
        fallback_sr = 16000 if is_hfp else PROCESSING_SR
        configs.append(
            InputStreamConfig(
                sample_rate=fallback_sr,
                channels=fallback_ch,
                extra_settings=wasapi_extra,
                latency="high" if is_hfp else "low",
            )
        )
    return configs


def _pick_output_sample_rate(output_index: int) -> int:
    candidates = [PROCESSING_SR, 48000, 96000, 88200, 44100]
    dev = sd.query_devices(output_index)
    default_sr = int(dev.get("default_samplerate") or 0)
    if default_sr and default_sr not in candidates:
        candidates.insert(1, default_sr)

    for sr in candidates:
        try:
            sd.check_output_settings(
                device=output_index,
                channels=SATORI_OUTPUT_CHANNELS,
                samplerate=sr,
                dtype="float32",
            )
            return sr
        except Exception:
            continue
    return PROCESSING_SR


def _validate_output_device(output_index: int) -> None:
    dev = sd.query_devices(output_index)
    channels = int(dev["max_output_channels"])
    if channels < SATORI_OUTPUT_CHANNELS:
        raise RuntimeError(
            f"Output device '{dev['name']}' has only {channels} channel(s). "
            f"Satori needs {SATORI_OUTPUT_CHANNELS} channels (Gigaport). "
            "Select Gigaport in Output Device, not Intel Speakers."
        )


class LiveStreamProcessor:
    """Thin wrapper around SatoriLiveProcessor for the live audio engine."""

    def __init__(
        self,
        sample_rate: int = PROCESSING_SR,
        segmentation_id: str = "default",
        frequency_profile_id: str = "satori",
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        self._core = SatoriLiveProcessor(
            sample_rate=sample_rate,
            frequency_profile_id=frequency_profile_id,
            segmentation_id=segmentation_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )

    def update_presets(
        self,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        self._core.update_presets(
            frequency_profile_id=frequency_profile_id,
            segmentation_id=segmentation_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )

    def process_block(self, stereo: np.ndarray) -> dict[str, Any]:
        return self._core.process_block(stereo)


class LiveAudioEngine:
    """Capture live/Bluetooth audio and route it through the 6-channel Satori output."""

    def __init__(self) -> None:
        self.input_stream: sd.InputStream | None = None
        self.output_stream: sd.OutputStream | None = None
        self._loopback_thread: SoundcardLoopbackThread | None = None
        self._vib_thread: threading.Thread | None = None
        self._loopback_stop = threading.Event()
        self._capture_queue: collections.deque[np.ndarray] = collections.deque(maxlen=RING_MAX_BLOCKS)
        self.processor = LiveStreamProcessor()
        self.capture_device_index: int | None = None
        self.output_device_index: int | None = None
        self.capture_channels = 2
        self.input_sample_rate = PROCESSING_SR
        self.output_sample_rate = PROCESSING_SR
        self.is_active = False
        self.vibration_overlay = False
        self.output_channel_count = SATORI_OUTPUT_CHANNELS
        self.volume = 0.85
        self.intensity_mid = 1.0
        self.intensity_legs = 1.0
        self.intensity_upper = 1.0
        self.intensity_head = 1.0
        self.lock = threading.Lock()
        self._output_ring: collections.deque[np.ndarray] = collections.deque(maxlen=RING_MAX_BLOCKS)
        self._current_block: np.ndarray | None = None
        self._current_offset = 0
        self.stats: dict[str, float] = {
            "rms_legs": 0.0,
            "rms_mid": 0.0,
            "rms_upper_mid": 0.0,
            "rms_head": 0.0,
            "bass_energy": 0.0,
            "input_rms": 0.0,
            "generated_freq_hz": 0.0,
            "cpu_usage_estimate": 0.0,
            "progress_fraction": 0.0,
        }

    def set_vibration_overlay(self, enabled: bool) -> None:
        self.vibration_overlay = enabled
        self.output_channel_count = 4 if enabled else SATORI_OUTPUT_CHANNELS

    def _passthrough_speakers(self, capture: np.ndarray) -> np.ndarray:
        """Fast L/R path — no vibration processing, avoids speaker dropouts."""
        if capture.ndim == 1:
            capture = capture.reshape(-1, 1)
        if capture.shape[1] == 1:
            capture = np.repeat(capture, 2, axis=1)
        stereo = ensure_stereo(capture)
        if self.input_sample_rate != PROCESSING_SR:
            stereo = resample_if_needed(stereo, self.input_sample_rate, PROCESSING_SR)
        if self.output_sample_rate != PROCESSING_SR:
            stereo = resample_if_needed(stereo, PROCESSING_SR, self.output_sample_rate)
        return (stereo * self.volume).astype(np.float32)

    def _render_vibration_only(self, capture: np.ndarray) -> np.ndarray:
        """Vibration zones only (overlay 4ch or full-mode ch 2–5)."""
        if capture.ndim == 1:
            capture = capture.reshape(-1, 1)
        if capture.shape[1] == 1:
            capture = np.repeat(capture, 2, axis=1)

        if self.input_sample_rate != PROCESSING_SR:
            capture = resample_if_needed(capture, self.input_sample_rate, PROCESSING_SR)

        capture = self._prepare_capture(capture)
        result = self.processor.process_block(capture)
        frames = len(capture)
        head = fit_frames(result["head"] * self.intensity_head, frames)
        upper = fit_frames(result["upper_mid"] * self.intensity_upper, frames)
        legs = fit_frames(result["legs"] * self.intensity_legs, frames)
        mid = fit_frames(result["mid"] * self.intensity_mid, frames)

        if self.vibration_overlay:
            vib = np.zeros((frames, 4), dtype=np.float32)
            vib[:, 0] = head
            vib[:, 1] = upper
            vib[:, 2] = legs
            vib[:, 3] = mid
        else:
            vib = np.zeros((frames, 4), dtype=np.float32)
            vib[:, 0] = head
            vib[:, 1] = upper
            vib[:, 2] = legs
            vib[:, 3] = mid

        if self.output_sample_rate != PROCESSING_SR:
            vib = resample_if_needed(vib, PROCESSING_SR, self.output_sample_rate)

        with self.lock:
            self.stats["rms_legs"] = result["rms_legs"]
            self.stats["rms_mid"] = result["rms_mid"]
            self.stats["rms_upper_mid"] = result["rms_upper_mid"]
            self.stats["rms_head"] = result["rms_head"]
            self.stats["bass_energy"] = result["bass_energy"]
            self.stats["generated_freq_hz"] = result.get("generated_freq_hz", 0.0)

        return np.clip(vib * LIVE_VIB_GAIN, -1.0, 1.0)

    def _assemble_output_block(self, raw: np.ndarray) -> np.ndarray:
        """Build one output block so L/R and vibration stay frame-aligned."""
        vib = self._render_vibration_only(raw)
        if self.vibration_overlay:
            return vib

        speakers = self._passthrough_speakers(raw)
        frames = len(speakers)
        if len(vib) != frames:
            vib = fit_frames(vib, frames)

        merged = np.zeros((frames, SATORI_OUTPUT_CHANNELS), dtype=np.float32)
        merged[:, 0] = speakers[:, 0]
        merged[:, 1] = speakers[:, 1]
        merged[:, 2] = vib[:, 0]
        merged[:, 3] = vib[:, 1]
        merged[:, 4] = vib[:, 2]
        merged[:, 5] = vib[:, 3]
        return merged

    def _open_output_stream(self, output_device_index: int) -> sd.OutputStream:
        if self.vibration_overlay:
            asio_extra = sd.AsioSettings(channel_selectors=list(GIGAPORT_VIBRATION_CHANNELS))
            return sd.OutputStream(
                device=output_device_index,
                channels=4,
                samplerate=self.output_sample_rate,
                dtype="float32",
                blocksize=BLOCKSIZE,
                callback=self._output_callback,
                latency="high",
                extra_settings=asio_extra,
            )
        return sd.OutputStream(
            device=output_device_index,
            channels=SATORI_OUTPUT_CHANNELS,
            samplerate=self.output_sample_rate,
            dtype="float32",
            blocksize=BLOCKSIZE,
            callback=self._output_callback,
            latency="high",
        )

    def set_volume(self, volume: float) -> None:
        self.volume = float(np.clip(volume, 0.0, 1.2))

    def set_zone_intensities(
        self,
        mid: float | None = None,
        legs: float | None = None,
        upper: float | None = None,
        head: float | None = None,
    ) -> None:
        if mid is not None:
            self.intensity_mid = float(np.clip(mid, 0.0, 3.0))
        if legs is not None:
            self.intensity_legs = float(np.clip(legs, 0.0, 3.0))
        if upper is not None:
            self.intensity_upper = float(np.clip(upper, 0.0, 3.0))
        if head is not None:
            self.intensity_head = float(np.clip(head, 0.0, 3.0))

    def update_presets(
        self,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
    ) -> None:
        self.processor.update_presets(
            segmentation_id,
            frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )

    def get_runtime_stats(self) -> dict[str, float]:
        with self.lock:
            return dict(self.stats)

    def start(
        self,
        capture_device: dict[str, Any],
        output_device_index: int,
        *,
        loopback: bool = False,  # noqa: ARG002 - kept for API compatibility
        capture_channels: int = 2,  # noqa: ARG002 - probed per device
        segmentation_id: str = "default",
        frequency_profile_id: str = "satori",
        synthetic_vibro: bool = False,
        synthetic_type_id: str = "sine",
        vibration_overlay: bool = False,
    ) -> None:
        self.set_vibration_overlay(vibration_overlay)
        _validate_output_device(output_device_index)
        if capture_device.get("backend") == "soundcard":
            self._start_soundcard(
                capture_device,
                output_device_index,
                segmentation_id,
                frequency_profile_id,
                synthetic_vibro,
                synthetic_type_id,
            )
            return
        if capture_device.get("is_hands_free"):
            raise RuntimeError(
                "Hands-Free Bluetooth cannot capture music on Windows. "
                "Pick a [Speaker Loopback] entry instead."
            )
        self._start_sounddevice(
            int(capture_device["index"]),
            output_device_index,
            segmentation_id,
            frequency_profile_id,
            synthetic_vibro,
            synthetic_type_id,
        )

    def _start_soundcard(
        self,
        capture_device: dict[str, Any],
        output_device_index: int,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool,
        synthetic_type_id: str,
    ) -> None:
        if not soundcard_available():
            raise RuntimeError(
                "Speaker loopback requires the soundcard package.\n"
                "On the tablet run: pip install soundcard"
            )

        self.stop()
        self.output_device_index = output_device_index
        self.output_sample_rate = _pick_output_sample_rate(output_device_index)
        capture_rate = self.output_sample_rate
        soundcard_id = str(capture_device["soundcard_id"])

        self.input_sample_rate = capture_rate
        self.processor = LiveStreamProcessor(
            sample_rate=PROCESSING_SR,
            segmentation_id=segmentation_id,
            frequency_profile_id=frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )

        with self.lock:
            self._output_ring.clear()
            self._capture_queue.clear()
            self._current_block = None
            self._current_offset = 0

        self.output_stream = self._open_output_stream(output_device_index)
        self.output_stream.start()

        self._loopback_stop.clear()

        def on_capture(data: np.ndarray) -> None:
            mono = data.mean(axis=1) if data.ndim > 1 else data
            level = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2) + 1e-10))
            with self.lock:
                self.stats["input_rms"] = level
                self._capture_queue.append(data.copy())

        def vibration_loop() -> None:
            while not self._loopback_stop.is_set():
                raw: np.ndarray | None = None
                with self.lock:
                    if self._capture_queue:
                        raw = self._capture_queue.popleft()
                if raw is None:
                    time.sleep(0.001)
                    continue
                try:
                    block = self._assemble_output_block(raw)
                    with self.lock:
                        self._output_ring.append(block)
                except Exception:
                    continue

        self._vib_thread = threading.Thread(target=vibration_loop, daemon=True)
        self._vib_thread.start()

        self._loopback_thread = SoundcardLoopbackThread(
            soundcard_id=soundcard_id,
            sample_rate=capture_rate,
            on_block=on_capture,
            stop_event=self._loopback_stop,
        )
        self._loopback_thread.start()
        self.is_active = True

    def _start_sounddevice(
        self,
        capture_device_index: int,
        output_device_index: int,
        segmentation_id: str,
        frequency_profile_id: str,
        synthetic_vibro: bool,
        synthetic_type_id: str,
    ) -> None:
        _validate_output_device(output_device_index)
        self.stop()
        self.capture_device_index = capture_device_index
        self.output_device_index = output_device_index
        self.output_sample_rate = _pick_output_sample_rate(output_device_index)
        self.processor = LiveStreamProcessor(
            sample_rate=PROCESSING_SR,
            segmentation_id=segmentation_id,
            frequency_profile_id=frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
        )

        with self.lock:
            self._output_ring.clear()
            self._capture_queue.clear()
            self._current_block = None
            self._current_offset = 0

        self._loopback_stop.clear()

        def vibration_loop() -> None:
            while not self._loopback_stop.is_set():
                raw: np.ndarray | None = None
                with self.lock:
                    if self._capture_queue:
                        raw = self._capture_queue.popleft()
                if raw is None:
                    time.sleep(0.001)
                    continue
                try:
                    block = self._assemble_output_block(raw)
                    with self.lock:
                        self._output_ring.append(block)
                except Exception:
                    continue

        self._vib_thread = threading.Thread(target=vibration_loop, daemon=True)
        self._vib_thread.start()

        input_configs = _enumerate_input_configs(capture_device_index)
        last_error: Exception | None = None

        for cfg in input_configs:
            self.capture_channels = cfg.channels
            self.input_sample_rate = cfg.sample_rate
            input_blocksize = max(
                256, int(BLOCKSIZE * self.input_sample_rate / self.output_sample_rate)
            )
            try:
                self.output_stream = self._open_output_stream(output_device_index)
                self.input_stream = sd.InputStream(
                    device=capture_device_index,
                    channels=cfg.channels,
                    samplerate=cfg.sample_rate,
                    dtype="float32",
                    blocksize=input_blocksize,
                    callback=self._input_callback,
                    latency=cfg.latency,
                    extra_settings=cfg.extra_settings,
                )
                self.output_stream.start()
                self.input_stream.start()
                self.is_active = True
                return
            except Exception as exc:
                last_error = exc
                self.stop()

        device_name = sd.query_devices(capture_device_index)["name"]
        hint = ""
        if _is_hands_free(str(device_name)):
            hint = (
                f"\n\nThe device '{device_name}' is a Hands-Free (phone-call) profile. "
                "Windows often cannot capture music from it.\n"
                "On your phone: disconnect Hands-Free, connect 'Stereo' / 'Media audio' instead.\n"
                "Or pick a [Loopback] speaker entry in the app."
            )
        raise RuntimeError(
            f"Could not open capture device after {len(input_configs)} attempts.{hint}"
        ) from last_error

    def stop(self) -> None:
        self.is_active = False
        self._loopback_stop.set()
        if self._loopback_thread is not None:
            self._loopback_thread.join(timeout=2.0)
            self._loopback_thread = None
        if self._vib_thread is not None:
            self._vib_thread.join(timeout=2.0)
            self._vib_thread = None
        for stream_attr in ("input_stream", "output_stream"):
            stream = getattr(self, stream_attr)
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            setattr(self, stream_attr, None)

        with self.lock:
            self._output_ring.clear()
            self._capture_queue.clear()
            self._current_block = None
            self._current_offset = 0
            self.stats = {
                "rms_legs": 0.0,
                "rms_mid": 0.0,
                "rms_upper_mid": 0.0,
                "rms_head": 0.0,
                "bass_energy": 0.0,
                "input_rms": 0.0,
                "generated_freq_hz": 0.0,
                "cpu_usage_estimate": 0.0,
                "progress_fraction": 0.0,
            }

    def _prepare_capture(self, capture: np.ndarray) -> np.ndarray:
        """Pass L/R loopback through unchanged — vibration is derived from this signal."""
        capture = np.asarray(capture, dtype=np.float32)
        if capture.ndim == 1:
            capture = capture.reshape(-1, 1)
        return capture

    def _take_next_output_block_locked(self) -> np.ndarray | None:
        """Pop the next output block. Caller must already hold self.lock."""
        if not self._output_ring:
            return None
        return self._output_ring.popleft()

    def _input_callback(self, indata, frames, _time_info, _status) -> None:
        capture = np.asarray(indata, dtype=np.float32)
        mono = capture.mean(axis=1) if capture.ndim > 1 else capture
        input_level = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2) + 1e-10))
        with self.lock:
            self.stats["input_rms"] = input_level
            self._capture_queue.append(capture.copy())

    def _output_callback(self, outdata, frames, _time_info, _status) -> None:
        needed = frames
        offset = 0
        ch_count = self.output_channel_count
        output = np.zeros((frames, ch_count), dtype=np.float32)

        while needed > 0:
            with self.lock:
                if self._current_block is None:
                    self._current_block = self._take_next_output_block_locked()
                    self._current_offset = 0

                if self._current_block is None:
                    break

                block = self._current_block
                start = self._current_offset
                available = len(block) - start
                take = min(needed, available)
                chunk = block[start : start + take]

            if take <= 0:
                break

            output[offset : offset + take] = chunk
            offset += take
            needed -= take

            with self.lock:
                self._current_offset += take
                if self._current_block is not None and self._current_offset >= len(self._current_block):
                    self._current_block = None
                    self._current_offset = 0

        outdata[:] = output

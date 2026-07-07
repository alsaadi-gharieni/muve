"""Windows speaker loopback capture via the soundcard library."""

from __future__ import annotations

import threading
import warnings
from typing import Callable

import numpy as np

from audio.audio_utils import TARGET_SR

CAPTURE_BLOCK_FRAMES = 2048
CAPTURE_SAMPLE_RATES = (44100, 48000, 96000, 88200, TARGET_SR)


def soundcard_available() -> bool:
    try:
        import soundcard  # noqa: F401
    except ImportError:
        return False
    return True


def _looks_like_bluetooth(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in ("bluetooth", "bt ", "a2dp", "poco", "galaxy", "iphone", "pixel", "redmi", "xiaomi")
    )


def get_default_playback_speaker_id() -> str | None:
    """Return the Windows default playback speaker name, if soundcard is available."""
    if not soundcard_available():
        return None
    try:
        import soundcard as sc

        return str(sc.default_speaker().name)
    except Exception:
        return None


def find_portaudio_loopback_input(matching_name: str) -> int | None:
    """Find a PortAudio WASAPI [Loopback] input matching a speaker name."""
    try:
        import sounddevice as sd
    except ImportError:
        return None

    needle = matching_name.strip().lower()
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) <= 0:
            continue
        name = str(dev["name"]).lower()
        if "loopback" not in name:
            continue
        stem = name.replace("[loopback]", "").replace("(loopback)", "").strip()
        if needle in stem or stem in needle:
            return idx
    return None


def probe_loopback_sample_rate(soundcard_id: str, preferred_rate: int = 44100) -> int:
    """Find a sample rate the loopback device accepts (Gigaport often needs 44100)."""
    import soundcard as sc

    mic = sc.get_microphone(id=soundcard_id, include_loopback=True)
    candidates: list[int] = []
    for sr in (preferred_rate, *CAPTURE_SAMPLE_RATES):
        if sr not in candidates:
            candidates.append(sr)

    last_error: Exception | None = None
    for sr in candidates:
        try:
            with mic.recorder(samplerate=sr, channels=2) as recorder:
                data = recorder.record(numframes=min(512, CAPTURE_BLOCK_FRAMES))
            if data is not None and np.asarray(data).size > 0:
                return sr
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError(
        f"Loopback device '{soundcard_id}' rejected all sample rates. "
        f"Last error: {last_error}"
    ) from last_error


def probe_portaudio_loopback_rate(device_index: int, preferred_rate: int = 44100) -> int:
    """Find a sample rate for a WASAPI [Loopback] input device."""
    import sounddevice as sd

    candidates: list[int] = []
    try:
        default_sr = int(sd.query_devices(device_index).get("default_samplerate", preferred_rate))
        candidates.append(default_sr)
    except Exception:
        pass
    for sr in (preferred_rate, *CAPTURE_SAMPLE_RATES):
        if sr not in candidates:
            candidates.append(sr)

    last_error: Exception | None = None
    for sr in candidates:
        try:
            with sd.InputStream(
                device=device_index,
                channels=2,
                samplerate=sr,
                dtype="float32",
                blocksize=min(512, CAPTURE_BLOCK_FRAMES),
            ) as stream:
                data, _overflowed = stream.read(min(512, CAPTURE_BLOCK_FRAMES))
            if data is not None and np.asarray(data).size > 0:
                return sr
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError(
        f"PortAudio loopback device #{device_index} rejected all sample rates. "
        f"Last error: {last_error}"
    ) from last_error


def list_speaker_loopback_devices() -> list[dict]:
    if not soundcard_available():
        return []

    import soundcard as sc

    devices: list[dict] = []
    seen: set[str] = set()

    for speaker in sc.all_speakers():
        speaker_id = str(speaker.name)
        if speaker_id in seen:
            continue
        seen.add(speaker_id)
        is_bt = _looks_like_bluetooth(speaker_id)
        is_gigaport = "gigaport" in speaker_id.lower()
        label = speaker.name
        if is_gigaport:
            label = f"Gigaport → {speaker.name}"
        elif is_bt:
            label = f"Phone/Bluetooth → {speaker.name}"
        pa_loopback = find_portaudio_loopback_input(speaker_id)
        devices.append(
            {
                "backend": "soundcard",
                "soundcard_id": speaker_id,
                "name": f"{label} [Speaker Loopback]",
                "loopback": True,
                "channels": 2,
                "is_bluetooth": is_bt,
                "is_gigaport": is_gigaport,
                "portaudio_loopback_index": pa_loopback,
            }
        )

    try:
        for mic in sc.all_microphones(include_loopback=True):
            mic_id = str(mic.name)
            if mic_id in seen:
                continue
            if not getattr(mic, "isloopback", False) and "loopback" not in mic_id.lower():
                continue
            seen.add(mic_id)
            devices.append(
                {
                    "backend": "soundcard",
                    "soundcard_id": mic_id,
                    "name": mic.name,
                    "loopback": True,
                    "channels": 2,
                    "is_bluetooth": _looks_like_bluetooth(mic_id),
                    "is_gigaport": "gigaport" in mic_id.lower(),
                    "portaudio_loopback_index": find_portaudio_loopback_input(mic_id),
                }
            )
    except Exception:
        pass

    default_id = get_default_playback_speaker_id()
    if default_id and default_id not in seen:
        seen.add(default_id)
        devices.insert(
            0,
            {
                "backend": "soundcard",
                "soundcard_id": default_id,
                "name": f"★ DEFAULT speaker → {default_id} [Speaker Loopback]",
                "loopback": True,
                "channels": 2,
                "is_bluetooth": _looks_like_bluetooth(default_id),
                "is_gigaport": "gigaport" in default_id.lower(),
                "is_default_playback": True,
                "portaudio_loopback_index": find_portaudio_loopback_input(default_id),
            },
        )

    return devices


class SoundcardLoopbackThread(threading.Thread):
    """Capture speaker loopback; only copies audio — no heavy processing here."""

    def __init__(
        self,
        soundcard_id: str,
        sample_rate: int,
        on_block: Callable[[np.ndarray], None],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.soundcard_id = soundcard_id
        self.sample_rate = sample_rate
        self.on_block = on_block
        self.stop_event = stop_event
        self.last_error: Exception | None = None

    def run(self) -> None:
        import soundcard as sc

        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="data discontinuity in recording")

                mic = sc.get_microphone(id=self.soundcard_id, include_loopback=True)
                with mic.recorder(samplerate=self.sample_rate, channels=2) as recorder:
                    while not self.stop_event.is_set():
                        try:
                            data = recorder.record(numframes=CAPTURE_BLOCK_FRAMES)
                        except Exception:
                            continue
                        if data is None:
                            continue
                        block = np.asarray(data, dtype=np.float32)
                        if block.size == 0:
                            continue
                        if block.ndim == 1:
                            block = block.reshape(-1, 1)
                        self.on_block(block)
        except Exception as exc:
            self.last_error = exc


class PortAudioLoopbackThread(threading.Thread):
    """Fallback loopback capture through sounddevice WASAPI [Loopback] input."""

    def __init__(
        self,
        device_index: int,
        sample_rate: int,
        on_block: Callable[[np.ndarray], None],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.on_block = on_block
        self.stop_event = stop_event
        self.last_error: Exception | None = None
        self._stream = None

    def run(self) -> None:
        import sounddevice as sd

        def callback(indata, frames, _time, _status) -> None:
            if self.stop_event.is_set():
                return
            self.on_block(np.asarray(indata, dtype=np.float32).copy())

        try:
            self._stream = sd.InputStream(
                device=self.device_index,
                channels=2,
                samplerate=self.sample_rate,
                dtype="float32",
                blocksize=CAPTURE_BLOCK_FRAMES,
                callback=callback,
                latency="high",
            )
            with self._stream:
                while not self.stop_event.is_set():
                    self.stop_event.wait(0.05)
        except Exception as exc:
            self.last_error = exc

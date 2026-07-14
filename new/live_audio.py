"""Start Live / Stop Live.

Modes (auto):
  1) Bluetooth connected in app → CABLE → Gigaport ASIO (phone via BT)
  2) Otherwise AUX if Behringer/USB interface present → Gigaport ASIO
  3) Else CABLE / default capture

So a plugged-in Behringer no longer steals capture while the phone is on Bluetooth.
"""

from __future__ import annotations

from typing import Any

from sounddevice import PortAudioError

from audio.gigaport_output import GigaportOutput
from audio.live_input import (
    DEFAULT_VIBE_SYNC_MS,
    LiveAudioEngine,
    find_gigaport_loopback_device,
    pick_aux_capture_device,
    pick_cable_capture_device,
    pick_default_capture_device,
)
from windows_cable import ensure_cable_default_playback

# Same as MainWindow.__init__
_gigaport = GigaportOutput()
_live_engine = LiveAudioEngine()

# Demo shares this engine so Start Live / Demo never fight over ASIO.
try:
    import demo_audio as _demo_audio

    _demo_audio.bind_engine(_live_engine, _gigaport)
except Exception:  # noqa: BLE001
    pass


def _log_input_devices() -> None:
    """Print PortAudio inputs so we can see Behringer naming on the tablet."""
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        print("[live_audio] PortAudio INPUT devices:", flush=True)
        for idx, d in enumerate(sd.query_devices()):
            if int(d["max_input_channels"]) <= 0:
                continue
            try:
                api = str(hostapis[int(d["hostapi"])]["name"])
            except Exception:  # noqa: BLE001
                api = "?"
            print(
                f"  [{idx}] {d['name']} — {api} in={d['max_input_channels']}",
                flush=True,
            )
        try:
            default_in = sd.default.device[0]
            print(f"[live_audio] default input index={default_in}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"[live_audio] input list failed: {exc}", flush=True)


def _pick_cable_or_default() -> tuple[dict[str, Any], str]:
    capture = pick_cable_capture_device()
    if capture is not None:
        return capture, "cable"

    capture = pick_default_capture_device()
    if capture is None:
        raise RuntimeError(
            "No capture device. Plug in Behringer (AUX) or install VB-Audio Virtual Cable (Bluetooth)."
        )
    name = str(capture.get("name", "")).lower()
    if "gigaport" in name or capture.get("is_gigaport"):
        raise RuntimeError(
            "No Behringer/CABLE capture found. Connect AUX interface or set up VB-Cable."
        )
    return capture, "cable"


def _pick_capture(
    *,
    vibration_overlay: bool,
    prefer_bluetooth: bool = False,
) -> tuple[dict[str, Any], str]:
    """Return (capture_device, mode) where mode is 'aux' | 'cable' | 'overlay'.

    Priority:
      - Bluetooth connected → CABLE first (phone is streaming over BT)
      - No Bluetooth → AUX first if interface present, else CABLE
    """
    _log_input_devices()

    if vibration_overlay:
        gigaport_lb = find_gigaport_loopback_device()
        if gigaport_lb is not None:
            return gigaport_lb, "overlay"

    aux = pick_aux_capture_device()

    if prefer_bluetooth:
        print(
            "[live_audio] Bluetooth connected — preferring CABLE over AUX",
            flush=True,
        )
        try:
            return _pick_cable_or_default()
        except RuntimeError:
            if aux is not None:
                print(
                    "[live_audio] CABLE unavailable — falling back to AUX",
                    flush=True,
                )
                return aux, "aux"
            raise

    # Phone on AUX (no BT session): use Behringer when present.
    if aux is not None:
        print("[live_audio] no Bluetooth session — using AUX", flush=True)
        return aux, "aux"

    print(
        "[live_audio] no AUX/Behringer/USB input matched — using CABLE (Bluetooth)",
        flush=True,
    )
    return _pick_cable_or_default()


def start_live_audio(
    *,
    volume: float = 0.85,
    mid: float = 1.0,
    legs: float = 1.0,
    upper: float = 1.0,
    head: float = 1.0,
    vibe_sync_ms: float = DEFAULT_VIBE_SYNC_MS,
    highpass_hz: float = 30.0,
    cutoff_hz: float = 180.0,
    vibration_overlay: bool = False,
    segmentation_id: str = "default",
    frequency_profile_id: str = "satori",
    synthetic_vibro: bool = False,
    synthetic_type_id: str = "sine",
    prefer_bluetooth: bool = False,
) -> dict[str, Any]:
    """Start Live: BT session → CABLE; else AUX if present; else CABLE."""
    capture, mode = _pick_capture(
        vibration_overlay=vibration_overlay,
        prefer_bluetooth=prefer_bluetooth,
    )

    cable_route: dict[str, Any] = {"ok": True, "skipped": True}
    if mode == "cable":
        # Only force CABLE as Windows default for Bluetooth phone routing.
        cable_route = ensure_cable_default_playback()
        if not cable_route.get("ok") and not cable_route.get("skipped"):
            print(f"[live_audio] warn: {cable_route.get('error')}")

    devices = _gigaport.list_output_devices(min_channels=6)
    if not devices:
        raise RuntimeError(
            "No 6-channel output device is available.\n\n"
            "On Windows:\n"
            "1. Connect Gigaport eX via USB\n"
            "2. Install the Gigaport ASIO driver from ESI\n"
            "3. Close and restart this app (ASIO is enabled at startup)\n"
            "4. Look for 'Gigaport' with ASIO and 6+ channels"
        )
    # Never use VB-Cable as output — always Gigaport ASIO first.
    output_index = devices[0]["index"]
    output_name = (
        f"{devices[0]['name']} ({devices[0]['channels']} ch, "
        f"{devices[0]['hostapi']}, #{output_index})"
    )

    print(
        f"[live_audio] mode={mode} prefer_bt={prefer_bluetooth} "
        f"capture={capture.get('name')!r} "
        f"backend={capture.get('backend')} "
        f"output={output_name!r}",
        flush=True,
    )

    _gigaport.stop()
    _gigaport.release_output_device()
    _live_engine.set_volume(volume)
    _live_engine.set_vibe_sync_ms(vibe_sync_ms)
    _live_engine.set_zone_intensities(mid=mid, legs=legs, upper=upper, head=head)
    _live_engine.set_highpass_hz(highpass_hz)
    _live_engine.set_lowpass_hz(cutoff_hz)

    try:
        _live_engine.start(
            capture_device=capture,
            output_device_index=output_index,
            loopback=bool(capture.get("loopback")),
            capture_channels=int(capture.get("channels", 2)),
            segmentation_id=segmentation_id,
            frequency_profile_id=frequency_profile_id,
            synthetic_vibro=synthetic_vibro,
            synthetic_type_id=synthetic_type_id,
            vibration_overlay=vibration_overlay,
        )
    except (PortAudioError, RuntimeError, OSError) as exc:
        raise RuntimeError(str(exc)) from exc

    return {
        "ok": True,
        "capture": str(capture.get("name")),
        "output_index": output_index,
        "output_name": output_name,
        "vibration_overlay": vibration_overlay,
        "mode": mode,
        "cable_default": cable_route.get("name"),
    }


def stop_live_audio() -> None:
    if _live_engine.is_active:
        _live_engine.stop()


def set_volume(value: float) -> None:
    _live_engine.set_volume(value)


def set_vibration(value: float) -> None:
    _live_engine.set_zone_intensities(mid=value, legs=value, upper=value, head=value)


def set_highpass_hz(value: float) -> None:
    _live_engine.set_highpass_hz(value)


def set_cutoff_hz(value: float) -> None:
    """Bass cutoff = muvi vibe_lowpass_hz."""
    _live_engine.set_lowpass_hz(value)


def get_runtime_stats() -> dict[str, float]:
    return _live_engine.get_runtime_stats()


def is_active() -> bool:
    return _live_engine.is_active

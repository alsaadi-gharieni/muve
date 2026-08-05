"""USB hardware presence for UI status icons (Gigaport vibration, headphones audio)."""

from __future__ import annotations

import re
import sys
import time
from typing import Any

import sounddevice as sd

from audio.output_devices import (
    _group_units,
    _hostapi_name,
    is_audio_interface_name,
    is_gigaport_name,
    is_usable_output_hostapi,
    list_output_devices,
)

_last_refresh_ts = 0.0
_REFRESH_INTERVAL_SEC = 2.5


def _should_refresh_portaudio(playing: bool) -> bool:
    if playing:
        return False
    global _last_refresh_ts
    now = time.time()
    if (now - _last_refresh_ts) >= _REFRESH_INTERVAL_SEC:
        _last_refresh_ts = now
        return True
    return False


def _refresh_portaudio() -> None:
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def _short_name(raw: str) -> str:
    return re.sub(r"\s*\[.*?\]\s*", "", str(raw)).strip()


def _gigaport_endpoint_visible(name: str) -> bool:
    """True when a Gigaport endpoint reflects real hardware (not ghost driver)."""
    n = name.lower()
    if "driver" in n and "gigaport ex driver" in n:
        return False
    if sys.platform != "win32":
        return True
    if ("multi" in n and "8" in n) or any(
        token in n for token in ("ch1&2", "ch3&4", "ch5&6", "ch7&8", "ch182", "ch384", "ch586", "ch788")
    ):
        return True
    if re.search(r"ch\s*(12|34|56|78)\b", n) is not None:
        return True
    if "speakers" in n and "gigaport" in n:
        return True
    return False


def _scan_outputs() -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Return (gigaport_units, codec_name) from usable PortAudio outputs."""
    units: dict[str, list[dict[str, Any]]] = {}
    codec_name: str | None = None

    try:
        raw: list[dict[str, Any]] = []
        for idx, dev in enumerate(sd.query_devices()):
            out_ch = int(dev.get("max_output_channels", 0))
            if out_ch <= 0:
                continue
            name = str(dev.get("name", ""))
            api = _hostapi_name(int(dev.get("hostapi", 0)))
            if not is_usable_output_hostapi(api):
                continue
            entry = {
                "index": idx,
                "name": name,
                "channels": out_ch,
                "hostapi": api,
                "is_gigaport": is_gigaport_name(name),
            }
            if entry["is_gigaport"] and _gigaport_endpoint_visible(name):
                raw.append(entry)
            elif (
                codec_name is None
                and not entry["is_gigaport"]
                and is_audio_interface_name(name)
                and out_ch >= 2
            ):
                codec_name = _short_name(name)

        if raw:
            units = _group_units(raw)
    except Exception as exc:
        print(f"[hardware] device scan failed: {exc}")

    # Fallback: shared list_output_devices path (preferred APIs only).
    if not units or codec_name is None:
        try:
            all_devs = list_output_devices()
            if not units:
                gigaports = [d for d in all_devs if d.get("is_gigaport")]
                units = _group_units(gigaports)
            if codec_name is None:
                for d in all_devs:
                    if (
                        not d.get("is_gigaport")
                        and is_audio_interface_name(d["name"])
                        and int(d.get("channels", 0)) >= 2
                    ):
                        codec_name = _short_name(d["name"])
                        break
        except Exception as exc:
            print(f"[hardware] list_output_devices failed: {exc}")

    return units, codec_name


def _vibration_unit(units: dict[str, list[dict[str, Any]]]) -> tuple[str | None, dict[str, Any] | None]:
    """Prefer the physical unit with the most channels (8-ch vibration Gigaport)."""
    vib_key = None
    vib = None
    for key in sorted(units.keys()):
        best = units[key][0]
        if vib is None or int(best.get("channels", 0)) > int(vib.get("channels", 0)):
            vib = best
            vib_key = key
    return vib_key, vib


def probe_hardware_status(playing: bool = False, force_refresh: bool = False) -> dict[str, Any]:
    """Detect vibration Gigaport + headphones audio.

    Headphones is connected when either is present, priority order:
      1. USB audio codec (Behringer / UFO202 / USB Audio CODEC)
      2. Second physical Gigaport
    """
    if force_refresh or _should_refresh_portaudio(playing):
        _refresh_portaudio()

    units, codec_name = _scan_outputs()
    vib_key, vib = _vibration_unit(units)
    gigaport_count = len(units)
    gigaport_name = _short_name(vib["name"]) if vib else None

    headphones_name: str | None = None
    headphones_kind: str | None = None

    # Priority 1: USB codec
    if codec_name is not None:
        headphones_name = codec_name
        headphones_kind = "codec"
    else:
        # Priority 2: second Gigaport (not the vibration unit)
        for key in sorted(units.keys()):
            if key == vib_key:
                continue
            headphones_name = _short_name(units[key][0]["name"])
            headphones_kind = "gigaport"
            break

    return {
        "gigaport_connected": gigaport_count > 0,
        "gigaport_count": gigaport_count,
        "gigaport_name": gigaport_name,
        # Front-end headphones icon/row still uses codec_* keys.
        "codec_connected": headphones_name is not None,
        "codec_name": headphones_name,
        "headphones_kind": headphones_kind,
    }

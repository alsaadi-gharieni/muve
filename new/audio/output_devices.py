"""Playback device discovery.

Windows: WASAPI / WDM-KS only (no ASIO). macOS: Core Audio. Linux: ALSA/JACK/Pulse.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import sounddevice as sd

from audio.gigaport_routing import OutputLayout

# Host APIs safe for multichannel Gigaport output, per platform.
# Windows: WASAPI / WDM-KS only (no ASIO, MME, DirectSound).
# macOS:   Core Audio (the only native API; works with the Gigaport).
# Linux:   ALSA / JACK / PulseAudio.
if sys.platform == "darwin":
    _PREFERRED_HOSTAPIS = ("core audio", "coreaudio")
    _BLOCKED_HOSTAPIS: tuple[str, ...] = ()
elif sys.platform.startswith("linux"):
    _PREFERRED_HOSTAPIS = ("alsa", "jack", "pulse")
    _BLOCKED_HOSTAPIS = ()
else:  # win32 and anything else
    _PREFERRED_HOSTAPIS = ("wdm-ks", "wasapi")
    _BLOCKED_HOSTAPIS = ("asio", "mme", "directsound")


def _hostapi_name(hostapi_index: int) -> str:
    try:
        return str(sd.query_hostapis()[int(hostapi_index)]["name"])
    except Exception:
        return "?"


def is_usable_output_hostapi(api_name: str) -> bool:
    lowered = api_name.lower()
    if any(b in lowered for b in _BLOCKED_HOSTAPIS):
        return False
    return any(p in lowered for p in _PREFERRED_HOSTAPIS)


def is_gigaport_name(name: str) -> bool:
    return "gigaport" in name.lower()


def is_audio_interface_name(name: str) -> bool:
    """A plain stereo USB audio interface usable as the audio (sound) output.

    Covers the Behringer U-Phono UFO202 and similar 2-channel USB codecs that
    can replace the 2nd Gigaport for headphone/line audio.
    """
    n = name.lower()
    return any(
        k in n
        for k in (
            "u-phono",
            "uphono",
            "u phono",
            "ufo202",
            "ufo-202",
            "behringer",
            # The UFO202 uses a generic USB-audio-class chip that Windows often
            # enumerates simply as "USB Audio CODEC".
            "usb audio codec",
        )
    )


def wasapi_output_extra(device_index: int) -> Any | None:
    if sys.platform != "win32":
        return None
    api = _hostapi_name(int(sd.query_devices(device_index)["hostapi"])).lower()
    if "wasapi" not in api:
        return None
    try:
        return sd.WasapiSettings(exclusive=False, auto_convert=True)
    except Exception:
        return None


def probe_output_channels(device_index: int, sample_rate: int, candidates: tuple[int, ...]) -> int | None:
    extra = wasapi_output_extra(device_index)
    for ch in candidates:
        try:
            kwargs: dict[str, Any] = {
                "device": device_index,
                "channels": ch,
                "samplerate": sample_rate,
                "dtype": "float32",
            }
            if extra is not None:
                kwargs["extra_settings"] = extra
            sd.check_output_settings(**kwargs)
            return ch
        except Exception:
            continue
    return None


def list_output_devices(*, include_all: bool = False) -> list[dict[str, Any]]:
    """List playback devices. By default only WASAPI / WDM-KS (no ASIO/MME)."""
    devices: list[dict[str, Any]] = []
    for idx, dev in enumerate(sd.query_devices()):
        out_ch = int(dev["max_output_channels"])
        if out_ch <= 0:
            continue
        name = str(dev["name"])
        api_name = _hostapi_name(int(dev["hostapi"]))
        usable = is_usable_output_hostapi(api_name)
        if not include_all and not usable:
            continue
        gigaport = is_gigaport_name(name)
        devices.append(
            {
                "index": idx,
                "name": name,
                "channels": out_ch,
                "hostapi": api_name,
                "is_gigaport": gigaport,
                "usable": usable,
                "default_sr": int(dev.get("default_samplerate") or 44100),
            }
        )

    def _sort_key(d: dict[str, Any]) -> tuple:
        api = str(d.get("hostapi", "")).lower()
        wdm = "wdm" in api
        wasapi = "wasapi" in api
        # WASAPI first: it goes through the Windows audio engine that is
        # actually wired to the device. WDM-KS on this Gigaport opens (and even
        # "plays") but produces no physical signal, and some endpoints fail to
        # open at all (PaErrorCode -9996). WDM-KS is kept only as a fallback.
        return (
            not d.get("is_gigaport", False),
            not wasapi,
            not wdm,
            -int(d.get("channels", 0)),
            d["name"].lower(),
        )

    devices.sort(key=_sort_key)
    return devices


def gigaport_unit_key(name: str) -> str:
    """Group Gigaport endpoints by physical USB unit.

    Windows enumerates a second identical device as '2- <name>', a third as
    '3- <name>', and so on. Endpoints without that numeric prefix belong to the
    first unit. All endpoints of one physical Gigaport (WASAPI 'Speakers' +
    each WDM-KS 'Line Out (… CHx&y)') share the same key.
    """
    m = re.search(r"(\d+)-\s", name)
    return m.group(1) if m else "1"


def _unit_key(dev: dict[str, Any]) -> str:
    """Physical-unit key for a Gigaport device, per platform.

    Windows exposes one physical Gigaport as several endpoints that share a
    name (WASAPI 'Speakers' + WDM-KS 'Line Out …'), told apart only by the
    '2- ' numeric prefix — so we group by that prefix. macOS Core Audio and
    Linux ALSA expose one device per physical unit, and two identical units get
    the *same* name, so there we key by the unique device index instead.
    """
    if sys.platform == "win32":
        return gigaport_unit_key(dev["name"])
    return str(dev["index"])


def _group_units(gigaports: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Map physical-unit key -> its endpoints (kept in best-first sort order)."""
    units: dict[str, list[dict[str, Any]]] = {}
    for d in gigaports:
        units.setdefault(_unit_key(d), []).append(d)
    return units


def _short_device_name(name: str) -> str:
    return re.sub(r"\s*\[.*?\]\s*", "", str(name)).strip()


def settings_display_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Usable outputs for Settings UI — one Gigaport endpoint per physical unit."""
    usable = [d for d in devices if d.get("usable")]
    gigaports = [d for d in usable if d.get("is_gigaport")]
    non_gig = [d for d in usable if not d.get("is_gigaport")]
    units = _group_units(gigaports)
    gig_display = [units[k][0] for k in sorted(units.keys())]
    non_gig.sort(key=lambda d: (-int(d.get("channels", 0)), str(d.get("name", "")).lower()))
    return gig_display + non_gig


def _device_label(dev: dict[str, Any]) -> str:
    return (
        f"{dev['name']} ({dev['channels']} ch, {dev['hostapi']}, #{dev['index']})"
    )


def pick_output_devices(
    *,
    vibration_index: int | None = None,
    audio_index: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, OutputLayout, dict[str, Any]]:
    """Auto-detect vibration (+ optional audio) Gigaport outputs on WASAPI/WDM-KS.

    Two physical Gigaports are separated by their Windows unit prefix so the
    audio device is never just another channel-pair of the vibration unit.
    """
    meta: dict[str, Any] = {"warnings": []}
    all_devs = list_output_devices()
    gigaports = [d for d in all_devs if d.get("is_gigaport")]
    units = _group_units(gigaports)

    # ------------------------------------------------------ vibration device --
    if vibration_index is not None:
        vib = next((d for d in all_devs if d["index"] == vibration_index), None)
        if vib is None:
            raise RuntimeError(f"Vibration output device #{vibration_index} not found")
        vib_unit = _unit_key(vib)
    else:
        # Prefer the physical unit with the most output channels (the 8-ch one);
        # ties resolve to the lowest unit number ('1' before '2').
        vib_unit = None
        vib = None
        for key in sorted(units.keys()):
            best = units[key][0]  # already sorted: WDM-KS first, most channels first
            if vib is None or int(best["channels"]) > int(vib["channels"]):
                vib = best
                vib_unit = key

    if vib is None:
        apis = " / ".join(a.upper() if len(a) <= 6 else a.title() for a in _PREFERRED_HOSTAPIS)
        raise RuntimeError(
            f"No Gigaport output on {apis}.\n\n"
            "Connect the Gigaport(s) via USB and install the ESI driver.\n"
            "Open Settings to see detected devices — ASIO is not used."
        )

    # ---------------------------------------------------------- audio device --
    if audio_index is not None:
        audio = next((d for d in all_devs if d["index"] == audio_index), None)
        if audio is None:
            raise RuntimeError(f"Audio output device #{audio_index} not found")
    else:
        # Headphones / audio priority:
        #   1. USB codec (Behringer / UFO202 / USB Audio CODEC)
        #   2. Second physical Gigaport
        audio = next(
            (
                d
                for d in all_devs
                if not d.get("is_gigaport")
                and d["index"] != vib["index"]
                and int(d["channels"]) >= 2
                and is_audio_interface_name(d["name"])
            ),
            None,
        )
        if audio is None:
            for key in sorted(units.keys()):
                if key != vib_unit:
                    audio = units[key][0]
                    break

    # Dual: separate vibration + audio devices. A non-Gigaport audio interface
    # (UFO202) is never part of the vibration unit, so only collapse when both
    # devices are Gigaport endpoints of the same physical unit.
    same_unit = (
        audio is not None and audio.get("is_gigaport") and _unit_key(audio) == vib_unit
    )
    if audio is not None and not same_unit and audio["index"] != vib["index"]:
        vib_ch = probe_output_channels(vib["index"], 44100, (8, 6, 4, 2))
        audio_ch = probe_output_channels(audio["index"], 44100, (4, 2))
        if vib_ch is None:
            raise RuntimeError(
                f"Cannot open vibration output on {_device_label(vib)}"
            )
        if audio_ch is None:
            raise RuntimeError(
                f"Cannot open audio output on {_device_label(audio)}"
            )
        if vib_ch < 8:
            meta["warnings"].append(
                f"Vibration device supports {vib_ch} ch (wanted 8) — zones may be limited"
            )
        vib = {**vib, "probed_channels": vib_ch}
        audio = {**audio, "probed_channels": audio_ch}
        return vib, audio, "dual_native", meta

    if same_unit:
        # One physical Gigaport drives both audible output and vibration (legacy 6ch).
        single_ch = probe_output_channels(vib["index"], 44100, (6, 4, 2))
        if single_ch is None:
            raise RuntimeError(
                f"Cannot open output stream on {_device_label(vib)}"
            )
        if single_ch < 6:
            meta["warnings"].append(
                f"Single device supports {single_ch} ch (wanted 6) — using reduced layout"
            )
        vib = {**vib, "probed_channels": single_ch}
        return vib, audio, "single", meta

    # No separate audio device: all Gigaport outputs are vibration-only (no L/R on 1-2).
    vib_ch = probe_output_channels(vib["index"], 44100, (8, 6, 4, 2))
    if vib_ch is None:
        raise RuntimeError(
            f"Cannot open vibration output on {_device_label(vib)}"
        )
    if vib_ch < 8:
        meta["warnings"].append(
            f"Vibration device supports {vib_ch} ch (wanted 8) — zones may be limited"
        )
    vib = {**vib, "probed_channels": vib_ch}
    return vib, None, "vibration_only", meta


def build_audio_settings(
    *,
    active: bool = False,
    output_layout: str | None = None,
    vibration_index: int | None = None,
    audio_index: int | None = None,
    output_name: str | None = None,
    engine_error: str | None = None,
) -> dict[str, Any]:
    """Snapshot for the Settings screen."""
    all_devices = list_output_devices(include_all=True)
    display_devs = settings_display_devices(all_devices)
    gigaports = [d for d in display_devs if d.get("is_gigaport")]
    try:
        picked_vib, picked_audio, layout, meta = pick_output_devices(
            vibration_index=vibration_index,
            audio_index=audio_index,
        )
        auto_ok = True
        auto_error = None
    except Exception as exc:
        picked_vib, picked_audio, layout, meta = None, None, "single", {"warnings": []}
        auto_ok = False
        auto_error = str(exc)

    sel_vib_idx = vibration_index
    sel_aud_idx = audio_index

    rows: list[dict[str, Any]] = []
    for d in display_devs:
        short = _short_device_name(d["name"])
        status = "ready"
        if active and picked_vib and d["index"] == picked_vib["index"]:
            status = "active"
        elif active and picked_audio and d["index"] == picked_audio["index"]:
            status = "active"
        elif vibration_index is not None and d["index"] == vibration_index:
            status = "selected"
        elif audio_index is not None and d["index"] == audio_index:
            status = "selected"
        rows.append({
            **d,
            "short_name": short,
            "status": status,
        })

    return {
        "backend": "wasapi/wdm-ks",
        "asio_enabled": False,
        "devices": rows,
        "gigaport_count": len(gigaports),
        "auto_pick_ok": auto_ok,
        "auto_pick_error": auto_error,
        "picked_vibration": picked_vib,
        "picked_audio": picked_audio,
        "selected_vibration_index": sel_vib_idx,
        "selected_audio_index": sel_aud_idx,
        "in_use_vibration_index": picked_vib["index"] if active and picked_vib else None,
        "in_use_audio_index": picked_audio["index"] if active and picked_audio else None,
        "output_layout": output_layout or layout,
        "active": active,
        "output_name": output_name,
        "engine_error": engine_error,
        "warnings": meta.get("warnings") or [],
    }

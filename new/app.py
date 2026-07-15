"""muve — standalone Windows app shell (pywebview).

Play button = Start Live / Stop Live (same as the old PyQt app).
Bluetooth Connect opens AudioPlaybackConnection (replaces APC).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

# Frozen exe doubles as the audio worker (sys.executable is app.exe, not python).
# Must run before webview/WinRT imports — WinRT in-process breaks ASIO.
if "--engine-worker" in sys.argv:
    import engine_worker

    raise SystemExit(engine_worker.main())

# Gigaport eX exposes 6 channels via ASIO — must be set before sounddevice loads.
if sys.platform == "win32":
    os.environ.setdefault("SD_ENABLE_ASIO", "1")

import webview

from bt_audio import BluetoothAudioService
from engine_bridge import EngineBridge
from media_session import MediaSessionService

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# Set True to open the app fullscreen on the Windows tablet.
FULLSCREEN = True


class Api:
    """JS <-> Python bridge. Keep every return value JSON-serializable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {
            "playing": False,
            "volume": 0.70,
            "vibration": 0.27,
            "cutoff_hz": 200.0,  # ~76% bass cutoff (40–250 Hz)
            "highpass_hz": 200.0,
            "position": 0.0,
            "input_rms": 0.0,
            "zone_rms": {
                "head": 0.0, "upper": 0.0, "mid": 0.0, "legs": 0.0,
                "left": 0.0, "right": 0.0,
            },
            "track": {
                "title": "Live Bluetooth",
                "artist": "Phone audio",
                "album": "AUX: plug in → Play · Bluetooth: Connect → play phone → Play",
                "duration": 0.0,
                "artwork": "assets/artwork.svg",
            },
            "bluetooth": {
                "connected_id": None,
                "connecting_id": None,
                "scanning": False,
                "discovering": False,
                "pairing_id": None,
                "forgetting": False,
                "error": None,
                "alert": None,
                "supported": False,
            },
            "engine_ok": False,
            "engine_error": None,
            "capture_name": None,
            "output_name": None,
            "live_mode": None,  # 'aux' | 'cable' | 'overlay' | 'demo' while Live is on
            "play_busy": False,
            "media": {
                "ok": False,
                "can_seek": False,
                "can_prev": False,
                "can_next": False,
            },
            "demo": {
                "available": os.path.isfile(os.path.join(BASE_DIR, "assets", "demo.wav")),
                "error": None,
            },
        }
        self._bt = BluetoothAudioService()
        self._media = MediaSessionService()
        # Real paired devices only — no mock phones in the list.
        self._devices: list[dict[str, Any]] = []
        self._nearby: list[dict[str, Any]] = []
        self.state["bluetooth"]["supported"] = self._bt.available
        if not self._bt.available:
            self.state["bluetooth"]["error"] = self._bt.last_error
        self._last_tick = time.monotonic()
        self._default_track = dict(self.state["track"])
        self._demo_track = {
            "title": "Demo",
            "artist": "MUVI test track",
            "album": "assets/demo.wav",
            "duration": 0.0,
            "artwork": "assets/artwork.svg",
        }
        self._live_started_at: float | None = None
        self._last_paired_refresh = 0.0
        self._paired_refresh_busy = False

        # Private so pywebview does not recurse into the engine object graph.
        self._engine = EngineBridge()
        self.state["engine_ok"] = self._engine.available
        self.state["engine_error"] = None if self._engine.available else self._engine.last_error
        print(
            "[app] engine ready"
            if self._engine.available
            else f"[app] engine unavailable: {self._engine.last_error}"
        )
        # Show already-paired phones without requiring Scan.
        if self._bt.available:
            self._schedule_paired_refresh(force=True)

    # ------------------------------------------------------------------ state --
    def get_state(self) -> dict[str, Any]:
        self._refresh_bluetooth_link()
        self._schedule_paired_refresh()
        self._refresh_media()
        with self._lock:
            if self.state["playing"] and self._engine.available:
                stats = self._engine.get_runtime_stats()
                self.state["input_rms"] = float(stats.get("input_rms", 0.0))
                # One signal per shaker row (head, upper back, mid back, legs)
                # plus the stereo audio channels (left, right).
                self.state["zone_rms"] = {
                    "head": float(stats.get("rms_head", 0.0)),
                    "upper": float(stats.get("rms_upper_mid", 0.0)),
                    "mid": float(stats.get("rms_mid", 0.0)),
                    "legs": float(stats.get("rms_legs", 0.0)),
                    "left": float(stats.get("rms_audio_left", 0.0)),
                    "right": float(stats.get("rms_audio_right", 0.0)),
                }
            else:
                self.state["zone_rms"] = {
                    "head": 0.0, "upper": 0.0, "mid": 0.0, "legs": 0.0,
                    "left": 0.0, "right": 0.0,
                }
            return self._snapshot()

    def _schedule_paired_refresh(self, *, force: bool = False) -> None:
        """Keep the paired-phones list current without a Scan button."""
        if not self._bt.available:
            return
        now = time.monotonic()
        with self._lock:
            busy = bool(
                self.state["bluetooth"].get("scanning")
                or self.state["bluetooth"].get("discovering")
                or self.state["bluetooth"].get("pairing_id")
                or self.state["bluetooth"].get("connecting_id")
                or self.state["bluetooth"].get("forgetting")
            )
            if self._paired_refresh_busy or busy:
                return
            if not force and (now - self._last_paired_refresh) < 6.0:
                return
            self._paired_refresh_busy = True
            self._last_paired_refresh = now

        def _work() -> None:
            try:
                devices = self._bt.list_devices()
                with self._lock:
                    connected_id = self.state["bluetooth"].get("connected_id")
                    by_id = {d["id"]: d for d in devices}
                    if connected_id:
                        for d in self._devices:
                            if d.get("id") == connected_id and connected_id not in by_id:
                                by_id[connected_id] = d
                    self._devices = list(by_id.values())
            except Exception as exc:  # noqa: BLE001
                print(f"[app] paired refresh failed: {exc}")
            finally:
                with self._lock:
                    self._paired_refresh_busy = False

        threading.Thread(target=_work, daemon=True, name="bt-paired-refresh").start()

    def _refresh_bluetooth_link(self) -> None:
        """Clear stale 'connected' when phone drops or Windows Bluetooth is off."""
        with self._lock:
            connected_id = self.state["bluetooth"].get("connected_id")
            connecting = self.state["bluetooth"].get("connecting_id")
            live_mode = self.state.get("live_mode")
            playing = self.state.get("playing")
        if not connected_id or connecting:
            return

        status = self._bt.get_status(connected_id)
        if status.get("connected"):
            return

        reason = status.get("reason") or "disconnected"
        message = status.get("message") or "Bluetooth disconnected"
        print(f"[app] Bluetooth link lost ({reason}): {message}")

        # Drop app-side connection; stop Live if we were on the BT/CABLE path.
        if playing and live_mode == "cable":
            try:
                self._engine.stop_live()
            except Exception as exc:  # noqa: BLE001
                print(f"[app] stop live after BT drop: {exc}")

        with self._lock:
            self.state["bluetooth"]["connected_id"] = None
            self.state["bluetooth"]["connecting_id"] = None
            self.state["bluetooth"]["error"] = message
            if playing and live_mode == "cable":
                self.state["playing"] = False
                self.state["input_rms"] = 0.0
                self.state["live_mode"] = None
                self._live_started_at = None
            # Drop vanished device from list so UI doesn't keep showing it as connected.
            self._devices = [d for d in self._devices if d.get("id") != connected_id]

    def _refresh_media(self) -> None:
        """Pull SMTC metadata when Windows exposes it; else Live elapsed + device name."""
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")

        if is_demo:
            progress = self._engine.get_demo_progress()
            with self._lock:
                self.state["media"] = {
                    "ok": True,
                    "can_seek": True,
                    "can_prev": True,
                    "can_next": True,
                    "via": "demo",
                }
                duration = float(
                    progress.get("duration")
                    or self._demo_track.get("duration")
                    or 0.0
                )
                self.state["position"] = float(progress.get("position") or 0.0)
                self.state["track"] = {
                    "title": self._demo_track.get("title") or "Demo",
                    "artist": self._demo_track.get("artist") or "MUVI test track",
                    "album": self._demo_track.get("album") or "assets/demo.wav",
                    "duration": duration,
                    "artwork": self._demo_track.get("artwork") or "assets/artwork.svg",
                }
                if duration > 0:
                    self._demo_track["duration"] = duration
            return

        info = self._media.get_now_playing()
        with self._lock:
            connected_id = self.state["bluetooth"].get("connected_id")
            device_name = None
            if connected_id:
                for d in self._devices:
                    if d.get("id") == connected_id:
                        device_name = d.get("name")
                        break

            has_smtc = bool(info.get("ok") and (info.get("title") or info.get("duration")))
            self.state["media"] = {
                "ok": has_smtc,
                "can_seek": bool(info.get("can_seek")) and has_smtc,
                # Prev/next work via media keys even without song metadata.
                "can_prev": bool(connected_id) or bool(info.get("can_prev")),
                "can_next": bool(connected_id) or bool(info.get("can_next")),
                "via": info.get("via"),
            }

            if has_smtc:
                if info.get("title"):
                    self.state["track"]["title"] = info["title"]
                if info.get("artist"):
                    self.state["track"]["artist"] = info["artist"]
                if info.get("album"):
                    self.state["track"]["album"] = info["album"]
                self.state["track"]["duration"] = float(info.get("duration") or 0.0)
                self.state["position"] = float(info.get("position") or 0.0)
                return

            # No SMTC song metadata (typical for phone→PC Bluetooth).
            if self.state["playing"] and self._live_started_at is not None:
                elapsed = max(0.0, time.monotonic() - self._live_started_at)
                self.state["position"] = elapsed
                self.state["track"]["duration"] = 0.0  # unknown song length
                mode = self.state.get("live_mode")
                if mode == "aux":
                    self.state["track"]["title"] = "AUX Live"
                    self.state["track"]["artist"] = self.state.get("capture_name") or "Line-in"
                    self.state["track"]["album"] = "Song info not available over AUX"
                else:
                    self.state["track"]["title"] = "Bluetooth Live"
                    self.state["track"]["artist"] = device_name or "Phone"
                    self.state["track"]["album"] = (
                        "Song title not available from phone over Bluetooth"
                    )
            elif connected_id:
                self.state["position"] = 0.0
                self.state["track"]["duration"] = 0.0
                self.state["track"]["title"] = "Ready"
                self.state["track"]["artist"] = device_name or "Phone connected"
                self.state["track"]["album"] = "Play music on the phone, then press Play"
            else:
                self.state["track"]["title"] = self._default_track["title"]
                self.state["track"]["artist"] = self._default_track["artist"]
                self.state["track"]["album"] = self._default_track["album"]
                self.state["track"]["duration"] = 0.0
                self.state["position"] = 0.0

    def _snapshot(self) -> dict[str, Any]:
        snap = dict(self.state)
        snap["track"] = dict(self.state["track"])
        snap["bluetooth"] = dict(self.state["bluetooth"])
        snap["media"] = dict(self.state.get("media") or {})
        snap["zone_rms"] = dict(self.state.get("zone_rms") or {})
        snap["demo"] = dict(self.state.get("demo") or {})
        snap["devices"] = [dict(d) for d in self._devices]
        snap["nearby_devices"] = [dict(d) for d in self._nearby]
        return snap

    # -------------------------------------------------------- Start / Stop Live --
    def start_live(self) -> dict[str, Any]:
        """Same as old PyQt Start Live button."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            self.state["play_busy"] = True
            was_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")

        try:
            if was_demo:
                try:
                    self._engine.stop_live()
                except Exception:  # noqa: BLE001
                    pass

            if not self._engine.available:
                with self._lock:
                    msg = self._engine.last_error or "engine unavailable"
                    self.state["engine_error"] = msg
                    self.state["playing"] = False
                    self.state["play_busy"] = False
                    return self._snapshot()

            result = self._engine.start_live(
                volume=self.state["volume"],
                vibration=self.state["vibration"],
                cutoff_hz=self.state["cutoff_hz"],
                prefer_bluetooth=bool(self.state["bluetooth"].get("connected_id")),
            )
            with self._lock:
                self.state["playing"] = bool(result.get("ok"))
                self.state["capture_name"] = result.get("capture")
                self.state["output_name"] = result.get("output_name")
                self.state["live_mode"] = result.get("mode") if result.get("ok") else None
                self.state["engine_error"] = None if result.get("ok") else result.get("error")
                self.state["demo"]["error"] = None
                if result.get("ok"):
                    self._live_started_at = time.monotonic()
                else:
                    self._live_started_at = None
                    self.state["bluetooth"]["error"] = result.get("error")
                self.state["play_busy"] = False
                return self._snapshot()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state["playing"] = False
                self.state["engine_error"] = str(exc)
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()

    def stop_live(self) -> dict[str, Any]:
        """Same as old PyQt Stop Live button (also stops Demo)."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            self.state["play_busy"] = True
        try:
            self._engine.stop_live()
            with self._lock:
                self.state["playing"] = False
                self.state["input_rms"] = 0.0
                self.state["live_mode"] = None
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()
        except Exception:  # noqa: BLE001
            with self._lock:
                self.state["playing"] = False
                self.state["live_mode"] = None
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()

    def toggle_play(self) -> dict[str, Any]:
        """Now Playing Play = Start Live / Stop Live.

        If Demo is playing, Play starts Live (replaces demo) instead of only stopping.
        """
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            playing = self.state["playing"]
            is_demo = self.state.get("live_mode") == "demo"
        if playing and is_demo:
            return self.start_live()
        if playing:
            return self.stop_live()
        return self.start_live()

    def start_demo(self) -> dict[str, Any]:
        """Play assets/demo.wav through the same Gigaport vibration path."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            self.state["play_busy"] = True
            was_live = self.state.get("playing") and self.state.get("live_mode") != "demo"

        try:
            if was_live:
                try:
                    self._engine.stop_live()
                except Exception:  # noqa: BLE001
                    pass

            if not self._engine.available:
                with self._lock:
                    msg = self._engine.last_error or "engine unavailable"
                    self.state["engine_error"] = msg
                    self.state["demo"]["error"] = msg
                    self.state["playing"] = False
                    self.state["play_busy"] = False
                    return self._snapshot()

            result = self._engine.start_demo(
                volume=self.state["volume"],
                vibration=self.state["vibration"],
                cutoff_hz=self.state["cutoff_hz"],
            )
            with self._lock:
                ok = bool(result.get("ok"))
                self.state["playing"] = ok
                self.state["capture_name"] = result.get("capture")
                self.state["output_name"] = result.get("output_name")
                self.state["live_mode"] = "demo" if ok else None
                self.state["engine_error"] = None if ok else result.get("error")
                self.state["demo"]["error"] = None if ok else result.get("error")
                self.state["demo"]["available"] = True
                if ok:
                    self._live_started_at = time.monotonic()
                    if result.get("title"):
                        self._demo_track["title"] = result["title"]
                    if result.get("artist"):
                        self._demo_track["artist"] = result["artist"]
                    if result.get("album"):
                        self._demo_track["album"] = result["album"]
                    if result.get("duration"):
                        self._demo_track["duration"] = float(result["duration"])
                    self.state["track"] = dict(self._demo_track)
                    self.state["position"] = 0.0
                    self.state["media"] = {
                        "ok": True,
                        "can_seek": True,
                        "can_prev": True,
                        "can_next": True,
                        "via": "demo",
                    }
                else:
                    self._live_started_at = None
                self.state["play_busy"] = False
                return self._snapshot()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state["playing"] = False
                self.state["live_mode"] = None
                self.state["engine_error"] = str(exc)
                self.state["demo"]["error"] = str(exc)
                self.state["play_busy"] = False
                self._live_started_at = None
                return self._snapshot()

    def toggle_demo_play(self) -> dict[str, Any]:
        """Demo tab Play = start/stop demo.wav."""
        with self._lock:
            if self.state["play_busy"]:
                return self._snapshot()
            playing_demo = self.state["playing"] and self.state.get("live_mode") == "demo"
        if playing_demo:
            return self.stop_live()
        return self.start_demo()

    def seek(self, seconds: float) -> dict[str, Any]:
        """Seek demo.wav or phone track via Windows media session."""
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")
        if is_demo:
            progress = self._engine.seek_demo(float(seconds))
            with self._lock:
                self.state["position"] = float(progress.get("position") or seconds)
                if progress.get("duration"):
                    self.state["track"]["duration"] = float(progress["duration"])
            self._refresh_media()
            with self._lock:
                return self._snapshot()

        result = self._media.seek(float(seconds))
        if result.get("ok"):
            with self._lock:
                self.state["position"] = max(0.0, float(seconds))
        self._refresh_media()
        with self._lock:
            return self._snapshot()

    def previous_track(self) -> dict[str, Any]:
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")
        if is_demo:
            return self.seek(0.0)
        self._media.previous()
        self._refresh_media()
        with self._lock:
            return self._snapshot()

    def next_track(self) -> dict[str, Any]:
        with self._lock:
            is_demo = self.state.get("live_mode") == "demo" and self.state.get("playing")
        if is_demo:
            return self.seek(0.0)
        self._media.next()
        self._refresh_media()
        with self._lock:
            return self._snapshot()

    # ---------------------------------------------------------------- controls --
    def set_volume(self, value: float) -> dict[str, Any]:
        with self._lock:
            self.state["volume"] = max(0.0, min(1.0, float(value)))
        self._engine.set_volume(self.state["volume"])
        with self._lock:
            return self._snapshot()

    def set_vibration(self, value: float) -> dict[str, Any]:
        with self._lock:
            self.state["vibration"] = max(0.0, min(2.0, float(value)))
        self._engine.set_vibration(self.state["vibration"])
        with self._lock:
            return self._snapshot()

    def set_highpass_hz(self, value: float) -> dict[str, Any]:
        """UI Bass cutoff → vibration low-pass (muvi --cutoff)."""
        return self.set_cutoff_hz(value)

    def set_cutoff_hz(self, value: float) -> dict[str, Any]:
        with self._lock:
            hz = max(40.0, min(250.0, float(value)))
            self.state["cutoff_hz"] = hz
            self.state["highpass_hz"] = hz  # keep UI field in sync
        self._engine.set_cutoff_hz(hz)
        with self._lock:
            return self._snapshot()

    # --------------------------------------------------------------- bluetooth --
    def list_devices(self) -> list[dict[str, Any]]:
        if self._bt.available:
            devices = self._bt.list_devices()
            with self._lock:
                self._devices = devices
                return [dict(d) for d in self._devices]
        with self._lock:
            return []

    def scan_bluetooth(self) -> dict[str, Any]:
        """Scan = find nearby unpaired phones. Paired list refreshes automatically."""
        with self._lock:
            if (
                self.state["bluetooth"].get("scanning")
                or self.state["bluetooth"].get("discovering")
                or self.state["bluetooth"].get("pairing_id")
            ):
                return self._snapshot()
            self.state["bluetooth"]["scanning"] = True
            self.state["bluetooth"]["discovering"] = True
            self.state["bluetooth"]["error"] = None
            self._nearby = []
            snap = self._snapshot()

        def _on_nearby(devices: list[dict[str, Any]]) -> None:
            with self._lock:
                self._nearby = list(devices)

        def _work() -> None:
            nearby: list[dict[str, Any]] = []
            error: str | None = None
            try:
                if not self._bt.available:
                    error = self._bt.last_error or "Bluetooth unavailable"
                else:
                    # Keep paired list fresh while searching nearby.
                    try:
                        paired = self._bt.list_devices()
                        with self._lock:
                            connected_id = self.state["bluetooth"].get("connected_id")
                            by_id = {d["id"]: d for d in paired}
                            if connected_id:
                                for d in self._devices:
                                    if d.get("id") == connected_id and connected_id not in by_id:
                                        by_id[connected_id] = d
                            self._devices = list(by_id.values())
                    except Exception as exc:  # noqa: BLE001
                        print(f"[app] paired refresh during scan: {exc}")

                    nearby = self._bt.discover_nearby(progress_cb=_on_nearby)
                    if self._bt.last_error and not nearby:
                        error = self._bt.last_error
                    elif not nearby:
                        error = (
                            "No nearby phones found. Put the phone in pairing mode, "
                            "keep it close, then Scan again."
                        )
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] scan_bluetooth failed:", exc)
            finally:
                with self._lock:
                    self._nearby = nearby or self._bt.get_partial_nearby()
                    self.state["bluetooth"]["scanning"] = False
                    self.state["bluetooth"]["discovering"] = False
                    self.state["bluetooth"]["error"] = error
                    self._last_paired_refresh = time.monotonic()

        threading.Thread(target=_work, daemon=True, name="bt-scan").start()
        return snap

    def discover_nearby(self) -> dict[str, Any]:
        """Alias — UI Scan button uses scan_bluetooth for nearby discovery."""
        return self.scan_bluetooth()

    def pair_device(self, device_id: str) -> dict[str, Any]:
        """CustomPairing CONFIRM_ONLY for a nearby unpaired phone."""
        with self._lock:
            if self.state["bluetooth"].get("pairing_id") or self.state["bluetooth"].get("connecting_id"):
                return self._snapshot()
            self.state["bluetooth"]["pairing_id"] = device_id
            self.state["bluetooth"]["error"] = None
            snap = self._snapshot()

        def _work() -> None:
            error: str | None = None
            alert: str | None = None
            paired_ok = False
            try:
                if not self._bt.available:
                    error = self._bt.last_error or "Bluetooth unavailable"
                else:
                    result = self._bt.pair_device(device_id)
                    paired_ok = bool(result.get("ok"))
                    if not paired_ok:
                        error = result.get("error") or "Pairing wasn't confirmed, try again."
                        if result.get("alert") or str(result.get("status", "")).upper() in (
                            "FAILED",
                            "19",
                        ):
                            alert = error
                    else:
                        # Refresh APC list so Connect is available immediately.
                        devices = self._bt.list_devices()
                        with self._lock:
                            self._devices = devices
                            self._nearby = [d for d in self._nearby if d.get("id") != device_id]
                            self._last_paired_refresh = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] pair_device failed:", exc)
            finally:
                with self._lock:
                    self.state["bluetooth"]["pairing_id"] = None
                    self.state["bluetooth"]["error"] = error
                    self.state["bluetooth"]["alert"] = alert
                    if paired_ok and not error:
                        self.state["bluetooth"]["error"] = (
                            "Paired — tap Connect on the phone below."
                        )

        threading.Thread(target=_work, daemon=True, name="bt-pair").start()
        return snap

    def clear_bluetooth_alert(self) -> dict[str, Any]:
        with self._lock:
            self.state["bluetooth"]["alert"] = None
            return self._snapshot()

    def open_bluetooth_settings(self) -> dict[str, Any]:
        """Optional fallback: open Windows Bluetooth Settings."""
        if not self._bt.available:
            with self._lock:
                self.state["bluetooth"]["error"] = self._bt.last_error or "Bluetooth unavailable"
                return self._snapshot()
        result = self._bt.open_bluetooth_settings()
        with self._lock:
            if not result.get("ok"):
                self.state["bluetooth"]["error"] = result.get("error") or "Could not open Settings"
            else:
                self.state["bluetooth"]["error"] = (
                    "Opened Windows Bluetooth (fallback). Prefer Find nearby + Pair in the app."
                )
            return self._snapshot()

    def connect_device(self, device_id: str) -> dict[str, Any]:
        """Pair if needed, then open AudioPlaybackConnection. UI shows Connecting…."""
        with self._lock:
            if self.state["bluetooth"].get("connecting_id"):
                return self._snapshot()
            self.state["bluetooth"]["connecting_id"] = device_id
            self.state["bluetooth"]["error"] = None
            snap = self._snapshot()

        def _work() -> None:
            try:
                from windows_cable import ensure_cable_default_playback

                ensure_cable_default_playback()
            except Exception as exc:  # noqa: BLE001
                print(f"[app] cable default warn: {exc}")

            connected = False
            error: str | None = None
            resolved_id = device_id
            if self._bt.available:
                result = self._bt.connect(device_id)
                connected = bool(result.get("ok") or result.get("waiting"))
                resolved_id = str(result.get("device_id") or device_id)
                error = None if connected else (result.get("error") or "Connect failed")
            else:
                error = self._bt.last_error or "Bluetooth unavailable"

            with self._lock:
                self.state["bluetooth"]["connecting_id"] = None
                if connected:
                    self.state["bluetooth"]["connected_id"] = resolved_id
                    # Refresh list entry so the connected device shows as audio-ready.
                    for d in self._devices:
                        if d.get("id") in (device_id, resolved_id):
                            d["id"] = resolved_id
                            d["paired"] = True
                            d["audio_ready"] = True
                            d["profile"] = "Connected · Audio ready"
                self.state["bluetooth"]["error"] = error

        threading.Thread(target=_work, daemon=True, name="bt-connect").start()
        return snap

    def disconnect_device(self) -> dict[str, Any]:
        """Close audio link only — keeps Windows pairing."""
        self.stop_live()
        if self._bt.available:
            self._bt.disconnect(self.state["bluetooth"].get("connected_id"), forget=False)
        with self._lock:
            self.state["bluetooth"]["connected_id"] = None
            self.state["bluetooth"]["connecting_id"] = None
            self.state["bluetooth"]["error"] = None
            return self._snapshot()

    def forget_device(self, device_id: str | None = None) -> dict[str, Any]:
        """Disconnect if needed, then real Windows unpair for one device."""
        with self._lock:
            if self.state["bluetooth"].get("forgetting"):
                return self._snapshot()
            did = device_id or self.state["bluetooth"].get("connected_id")
            if not did:
                self.state["bluetooth"]["error"] = "No device to forget."
                return self._snapshot()
            self.state["bluetooth"]["forgetting"] = True
            self.state["bluetooth"]["error"] = None
            connected = self.state["bluetooth"].get("connected_id")
            snap = self._snapshot()

        if connected and connected == did:
            self.stop_live()

        def _work() -> None:
            error: str | None = None
            ok = False
            try:
                if self._bt.available:
                    result = self._bt.forget_device(did)
                    ok = bool(result.get("ok"))
                    if not ok:
                        error = result.get("error") or "Forget failed"
                else:
                    error = self._bt.last_error or "Bluetooth unavailable"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] forget_device failed:", exc)
            finally:
                with self._lock:
                    self.state["bluetooth"]["forgetting"] = False
                    if connected == did:
                        self.state["bluetooth"]["connected_id"] = None
                        self.state["bluetooth"]["connecting_id"] = None
                    self._devices = [d for d in self._devices if d.get("id") != did]
                    self._nearby = [d for d in self._nearby if d.get("id") != did]
                    self.state["bluetooth"]["error"] = (
                        error if not ok else "Device forgotten from Windows."
                    )
                    self._last_paired_refresh = 0.0
                self._schedule_paired_refresh(force=True)

        threading.Thread(target=_work, daemon=True, name="bt-forget").start()
        return snap

    def forget_all_devices(self) -> dict[str, Any]:
        """Real Windows unpair for all paired Bluetooth devices."""
        with self._lock:
            if self.state["bluetooth"].get("forgetting"):
                return self._snapshot()
            self.state["bluetooth"]["forgetting"] = True
            self.state["bluetooth"]["error"] = None
            snap = self._snapshot()

        self.stop_live()

        def _work() -> None:
            error: str | None = None
            msg = "All Bluetooth devices forgotten from Windows."
            try:
                if self._bt.available:
                    result = self._bt.forget_all()
                    n = int(result.get("forgotten") or 0)
                    total = int(result.get("total") or 0)
                    msg = f"Forgot {n} of {total} paired Bluetooth device(s) from Windows."
                    if result.get("errors"):
                        error = msg + " Some failed — check console."
                    else:
                        error = msg
                else:
                    error = self._bt.last_error or "Bluetooth unavailable"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                print("[app] forget_all failed:", exc)
            finally:
                with self._lock:
                    self.state["bluetooth"]["forgetting"] = False
                    self.state["bluetooth"]["connected_id"] = None
                    self.state["bluetooth"]["connecting_id"] = None
                    self._devices = []
                    self._nearby = []
                    self.state["bluetooth"]["error"] = error
                    self._last_paired_refresh = 0.0
                self._schedule_paired_refresh(force=True)

        threading.Thread(target=_work, daemon=True, name="bt-forget-all").start()
        return snap


def main() -> None:
    api = Api()
    window = webview.create_window(
        "muve",
        url=os.path.join(WEB_DIR, "index.html"),
        js_api=api,
        width=1920,
        height=1080,
        min_size=(1280, 720),
        fullscreen=FULLSCREEN,
        background_color="#0b0b0f",
    )

    def _on_closing() -> None:
        api._engine.stop_live()
        if api._bt.available:
            api._bt.disconnect(forget=False)

    window.events.closing += _on_closing
    webview.start()


if __name__ == "__main__":
    main()
"""Runs live_audio.py in a clean process (WinRT in UI process breaks ASIO).

stdout = JSON only (one object per line). All logs go to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

if sys.platform == "win32":
    os.environ.setdefault("SD_ENABLE_ASIO", "1")

# Keep a private handle for JSON replies; divert all other prints to stderr.
_JSON_OUT = sys.stdout
sys.stdout = sys.stderr

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from live_audio import (  # noqa: E402
    get_runtime_stats,
    is_active,
    set_cutoff_hz,
    set_vibration,
    set_volume,
    start_live_audio,
    stop_live_audio,
)


def _reply(obj: dict[str, Any]) -> None:
    _JSON_OUT.write(json.dumps(obj) + "\n")
    _JSON_OUT.flush()


def main() -> int:
    _reply({"ok": True, "event": "ready"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _reply({"ok": False, "error": f"bad json: {exc}"})
            continue

        cmd = str(msg.get("cmd", ""))
        try:
            if cmd == "start":
                v = float(msg.get("vibration", 0.27))
                result = start_live_audio(
                    volume=float(msg.get("volume", 0.70)),
                    mid=v,
                    legs=v,
                    upper=v,
                    head=v,
                    cutoff_hz=float(msg.get("cutoff_hz", msg.get("highpass_hz", 200.0))),
                    vibration_overlay=False,
                    prefer_bluetooth=bool(msg.get("prefer_bluetooth", False)),
                )
                print(
                    f"[engine_worker] START LIVE ok — capture={result.get('capture')!r} "
                    f"output={result.get('output_name')!r}"
                )
                _reply({**result, "event": "started"})

            elif cmd == "stop":
                stop_live_audio()
                print("[engine_worker] STOP LIVE")
                _reply({"ok": True, "event": "stopped"})

            elif cmd == "set_volume":
                set_volume(float(msg.get("value", 0.70)))
                _reply({"ok": True})

            elif cmd == "set_vibration":
                set_vibration(float(msg.get("value", 0.27)))
                _reply({"ok": True})

            elif cmd == "set_cutoff_hz" or cmd == "set_highpass_hz":
                set_cutoff_hz(float(msg.get("value", 200.0)))
                _reply({"ok": True})

            elif cmd == "stats":
                _reply({"ok": True, "stats": get_runtime_stats(), "running": is_active()})

            elif cmd == "quit":
                stop_live_audio()
                _reply({"ok": True, "event": "bye"})
                return 0

            else:
                _reply({"ok": False, "error": f"unknown cmd: {cmd}"})

        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            stop_live_audio()
            _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

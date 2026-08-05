"""Tablet battery level — Windows GetSystemPowerStatus; macOS pmset for dev preview."""

from __future__ import annotations

import ctypes
import platform
import re
import subprocess


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


_AC_LINE_ONLINE = 1
_BATTERY_FLAG_CHARGING = 8
_BATTERY_PERCENT_UNKNOWN = 255
_MAC_PREVIEW_PERCENT = 72
_MAC_PREVIEW_CHARGING = False


class BatteryManager:
    """Read tablet battery percentage on Windows; macOS supported for dev preview."""

    def __init__(self) -> None:
        self._system = platform.system()

    def is_available(self) -> bool:
        return self.get_battery_percent() is not None

    def _read_windows_power_status(self) -> _SYSTEM_POWER_STATUS | None:
        try:
            status = _SYSTEM_POWER_STATUS()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return None
            return status
        except Exception as exc:
            print(f"[battery] power status error: {exc}")
            return None

    def _read_macos_power(self) -> tuple[int, bool]:
        percent: int | None = None
        charging = False
        try:
            result = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
        except Exception as exc:
            print(f"[battery] pmset failed: {exc}")
            return _MAC_PREVIEW_PERCENT, _MAC_PREVIEW_CHARGING

        match = re.search(
            r"(\d{1,3})%;\s*(charging|discharging|charged|finishing charge)?",
            output,
            re.IGNORECASE,
        )
        if match:
            percent = max(0, min(100, int(match.group(1))))
            state = (match.group(2) or "").lower()
            charging = state in ("charging", "finishing charge")

        if percent is None:
            percent = _MAC_PREVIEW_PERCENT
            charging = _MAC_PREVIEW_CHARGING
        return percent, charging

    def get_battery_percent(self) -> int | None:
        if self._system == "Windows":
            status = self._read_windows_power_status()
            if status is None:
                return None
            percent = int(status.BatteryLifePercent)
            if percent == _BATTERY_PERCENT_UNKNOWN:
                return None
            percent = max(0, min(100, percent))
            if (
                percent == 0
                and status.ACLineStatus == _AC_LINE_ONLINE
                and status.BatteryFlag & (_BATTERY_FLAG_CHARGING | 1)
            ):
                return 100
            return percent

        if self._system == "Darwin":
            percent, _ = self._read_macos_power()
            return percent

        return None

    def is_charging(self) -> bool:
        if self._system == "Windows":
            status = self._read_windows_power_status()
            if status is None:
                return False
            return bool(status.BatteryFlag & _BATTERY_FLAG_CHARGING)

        if self._system == "Darwin":
            _, charging = self._read_macos_power()
            return charging

        return False

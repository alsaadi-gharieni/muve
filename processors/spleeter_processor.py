"""Spleeter wrapper for stem separation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class SpleeterError(RuntimeError):
    """Raised when spleeter command is unavailable or fails."""


def spleeter_is_available() -> bool:
    """Check whether spleeter executable can be run."""
    commands = (["spleeter", "--help"], [sys.executable, "-m", "spleeter", "--help"])
    for cmd in commands:
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
            if completed.returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False


def run_spleeter(input_file: str, output_root: str) -> Path:
    """Run spleeter and return folder containing output stems."""
    source = Path(input_file)
    out_root = Path(output_root)
    out_root.mkdir(parents=True, exist_ok=True)

    commands = (
        ["spleeter", "separate", "-p", "spleeter:4stems", "-o", str(out_root), str(source)],
        [sys.executable, "-m", "spleeter", "separate", "-p", "spleeter:4stems", "-o", str(out_root), str(source)],
    )
    last_error = ""
    completed = None
    for cmd in commands:
        try:
            completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if completed.returncode == 0:
                break
            last_error = completed.stderr.strip() or completed.stdout.strip()
        except FileNotFoundError:
            last_error = f"Command not found: {' '.join(cmd)}"

    if completed is None or completed.returncode != 0:
        raise SpleeterError(f"Spleeter failed: {last_error}")

    candidate_base = out_root / source.stem
    if candidate_base.exists():
        return candidate_base

    for root, _dirs, files in os.walk(out_root):
        if "vocals.wav" in files:
            return Path(root)

    raise SpleeterError("Spleeter completed but output stems were not found.")

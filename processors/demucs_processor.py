"""Demucs wrapper for stem separation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class DemucsError(RuntimeError):
    """Raised when demucs command is unavailable or fails."""


def demucs_is_available() -> bool:
    """Check whether demucs executable can be run."""
    commands = (["demucs", "--help"], [sys.executable, "-m", "demucs", "--help"])
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


def run_demucs(input_file: str, output_root: str) -> Path:
    """Run demucs and return folder containing output stems."""
    source = Path(input_file)
    out_root = Path(output_root)
    out_root.mkdir(parents=True, exist_ok=True)

    track_name = source.stem
    candidate_base = out_root / "htdemucs" / track_name
    if candidate_base.exists() and (candidate_base / "vocals.wav").exists():
        return candidate_base

    commands = (
        ["demucs", "-o", str(out_root), "-j", "2", str(source)],
        [sys.executable, "-m", "demucs", "-o", str(out_root), "-j", "2", str(source)],
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
        if "TorchCodec is required for save_with_torchcodec" in last_error or "No module named 'torchcodec'" in last_error:
            raise DemucsError(
                "Demucs export failed because torchcodec is missing. "
                "Install it in your active environment with: pip install torchcodec"
            )
        raise DemucsError(f"Demucs failed: {last_error}")

    # Typical path: out_root/htdemucs/<track_name>/
    if candidate_base.exists():
        return candidate_base

    # fallback: search for directory containing vocals.wav.
    for root, _dirs, files in os.walk(out_root):
        if "vocals.wav" in files:
            return Path(root)

    raise DemucsError("Demucs completed but output stems were not found.")

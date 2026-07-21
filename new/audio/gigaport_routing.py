"""Channel routing for single-Gigaport and dual-Gigaport layouts.

Dual layout (ASIO4ALL aggregates two Gigaport eX units):
  Gigaport 1 (ch 1-8) — vibration only, each zone duplicated to a stereo pair:
    1,2 → head    (was single-unit ch 3)
    3,4 → upper   (was ch 4)
    5,6 → legs    (was ch 5)
    7,8 → mid     (was ch 6)

  Gigaport 2 (ch 9-16) — audio only:
    9,10  → L/R headphones
    11,12 → L/R secondary speaker
    Switch between the two pairs at runtime (inactive pair is silent).
"""

from __future__ import annotations

from typing import Literal

import numpy as np

from audio.vibration_presets import SATORI_OUTPUT_CHANNELS

DUAL_GIGAPORT_CHANNELS = 16
AUDIO_GIGAPORT_OFFSET = 8
MIN_DUAL_CHANNELS = DUAL_GIGAPORT_CHANNELS

OutputLayout = Literal["single", "dual_asio4all", "dual_native"]
SpeakerRoute = Literal["headphones", "secondary"]


def is_asio4all_name(name: str) -> bool:
    return "asio4all" in name.lower()


def output_channels_for_layout(layout: OutputLayout) -> int:
    if layout == "dual_asio4all":
        return DUAL_GIGAPORT_CHANNELS
    if layout == "dual_native":
        return 8
    return SATORI_OUTPUT_CHANNELS


def build_output_block(
    audio: np.ndarray,
    vib: np.ndarray,
    *,
    layout: OutputLayout = "single",
    speaker_route: SpeakerRoute = "headphones",
) -> np.ndarray:
    """Assemble a multichannel output block from stereo audio and 4-zone vibration.

    audio: (n, 2) left/right at output sample rate
    vib:   (n, 4) head, upper, legs, mid
    """
    n = min(len(audio), len(vib))
    if n <= 0:
        ch = output_channels_for_layout(layout)
        return np.zeros((0, ch), dtype=np.float32)

    audio = np.clip(audio[:n], -1.0, 1.0).astype(np.float32, copy=False)
    vib = np.clip(vib[:n], -1.0, 1.0).astype(np.float32, copy=False)

    if layout == "dual_asio4all":
        out = np.zeros((n, DUAL_GIGAPORT_CHANNELS), dtype=np.float32)
        head, upper, legs, mid = vib[:, 0], vib[:, 1], vib[:, 2], vib[:, 3]
        out[:, 0] = head
        out[:, 1] = head
        out[:, 2] = upper
        out[:, 3] = upper
        out[:, 4] = legs
        out[:, 5] = legs
        out[:, 6] = mid
        out[:, 7] = mid
        if speaker_route == "secondary":
            out[:, 10:12] = audio
        else:
            out[:, 8:10] = audio
        return out

    out = np.zeros((n, SATORI_OUTPUT_CHANNELS), dtype=np.float32)
    out[:, 0:2] = audio
    out[:, 2:6] = vib
    return out

"""Standalone Gigaport output test — bypasses the whole MUVI pipeline.

It writes a raw sine wave straight to a device via sounddevice, so we can tell
whether a "no signal" problem is in MUVI (data feeding) or in the Windows
driver / endpoint / format itself.

Usage (from the `new/` folder):

    python test_tone.py                 # list devices, then play to auto-picked vibration Gigaport
    python test_tone.py --list          # only list usable output devices and exit
    python test_tone.py --device 58     # play to a specific device index
    python test_tone.py --device 58 --channel 0   # play ONLY on channel 0 (find which pair is live)
    python test_tone.py --seconds 8 --freq 220    # longer / lower tone

If you HEAR the tone, MUVI's audio path is fine and the issue is data feeding.
If it's SILENT, it's the device/driver/format — no MUVI code change fixes that.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np
import sounddevice as sd

from audio.output_devices import (
    list_output_devices,
    pick_output_devices,
    probe_output_channels,
    wasapi_output_extra,
)


def _print_devices() -> None:
    print("--- usable output devices ---")
    devs = list_output_devices()
    if not devs:
        print("  (none — is the Gigaport connected and its driver installed?)")
    for d in devs:
        tag = "  <-- Gigaport" if d.get("is_gigaport") else ""
        print(f"  #{d['index']:<3} {d['channels']}ch  [{d['hostapi']}]  {d['name']}{tag}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Play a test tone to an output device.")
    ap.add_argument("--device", type=int, default=None, help="device index (default: auto-pick vibration Gigaport)")
    ap.add_argument("--channel", type=int, default=None, help="play only on this channel index (default: all)")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--amp", type=float, default=0.3, help="amplitude 0..1")
    ap.add_argument("--list", action="store_true", help="list devices and exit")
    args = ap.parse_args()

    _print_devices()
    if args.list:
        return 0

    device_index = args.device
    if device_index is None:
        try:
            vib, _audio, _layout, _meta = pick_output_devices()
            device_index = int(vib["index"])
            print(f"auto-picked vibration device: #{device_index} {vib['name']}")
        except Exception as exc:  # noqa: BLE001
            print(f"could not auto-pick a Gigaport: {exc}")
            print("pass --device <index> from the list above.")
            return 2

    info = sd.query_devices(device_index)
    max_ch = int(info["max_output_channels"])
    ch = probe_output_channels(device_index, args.rate, (max_ch, 8, 6, 4, 2)) or max_ch
    ch = max(1, min(ch, max_ch))
    extra = wasapi_output_extra(device_index)

    print(
        f"\nplaying {args.freq:.0f} Hz for {args.seconds:.0f}s -> "
        f"device #{device_index} '{info['name']}' | {ch} ch @ {args.rate} Hz"
        + (f" | ONLY channel {args.channel}" if args.channel is not None else " | ALL channels")
    )

    n = int(args.seconds * args.rate)
    t = np.arange(n, dtype=np.float32) / args.rate
    tone = (args.amp * np.sin(2 * np.pi * args.freq * t)).astype(np.float32)
    block = np.zeros((n, ch), dtype=np.float32)
    if args.channel is None:
        for c in range(ch):
            block[:, c] = tone
    else:
        if not (0 <= args.channel < ch):
            print(f"channel {args.channel} out of range 0..{ch - 1}")
            return 2
        block[:, args.channel] = tone

    kwargs: dict = {"samplerate": args.rate, "device": device_index, "channels": ch, "dtype": "float32"}
    if extra is not None:
        kwargs["extra_settings"] = extra

    try:
        sd.play(block, **kwargs)
        sd.wait()
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED to open/play: {exc}")
        return 1

    time.sleep(0.2)
    print("done. Did you hear it?")
    return 0


if __name__ == "__main__":
    sys.exit(main())

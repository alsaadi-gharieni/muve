#!/usr/bin/env python3
"""
muvi_player - Aurasens-style stereo-to-tactile player for the ESI Gigaport eX.

What it does (mirrors the Aurasens "SmartLinear4D" behaviour, real-time path):
  * Loads an audio file (MP3/WAV/FLAC/...).
  * Plays the audible stereo mix to the headphones on outputs 1-2.
  * Derives a LOW-FREQUENCY "vibration" feed - a low-pass / band-limited copy of
    the audio, i.e. only the tactile bass band that transducers can reproduce.
  * Preserves stereo the way Aurasens does with its odd/even split:
        Left  -> head  (ch3) and knees (ch5)
        Right -> back  (ch4) and feet  (ch6)
  * Applies per-zone gains, a master vibe gain, a peak limiter, and an
    audio<->vibration timing offset, then streams channels 1-6 to the Gigaport eX.

Usage:
    python muvi_player.py song.mp3
    python muvi_player.py song.mp3 --cutoff 180 --delay-ms -20
    python muvi_player.py --list-devices

Tweak the CONFIG block below for gains, cutoff, device name, etc.
"""

import argparse
import sys
import numpy as np

try:
    from scipy.signal import butter, sosfilt
except ImportError:
    sys.exit("Missing scipy. Install deps with:  pip install -r requirements.txt")

# =============================== CONFIG ====================================
CONFIG = {
    # ---- Output device -------------------------------------------------
    # Substring matched (case-insensitive) against the device name.
    # None = system default output. Override on the CLI with --device.
    "device_name": "Gigaport",
    "output_channels": 6,          # Gigaport eX: fill outputs 1-6, leave 7-8 silent

    # ---- Physical channel map (frame column -> Gigaport output) --------
    #   col 0 -> ch1  Left  headphone      (audible L)
    #   col 1 -> ch2  Right headphone      (audible R)
    #   col 2 -> ch3  head    vibration    (from Left)
    #   col 3 -> ch4  back    vibration    (from Right)
    #   col 4 -> ch5  knees   vibration    (from Left)
    #   col 5 -> ch6  feet    vibration    (from Right)

    # ---- Tactile band (Hz): what actually reaches the transducers ------
    "vibe_lowpass_hz": 200.0,      # SmartLinear4D low-pass cutoff
    "vibe_highpass_hz": 30.0,      # remove subsonic/DC rumble (0 to disable)
    "filter_order": 4,

    # ---- Levels --------------------------------------------------------
    "headphone_gain": 1.0,
    "master_vibe_gain": 1.0,
    "zone_gain": {"head": 1.0, "back": 1.0, "knees": 1.0, "feet": 1.0},

    # ---- Peak limiter on the vibration channels (dBFS ceiling) ---------
    "vibe_limit_db": -1.0,         # None disables

    # ---- Audio/vibration alignment ------------------------------------
    # Positive = delay the AUDIO; negative = delay the VIBRATION.
    "audio_vibe_delay_ms": 0.0,
}
# ==========================================================================

# Which stereo side feeds each zone, and the output-column order.
# (col2..col5 -> head, back, knees, feet)
ZONE_ORDER = [
    ("head",  "L"),
    ("back",  "R"),
    ("knees", "L"),
    ("feet",  "R"),
]


def load_audio(path):
    """Return (float32 array shape (n, 2), samplerate). Forces stereo."""
    data = None
    sr = None
    try:
        import soundfile as sf
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception as e_sf:
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_file(path)
            sr = seg.frame_rate
            arr = np.array(seg.get_array_of_samples()).astype(np.float32)
            arr /= float(1 << (8 * seg.sample_width - 1))
            data = arr.reshape((-1, seg.channels))
        except Exception as e_pd:
            sys.exit(
                f"Could not decode '{path}'.\n"
                f"  soundfile: {e_sf}\n"
                f"  pydub:     {e_pd}\n"
                "For MP3 support install a recent 'soundfile' (libsndfile >= 1.1), "
                "or 'pip install pydub' plus the ffmpeg binary."
            )
    if data.ndim == 1:
        data = data[:, None]
    if data.shape[1] == 1:            # mono -> dual mono
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:           # take first two channels
        data = data[:, :2]
    return data.astype(np.float64), sr


def make_sos(sr, lo, hi, order):
    """Build a cascaded SOS band-limit filter (high-pass lo, low-pass hi)."""
    nyq = sr / 2.0
    sections = []
    if hi and hi < nyq:
        sections.append(butter(order, hi / nyq, btype="low", output="sos"))
    if lo and lo > 0:
        sections.append(butter(order, lo / nyq, btype="high", output="sos"))
    if not sections:
        return None
    return np.vstack(sections)


def band_limit(x, sos):
    return x if sos is None else sosfilt(sos, x)


def render(data, sr, cfg):
    """Turn stereo (n,2) into the multichannel (n, output_channels) output frame."""
    L = data[:, 0]
    R = data[:, 1]

    sos = make_sos(sr, cfg["vibe_highpass_hz"], cfg["vibe_lowpass_hz"], cfg["filter_order"])
    vibe = {"L": band_limit(L, sos), "R": band_limit(R, sos)}

    mv = cfg["master_vibe_gain"]
    zones = [vibe[side] * cfg["zone_gain"][name] * mv for name, side in ZONE_ORDER]

    # Peak limiter on tactile channels.
    if cfg["vibe_limit_db"] is not None:
        ceil = 10.0 ** (cfg["vibe_limit_db"] / 20.0)
        zones = [np.clip(z, -ceil, ceil) for z in zones]

    audible = [L * cfg["headphone_gain"], R * cfg["headphone_gain"]]

    # Audio/vibration timing offset.
    delay = int(round(abs(cfg["audio_vibe_delay_ms"]) * sr / 1000.0))
    if delay > 0:
        pad = np.zeros(delay)
        if cfg["audio_vibe_delay_ms"] > 0:        # delay audio
            audible = [np.concatenate([pad, a]) for a in audible]
            zones = [np.concatenate([z, pad]) for z in zones]
        else:                                     # delay vibration
            audible = [np.concatenate([a, pad]) for a in audible]
            zones = [np.concatenate([pad, z]) for z in zones]

    cols = audible + zones                        # [L, R, head, back, knees, feet]
    n = max(len(c) for c in cols)
    out = np.zeros((n, cfg["output_channels"]), dtype=np.float32)
    for i, c in enumerate(cols[: cfg["output_channels"]]):
        out[: len(c), i] = c
    np.clip(out, -1.0, 1.0, out=out)
    return out


def find_device(name):
    import sounddevice as sd
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_output_channels"] >= 1:
            return i
    return None


def main():
    ap = argparse.ArgumentParser(description="Aurasens-style tactile player for the Gigaport eX")
    ap.add_argument("audio", nargs="?", help="audio file to play (mp3/wav/flac/...)")
    ap.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    ap.add_argument("--device", help="output device name substring or numeric index")
    ap.add_argument("--cutoff", type=float, help="override vibration low-pass cutoff (Hz)")
    ap.add_argument("--delay-ms", type=float, help="audio<->vibration offset (ms; +audio/-vibe)")
    args = ap.parse_args()

    try:
        import sounddevice as sd
    except Exception as e:
        sys.exit(f"Missing/broken sounddevice ({e}). Install PortAudio + `pip install sounddevice`.")

    if args.list_devices:
        print(sd.query_devices())
        return
    if not args.audio:
        ap.error("provide an audio file, or use --list-devices")

    cfg = dict(CONFIG)
    cfg["zone_gain"] = dict(CONFIG["zone_gain"])
    if args.cutoff:
        cfg["vibe_lowpass_hz"] = args.cutoff
    if args.delay_ms is not None:
        cfg["audio_vibe_delay_ms"] = args.delay_ms

    dev = args.device if args.device is not None else cfg["device_name"]
    try:
        dev_idx = int(dev)
    except (TypeError, ValueError):
        dev_idx = find_device(dev)
    if dev_idx is None:
        print(f"[warn] device '{dev}' not found - using system default output.")

    if dev_idx is not None:
        info = sd.query_devices(dev_idx)
        if info["max_output_channels"] < cfg["output_channels"]:
            sys.exit(
                f"Device '{info['name']}' has {info['max_output_channels']} outputs, "
                f"need {cfg['output_channels']}. Is the Gigaport eX selected?"
            )

    data, sr = load_audio(args.audio)
    out = render(data, sr, cfg)
    dur = out.shape[0] / sr
    print(
        f"Playing {args.audio}\n"
        f"  {sr} Hz, {dur:0.1f}s, {cfg['output_channels']} ch -> device index {dev_idx}\n"
        f"  vibe band {cfg['vibe_highpass_hz']:.0f}-{cfg['vibe_lowpass_hz']:.0f} Hz | "
        f"delay {cfg['audio_vibe_delay_ms']:+.0f} ms\n"
        f"  ch1/2 headphones | ch3 head | ch4 back | ch5 knees | ch6 feet"
    )
    sd.play(out, sr, device=dev_idx)
    sd.wait()


if __name__ == "__main__":
    main()

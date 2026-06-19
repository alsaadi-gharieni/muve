# Satori Vibro Test

PyQt5 desktop app for Raspberry Pi 4 and Windows that mirrors **Satori 6-channel** output (L, R, C, LFE, Ls, Rs) through Gigaport eX at **44.1 kHz**.

## Features

- Loads `.wav` and `.mp3` audio files
- **Quick preview** then **Demucs** stem separation in background
- L/R play the **original** file; vibration uses bass/drums/other (no vocals)
- **Satori body zones** (default frequency profile):
  - **LFE** — Legs (30–40 Hz)
  - **C** — Mid (50–68 Hz)
  - **Ls** — Upper mid (80–100 Hz)
  - **Rs** — Head (100–150 Hz)
- Segmentation and frequency presets for music vs healing content
- Vibration intensity slider (C, LFE, Ls, Rs only)
- Optional synthetic vibration mode

## Channel Mapping (Satori 5.1)

| Index | Channel | Role |
|-------|---------|------|
| 0 | L | Original audio (left) |
| 1 | R | Original audio (right) |
| 2 | C | Mid body vibration |
| 3 | LFE | Legs vibration |
| 4 | Ls | Upper mid vibration |
| 5 | Rs | Head vibration |

## Run

```bash
python main.py
```

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install demucs torchcodec
```

Verify 6+ output channels:

```bash
python -c "import sounddevice as sd; print([d for d in sd.query_devices() if d['max_output_channels'] >= 6])"
```

## Notes

- Stem separation runs in background after instant quick preview.
- Files longer than 20 minutes are trimmed for memory stability.
- Demucs results are cached — reloading the same file is faster.

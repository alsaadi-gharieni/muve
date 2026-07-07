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
```

`requirements.txt` includes **Demucs** (primary separator), **torchcodec** (Demucs WAV export), and **Spleeter** (fallback if Demucs fails).

Verify 6+ output channels (Gigaport needs ASIO on Windows):

```bash
# Windows: restart terminal after setting ASIO, then:
python main.py
# Or list devices:
python -c "import os; os.environ['SD_ENABLE_ASIO']='1'; import sounddevice as sd; [print(i,d) for i,d in enumerate(sd.query_devices()) if d['max_output_channels']>=6 or 'gigaport' in d['name'].lower()]"
```

**Gigaport not in the list?**
1. Connect Gigaport eX via USB
2. Install the **Gigaport ASIO driver** from [ESI](https://www.esi-audio.com)
3. **Restart the app** (ASIO is enabled automatically on Windows at startup)
4. Click **Refresh Devices** — select the entry marked **★ Gigaport** with **ASIO** and **6 ch**

## Notes

- Stem separation runs in background after instant quick preview.
- Files longer than 20 minutes are trimmed for memory stability.
- Demucs results are cached — reloading the same file is faster.

## Bluetooth / Live Audio (Windows Tablet)

Play audio from your phone through the Satori bed:

1. Pair the phone to the tablet in **Windows Bluetooth** settings.
2. On the phone: enable **Media audio / Stereo** for the tablet (disable Calls/Hands-Free).
3. Play music on the phone — audio should play through the tablet speaker/Bluetooth output.
4. On the tablet run: `pip install soundcard` (once).
5. In the app: **Refresh** → pick a **★ [Speaker Loopback]** entry for the speaker that plays phone audio.
6. Select **Gigaport** as the 6-channel output → **Start Live**.

Hands-Free Bluetooth cannot capture music on Windows — it is hidden from the device list.

**Tips**

- ★ entries use speaker loopback (capture what you hear).
- Gigaport (ASIO) and Bluetooth use separate streams — they work together.
- Volume and zone sliders apply to live audio.

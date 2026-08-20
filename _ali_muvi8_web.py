#!/usr/bin/env python3
"""
muvi 8 channels - browser UI + real-time engine.

The ESI Gigaport eX has 8 outputs. This build uses ALL 8 as tactile/vibration
channels, and plays the audible stereo through a SEPARATE output device (your
Mac's headphones / built-in output), kept in sync.

Vibration routing on the Gigaport (8 seat zones):
    Left  side -> outputs 1, 3, 5, 7  (odd)
    Right side -> outputs 2, 4, 6, 8  (even)
All 8 carry the same low-frequency tactile band, scaled by Intensity.

Controls:
    Volume       -> audio device (headphones) only
    Intensity    -> all 8 vibration channels
    Remove highs -> low-pass on the 8 vibration channels only

Run:  python3 muvi8_web.py
"""

import json
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

try:
    from scipy.signal import butter, sosfilt
except ImportError:
    sys.exit("Missing scipy. Run: pip3 install -r requirements.txt")

try:
    import sounddevice as sd
except Exception as e:
    sys.exit(f"sounddevice unavailable ({e}). pip3 install -r requirements.txt")

try:
    import muvi_player  # optional reuse of the desktop decoder, if present
    _load_audio = muvi_player.load_audio
except Exception:
    _load_audio = None

PORT = 8766
VIB_CHANNELS = 8            # all 8 Gigaport outputs are vibration
VIBE_HIGHPASS_HZ = 30.0
REMOVE_HI_MAX_HZ = 500.0    # 0%
REMOVE_HI_MIN_HZ = 40.0     # 100%
FILTER_ORDER = 4
BLOCK = 1024

# Output-column -> stereo side for the 8 vibration channels (odd=L, even=R).
VIB_SIDE = ["L", "R", "L", "R", "L", "R", "L", "R"]


def load_audio(path):
    """Return (float32 (n,2), sr). mp3/wav/flac. Forces stereo."""
    if _load_audio is not None:
        return _load_audio(path)
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
            raise RuntimeError(f"decode failed: soundfile={e_sf}; pydub={e_pd}")
    if data.ndim == 1:
        data = data[:, None]
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    return data.astype(np.float32), sr


class Player:
    """Two synchronized output streams sharing one decoded buffer:
       - vibration stream (8ch) to the Gigaport  (master clock)
       - audio stream (2ch) to the headphone device"""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.sr = 48000
        self.n = 0
        self.vib_pos = 0
        self.aud_pos = 0
        self.vib_stream = None
        self.aud_stream = None
        self.vib_device = None      # Gigaport (>=8 out)
        self.aud_device = None      # headphones (>=2 out); None = system default
        self.volume = 1.0
        self.intensity = 1.0
        self.remove_pct = 50.0
        self.playing = False
        self.paused = False
        self.filename = ""
        self.last_error = ""
        self._cur_cut = None
        self.sos = None
        self.zi_l = None
        self.zi_r = None

    # ---- filter ----
    def _cutoff(self):
        p = max(0.0, min(100.0, self.remove_pct))
        return REMOVE_HI_MAX_HZ * (REMOVE_HI_MIN_HZ / REMOVE_HI_MAX_HZ) ** (p / 100.0)

    def _ensure_sos(self):
        cut = self._cutoff()
        if self._cur_cut is None or abs(cut - self._cur_cut) > 0.5:
            nyq = self.sr / 2.0
            secs = [butter(FILTER_ORDER, min(cut, nyq * 0.99) / nyq, btype="low", output="sos")]
            if VIBE_HIGHPASS_HZ > 0:
                secs.append(butter(FILTER_ORDER, VIBE_HIGHPASS_HZ / nyq, btype="high", output="sos"))
            self.sos = np.vstack(secs)
            self._cur_cut = cut
            if self.zi_l is None or self.zi_l.shape[0] != self.sos.shape[0]:
                self.zi_l = np.zeros((self.sos.shape[0], 2))
                self.zi_r = np.zeros((self.sos.shape[0], 2))

    # ---- file / streams ----
    def load(self, path):
        data, sr = load_audio(path)
        with self.lock:
            self.data = data.astype(np.float32)
            self.sr = sr
            self.n = len(data)
            self.vib_pos = self.aud_pos = 0
            self._cur_cut = None
            self.zi_l = self.zi_r = None
            self._ensure_sos()
            self.filename = path.split("/")[-1]
            self.playing = self.paused = False
        self._close_streams()

    def _close_streams(self):
        for name in ("vib_stream", "aud_stream"):
            s = getattr(self, name)
            if s is not None:
                try:
                    s.stop(); s.close()
                except Exception:
                    pass
                setattr(self, name, None)

    def _open_streams(self):
        info = sd.query_devices(self.vib_device) if self.vib_device is not None else None
        if info is None or info["max_output_channels"] < VIB_CHANNELS:
            raise RuntimeError(
                f"Vibration device needs {VIB_CHANNELS} outputs; "
                f"{'none selected' if info is None else info['name']+' has '+str(info['max_output_channels'])}."
            )
        self.vib_stream = sd.OutputStream(
            samplerate=self.sr, channels=VIB_CHANNELS, device=self.vib_device,
            dtype="float32", blocksize=BLOCK, callback=self._vib_cb)
        self.aud_stream = sd.OutputStream(
            samplerate=self.sr, channels=2, device=self.aud_device,
            dtype="float32", blocksize=BLOCK, callback=self._aud_cb)
        self.vib_stream.start()
        self.aud_stream.start()

    def set_devices(self, vib=None, aud=None):
        with self.lock:
            if vib is not None:
                self.vib_device = vib
            if aud is not None:
                self.aud_device = aud
            self.playing = self.paused = False
        self._close_streams()

    # ---- transport ----
    def play(self):
        self.last_error = ""
        try:
            if self.data is None:
                self.last_error = "Load a song first."
                return
            if self.vib_stream is None:
                self._open_streams()
            with self.lock:
                if self.vib_pos >= self.n:
                    self.vib_pos = self.aud_pos = 0
                self.playing = True
                self.paused = False
        except Exception as e:
            self.last_error = str(e)
            self._close_streams()

    def pause(self):
        with self.lock:
            self.paused = True

    def stop(self):
        with self.lock:
            self.playing = False
            self.paused = False
            self.vib_pos = self.aud_pos = 0

    # ---- callbacks ----
    def _vib_cb(self, outdata, frames, t, status):
        with self.lock:
            if self.data is None or not self.playing or self.paused:
                outdata.fill(0.0)
                return
            self._ensure_sos()
            block = self.data[self.vib_pos:self.vib_pos + frames]
            if len(block) < frames:
                block = np.vstack([block, np.zeros((frames - len(block), 2), np.float32)])
            L = block[:, 0].astype(np.float64)
            R = block[:, 1].astype(np.float64)
            vibe_l, self.zi_l = sosfilt(self.sos, L, zi=self.zi_l)
            vibe_r, self.zi_r = sosfilt(self.sos, R, zi=self.zi_r)
            g = self.intensity
            for c in range(VIB_CHANNELS):
                src = vibe_l if VIB_SIDE[c] == "L" else vibe_r
                outdata[:, c] = (src * g).astype(np.float32)
            np.clip(outdata, -1.0, 1.0, out=outdata)
            self.vib_pos += frames
            if self.vib_pos >= self.n:
                self.playing = False
                self.vib_pos = self.n

    def _aud_cb(self, outdata, frames, t, status):
        with self.lock:
            if self.data is None or not self.playing or self.paused:
                outdata.fill(0.0)
                return
            block = self.data[self.aud_pos:self.aud_pos + frames]
            if len(block) < frames:
                block = np.vstack([block, np.zeros((frames - len(block), 2), np.float32)])
            outdata[:, 0] = block[:, 0] * self.volume
            outdata[:, 1] = block[:, 1] * self.volume
            np.clip(outdata, -1.0, 1.0, out=outdata)
            self.aud_pos += frames

    def status(self):
        with self.lock:
            return {
                "filename": self.filename,
                "playing": self.playing and not self.paused,
                "paused": self.paused,
                "pos": round(self.vib_pos / self.sr, 1) if self.sr else 0,
                "dur": round(self.n / self.sr, 1) if self.sr else 0,
                "volume": round(self.volume, 3),
                "intensity": round(self.intensity, 3),
                "remove_pct": round(self.remove_pct, 1),
                "cutoff_hz": round(self._cutoff()),
                "vib_device": self.vib_device,
                "aud_device": self.aud_device,
                "error": self.last_error,
            }


PLAYER = Player()


def out_devices():
    return [{"idx": i, "name": d["name"], "ch": d["max_output_channels"]}
            for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] >= 1]


def native_pick_file():
    r = subprocess.run(
        ["osascript", "-e", 'POSIX path of (choose file with prompt "Choose an audio file")'],
        capture_output=True, text=True)
    return r.stdout.strip() or None


HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>muvi 8</title><style>
:root{color-scheme:dark}
body{margin:0;background:#111;color:#eee;font:15px/1.4 -apple-system,Segoe UI,sans-serif}
.wrap{max-width:580px;margin:24px auto;padding:0 18px}
h1{font-size:20px;font-weight:600;margin:0 0 4px}.tag{color:#8a8a8e;font-size:12px;margin:0 0 16px}
.card{background:#1c1c1e;border:1px solid #2c2c2e;border-radius:12px;padding:16px;margin-bottom:14px}
label{display:block;font-size:12px;color:#9a9a9e;margin:0 0 4px;text-transform:uppercase;letter-spacing:.04em}
select,button{font:inherit;color:#eee;background:#2c2c2e;border:1px solid #3a3a3c;border-radius:8px;padding:8px 12px}
button{cursor:pointer}button:hover{background:#3a3a3c}
button.primary{background:#0a84ff;border-color:#0a84ff}button.primary:hover{background:#3a9dff}
.row{display:flex;gap:10px;align-items:center;margin-bottom:8px}
.row>*{flex:0 0 auto}select{flex:1 1 auto;min-width:0}
input[type=range]{width:100%;accent-color:#0a84ff}
.val{float:right;color:#0a84ff;font-variant-numeric:tabular-nums}
.now{font-size:14px;color:#ddd}.sub{font-size:12px;color:#8a8a8e}
.bar{height:5px;background:#2c2c2e;border-radius:3px;overflow:hidden;margin-top:8px}
.bar>div{height:100%;width:0;background:#0a84ff;transition:width .3s linear}
.err{color:#ff6961;font-size:12px;min-height:14px;margin-top:6px}
.map{font-size:11px;color:#6a6a6e;margin-top:8px}
</style></head><body><div class="wrap">
<h1>muvi · 8 channels</h1>
<p class="tag">8 vibration outputs on the Gigaport · audio on a separate device</p>

<div class="card">
  <label>Vibration device (Gigaport, needs 8 out)</label>
  <div class="row"><select id="vib"></select></div>
  <label style="margin-top:6px">Audio device (headphones)</label>
  <div class="row"><select id="aud"></select><button onclick="loadFile()">Load…</button></div>
  <div class="map">Ch1/3/5/7 ← Left · Ch2/4/6/8 ← Right · all 8 = tactile band</div>
</div>

<div class="card">
  <div class="now" id="now">No song loaded</div>
  <div class="sub" id="time">0:00 / 0:00</div>
  <div class="bar"><div id="prog"></div></div>
  <div class="row" style="margin-top:12px">
    <button class="primary" onclick="cmd('play')">▶ Play</button>
    <button onclick="cmd('pause')">❚❚ Pause</button>
    <button onclick="cmd('stop')">■ Stop</button>
  </div>
  <div class="err" id="err"></div>
</div>

<div class="card">
  <label>Volume <span class="val" id="volv"></span><span class="sub"> · audio only</span></label>
  <input type="range" id="vol" min="0" max="150" value="100" oninput="setp()">
  <label style="margin-top:14px">Intensity <span class="val" id="intv"></span><span class="sub"> · all 8 vibration ch</span></label>
  <input type="range" id="int" min="0" max="200" value="100" oninput="setp()">
  <label style="margin-top:14px">Remove highs <span class="val" id="remv"></span><span class="sub"> · vibration low-pass</span></label>
  <input type="range" id="rem" min="0" max="100" value="50" oninput="setp()">
</div>
</div>
<script>
function fmt(s){s=Math.max(0,Math.round(s));return Math.floor(s/60)+":"+String(s%60).padStart(2,'0')}
async function j(u){const r=await fetch(u);return r.json()}
async function loadDevices(){
  const d=await j('/devices');
  const vib=document.getElementById('vib'), aud=document.getElementById('aud');
  vib.innerHTML='';aud.innerHTML='';
  d.devices.forEach(x=>{
    const label=`[${x.idx}] ${x.name} (${x.ch} out)`;
    const o1=document.createElement('option');o1.value=x.idx;o1.textContent=label;
    if(/gigaport/i.test(x.name)&&x.ch>=8)o1.selected=true;vib.appendChild(o1);
    const o2=document.createElement('option');o2.value=x.idx;o2.textContent=label;
    if(/built-?in|headphone|speaker|default/i.test(x.name)&&x.ch>=2&&x.ch<8)o2.selected=true;aud.appendChild(o2);
  });
  setDevices();
}
async function setDevices(){
  await fetch(`/setdevices?vib=${document.getElementById('vib').value}&aud=${document.getElementById('aud').value}`);
}
document.getElementById('vib').addEventListener('change',setDevices);
document.getElementById('aud').addEventListener('change',setDevices);
async function loadFile(){document.getElementById('now').textContent='Opening file picker…';await fetch('/load');poll()}
async function cmd(c){await fetch('/'+c);poll()}
let t=null;
function setp(){
  const v=document.getElementById('vol').value,i=document.getElementById('int').value,r=document.getElementById('rem').value;
  document.getElementById('volv').textContent=v+'%';
  document.getElementById('intv').textContent=i+'%';
  document.getElementById('remv').textContent=r+'%';
  clearTimeout(t);t=setTimeout(()=>fetch(`/set?volume=${v/100}&intensity=${i/100}&remove=${r}`),60);
}
async function poll(){
  const s=await j('/status');
  document.getElementById('now').textContent=s.filename?((s.playing?'▶ ':(s.paused?'❚❚ ':''))+s.filename):'No song loaded';
  document.getElementById('time').textContent=fmt(s.pos)+' / '+fmt(s.dur);
  document.getElementById('prog').style.width=(s.dur?100*s.pos/s.dur:0)+'%';
  document.getElementById('err').textContent=s.error||'';
}
loadDevices();setp();poll();setInterval(poll,600);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == "/":
            return self._send(HTML, "text/html; charset=utf-8")
        if p == "/devices":
            return self._send(json.dumps({"devices": out_devices()}))
        if p == "/status":
            return self._send(json.dumps(PLAYER.status()))
        if p == "/setdevices":
            try:
                vib = int(q["vib"][0]) if "vib" in q else None
                aud = int(q["aud"][0]) if "aud" in q else None
                PLAYER.set_devices(vib=vib, aud=aud)
            except Exception as e:
                PLAYER.last_error = str(e)
            return self._send("{}")
        if p == "/load":
            path = native_pick_file()
            if path:
                try:
                    PLAYER.load(path)
                except Exception as e:
                    PLAYER.last_error = str(e)
            return self._send(json.dumps(PLAYER.status()))
        if p == "/play":
            PLAYER.play(); return self._send("{}")
        if p == "/pause":
            PLAYER.pause(); return self._send("{}")
        if p == "/stop":
            PLAYER.stop(); return self._send("{}")
        if p == "/set":
            try:
                if "volume" in q:
                    PLAYER.volume = float(q["volume"][0])
                if "intensity" in q:
                    PLAYER.intensity = float(q["intensity"][0])
                if "remove" in q:
                    PLAYER.remove_pct = float(q["remove"][0])
            except ValueError:
                pass
            return self._send("{}")
        self.send_response(404); self.end_headers()


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"muvi 8-channel UI at {url}  (Ctrl-C to quit)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        PLAYER._close_streams()


if __name__ == "__main__":
    main()

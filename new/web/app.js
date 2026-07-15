// muve — frontend logic. Talks to Python via window.pywebview.api.*
// Falls back to a local mock so the UI is previewable in a plain browser too.

const el = (id) => document.getElementById(id);

// --------------------------------------------------------------- API bridge --
const MockApi = (() => {
  const state = {
    playing: false, play_busy: false, volume: 0.70, vibration: 0.27, highpass_hz: 200, cutoff_hz: 200, position: 0,
    live_mode: null,
    track: { title: "Weightless", artist: "Marconi Union", album: "Ambient Transmissions", duration: 486, artwork: "assets/artwork.svg" },
    bluetooth: {
      connected_id: null, connecting_id: null, scanning: false,
      discovering: false, pairing_id: null, forgetting: false, error: null, supported: false,
    },
    media: { ok: true, can_seek: true, can_prev: true, can_next: true },
    demo: { available: true, error: null },
    devices: [],
    nearby_devices: [],
  };
  let last = performance.now();
  const snap = () => {
    const now = performance.now();
    if (state.playing && state.live_mode === "demo") {
      state.position = Math.min(state.track.duration || 30, state.position + (now - last) / 1000);
    } else if (state.media.ok) {
      state.position = Math.min(state.track.duration, state.position + (now - last) / 1000);
    }
    last = now;
    // Fake per-zone shaker levels so the visualization previews in a browser.
    const t = now / 1000;
    state.zone_rms = state.playing
      ? {
          head: 0.10 + 0.09 * Math.abs(Math.sin(t * 1.3)),
          upper: 0.10 + 0.09 * Math.abs(Math.sin(t * 1.7 + 1)),
          mid: 0.10 + 0.09 * Math.abs(Math.sin(t * 2.1 + 2)),
          legs: 0.10 + 0.09 * Math.abs(Math.sin(t * 0.9 + 3)),
          left: state.volume * (0.14 + 0.13 * Math.abs(Math.sin(t * 1.5 + 4))),
          right: state.volume * (0.14 + 0.13 * Math.abs(Math.sin(t * 1.1 + 5))),
        }
      : { head: 0, upper: 0, mid: 0, legs: 0, left: 0, right: 0 };
    return JSON.parse(JSON.stringify(state));
  };
  return {
    get_state: async () => snap(),
    toggle_play: async () => {
      if (state.playing && state.live_mode === "demo") {
        state.playing = false;
        state.live_mode = null;
      } else {
        state.playing = !state.playing;
        state.live_mode = state.playing ? "cable" : null;
      }
      last = performance.now();
      return snap();
    },
    toggle_demo_play: async () => {
      const on = !(state.playing && state.live_mode === "demo");
      state.playing = on;
      state.live_mode = on ? "demo" : null;
      if (on) {
        state.track = {
          title: "Demo", artist: "MUVI test track", album: "assets/demo.wav",
          duration: 30, artwork: "assets/artwork.svg",
        };
        state.position = 0;
        state.media = { ok: true, can_seek: true, can_prev: true, can_next: true, via: "demo" };
      }
      last = performance.now();
      return snap();
    },
    seek: async (s) => { state.position = Math.max(0, Math.min(state.track.duration, s)); return snap(); },
    previous_track: async () => { state.position = 0; return snap(); },
    next_track: async () => { state.position = 0; if (state.live_mode !== "demo") state.track.title = "Next Track"; return snap(); },
    set_volume: async (v) => { state.volume = v; return snap(); },
    set_vibration: async (v) => { state.vibration = v; return snap(); },
    set_highpass_hz: async (v) => { state.highpass_hz = v; state.cutoff_hz = v; return snap(); },
    scan_bluetooth: async () => {
      state.bluetooth.scanning = true;
      state.bluetooth.discovering = true;
      state.nearby_devices = [];
      setTimeout(() => {
        state.bluetooth.scanning = false;
        state.bluetooth.discovering = false;
        state.nearby_devices = [
          { id: "demo-nearby", name: "Demo Phone", profile: "Nearby — tap Pair", paired: false, nearby: true, can_pair: true, kind: "phone" },
        ];
      }, 900);
      return snap();
    },
    discover_nearby: async () => MockApi.scan_bluetooth(),
    pair_device: async (id) => {
      state.bluetooth.pairing_id = id;
      setTimeout(() => {
        state.bluetooth.pairing_id = null;
        state.nearby_devices = [];
        state.devices = [{ id: "demo-paired", name: "Demo Phone", profile: "Audio ready — tap Connect", paired: true, audio_ready: true, kind: "phone" }];
        state.bluetooth.error = "Paired — tap Connect on the phone below.";
      }, 1200);
      return snap();
    },
    open_bluetooth_settings: async () => {
      state.bluetooth.error = "Fallback Settings opened.";
      return snap();
    },
    connect_device: async (id) => {
      state.bluetooth.connecting_id = id;
      setTimeout(() => {
        state.bluetooth.connecting_id = null;
        state.bluetooth.connected_id = id;
      }, 900);
      return snap();
    },
    disconnect_device: async () => {
      state.bluetooth.connected_id = null;
      state.bluetooth.connecting_id = null;
      state.playing = false;
      state.live_mode = null;
      return snap();
    },
    forget_device: async (id) => {
      state.bluetooth.forgetting = true;
      setTimeout(() => {
        state.bluetooth.forgetting = false;
        state.bluetooth.connected_id = null;
        state.devices = (state.devices || []).filter((d) => d.id !== id);
        state.bluetooth.error = "Device forgotten from Windows.";
      }, 700);
      return snap();
    },
    forget_all_devices: async () => {
      state.bluetooth.forgetting = true;
      setTimeout(() => {
        state.bluetooth.forgetting = false;
        state.bluetooth.connected_id = null;
        state.devices = [];
        state.bluetooth.error = "Forgot all paired Bluetooth devices from Windows.";
      }, 900);
      return snap();
    },
    clear_bluetooth_alert: async () => {
      state.bluetooth.alert = null;
      return snap();
    },
  };
})();

let api = MockApi;
function bindApi() {
  if (window.pywebview && window.pywebview.api) api = window.pywebview.api;
}

// ----------------------------------------------------------------- helpers --
const fmtTime = (s) => {
  s = Math.max(0, Math.floor(Number(s) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
};
const deviceIcon = (kind) =>
  kind === "laptop"
    ? '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M2 20h20" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
    : '<svg viewBox="0 0 24 24"><rect x="7" y="2" width="10" height="20" rx="2.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M11 18h2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

let seeking = false;
let demoSeeking = false;
let playClickLock = false;
let demoPlayClickLock = false;
let lastState = { playing: false };
let lastDevicesSig = "";
let lastNearbySig = "";
let lastBtAlert = "";

// Bass cutoff UI: 0% = deepest (40 Hz), 100% = brightest (250 Hz). Default 76% ≈ 200 Hz.
const CUTOFF_HZ_MIN = 40;
const CUTOFF_HZ_MAX = 250;
const pctToCutoffHz = (pct) =>
  CUTOFF_HZ_MIN + (Math.max(0, Math.min(100, Number(pct))) / 100) * (CUTOFF_HZ_MAX - CUTOFF_HZ_MIN);
const cutoffHzToPct = (hz) =>
  Math.round(
    (100 * (Math.max(CUTOFF_HZ_MIN, Math.min(CUTOFF_HZ_MAX, Number(hz))) - CUTOFF_HZ_MIN) /
      (CUTOFF_HZ_MAX - CUTOFF_HZ_MIN))
  );

function isDemoPlaying(s) {
  return Boolean(s && s.playing && s.live_mode === "demo");
}
function isLivePlaying(s) {
  return Boolean(s && s.playing && s.live_mode !== "demo");
}

// ------------------------------------------------- shaker visualization --
// 8 shakers over the bed image. Each row of 2 shakers shares one signal
// (zone_rms from the engine): head, upper back, mid back, legs.
//
// VIZ_EDIT_MODE = true  → shakers are always visible and draggable; the
// current positions are shown bottom-left as ready-to-paste VIZ_ZONES lines
// and saved in localStorage. Set to false once positions are final.
const VIZ_EDIT_MODE = false;
const VIZ_POS_KEY = "muvi-shaker-positions-v1";

const VIZ_ZONES = [
  { key: "headL",  zone: "head",  x: 29.4, y: 43.3, color: "#FFC97C" },
  { key: "headR",  zone: "head",  x: 38.1, y: 38.7, color: "#FFC97C" },
  { key: "upperL", zone: "upper", x: 40.3, y: 55.7, color: "#FF8FC9" },
  { key: "upperR", zone: "upper", x: 48.3, y: 51.4, color: "#FF8FC9" },
  { key: "midL",   zone: "mid",   x: 59.5, y: 58.2, color: "#7CFFC4" },
  { key: "midR",   zone: "mid",   x: 67.7, y: 52.2, color: "#7CFFC4" },
  { key: "legL",   zone: "legs",  x: 71.1, y: 67.9, color: "#7C9EFF" },
  { key: "legR",   zone: "legs",  x: 79.8, y: 60.7, color: "#7C9EFF" },
  // Stereo audio channels (speakers) — driven by the music itself.
  { key: "audioL", zone: "left",  x: 20.7, y: 31.8, color: "#ff0505" },
  { key: "audioR", zone: "right", x: 29.1, y: 28.0, color: "#ff0505" },
];

function loadVizPositions() {
  if (!VIZ_EDIT_MODE) return {};
  try {
    return JSON.parse(localStorage.getItem(VIZ_POS_KEY) || "{}") || {};
  } catch (e) {
    return {};
  }
}

function vizPosition(z) {
  const saved = loadVizPositions()[z.key];
  return saved ? { x: saved.x, y: saved.y } : { x: z.x, y: z.y };
}

function setShakerPosition(key, x, y) {
  vizInstances.forEach((shakers) =>
    shakers.forEach((sh) => {
      if (sh.key === key) {
        sh.wrap.style.left = `${x}%`;
        sh.wrap.style.top = `${y}%`;
      }
    })
  );
  const pos = loadVizPositions();
  pos[key] = { x, y };
  try { localStorage.setItem(VIZ_POS_KEY, JSON.stringify(pos)); } catch (e) {}
  updateVizDump();
}

function updateVizDump() {
  if (!VIZ_EDIT_MODE) return;
  let dump = document.getElementById("vizPosDump");
  if (!dump) {
    dump = document.createElement("pre");
    dump.id = "vizPosDump";
    dump.className = "viz-pos-dump";
    document.body.appendChild(dump);
  }
  dump.textContent =
    "EDIT MODE — drag the glowing dots. Paste into VIZ_ZONES:\n" +
    VIZ_ZONES.map((z) => {
      const p = vizPosition(z);
      const key = `"${z.key}",`.padEnd(9);
      return `{ key: ${key} zone: "${z.zone}", x: ${p.x.toFixed(1)}, y: ${p.y.toFixed(1)}, color: "${z.color}" },`;
    }).join("\n");
}

function makeDraggable(sh, container) {
  const hit = document.createElement("div");
  hit.className = "shaker-hit";
  sh.wrap.appendChild(hit);
  let dragging = false;
  hit.addEventListener("pointerdown", (e) => {
    dragging = true;
    e.preventDefault();
    try { hit.setPointerCapture(e.pointerId); } catch (err) {}
  });
  hit.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = container.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = Math.max(0, Math.min(100, ((e.clientX - rect.left) / rect.width) * 100));
    const y = Math.max(0, Math.min(100, ((e.clientY - rect.top) / rect.height) * 100));
    setShakerPosition(sh.key, x, y);
  });
  const end = () => { dragging = false; };
  hit.addEventListener("pointerup", end);
  hit.addEventListener("pointercancel", end);
}

const hexToRgb = (hex) => {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  if (!m) return { r: 160, g: 228, b: 255 };
  return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
};

const vizInstances = [];

function buildViz(container) {
  const shakers = VIZ_ZONES.map((z) => {
    const wrap = document.createElement("div");
    wrap.className = "shaker";
    const pos = vizPosition(z);
    wrap.style.left = `${pos.x}%`;
    wrap.style.top = `${pos.y}%`;
    for (let i = 0; i < 3; i++) {
      const ring = document.createElement("div");
      ring.className = "shaker-ring";
      wrap.appendChild(ring);
    }
    const core = document.createElement("div");
    core.className = "shaker-core";
    wrap.appendChild(core);
    container.appendChild(wrap);
    const sh = { key: z.key, zone: z.zone, rgb: hexToRgb(z.color), wrap, level: 0 };
    if (VIZ_EDIT_MODE) makeDraggable(sh, container);
    return sh;
  });
  vizInstances.push(shakers);
}

function initViz() {
  ["npViz", "demoViz"].forEach((id) => {
    const c = el(id);
    if (c) buildViz(c);
  });
  updateVizDump();
}

// Engine RMS (~0–0.3 typical) → 0..1 wave intensity.
// Steep at the low end so quiet/mid signals still produce visible waves.
function rmsToIntensity(rms) {
  const v = Math.max(0, Number(rms) || 0);
  return Math.max(0, Math.min(1, Math.pow(v * 5, 0.5)));
}

function applyShaker(sh) {
  // Edit mode keeps every shaker visible so it can be dragged while idle.
  let t = Math.max(0, Math.min(1, sh.level));
  if (VIZ_EDIT_MODE) t = Math.max(t, 0.3);
  const { r, g, b } = sh.rgb;
  sh.wrap.style.opacity = VIZ_EDIT_MODE || t > 0.02 ? "1" : "0";
  const st = sh.wrap.style;
  // Slow, calm ripple (3.6s → 2.2s). Quantized to 0.35s steps so the running
  // animation isn't retimed on every poll — retiming reads as stutter/clipping.
  const dur = Math.round((3.6 - t * 1.4) / 0.35) * 0.35;
  st.setProperty("--dur", `${dur.toFixed(2)}s`);
  st.setProperty("--ring", `${(40 + t * 230).toFixed(1)}px`);
  st.setProperty("--ring-color", `rgba(${r},${g},${b},${(0.4 + t * 0.55).toFixed(3)})`);
  st.setProperty("--glow", `${(12 + t * 40).toFixed(1)}px`);
  st.setProperty("--glow-color", `rgba(${r},${g},${b},${(0.18 + t * 0.5).toFixed(3)})`);
  st.setProperty("--core", `${(22 + t * 44).toFixed(1)}px`);
  st.setProperty("--core-a", `rgba(${r},${g},${b},${(0.45 + t * 0.5).toFixed(3)})`);
  st.setProperty("--core-b", `rgba(${r},${g},${b},0)`);
}

function updateViz(s) {
  const playing = Boolean(s && s.playing);
  const zoneRms = s && s.zone_rms;
  vizInstances.forEach((shakers) =>
    shakers.forEach((sh) => {
      let rms = 0;
      if (playing) {
        // Older backends don't send zone_rms — fall back to overall input level.
        rms = zoneRms ? zoneRms[sh.zone] : Number(s.input_rms || 0);
      }
      const target = rmsToIntensity(rms);
      // Gentle attack/release so wave size drifts instead of jumping,
      // but drop quickly to zero when the signal goes fully silent.
      const rate = target > sh.level ? 0.35 : target < 0.02 ? 0.5 : 0.15;
      sh.level += (target - sh.level) * rate;
      if (target === 0 && sh.level < 0.03) sh.level = 0;
      applyShaker(sh);
    })
  );
}

const busyIcon = '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity="0.35"/><path d="M12 4a8 8 0 0 1 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>';
const pauseIcon = '<path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/>';
const playIconSvg = '<path d="M8 5v14l11-7z" fill="currentColor"/>';

// ------------------------------------------------------------------ render --
function render(s) {
  if (!s) return;
  lastState = s;
  // Pop pairing guidance once (e.g. status 19 FAILED → forget on phone).
  const alertMsg = s.bluetooth && s.bluetooth.alert;
  if (alertMsg && alertMsg !== lastBtAlert) {
    lastBtAlert = alertMsg;
    window.alert(alertMsg);
    if (api.clear_bluetooth_alert) {
      Promise.resolve(api.clear_bluetooth_alert()).catch(() => {});
    }
  } else if (!alertMsg) {
    lastBtAlert = "";
  }

  const liveOn = isLivePlaying(s);
  const demoOn = isDemoPlaying(s);
  const busy = Boolean(s.play_busy) || playClickLock;
  const demoBusy = Boolean(s.play_busy) || demoPlayClickLock;

  // Now playing (Live / AUX / Bluetooth only)
  el("npTitle").textContent = demoOn ? "Ready" : s.track.title;
  el("npArtist").textContent = demoOn ? "Phone audio" : s.track.artist;
  el("npAlbum").textContent = demoOn
    ? "AUX: plug in → Play · Bluetooth: Connect → play phone → Play"
    : s.track.album;

  updateViz(s);

  const playBtn = el("playBtn");
  playBtn.disabled = busy;
  playBtn.classList.toggle("busy", busy);
  playBtn.setAttribute("aria-busy", busy ? "true" : "false");
  el("playIcon").innerHTML = busy ? busyIcon : (liveOn ? pauseIcon : playIconSvg);

  const connected = (s.devices || []).find((d) => d.id === s.bluetooth.connected_id);
  const online = Boolean(connected);
  const connectingId = s.bluetooth.connecting_id || null;
  const connectingDev = connectingId
    ? (s.devices || []).find((d) => d.id === connectingId)
    : null;

  const hint = el("liveHint");
  if (hint) {
    const capture = String(s.capture_name || "");
    const isAux = /behringer|umc|usb audio|line/i.test(capture);
    if (busy && !demoOn) {
      hint.textContent = liveOn ? "Stopping…" : "Starting Live — please wait…";
    } else if (s.engine_error && !demoOn) {
      hint.textContent = `Live failed: ${s.engine_error}`;
    } else if (liveOn) {
      const rms = Number(s.input_rms || 0);
      const waiting = isAux
        ? "no AUX signal — play music on the phone"
        : "no signal on CABLE — play music on the phone";
      const signal = rms > 0.002 ? `receiving audio (${rms.toFixed(3)})` : waiting;
      const out = s.output_name ? ` → ${s.output_name}` : "";
      hint.textContent = `Live ON · ${capture || "capture"}${out} · ${signal}`;
    } else {
      hint.textContent = "";
    }
  }

  if (!seeking) {
    const frac = (!demoOn && s.track.duration) ? s.position / s.track.duration : 0;
    el("seek").value = Math.round(Math.max(0, Math.min(1, frac)) * 1000);
  }
  el("timeCur").textContent = fmtTime(demoOn ? 0 : s.position);
  el("timeDur").textContent = (!demoOn && Number(s.track.duration) > 0) ? fmtTime(s.track.duration) : "LIVE";

  const media = s.media || {};
  const hasTimeline = !demoOn && Boolean(media.ok) && Number(s.track.duration) > 0;
  const seekEl = el("seek");
  seekEl.disabled = !hasTimeline;
  seekEl.classList.toggle("disabled", seekEl.disabled);
  const prevBtn = el("prevBtn");
  const nextBtn = el("nextBtn");
  const btConnected = Boolean(s.bluetooth && s.bluetooth.connected_id);
  prevBtn.disabled = demoOn || !(media.can_prev || btConnected);
  nextBtn.disabled = demoOn || !(media.can_next || btConnected);
  prevBtn.classList.toggle("disabled", prevBtn.disabled);
  nextBtn.classList.toggle("disabled", nextBtn.disabled);

  el("volume").value = Math.round(s.volume * 100);
  el("volVal").textContent = `${Math.round(s.volume * 100)}%`;
  el("vibration").value = Math.round(s.vibration * 100);
  el("vibVal").textContent = `${Math.round(s.vibration * 100)}%`;
  const cutoffPct = cutoffHzToPct(s.cutoff_hz ?? s.highpass_hz);
  el("highpass").value = cutoffPct;
  el("hpVal").textContent = `${cutoffPct}%`;

  // Demo screen (same controls, demo.wav)
  if (el("demoTitle")) {
    const demoTrack = demoOn
      ? s.track
      : { title: "Demo", artist: "MUVI test track", album: "assets/demo.wav", duration: s.track?.duration || 0, artwork: "assets/artwork.svg" };
    el("demoTitle").textContent = demoTrack.title || "Demo";
    el("demoArtist").textContent = demoTrack.artist || "MUVI test track";
    el("demoAlbum").textContent = demoTrack.album || "assets/demo.wav";

    const demoPlayBtn = el("demoPlayBtn");
    demoPlayBtn.disabled = demoBusy;
    demoPlayBtn.classList.toggle("busy", demoBusy);
    el("demoPlayIcon").innerHTML = demoBusy ? busyIcon : (demoOn ? pauseIcon : playIconSvg);

    if (!demoSeeking) {
      const dFrac = demoOn && s.track.duration ? s.position / s.track.duration : 0;
      el("demoSeek").value = Math.round(Math.max(0, Math.min(1, dFrac)) * 1000);
    }
    el("demoTimeCur").textContent = fmtTime(demoOn ? s.position : 0);
    el("demoTimeDur").textContent = Number(demoOn ? s.track.duration : 0) > 0
      ? fmtTime(s.track.duration)
      : (demoOn ? fmtTime(s.track.duration || 0) : "—");

    const demoSeekEl = el("demoSeek");
    demoSeekEl.disabled = !demoOn;
    demoSeekEl.classList.toggle("disabled", demoSeekEl.disabled);
    el("demoPrevBtn").disabled = !demoOn;
    el("demoNextBtn").disabled = !demoOn;
    el("demoPrevBtn").classList.toggle("disabled", !demoOn);
    el("demoNextBtn").classList.toggle("disabled", !demoOn);

    el("demoVolume").value = Math.round(s.volume * 100);
    el("demoVolVal").textContent = `${Math.round(s.volume * 100)}%`;
    el("demoVibration").value = Math.round(s.vibration * 100);
    el("demoVibVal").textContent = `${Math.round(s.vibration * 100)}%`;
    el("demoHighpass").value = cutoffPct;
    el("demoHpVal").textContent = `${cutoffPct}%`;

    const demoHint = el("demoHint");
    if (demoHint) {
      if (demoBusy) {
        demoHint.textContent = demoOn ? "Stopping…" : "Starting demo — please wait…";
      } else if (s.demo && s.demo.error) {
        demoHint.textContent = `Demo failed: ${s.demo.error}`;
      } else if (demoOn) {
        const out = s.output_name ? ` → ${s.output_name}` : "";
        demoHint.textContent = `Demo ON · demo.wav${out}`;
      } else if (s.demo && s.demo.available === false) {
        demoHint.textContent = "demo.wav not found in assets/";
      } else {
        demoHint.textContent = "";
      }
    }
  }

  // Bottom-left status: AUX Live, Bluetooth name, Demo, or idle
  const sidebarConn = el("sidebarConn");
  const mode = s.live_mode || "";
  const capture = String(s.capture_name || "");
  const isAuxLive = liveOn && (mode === "aux" || /behringer|umc|usb audio|line/i.test(capture));
  const rms = Number(s.input_rms || 0);
  let sidebarText = "Not connected";
  let sidebarOnline = false;
  if (demoOn) {
    sidebarOnline = true;
    sidebarText = "Demo · demo.wav";
  } else if (isAuxLive) {
    sidebarOnline = true;
    const short =
      capture.replace(/\s*\[.*?\]\s*/g, "").trim().slice(0, 28) || "AUX";
    sidebarText = rms > 0.002 ? `AUX Live · ${short}` : `AUX Live · waiting…`;
  } else if (liveOn && mode === "cable") {
    sidebarOnline = true;
    sidebarText = rms > 0.002 ? "Bluetooth Live · signal" : "Bluetooth Live · waiting…";
  } else if (online) {
    sidebarOnline = true;
    sidebarText = connected.name;
  } else if (s.bluetooth && s.bluetooth.error && /bluetooth is off|disconnected|unavailable/i.test(String(s.bluetooth.error))) {
    sidebarText = String(s.bluetooth.error).split(".")[0];
  }
  sidebarConn.classList.toggle("online", sidebarOnline);
  el("sidebarConnText").textContent = sidebarText;

  el("statusCard").classList.toggle("online", online || isAuxLive);
  const bt = s.bluetooth;
  const pairingId = bt.pairing_id || null;
  el("statusCard").classList.toggle("connecting", Boolean(connectingId) || Boolean(pairingId));
  if (pairingId) {
    el("statusTitle").textContent = "Waiting for confirmation…";
  } else if (isAuxLive) {
    el("statusTitle").textContent = "AUX Live";
  } else if (connectingId) {
    el("statusTitle").textContent = connectingDev
      ? `Connecting to ${connectingDev.name}…`
      : "Connecting…";
  } else {
    el("statusTitle").textContent = online ? `Connected to ${connected.name}` : "Not connected";
  }
  let sub = online ? connected.profile : "Paired phones appear automatically. Scan to find a new phone.";
  if (isAuxLive) {
    sub = rms > 0.002
      ? `Capturing ${capture || "AUX"} → Gigaport`
      : "AUX Live — play music on the phone";
  } else if (pairingId) {
    sub = "Waiting for confirmation on the phone…";
  } else if (connectingId) {
    sub = "Please wait — opening Bluetooth audio connection…";
  } else if (online && s.engine_error) sub = `BT connected, but Start Live failed: ${s.engine_error}`;
  else if (online && liveOn && s.capture_name) sub = `Live ON · capturing ${s.capture_name}`;
  else if (online && s.engine_ok === false) sub = `BT connected, engine unavailable: ${s.engine_error || "unknown"}`;
  else if (!online && bt.error) sub = bt.error;
  else if (!online && bt.supported === false) sub = "Bluetooth receiver unavailable on this machine (Windows only).";
  else if (online) sub = "Connected — play music on the phone, then go to Now Playing and press Play.";
  else if (bt.scanning || bt.discovering) sub = "Scanning nearby — put the phone in pairing mode…";
  el("statusSub").textContent = sub;
  const forgetting = Boolean(bt.forgetting);
  el("disconnectBtn").hidden = !online || Boolean(connectingId) || Boolean(pairingId);
  el("disconnectBtn").disabled = forgetting;
  // Single-device Forget hidden for now — use Forget all.
  const forgetBtn = el("forgetBtn");
  if (forgetBtn) {
    forgetBtn.hidden = true;
    forgetBtn.disabled = true;
  }

  const scanBtn = el("scanBtn");
  const busyBt = Boolean(bt.scanning) || Boolean(bt.discovering) || Boolean(connectingId) || Boolean(pairingId) || forgetting;
  scanBtn.classList.toggle("scanning", Boolean(bt.scanning) || Boolean(bt.discovering));
  scanBtn.disabled = busyBt;
  el("scanLabel").textContent = (bt.scanning || bt.discovering) ? "Scanning…" : "Scan";
  const forgetAllBtn = el("forgetAllBtn");
  if (forgetAllBtn) {
    forgetAllBtn.disabled = busyBt;
    forgetAllBtn.textContent = forgetting ? "Forgetting…" : "Forget all";
  }

  renderDevices(s);
  renderNearby(s);
}

function devicesSig(s) {
  const devices = s.devices || [];
  const ids = devices.map((d) => `${d.id}|${d.name}|${d.profile}|${d.paired}|${d.audio_ready}`).join(";");
  return [
    ids,
    s.bluetooth.connected_id || "",
    s.bluetooth.connecting_id || "",
    s.bluetooth.scanning ? "1" : "0",
    s.bluetooth.pairing_id || "",
    s.bluetooth.forgetting ? "1" : "0",
  ].join("::");
}

function nearbySig(s) {
  const devices = s.nearby_devices || [];
  const ids = devices.map((d) => `${d.id}|${d.name}|${d.profile}`).join(";");
  return [
    ids,
    s.bluetooth.discovering ? "1" : "0",
    s.bluetooth.pairing_id || "",
  ].join("::");
}

function renderDevices(s) {
  const sig = devicesSig(s);
  if (sig === lastDevicesSig) return;
  lastDevicesSig = sig;

  const list = el("deviceList");
  list.innerHTML = "";
  const devices = s.devices || [];
  const connectingId = s.bluetooth.connecting_id || null;
  const pairingId = s.bluetooth.pairing_id || null;
  const scanning = Boolean(s.bluetooth.scanning);
  const busy = Boolean(connectingId) || Boolean(pairingId) || Boolean(s.bluetooth.forgetting);

  if (!devices.length) {
    const li = document.createElement("li");
    li.className = "device empty";
    const msg = "No paired phones yet — Scan below to find a phone, then Pair.";
    li.innerHTML = `<div class="device-meta"><span class="device-name">No paired phones</span><span class="device-sub">${msg}</span></div>`;
    list.appendChild(li);
    return;
  }

  devices.forEach((d) => {
    const isConn = d.id === s.bluetooth.connected_id;
    const isConnecting = d.id === connectingId;
    const li = document.createElement("li");
    li.className = "device"
      + (isConn ? " connected" : "")
      + (isConnecting ? " connecting" : "");
    let action = "";
    if (isConn) {
      action = '<span class="badge">Connected</span>';
    } else if (isConnecting) {
      action = '<button class="btn-connect busy" disabled><span class="btn-spin"></span>Connecting…</button>';
    } else {
      action = `<button class="btn-connect" data-id="${d.id}" ${busy ? "disabled" : ""}>Connect</button>`;
    }
    li.innerHTML = `
      <div class="device-ico">${deviceIcon(d.kind)}</div>
      <div class="device-meta">
        <span class="device-name">${d.name}</span>
        <span class="device-sub">${isConnecting ? "Opening connection…" : d.profile}</span>
      </div>
      <div class="device-action">${action}</div>`;
    list.appendChild(li);
  });

  list.querySelectorAll(".btn-connect:not(.busy)").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!b.dataset.id || busy) return;
      lastDevicesSig = "";
      render(await api.connect_device(b.dataset.id));
    })
  );
}

function renderNearby(s) {
  const list = el("nearbyList");
  if (!list) return;
  const sig = nearbySig(s);
  if (sig === lastNearbySig) return;
  lastNearbySig = sig;

  list.innerHTML = "";
  const devices = s.nearby_devices || [];
  const discovering = Boolean(s.bluetooth.discovering);
  const pairingId = s.bluetooth.pairing_id || null;
  const busy = Boolean(pairingId) || Boolean(s.bluetooth.connecting_id);

  if (discovering && !devices.length) {
    const li = document.createElement("li");
    li.className = "device empty scanning";
    li.innerHTML = `<div class="device-meta"><span class="device-name">Scanning nearby…</span><span class="device-sub">Put the phone in pairing / discoverable mode.</span></div>`;
    list.appendChild(li);
    return;
  }

  if (!devices.length) {
    const li = document.createElement("li");
    li.className = "device empty";
    const msg = "Press Scan to search for nearby phones.";
    li.innerHTML = `<div class="device-meta"><span class="device-name">No nearby phones</span><span class="device-sub">${msg}</span></div>`;
    list.appendChild(li);
    return;
  }

  devices.forEach((d) => {
    const isPairing = d.id === pairingId;
    const li = document.createElement("li");
    li.className = "device" + (isPairing ? " connecting" : "");
    let action = "";
    if (isPairing) {
      action = '<button class="btn-connect busy" disabled><span class="btn-spin"></span>Pairing…</button>';
    } else if (d.paired || d.can_pair === false) {
      action = '<span class="badge">Already paired</span>';
    } else {
      action = `<button class="btn-connect" data-pair-id="${d.id}" ${busy ? "disabled" : ""}>Pair</button>`;
    }
    li.innerHTML = `
      <div class="device-ico">${deviceIcon(d.kind)}</div>
      <div class="device-meta">
        <span class="device-name">${d.name}</span>
        <span class="device-sub">${isPairing ? "Waiting for confirmation…" : d.profile}</span>
      </div>
      <div class="device-action">${action}</div>`;
    list.appendChild(li);
  });

  list.querySelectorAll("[data-pair-id]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!b.dataset.pairId || busy) return;
      lastNearbySig = "";
      lastDevicesSig = "";
      render(await api.pair_device(b.dataset.pairId));
    })
  );
}

// ------------------------------------------------------------------ events --
function wire() {
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      document.querySelectorAll(".screen").forEach((sc) => sc.classList.remove("active"));
      el("screen-" + b.dataset.screen).classList.add("active");
    })
  );

  el("playBtn").addEventListener("click", async () => {
    if (playClickLock) return;
    playClickLock = true;
    const wasPlaying = Boolean(lastState.playing);
    el("playBtn").disabled = true;
    el("playBtn").classList.add("busy");
    const hint = el("liveHint");
    if (hint) hint.textContent = wasPlaying ? "Stopping…" : "Starting Live — please wait…";
    try {
      render(await api.toggle_play());
    } finally {
      playClickLock = false;
      render(await api.get_state());
    }
  });

  el("seek").addEventListener("input", () => {
    seeking = true;
    const dur = Number(lastState.track?.duration || 0);
    if (dur > 0) {
      const pos = (el("seek").value / 1000) * dur;
      el("timeCur").textContent = fmtTime(pos);
    }
  });
  el("seek").addEventListener("change", async () => {
    const dur = Number(lastState.track?.duration || 0);
    try {
      if (dur > 0) {
        const pos = (el("seek").value / 1000) * dur;
        render(await api.seek(pos));
      }
    } finally {
      seeking = false;
    }
  });

  el("prevBtn").addEventListener("click", async () => {
    if (el("prevBtn").disabled) return;
    render(await api.previous_track());
  });
  el("nextBtn").addEventListener("click", async () => {
    if (el("nextBtn").disabled) return;
    render(await api.next_track());
  });

  el("volume").addEventListener("input", async () => {
    el("volVal").textContent = `${el("volume").value}%`;
    render(await api.set_volume(el("volume").value / 100));
  });
  el("vibration").addEventListener("input", async () => {
    el("vibVal").textContent = `${el("vibration").value}%`;
    render(await api.set_vibration(el("vibration").value / 100));
  });
  el("highpass").addEventListener("input", async () => {
    const pct = Number(el("highpass").value);
    el("hpVal").textContent = `${pct}%`;
    render(await api.set_highpass_hz(pctToCutoffHz(pct)));
  });

  // Demo tab — same controls, plays demo.wav through Live path
  if (el("demoPlayBtn")) {
    el("demoPlayBtn").addEventListener("click", async () => {
      if (demoPlayClickLock) return;
      demoPlayClickLock = true;
      const wasDemo = isDemoPlaying(lastState);
      el("demoPlayBtn").disabled = true;
      el("demoPlayBtn").classList.add("busy");
      const hint = el("demoHint");
      if (hint) hint.textContent = wasDemo ? "Stopping…" : "Starting demo — please wait…";
      try {
        render(await api.toggle_demo_play());
      } finally {
        demoPlayClickLock = false;
        render(await api.get_state());
      }
    });

    el("demoSeek").addEventListener("input", () => {
      demoSeeking = true;
      const dur = Number(lastState.track?.duration || 0);
      if (dur > 0 && isDemoPlaying(lastState)) {
        const pos = (el("demoSeek").value / 1000) * dur;
        el("demoTimeCur").textContent = fmtTime(pos);
      }
    });
    el("demoSeek").addEventListener("change", async () => {
      const dur = Number(lastState.track?.duration || 0);
      try {
        if (dur > 0 && isDemoPlaying(lastState)) {
          const pos = (el("demoSeek").value / 1000) * dur;
          render(await api.seek(pos));
        }
      } finally {
        demoSeeking = false;
      }
    });

    el("demoPrevBtn").addEventListener("click", async () => {
      if (el("demoPrevBtn").disabled) return;
      render(await api.previous_track());
    });
    el("demoNextBtn").addEventListener("click", async () => {
      if (el("demoNextBtn").disabled) return;
      render(await api.next_track());
    });

    el("demoVolume").addEventListener("input", async () => {
      el("demoVolVal").textContent = `${el("demoVolume").value}%`;
      render(await api.set_volume(el("demoVolume").value / 100));
    });
    el("demoVibration").addEventListener("input", async () => {
      el("demoVibVal").textContent = `${el("demoVibration").value}%`;
      render(await api.set_vibration(el("demoVibration").value / 100));
    });
    el("demoHighpass").addEventListener("input", async () => {
      const pct = Number(el("demoHighpass").value);
      el("demoHpVal").textContent = `${pct}%`;
      render(await api.set_highpass_hz(pctToCutoffHz(pct)));
    });
  }

  el("scanBtn").addEventListener("click", async () => {
    if (el("scanBtn").disabled) return;
    lastNearbySig = "";
    lastDevicesSig = "";
    render(await api.scan_bluetooth());
  });
  el("disconnectBtn").addEventListener("click", async () => {
    lastDevicesSig = "";
    render(await api.disconnect_device());
  });
  el("forgetAllBtn").addEventListener("click", async () => {
    if (el("forgetAllBtn").disabled) return;
    if (!window.confirm("Forget ALL paired Bluetooth devices from Windows on this tablet?")) return;
    lastDevicesSig = "";
    lastNearbySig = "";
    render(await api.forget_all_devices());
  });
}

// -------------------------------------------------------------------- boot --
async function boot() {
  bindApi();
  wire();
  initViz();
  render(await api.get_state());
  setInterval(async () => {
    if (!seeking && !demoSeeking) render(await api.get_state());
  }, 400);
}

window.addEventListener("pywebviewready", bindApi);
window.addEventListener("DOMContentLoaded", boot);

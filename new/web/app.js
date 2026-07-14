// muve — frontend logic. Talks to Python via window.pywebview.api.*
// Falls back to a local mock so the UI is previewable in a plain browser too.

const el = (id) => document.getElementById(id);

// --------------------------------------------------------------- API bridge --
const MockApi = (() => {
  const state = {
    playing: false, play_busy: false, volume: 0.70, vibration: 0.27, highpass_hz: 200, cutoff_hz: 200, position: 0,
    track: { title: "Weightless", artist: "Marconi Union", album: "Ambient Transmissions", duration: 486, artwork: "assets/artwork.svg" },
    bluetooth: {
      connected_id: null, connecting_id: null, scanning: false,
      discovering: false, pairing_id: null, error: null, supported: false,
    },
    media: { ok: true, can_seek: true, can_prev: true, can_next: true },
    devices: [],
    nearby_devices: [],
  };
  let last = performance.now();
  const snap = () => {
    const now = performance.now();
    if (state.media.ok) state.position = Math.min(state.track.duration, state.position + (now - last) / 1000);
    last = now;
    return JSON.parse(JSON.stringify(state));
  };
  return {
    get_state: async () => snap(),
    toggle_play: async () => { state.playing = !state.playing; last = performance.now(); return snap(); },
    seek: async (s) => { state.position = Math.max(0, Math.min(state.track.duration, s)); return snap(); },
    previous_track: async () => { state.position = 0; return snap(); },
    next_track: async () => { state.position = 0; state.track.title = "Next Track"; return snap(); },
    set_volume: async (v) => { state.volume = v; return snap(); },
    set_vibration: async (v) => { state.vibration = v; return snap(); },
    set_highpass_hz: async (v) => { state.highpass_hz = v; return snap(); },
    scan_bluetooth: async () => {
      state.bluetooth.scanning = true;
      state.devices = [];
      setTimeout(() => {
        state.bluetooth.scanning = false;
        state.bluetooth.error = "No paired Bluetooth audio devices found.";
      }, 800);
      return snap();
    },
    discover_nearby: async () => {
      state.bluetooth.discovering = true;
      state.nearby_devices = [];
      setTimeout(() => {
        state.bluetooth.discovering = false;
        state.nearby_devices = [
          { id: "demo-nearby", name: "Demo Phone", profile: "Nearby — tap Pair", paired: false, nearby: true, kind: "phone" },
        ];
      }, 900);
      return snap();
    },
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
let playClickLock = false;
let lastState = { playing: false };
let lastDevicesSig = "";
let lastNearbySig = "";

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

// ------------------------------------------------------------------ render --
function render(s) {
  if (!s) return;
  lastState = s;
  // Now playing
  el("npTitle").textContent = s.track.title;
  el("npArtist").textContent = s.track.artist;
  el("npAlbum").textContent = s.track.album;
  el("npArt").src = s.track.artwork;

  const busy = Boolean(s.play_busy) || playClickLock;
  const playBtn = el("playBtn");
  playBtn.disabled = busy;
  playBtn.classList.toggle("busy", busy);
  playBtn.setAttribute("aria-busy", busy ? "true" : "false");

  el("playIcon").innerHTML = busy
    ? '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity="0.35"/><path d="M12 4a8 8 0 0 1 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
    : s.playing
      ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/>'
      : '<path d="M8 5v14l11-7z" fill="currentColor"/>';

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
    if (busy) {
      hint.textContent = s.playing ? "Stopping…" : "Starting Live — please wait…";
    } else if (s.engine_error) {
      hint.textContent = `Live failed: ${s.engine_error}`;
    } else if (s.playing) {
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
    const frac = s.track.duration ? s.position / s.track.duration : 0;
    el("seek").value = Math.round(Math.max(0, Math.min(1, frac)) * 1000);
  }
  el("timeCur").textContent = fmtTime(s.position);
  el("timeDur").textContent = Number(s.track.duration) > 0 ? fmtTime(s.track.duration) : "LIVE";

  const media = s.media || {};
  const hasTimeline = Boolean(media.ok) && Number(s.track.duration) > 0;
  const seekEl = el("seek");
  seekEl.disabled = !hasTimeline;
  seekEl.classList.toggle("disabled", seekEl.disabled);
  const prevBtn = el("prevBtn");
  const nextBtn = el("nextBtn");
  // Prev/next enabled when BT is connected or SMTC says so (media keys).
  const btConnected = Boolean(s.bluetooth && s.bluetooth.connected_id);
  prevBtn.disabled = !(media.can_prev || btConnected);
  nextBtn.disabled = !(media.can_next || btConnected);
  prevBtn.classList.toggle("disabled", prevBtn.disabled);
  nextBtn.classList.toggle("disabled", nextBtn.disabled);

  el("volume").value = Math.round(s.volume * 100);
  el("volVal").textContent = `${Math.round(s.volume * 100)}%`;
  el("vibration").value = Math.round(s.vibration * 100);
  el("vibVal").textContent = `${Math.round(s.vibration * 100)}%`;
  const cutoffPct = cutoffHzToPct(s.cutoff_hz ?? s.highpass_hz);
  el("highpass").value = cutoffPct;
  el("hpVal").textContent = `${cutoffPct}%`;

  // Bottom-left status: AUX Live, Bluetooth name, or idle
  const sidebarConn = el("sidebarConn");
  const mode = s.live_mode || "";
  const capture = String(s.capture_name || "");
  const isAuxLive = s.playing && (mode === "aux" || /behringer|umc|usb audio|line/i.test(capture));
  const rms = Number(s.input_rms || 0);
  let sidebarText = "Not connected";
  let sidebarOnline = false;
  if (isAuxLive) {
    sidebarOnline = true;
    const short =
      capture.replace(/\s*\[.*?\]\s*/g, "").trim().slice(0, 28) || "AUX";
    sidebarText = rms > 0.002 ? `AUX Live · ${short}` : `AUX Live · waiting…`;
  } else if (s.playing && mode === "cable") {
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
  let sub = online ? connected.profile : "Pair a nearby phone, then Connect.";
  if (isAuxLive) {
    sub = rms > 0.002
      ? `Capturing ${capture || "AUX"} → Gigaport`
      : "AUX Live — play music on the phone";
  } else if (pairingId) {
    sub = "Waiting for confirmation on the phone…";
  } else if (connectingId) {
    sub = "Please wait — opening Bluetooth audio connection…";
  } else if (online && s.engine_error) sub = `BT connected, but Start Live failed: ${s.engine_error}`;
  else if (online && s.playing && s.capture_name) sub = `Live ON · capturing ${s.capture_name}`;
  else if (online && s.engine_ok === false) sub = `BT connected, engine unavailable: ${s.engine_error || "unknown"}`;
  else if (!online && bt.error) sub = bt.error;
  else if (!online && bt.supported === false) sub = "Bluetooth receiver unavailable on this machine (Windows only).";
  else if (online) sub = "Connected — play music on the phone, then go to Now Playing and press Play.";
  else if (bt.discovering) sub = "Searching nearby phones — put the phone in pairing mode…";
  else if (bt.scanning) sub = "Refreshing paired audio phones…";
  el("statusSub").textContent = sub;
  el("disconnectBtn").hidden = !online || Boolean(connectingId) || Boolean(pairingId);

  const scanBtn = el("scanBtn");
  const busyBt = Boolean(bt.scanning) || Boolean(bt.discovering) || Boolean(connectingId) || Boolean(pairingId);
  scanBtn.classList.toggle("scanning", Boolean(bt.scanning));
  scanBtn.disabled = busyBt;
  el("scanLabel").textContent = bt.scanning ? "Scanning…" : "Scan";
  const addBtn = el("addPhoneBtn");
  if (addBtn) {
    addBtn.disabled = busyBt;
    addBtn.textContent = bt.discovering ? "Finding…" : "Find nearby";
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
  const busy = Boolean(connectingId) || Boolean(pairingId);

  if (scanning && !devices.length) {
    const li = document.createElement("li");
    li.className = "device empty scanning";
    li.innerHTML = `<div class="device-meta"><span class="device-name">Scanning…</span><span class="device-sub">Listing paired phones…</span></div>`;
    list.appendChild(li);
    return;
  }

  if (!devices.length) {
    const li = document.createElement("li");
    li.className = "device empty";
    const msg = "No paired phones yet — use Find nearby below, then Pair.";
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
    li.innerHTML = `<div class="device-meta"><span class="device-name">Finding nearby…</span><span class="device-sub">Put the phone in pairing / discoverable mode (~20s).</span></div>`;
    list.appendChild(li);
    return;
  }

  if (!devices.length) {
    const li = document.createElement("li");
    li.className = "device empty";
    const msg = s.bluetooth.error && !s.devices?.length
      ? s.bluetooth.error
      : "Tap Find nearby. Confirm on the phone when Windows/phone prompts appear.";
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
      action = '<span class="badge">Scan above</span>';
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

  el("scanBtn").addEventListener("click", async () => {
    if (el("scanBtn").disabled) return;
    lastDevicesSig = "";
    render(await api.scan_bluetooth());
  });
  el("addPhoneBtn").addEventListener("click", async () => {
    if (el("addPhoneBtn").disabled) return;
    lastNearbySig = "";
    render(await api.discover_nearby());
  });
  el("disconnectBtn").addEventListener("click", async () => {
    lastDevicesSig = "";
    render(await api.disconnect_device());
  });
}

// -------------------------------------------------------------------- boot --
async function boot() {
  bindApi();
  wire();
  render(await api.get_state());
  setInterval(async () => {
    if (!seeking) render(await api.get_state());
  }, 400);
}

window.addEventListener("pywebviewready", bindApi);
window.addEventListener("DOMContentLoaded", boot);

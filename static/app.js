const $ = (id) => document.getElementById(id);

const state = {
  sessionId: localStorage.getItem("shotgun_sid") || "",
  lastFix: null,
  prevFix: null,
  lastText: "",
  mode: "quiet_watch",
  watchId: null,
  lastTick: 0,
};

function toRad(d) {
  return (d * Math.PI) / 180;
}

function bearingFrom(a, b) {
  const p1 = toRad(a.lat);
  const p2 = toRad(b.lat);
  const dl = toRad(b.lon - a.lon);
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) * 180) / Math.PI + 360;
}

function metersBetween(a, b) {
  const r = 6371000;
  const dphi = toRad(b.lat - a.lat);
  const dl = toRad(b.lon - a.lon);
  const h =
    Math.sin(dphi / 2) ** 2 +
    Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.min(1, Math.sqrt(h)));
}

if ($("key")) $("key").value = localStorage.getItem("shotgun_key") || "";
if ($("base")) $("base").value = localStorage.getItem("shotgun_base") || "https://api.x.ai/v1";
if ($("model")) $("model").value = localStorage.getItem("shotgun_model") || "grok-4-fast";

function persistKeys() {
  localStorage.setItem("shotgun_key", $("key").value.trim());
  localStorage.setItem("shotgun_base", $("base").value.trim());
  localStorage.setItem("shotgun_model", $("model").value.trim());
}

["key", "base", "model"].forEach((id) => {
  $(id).addEventListener("change", persistKeys);
});

function addBubble(who, text) {
  if (!text) return;
  const el = document.createElement("div");
  el.className = "bubble " + (who === "You" ? "you" : "sys");
  el.innerHTML = `<span class="who">${who}</span>${escapeHtml(text)}`;
  $("transcript").prepend(el);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function speak(text) {
  state.lastText = text;
  if (!window.speechSynthesis || !text) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.02;
  u.pitch = 1;
  window.speechSynthesis.speak(u);
}

async function turn(payload) {
  persistKeys();
  const body = {
    session_id: state.sessionId || undefined,
    lat: state.lastFix && state.lastFix.lat,
    lon: state.lastFix && state.lastFix.lon,
    heading: state.lastFix && state.lastFix.heading,
    speed: state.lastFix && state.lastFix.speed,
    destination: $("dest").value.trim(),
    interests: $("interests").value.split(",").map((s) => s.trim()).filter(Boolean),
    mode: state.mode,
    llm_key: $("key").value.trim(),
    llm_base: $("base").value.trim(),
    llm_model: $("model").value.trim(),
    ...payload,
  };
  const res = await fetch("/api/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.session_id) {
    state.sessionId = data.session_id;
    localStorage.setItem("shotgun_sid", data.session_id);
  }
  if (data.mode) {
    state.mode = data.mode;
    $("mode").textContent = "Mode: " + data.mode.replace("_", " ");
  }
  $("llm").textContent = data.llm_configured
    ? "LLM: configured" + (data.llm_error ? " (last call failed)" : "")
    : "LLM: not set — geo-only answers";
  if (data.geo_label) {
    const f = $("fix").textContent;
    if (!f.includes(data.geo_label)) {
      $("fix").textContent = (f.split(" · ")[0] || f) + " · " + data.geo_label;
    }
  }
  if (data.speak && data.text) {
    addBubble("Shotgun", data.text);
    speak(data.text);
  }
  return data;
}

function onPos(pos) {
  const c = pos.coords;
  const next = {
    lat: c.latitude,
    lon: c.longitude,
    heading: Number.isFinite(c.heading) ? c.heading : null,
    speed: Number.isFinite(c.speed) ? c.speed : 0,
  };
  if (next.heading == null && state.lastFix && metersBetween(state.lastFix, next) >= 12) {
    next.heading = bearingFrom(state.lastFix, next) % 360;
    next.headingDerived = true;
  } else if (next.heading == null && state.lastFix && state.lastFix.heading != null) {
    next.heading = state.lastFix.heading;
    next.headingDerived = true;
  }
  state.prevFix = state.lastFix;
  state.lastFix = next;
  const bits = [
    next.lat.toFixed(5) + ", " + next.lon.toFixed(5),
    next.heading != null
      ? Math.round(next.heading) + "°" + (next.headingDerived ? " est" : "")
      : "no heading yet — keep walking",
    Number.isFinite(c.speed) ? Math.round(c.speed * 2.237) + " mph" : "",
  ].filter(Boolean);
  $("fix").textContent = bits.join(" · ");
  const now = Date.now();
  if (state.sessionId && now - state.lastTick > 20000) {
    state.lastTick = now;
    turn({ action: "tick" }).catch((err) => console.warn(err));
  }
}

function onErr(err) {
  $("fix").textContent = "GPS error: " + err.message;
}

function startWatch() {
  if (!navigator.geolocation) {
    $("fix").textContent = "No geolocation on this browser";
    return;
  }
  if (state.watchId != null) navigator.geolocation.clearWatch(state.watchId);
  state.watchId = navigator.geolocation.watchPosition(onPos, onErr, {
    enableHighAccuracy: true,
    maximumAge: 5000,
    timeout: 20000,
  });
}

$("start").addEventListener("click", async () => {
  startWatch();
  addBubble("You", "Start trip" + ($("dest").value ? " — " + $("dest").value : ""));
  try {
    await turn({ action: "open" });
  } catch (e) {
    addBubble("Shotgun", "Could not reach the local server. " + e.message);
  }
});

$("flag").addEventListener("click", async () => {
  state.mode = "flag_along";
  $("mode").textContent = "Mode: flag along";
  addBubble("You", "Flag things along the way.");
  await turn({ action: "ask", user_text: "Flag things along the way." });
});

$("speakLast").addEventListener("click", () => speak(state.lastText));

$("downloadLog").addEventListener("click", async () => {
  if (!state.sessionId) {
    addBubble("Shotgun", "Start a trip first.");
    return;
  }
  try {
    const res = await fetch("/api/log?session_id=" + encodeURIComponent(state.sessionId));
    const data = await res.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "shotgun-trip-" + state.sessionId.slice(0, 8) + ".json";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    addBubble("Shotgun", "Could not download the trip log. " + e.message);
  }
});

const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
let recArmed = false;

function setMicStatus(text, listening) {
  if ($("micStatus")) $("micStatus").textContent = text;
  if ($("mic")) $("mic").classList.toggle("listening", !!listening);
}

function sendUtterance(t) {
  const text = (t || "").trim();
  if (!text) return;
  addBubble("You", text);
  $("utter").value = "";
  if (!state.sessionId) startWatch();
  return turn({ action: "ask", user_text: text });
}

if (SpeechRec) {
  rec = new SpeechRec();
  rec.lang = "en-US";
  rec.interimResults = false;
  rec.maxAlternatives = 1;
  rec.continuous = false;
  rec.onstart = () => setMicStatus("Listening… tap Mic again to stop.", true);
  rec.onerror = (e) => {
    recArmed = false;
    setMicStatus("Mic error: " + (e.error || "unknown") + ". Use the box + Ask if needed.", false);
  };
  rec.onend = () => {
    recArmed = false;
    setMicStatus("Tap Mic, speak, it sends when you pause.", false);
  };
  rec.onresult = (ev) => {
    const said = ev.results && ev.results[0] && ev.results[0][0] && ev.results[0][0].transcript;
    if (said) sendUtterance(said);
  };
  $("mic").addEventListener("click", () => {
    if (recArmed) {
      try { rec.stop(); } catch (_) {}
      recArmed = false;
      return;
    }
    try {
      window.speechSynthesis && window.speechSynthesis.cancel();
      rec.start();
      recArmed = true;
    } catch (e) {
      setMicStatus("Could not start mic: " + e.message, false);
    }
  });
} else {
  setMicStatus("This browser has no speech input. Type, or use the keyboard mic, then Ask.", false);
  if ($("mic")) $("mic").disabled = true;
}

$("ask").addEventListener("click", async () => {
  const t = $("utter").value.trim();
  if (!t) return;
  await sendUtterance(t);
});

startWatch();

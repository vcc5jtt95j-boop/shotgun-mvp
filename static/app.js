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

$("ask").addEventListener("click", async () => {
  const t = $("utter").value.trim();
  if (!t) return;
  addBubble("You", t);
  $("utter").value = "";
  if (!state.sessionId) startWatch();
  await turn({ action: "ask", user_text: t });
});

$("speakLast").addEventListener("click", () => speak(state.lastText));

startWatch();

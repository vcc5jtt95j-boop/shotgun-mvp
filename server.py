#!/usr/bin/env python3
"""Shotgun MVP slice server. Baseline v0.1 §8."""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOG_DIR = ROOT / "logs"
SESS_DIR = ROOT / "sessions"
LOG_DIR.mkdir(exist_ok=True)
SESS_DIR.mkdir(exist_ok=True)

PORT = int(os.environ.get("PORT") or os.environ.get("SHOTGUN_PORT", "8765"))
LLM_KEY = os.environ.get("SHOTGUN_LLM_KEY", "").strip()
LLM_BASE = os.environ.get("SHOTGUN_LLM_BASE", "https://api.x.ai/v1").rstrip("/")
LLM_MODEL = os.environ.get("SHOTGUN_LLM_MODEL", "grok-4-fast")

OPENER = "What's on the agenda today?"
CHECKIN = "Still with you. Anything you want to know about this stretch?"

SYSTEM_PROMPT = """You are Shotgun, a voice-first road companion.
Follow these rules with no exceptions:
- Named, concrete, correctable. Hedge uncertainty in the same sentence.
- Never describe colors, shapes, or camera-like detail unless it is in the geo/nearby data or the user said it.
- Never defend an earlier error. If corrected, accept and move on.
- Do not repeat facts already listed in already_said.
- User question beats any proactive impulse.
- No source lists, no "as an AI", no tour-tape cadence.
- If you cannot identify something, say so and offer to look further.

Length (Baseline §5.5):
- Unasked identification or first name of a place: one short beat, 8-15 seconds. Then you may offer one door. Stop.
- After the user engages that place (tell me more, history, color, follow-up on the same thread): a short PACKET — what it is, one history beat, one color beat — about 20-30 seconds. Or two/three spoken doors ("history, film, or the building?"). Do not drip one date and wait for another ask.
- "That's enough" / quiet: stop immediately.

Drill-down (critical):
- GPS/nearby data is only for WHERE the user is and WHAT the candidate place is called.
- Once a place is named (Hotel del Coronado, Downtown Coronado, etc.), you MUST use ordinary well-known facts about that named place: history, who built it, why it is famous, what happened there.
- "Tell me more" / history / interesting facts is never answered with "I have no more information" if the place is a known named entity.
- Do not replay the identification.
- Visual invention is still forbidden. Encyclopedia-style facts about a named place are required.

Motion (critical):
- MOTION in the payload is live movement: heading degrees, compass name, speed, road if known.
- If the user asks which way they are heading or going, answer from MOTION. Do not say you have no information when compass or heading_deg is present.
- If heading_source is track, say you inferred it from recent movement.
- If MOTION has no heading, say you need them to keep moving a bit so the track can settle — not that you know nothing about the place.

Return JSON only: {"speak": "...", "confidence": "high|medium|low", "entity_name": "... or empty", "entity_status": "candidate|confirmed|rejected|none"}
"""


def haversine_m(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(h)))


def heading_delta(a, b):
    if a is None or b is None:
        return 0
    d = abs((b - a + 180) % 360 - 180)
    return d


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass_name(deg):
    if deg is None:
        return None
    pts = [
        "north", "northeast", "east", "southeast",
        "south", "southwest", "west", "northwest",
    ]
    return pts[int((deg + 22.5) % 360) // 45]


def is_direction_question(text: str) -> bool:
    t = (text or "").lower()
    keys = (
        "what direction", "which direction", "which way", "what way",
        "heading", "am i going", "where am i going", "which way am i",
        "north", "south", "east", "west",
    )
    return any(k in t for k in keys) and any(
        w in t for w in ("direction", "heading", "way", "going", "am i")
    )


def motion_from(sess, lat, lon, heading, speed):
    last = sess.get("last_fix") or {}
    derived = None
    dist = None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and last.get("lat") is not None:
        dist = haversine_m((last["lat"], last["lon"]), (lat, lon))
        if dist >= 12:
            derived = bearing_deg(last["lat"], last["lon"], lat, lon)
    use = heading if heading is not None else derived
    return {
        "heading_deg": use,
        "heading_source": "device" if heading is not None else ("track" if derived is not None else None),
        "compass": compass_name(use),
        "speed_mps": speed or 0,
        "speed_mph": round((speed or 0) * 2.237, 1),
        "moved_meters": dist,
        "road": ((last.get("geo") or {}).get("road")),
    }


def load_session(sid: str) -> dict:
    path = SESS_DIR / f"{sid}.json"
    if path.exists():
        return json.loads(path.read_text())
    data = {
        "id": sid,
        "created": time.time(),
        "mode": "quiet_watch",
        "interests": [],
        "destination": "",
        "said": [],
        "entities": [],
        "last_speak_ts": 0,
        "last_pulse_ts": 0,
        "last_fix": None,
        "started": False,
    }
    save_session(data)
    return data


def save_session(data: dict):
    path = SESS_DIR / f"{data['id']}.json"
    path.write_text(json.dumps(data, indent=2))


def append_log(sid: str, event: dict):
    event = dict(event)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    event["session"] = sid
    line = json.dumps(event, ensure_ascii=False)
    with (LOG_DIR / f"{sid}.jsonl").open("a") as f:
        f.write(line + "\n")


def http_json(method: str, url: str, headers=None, data=None, timeout=10):
    hdrs = {"User-Agent": "ShotgunMVP/0.1 (field test)"}
    if headers:
        hdrs.update(headers)
    body = data if isinstance(data, (bytes, type(None))) else data
    req = Request(url, data=body, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url}") from e
    except URLError as e:
        raise RuntimeError(str(e.reason or e)) from e


def reverse_geocode(lat: float, lon: float) -> dict:
    params = urlencode({"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14, "addressdetails": 1})
    url = "https://nominatim.openstreetmap.org/reverse?" + params
    try:
        data = http_json("GET", url, timeout=8)
    except Exception as e:
        return {"ok": False, "error": str(e), "label": "", "raw": {}}
    addr = data.get("address") or {}
    parts = [
        addr.get("natural"),
        addr.get("peak"),
        addr.get("water"),
        addr.get("village") or addr.get("town") or addr.get("city") or addr.get("hamlet"),
        addr.get("county"),
        addr.get("state"),
        addr.get("country"),
    ]
    label = ", ".join(p for p in parts if p)
    return {
        "ok": True,
        "label": label or data.get("display_name", ""),
        "name": data.get("name") or "",
        "category": data.get("category"),
        "type": data.get("type"),
        "road": addr.get("road") or addr.get("highway"),
        "raw_address": addr,
    }


def nearby_hints(lat: float, lon: float) -> list[str]:
    """Coarse Overpass peek: named peaks, water, towns within ~3km. Best-effort."""
    q = f"""
    [out:json][timeout:8];
    (
      node["place"~"town|city|village|hamlet"](around:3000,{lat},{lon});
      node["natural"~"peak|ridge|bay|beach|cliff"](around:4000,{lat},{lon});
      way["waterway"~"river"](around:3000,{lat},{lon});
    );
    out tags center 12;
    """
    try:
        data = http_json(
            "POST",
            "https://overpass-api.de/api/interpreter",
            headers={"Content-Type": "text/plain"},
            data=q.encode(),
            timeout=10,
        )
        els = data.get("elements") or []
    except Exception:
        return []
    names = []
    for el in els:
        tags = el.get("tags") or {}
        n = tags.get("name")
        if n and n not in names:
            kind = tags.get("place") or tags.get("natural") or tags.get("waterway") or ""
            names.append(f"{n} ({kind})" if kind else n)
        if len(names) >= 8:
            break
    return names


def path_changed(sess: dict, fix: dict) -> bool:
    last = sess.get("last_fix") or {}
    if not last:
        return False
    hd = heading_delta(last.get("heading"), fix.get("heading"))
    road_now = (fix.get("geo") or {}).get("road")
    road_then = (last.get("geo") or {}).get("road")
    if road_now and road_then and road_now != road_then:
        return True
    if hd >= 50 and (fix.get("speed") or 0) > 3:
        return True
    return False


def due_time_pulse(sess: dict) -> bool:
    now = time.time()
    if not sess.get("started"):
        return False
    if now - sess.get("last_speak_ts", 0) < 25 * 60:
        return False
    if now - sess.get("last_pulse_ts", 0) < 28 * 60:
        return False
    return True


def already_said_text(sess: dict) -> str:
    return " | ".join(sess.get("said", [])[-12:])


def call_llm(user_payload: dict, key: str, base: str, model: str) -> dict | None:
    if not key:
        return None
    body = {
        "model": model,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    url = f"{base}/chat/completions"
    try:
        data = http_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            data=json.dumps(body).encode(),
            timeout=30,
        )
        text = data["choices"][0]["message"]["content"]
    except Exception as e:
        return {"speak": None, "error": str(e)}
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        return {"speak": text[:400], "confidence": "medium", "entity_name": "", "entity_status": "none"}


def fallback_speak(trigger: str, geo: dict, user_text: str) -> dict:
    label = geo.get("label") or "this stretch"
    if trigger == "open":
        return {"speak": OPENER, "confidence": "high", "entity_name": "", "entity_status": "none"}
    if trigger == "time-pulse":
        return {"speak": CHECKIN, "confidence": "high", "entity_name": "", "entity_status": "none"}
    if trigger == "path-pulse":
        return {
            "speak": f"Looks like the corridor changed. We're around {label}. Say if you want anything on it.",
            "confidence": "medium",
            "entity_name": label,
            "entity_status": "candidate",
        }
    if user_text:
        return {
            "speak": f"We're around {label}. I can go deeper if you tell me what you want — name, history, or why it's built this way.",
            "confidence": "low",
            "entity_name": label,
            "entity_status": "candidate",
        }
    return {
        "speak": f"Something notable near {label}. Ask if you want the name or the background.",
        "confidence": "low",
        "entity_name": label,
        "entity_status": "candidate",
    }


def apply_entity(sess: dict, result: dict, lat, lon):
    name = (result.get("entity_name") or "").strip()
    if not name or result.get("entity_status") in (None, "none"):
        return
    for ent in sess["entities"]:
        if ent["name"].lower() == name.lower():
            ent["status"] = result.get("entity_status") or ent["status"]
            ent["updated"] = time.time()
            return
    sess["entities"].append(
        {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "lat": lat,
            "lon": lon,
            "status": result.get("entity_status") or "candidate",
            "confidence": result.get("confidence") or "medium",
            "created": time.time(),
        }
    )


def handle_turn(body: dict) -> dict:
    sid = body.get("session_id") or str(uuid.uuid4())
    sess = load_session(sid)
    lat = body.get("lat")
    lon = body.get("lon")
    heading = body.get("heading")
    speed = body.get("speed") or 0
    user_text = (body.get("user_text") or "").strip()
    destination = (body.get("destination") or "").strip()
    interests = body.get("interests") or sess.get("interests") or []
    mode = body.get("mode") or sess.get("mode") or "quiet_watch"
    action = body.get("action") or "tick"
    key = (body.get("llm_key") or LLM_KEY).strip()
    base = (body.get("llm_base") or LLM_BASE).rstrip("/")
    model = body.get("llm_model") or LLM_MODEL

    if destination:
        sess["destination"] = destination
    if interests:
        sess["interests"] = interests
    sess["mode"] = mode

    if heading is not None:
        try:
            heading = float(heading)
        except (TypeError, ValueError):
            heading = None
    try:
        speed = float(speed or 0)
    except (TypeError, ValueError):
        speed = 0

    motion = motion_from(sess, lat, lon, heading, speed)
    if motion.get("heading_deg") is not None and heading is None:
        heading = motion["heading_deg"]

    geo = {}
    nearby = []
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        geo = reverse_geocode(lat, lon)
        if action in ("ask", "open") or user_text:
            nearby = nearby_hints(lat, lon)
        motion["road"] = geo.get("road") or motion.get("road")
        motion["place"] = geo.get("label") or geo.get("name")

    trigger = "none"
    speak = False

    if action == "open" or not sess.get("started"):
        trigger = "open"
        speak = True
        sess["started"] = True
    elif user_text or action == "ask":
        trigger = "user"
        speak = True
        low = user_text.lower()
        if any(p in low for p in ("quiet", "stop talking", "that's enough", "thats enough", "pause")):
            sess["mode"] = "quiet_watch"
        if "more interesting" in low or "flag things" in low or "tell me as we go" in low:
            sess["mode"] = "flag_along"
    else:
        fix = {"lat": lat, "lon": lon, "heading": heading, "speed": speed, "geo": geo}
        if sess["mode"] == "quiet_watch":
            if path_changed(sess, fix):
                trigger = "path-pulse"
                speak = True
            elif due_time_pulse(sess):
                trigger = "time-pulse"
                speak = True
        elif sess["mode"] == "flag_along":
            if path_changed(sess, fix) or due_time_pulse(sess):
                trigger = "path-pulse" if path_changed(sess, fix) else "time-pulse"
                speak = True

    result = {"speak": "", "confidence": "medium", "entity_name": "", "entity_status": "none"}
    llm_error = None

    if speak:
        payload = {
            "trigger": trigger,
            "user_text": user_text,
            "mode": sess["mode"],
            "destination": sess.get("destination"),
            "interests": sess.get("interests"),
            "motion": motion,
            "geo": {k: geo.get(k) for k in ("label", "name", "road", "type", "category") if geo},
            "nearby": nearby,
            "already_said": sess.get("said", [])[-12:],
            "entities": sess.get("entities", [])[-20:],
        }
        if is_direction_question(user_text):
            comp = motion.get("compass")
            deg = motion.get("heading_deg")
            place = motion.get("place") or geo.get("label") or "this stretch"
            if comp and deg is not None:
                src = "from how you've been moving" if motion.get("heading_source") == "track" else "from the heading reading"
                result = {
                    "speak": f"You're heading {comp}, about {int(round(deg))} degrees, through {place}.",
                    "confidence": "high" if motion.get("heading_source") == "device" else "medium",
                    "entity_name": "",
                    "entity_status": "none",
                }
            else:
                result = {
                    "speak": "I have your position but not a settled heading yet. Keep moving a little and ask again.",
                    "confidence": "low",
                    "entity_name": "",
                    "entity_status": "none",
                }
            llm = None
        else:
            llm = call_llm(payload, key, base, model) if trigger != "open" else None
            if trigger == "open":
                result = fallback_speak("open", geo, user_text)
            elif llm and llm.get("speak"):
                result = llm
            else:
                if llm and llm.get("error"):
                    llm_error = llm["error"]
                result = fallback_speak(trigger, geo, user_text)
        text = (result.get("speak") or "").strip()
        if text:
            sess["said"].append(text)
            sess["last_speak_ts"] = time.time()
            if trigger in ("time-pulse", "path-pulse"):
                sess["last_pulse_ts"] = time.time()
            apply_entity(sess, result, lat, lon)

    if isinstance(lat, (int, float)):
        sess["last_fix"] = {
            "lat": lat,
            "lon": lon,
            "heading": heading,
            "speed": speed,
            "geo": {"road": geo.get("road"), "label": geo.get("label")},
            "t": time.time(),
        }
    save_session(sess)

    if speak or user_text:
        append_log(
            sid,
            {
                "trigger": trigger,
                "lat": lat,
                "lon": lon,
                "heading": heading,
                "speed": speed,
                "user_text": user_text,
                "system_text": result.get("speak") if speak else "",
                "confidence": result.get("confidence"),
                "entity_name": result.get("entity_name"),
                "grounding": geo.get("label") if geo else "",
                "nearby": nearby,
                "mode": sess["mode"],
                "llm_error": llm_error,
            },
        )

    return {
        "session_id": sid,
        "speak": speak,
        "trigger": trigger,
        "text": result.get("speak") if speak else "",
        "confidence": result.get("confidence") if speak else None,
        "geo_label": geo.get("label") if geo else "",
        "mode": sess["mode"],
        "entities": sess["entities"],
        "llm_configured": bool(key),
        "llm_error": llm_error,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt, *args):
        print("[http]", self.address_string(), fmt % args)

    def _json(self, code: int, obj: dict):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/log":
            return super().do_GET()
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query or "")
        sid = (qs.get("session_id") or [""])[0].strip()
        if not sid or "/" in sid or ".." in sid:
            self._json(400, {"error": "need session_id"})
            return
        path = LOG_DIR / f"{sid}.jsonl"
        events = []
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    events.append({"raw": line})
        self._json(200, {"session_id": sid, "events": events})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/turn":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        try:
            out = handle_turn(body)
            self._json(200, out)
        except Exception as e:
            self._json(500, {"error": str(e)})


def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Shotgun MVP slice at http://0.0.0.0:{PORT}")
    print(f"Logs: {LOG_DIR}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

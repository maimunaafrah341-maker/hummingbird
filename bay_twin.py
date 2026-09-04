"""
Bay Twin -- the floor plan, drawing itself from what the system decides.

Everything this project does well is currently invisible. The 3-of-8
gate eats 1798 of 1800 detections and prints one line. The confidence
router holds a borderline box, watches it fail to reconfirm, and
suppresses it -- one line. The escalation timer runs for twenty seconds
in a daemon thread and nobody sees the clock.

So this is a live top-down view of the bays, driven by an event stream,
showing three things:

  RED     an incident fired, with the acknowledgement window counting
          down on the tile itself
  BLACK   nobody acknowledged it, and the system escalated on its own
  AMBER   a borderline detection was held and then suppressed

That third one is the point. A red tile is YOLO working, and every team
in the room will have one. An amber dot appearing, hesitating and
fading is this system *deciding not to cry wolf* -- which is the part
that took the work and the part nobody can otherwise see.

## Shape

An in-process EventBus with a replay buffer, exposed three ways:

    GET  /twin           the page
    GET  /twin/stream    server-sent events
    POST /twin/event     ingest, for the trigger running in another
                         process

The replay buffer matters: a page opened halfway through a demo shows
the state of the room, not an empty grid.

## The rule this module lives under

**Telemetry must never be able to affect the safety path.** A browser
that stops reading, a twin process that is not running, a network that
drops -- none of it may slow down or fail a detection. So every publish
is non-blocking with a bounded queue that drops rather than waits, the
remote Emitter runs on its own daemon thread with a short timeout, and
every send swallows every exception. If the twin breaks, the system
keeps working and simply is not being watched.

Run it:

    python bay_twin.py --demo             # scripted stream, no camera
    python bay_twin.py --serve 8010       # standalone, then open /twin
    python bay_twin.py --selftest
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
from collections import deque

try:
    import requests
except ImportError:
    requests = None

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import HTMLResponse, StreamingResponse
    WEB_AVAILABLE = True
except ImportError:
    WEB_AVAILABLE = False


# How many recent events a newly-opened page receives before it starts
# following live. Enough to reconstruct the room, small enough that a
# long demo does not replay from the beginning.
REPLAY_LIMIT = int(os.getenv("HAZARDWATCH_TWIN_REPLAY", "200"))

# Per-subscriber queue depth. A browser that stops reading fills this
# and then loses events -- deliberately, because the alternative is
# blocking the thread that detected a hazard.
SUBSCRIBER_QUEUE = 512

# Comment frame sent when nothing else has been, so an idle connection
# is not killed by a proxy that considers it dead.
KEEPALIVE_SECONDS = 15.0

# How often the SSE generator drains its queue. Not a latency budget
# anyone can perceive; it exists so the generator can also notice a
# disconnect and stop.
POLL_SECONDS = 0.1

KINDS = ("incident", "decision", "ack", "escalation", "note")


# ============================================================
# THE BUS
# ============================================================

class EventBus(object):
    """
    Fan-out with a memory.

    Thread-safe because publishers are the camera loop, timer threads
    and FastAPI workers, all at once, while subscribers come and go
    with browser tabs.
    """

    def __init__(self, replay=REPLAY_LIMIT):
        self._lock = threading.Lock()
        self._subscribers = []
        self._replay = deque(maxlen=replay)
        self._seq = 0
        self.dropped = 0
        self.counts = {}

    def publish(self, kind, **fields):
        """
        Record an event and hand it to every subscriber. Never blocks,
        never raises, and returns the event it built.
        """

        event = dict(fields)
        event["kind"] = kind

        with self._lock:
            self._seq += 1
            event["seq"] = self._seq
            event.setdefault("at", time.time())
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self._replay.append(event)
            subscribers = list(self._subscribers)

        for sub in subscribers:
            try:
                sub.put_nowait(event)
            except queue.Full:
                # A tab that stopped reading. Drop the event and keep
                # going -- see the module docstring.
                self.dropped += 1

        return event

    def subscribe(self):
        """Returns (queue, replay-snapshot). Always unsubscribe after."""

        sub = queue.Queue(maxsize=SUBSCRIBER_QUEUE)

        with self._lock:
            history = list(self._replay)
            self._subscribers.append(sub)

        return sub, history

    def unsubscribe(self, sub):
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def snapshot(self):
        with self._lock:
            return {
                "events": self._seq,
                "subscribers": len(self._subscribers),
                "dropped": self.dropped,
                "counts": dict(self.counts),
            }

    def reset(self):
        """For tests. Not called by anything that runs in a demo."""

        with self._lock:
            self._replay.clear()
            self._seq = 0
            self.dropped = 0
            self.counts = {}


BUS = EventBus()


# ============================================================
# SHAPING -- what each kind of event carries
# ============================================================
#
# One function per event kind rather than callers building dicts, so
# the page's contract with the system is written down in exactly one
# place and a field rename cannot half-happen.

def emit_incident(event, response=None, incident_id=None, ack_window=None):
    """An incident was opened. `event` is the trigger's wire payload."""

    response = response or {}

    return BUS.publish(
        "incident",
        incident_id=incident_id or event.get("incident_id"),
        zone=event.get("bay_id"),
        violation=event.get("incident_type"),
        source=event.get("source"),
        severity=(response.get("severity") or "high").lower(),
        confidence=event.get("confidence"),
        substance=event.get("substance_name") or event.get("substance_code"),
        spoken=response.get("spoken_alert"),
        contraindication=response.get("contraindication"),
        ack_window=ack_window,
    )


def emit_decision(decision):
    """
    A confidence-routing decision. Accepts a Decision or a plain dict,
    so confidence_router does not have to import this module.
    """

    data = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)

    return BUS.publish(
        "decision",
        zone=data.get("zone"),
        violation=data.get("violation"),
        confidence=data.get("confidence"),
        tier=data.get("tier"),
        action=data.get("action"),
        reason=data.get("reason"),
    )


def emit_ack(incident_id, by="kiosk", zone=None, elapsed=None):
    return BUS.publish("ack", incident_id=incident_id, by=by,
                       zone=zone, elapsed=elapsed)


def emit_escalation(incident_id, zone=None, elapsed=None, route=None):
    return BUS.publish("escalation", incident_id=incident_id, zone=zone,
                       elapsed=elapsed, route=route)


def emit_note(text, zone=None):
    """Free text for the rail. Used by --demo to narrate itself."""

    return BUS.publish("note", text=text, zone=zone)


def ingest(payload):
    """
    Accept an event that arrived over HTTP from another process.

    The kind is validated against KINDS, and `seq`/`at` are stripped so
    a remote process cannot forge this bus's ordering.
    """

    if not isinstance(payload, dict):
        raise ValueError("event must be an object")

    kind = payload.get("kind")

    if kind not in KINDS:
        raise ValueError("unknown kind %r (expected one of %s)"
                         % (kind, ", ".join(KINDS)))

    fields = {k: v for k, v in payload.items()
              if k not in ("kind", "seq", "at")}

    return BUS.publish(kind, **fields)


# ============================================================
# THE REMOTE EMITTER -- used by the trigger process
# ============================================================

class Emitter(object):
    """
    Fire-and-forget telemetry to a Bay Twin in another process.

    The camera loop calls send() and returns immediately; a single
    daemon thread does the POST. The queue is bounded and drops when
    full, the timeout is short, and every exception is swallowed. This
    class is not allowed to be the reason a hazard went unreported.
    """

    def __init__(self, url, timeout=1.5, depth=256):
        self.url = url.rstrip("/")

        if not self.url.endswith("/twin/event"):
            self.url = self.url + "/twin/event"

        self.timeout = timeout
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if payload is None:
                break

            try:
                requests.post(self.url, json=payload, timeout=self.timeout)
                self.sent += 1
            except Exception:
                # Deliberately silent. A twin that is not running is a
                # normal state, and printing a stack trace per frame
                # would bury the log the operator actually reads.
                self.failed += 1

    def send(self, kind, **fields):
        if requests is None or self._stop.is_set():
            return False

        payload = dict(fields)
        payload["kind"] = kind

        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    # Same names as the local emitters, so a caller can hold either one.

    def decision(self, decision):
        data = decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
        return self.send("decision", **data)

    def incident(self, event, response=None, incident_id=None, ack_window=None):
        response = response or {}
        return self.send(
            "incident",
            incident_id=incident_id or event.get("incident_id"),
            zone=event.get("bay_id"),
            violation=event.get("incident_type"),
            source=event.get("source"),
            severity=(response.get("severity") or "high").lower(),
            confidence=event.get("confidence"),
            substance=event.get("substance_name") or event.get("substance_code"),
            spoken=response.get("spoken_alert"),
            ack_window=ack_window,
        )

    def ack(self, incident_id, by="kiosk", zone=None, elapsed=None):
        return self.send("ack", incident_id=incident_id, by=by,
                         zone=zone, elapsed=elapsed)

    def escalation(self, incident_id, zone=None, elapsed=None, route=None):
        return self.send("escalation", incident_id=incident_id, zone=zone,
                         elapsed=elapsed, route=route)

    def close(self, timeout=3.0):
        """Let queued telemetry finish. Safe to call more than once."""

        self._stop.set()

        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

        self._worker.join(timeout=timeout)


# ============================================================
# ROUTES
# ============================================================

def _sse(event):
    return "data: %s\n\n" % json.dumps(event, default=str)


def build_router():
    """
    The three routes, as an APIRouter so incident_api can mount them
    without this module knowing anything about that app.
    """

    import asyncio

    router = APIRouter()

    @router.get("/twin", response_class=HTMLResponse)
    def twin_page():
        return HTMLResponse(PAGE)

    @router.get("/twin/state")
    def twin_state():
        return BUS.snapshot()

    @router.post("/twin/event")
    async def twin_event(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return {"ok": False, "error": "body was not JSON"}

        try:
            event = ingest(payload)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        return {"ok": True, "seq": event["seq"]}

    @router.get("/twin/stream")
    async def twin_stream(request: Request):
        sub, history = BUS.subscribe()

        async def generate():
            try:
                for event in history:
                    yield _sse(event)

                # Tells the page the replay is done, so it can stop
                # animating events that already happened and start
                # reacting to live ones.
                yield "event: live\ndata: {}\n\n"

                last = time.monotonic()

                while True:
                    if await request.is_disconnected():
                        break

                    delivered = False

                    while True:
                        try:
                            event = sub.get_nowait()
                        except queue.Empty:
                            break

                        yield _sse(event)
                        delivered = True

                    if delivered:
                        last = time.monotonic()
                    elif time.monotonic() - last > KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        last = time.monotonic()

                    await asyncio.sleep(POLL_SECONDS)

            finally:
                BUS.unsubscribe(sub)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # nginx and several PaaS proxies buffer responses by
                # default, which turns a live stream into one long
                # silence followed by everything at once.
                "X-Accel-Buffering": "no",
            },
        )

    return router


# ============================================================
# THE PAGE
# ============================================================
#
# One file, no CDN, no build step. The demo room's wifi is not allowed
# to be a dependency of the dashboard that watches the demo.

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bay Twin</title>
<style>
:root{
  --ground:#0a0c10; --panel:#12161e; --panel-2:#171c26;
  --rule:#232a37; --rule-soft:#1b212c;
  --text:#c9d1de; --dim:#78829440; --mute:#7c8698;
  --clear:#3fa96b; --hold:#d99a2b; --alarm:#e5484d; --escalate:#f5f7fa;
  --escalate-bg:#3a0a0d;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;background:var(--ground);color:var(--text);
  font-family:var(--sans);font-size:14px;
  display:flex;flex-direction:column;overflow:hidden;
}
.label{
  font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mute);font-weight:600;
}
.num{font-variant-numeric:tabular-nums}

/* ---- header ---------------------------------------------------- */
header{
  display:flex;align-items:stretch;gap:0;
  border-bottom:1px solid var(--rule);background:var(--panel);
  flex:0 0 auto;
}
.brand{padding:14px 20px;border-right:1px solid var(--rule);min-width:210px}
.brand h1{margin:0;font-size:15px;letter-spacing:.02em;font-weight:650}
.brand p{margin:3px 0 0;font-size:11px;color:var(--mute)}
.stats{display:flex;flex:1;min-width:0;overflow-x:auto}
.stat{
  padding:12px 20px;border-right:1px solid var(--rule-soft);
  min-width:132px;flex:0 0 auto;
}
.stat .v{
  font-family:var(--mono);font-size:24px;line-height:1.15;font-weight:600;
  font-variant-numeric:tabular-nums;
}
.stat .k{margin-top:2px}
.stat.sup .v{color:var(--hold)}
.stat.inc .v{color:var(--alarm)}
.stat.esc .v{color:#ff8b8f}
.link{
  display:flex;align-items:center;gap:8px;padding:0 20px;
  border-left:1px solid var(--rule);font-size:11px;color:var(--mute);
}
.dot{width:7px;height:7px;border-radius:50%;background:var(--mute)}
.dot.on{background:var(--clear);box-shadow:0 0 0 3px #3fa96b22}
.dot.off{background:var(--alarm);box-shadow:0 0 0 3px #e5484d22}

/* ---- body ------------------------------------------------------ */
main{display:flex;flex:1;min-height:0}
.floor{
  flex:1;min-width:0;padding:20px;overflow:auto;
  display:grid;gap:14px;align-content:start;
  grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
}
.rail{
  flex:0 0 330px;border-left:1px solid var(--rule);background:var(--panel);
  display:flex;flex-direction:column;min-height:0;
}
.rail h2{
  margin:0;padding:13px 16px;border-bottom:1px solid var(--rule);
  font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--mute);font-weight:600;
}
.log{flex:1;overflow-y:auto;padding:6px 0}
.entry{
  padding:8px 16px;border-bottom:1px solid var(--rule-soft);
  display:grid;grid-template-columns:56px 1fr;gap:10px;
  animation:slide .3s ease;
}
@keyframes slide{from{opacity:0;transform:translateX(8px)}to{opacity:1}}
.entry time{font-family:var(--mono);font-size:11px;color:#5d6779}
.entry .body{min-width:0}
.entry .head{font-size:12px;font-weight:600;margin-bottom:2px}
.entry .sub{font-size:11px;color:var(--mute);line-height:1.45;word-wrap:break-word}
.entry.k-decision .head{color:var(--hold)}
.entry.k-incident .head{color:var(--alarm)}
.entry.k-ack .head{color:var(--clear)}
.entry.k-escalation .head{color:#ff8b8f}
.entry.k-note .head{color:var(--mute);font-weight:500}

/* ---- tiles ----------------------------------------------------- */
.bay{
  position:relative;background:var(--panel);border:1px solid var(--rule);
  border-radius:3px;padding:14px 15px 13px;min-height:132px;
  display:flex;flex-direction:column;
  transition:border-color .25s,background .25s;
}
.bay::before{
  content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--rule);transition:background .25s;
}
.bay .id{
  font-family:var(--mono);font-size:17px;font-weight:600;letter-spacing:.02em;
}
.bay .state{margin-top:3px}
.bay .detail{
  margin-top:auto;padding-top:10px;font-size:12px;color:var(--mute);
  line-height:1.5;min-height:34px;
}
.bay .detail b{color:var(--text);font-weight:600}

.bay.offline{opacity:.5}
.bay.offline .state{color:#5d6779}
.bay.monitoring::before{background:var(--clear)}
.bay.monitoring .state{color:var(--clear)}
.bay.holding::before{background:var(--hold)}
.bay.holding{border-color:#8a6a2288}
.bay.holding .state{color:var(--hold)}
.bay.alarm::before{background:var(--alarm)}
.bay.alarm{border-color:var(--alarm);background:#1c1116}
.bay.alarm .state{color:var(--alarm)}
.bay.alarm .id{color:#ff9ea1}
.bay.acked::before{background:var(--clear)}
.bay.acked{border-color:#2f6b4788}
.bay.acked .state{color:var(--clear)}
.bay.escalated::before{background:var(--escalate)}
.bay.escalated{border-color:#8a1f24;background:var(--escalate-bg)}
.bay.escalated .state{color:#ffd7d8}
.bay.escalated .id{color:#fff}

@keyframes pulse{
  0%,100%{box-shadow:0 0 0 0 #e5484d00}
  50%{box-shadow:0 0 0 5px #e5484d1f}
}
.bay.alarm{animation:pulse 1.4s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){
  .bay.alarm{animation:none}
  .entry{animation:none}
}

/* countdown */
.clock{display:flex;align-items:center;gap:9px;margin-top:9px;height:16px}
.clock .bar{
  flex:1;height:3px;background:#ffffff14;border-radius:2px;overflow:hidden;
}
.clock .fill{height:100%;background:var(--alarm);transition:width .2s linear}
.clock .t{
  font-family:var(--mono);font-size:12px;font-weight:600;color:var(--alarm);
  font-variant-numeric:tabular-nums;
}
.clock.hidden{visibility:hidden}

/* the amber suppression dots -- the whole reason this page exists */
.holds{
  position:absolute;top:13px;right:13px;display:flex;gap:5px;
  flex-direction:row-reverse;pointer-events:none;
}
.hold{
  width:8px;height:8px;border-radius:50%;background:var(--hold);
  box-shadow:0 0 0 3px #d99a2b22;
}
.hold.fading{animation:fade 2.4s ease forwards}
@keyframes fade{
  0%{opacity:1;transform:scale(1)}
  35%{opacity:1;transform:scale(1)}
  100%{opacity:0;transform:scale(.55)}
}
.hold.confirmed{background:var(--alarm);box-shadow:0 0 0 3px #e5484d22}

.ackbtn{
  margin-top:9px;width:100%;padding:7px 0;border:1px solid #6b2b2e;
  background:#2a1418;color:#ff9ea1;border-radius:2px;cursor:pointer;
  font-family:var(--sans);font-size:10px;font-weight:700;
  letter-spacing:.13em;text-transform:uppercase;
}
.ackbtn:hover{background:#3a181d}
.ackbtn:focus-visible{outline:2px solid var(--alarm);outline-offset:2px}
.ackbtn.hidden{display:none}

.empty{
  grid-column:1/-1;padding:60px 20px;text-align:center;color:var(--mute);
  font-size:13px;line-height:1.7;
}
.empty code{
  font-family:var(--mono);color:var(--text);background:var(--panel-2);
  padding:2px 6px;border-radius:2px;
}
</style>
</head>
<body>

<header>
  <div class="brand">
    <h1>Bay Twin</h1>
    <p>HazardWatch OS &middot; live floor</p>
  </div>
  <div class="stats">
    <div class="stat sup"><div class="v num" id="s-sup">0</div><div class="k label">Suppressed</div></div>
    <div class="stat"><div class="v num" id="s-hold">0</div><div class="k label">Verifying now</div></div>
    <div class="stat inc"><div class="v num" id="s-inc">0</div><div class="k label">Incidents raised</div></div>
    <div class="stat esc"><div class="v num" id="s-esc">0</div><div class="k label">Escalated</div></div>
    <div class="stat"><div class="v num" id="s-ack">0</div><div class="k label">Acknowledged</div></div>
  </div>
  <div class="link"><span class="dot" id="conn"></span><span id="conn-t">connecting</span></div>
</header>

<main>
  <section class="floor" id="floor" aria-label="Bay floor plan"></section>
  <aside class="rail">
    <h2>Decision trail</h2>
    <div class="log" id="log" role="log" aria-live="polite"></div>
  </aside>
</main>

<script>
(function(){
"use strict";

var qs = new URLSearchParams(location.search);
var SEED = (qs.get("zones") || "BAY-1,BAY-3,BAY-5,BAY-7")
             .split(",").map(function(s){return s.trim();}).filter(Boolean);

var floor = document.getElementById("floor");
var log = document.getElementById("log");
var bays = {};
var live = false;
var stats = {sup:0, hold:0, inc:0, esc:0, ack:0};

function setStat(k, v){
  stats[k] = v;
  document.getElementById("s-" + k).textContent = v;
}

function pad(n){ return n < 10 ? "0" + n : "" + n; }
function clockText(ts){
  var d = new Date((ts || Date.now()/1000) * 1000);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

/* ---- tiles ----------------------------------------------------- */

function makeBay(id){
  var el = document.createElement("article");
  el.className = "bay offline";
  el.innerHTML =
    '<div class="holds"></div>' +
    '<div class="id"></div>' +
    '<div class="state label">No signal</div>' +
    '<div class="detail"></div>' +
    '<div class="clock hidden"><div class="bar"><div class="fill"></div></div><div class="t"></div></div>' +
    '<button class="ackbtn hidden" type="button">Acknowledge &middot; kiosk</button>';
  el.querySelector(".id").textContent = id;

  var bay = {
    id:id, el:el,
    stateEl:el.querySelector(".state"),
    detailEl:el.querySelector(".detail"),
    holdsEl:el.querySelector(".holds"),
    clockEl:el.querySelector(".clock"),
    fillEl:el.querySelector(".fill"),
    timeEl:el.querySelector(".clock .t"),
    ackEl:el.querySelector(".ackbtn"),
    incident:null, deadline:0, window:0
  };

  bay.ackEl.addEventListener("click", function(){
    if (!bay.incident) return;
    post({kind:"ack", incident_id:bay.incident, zone:id, by:"kiosk (twin)"});
  });

  bays[id] = bay;
  var placeholder = floor.querySelector(".empty");
  if (placeholder) placeholder.remove();
  floor.appendChild(el);
  return bay;
}

function bayFor(id){
  if (!id) return null;
  return bays[id] || makeBay(id);
}

function setState(bay, cls, text){
  bay.el.className = "bay " + cls;
  bay.stateEl.textContent = text;
}

function stopClock(bay){
  bay.deadline = 0;
  bay.clockEl.classList.add("hidden");
  bay.ackEl.classList.add("hidden");
}

/* ---- the amber dots -------------------------------------------- */

function popHold(bay, action){
  var d = document.createElement("span");
  d.className = "hold" + (action === "confirmed" ? " confirmed" : "");
  bay.holdsEl.appendChild(d);

  /* Held detections sit lit. A suppression fades out -- that fade is
     the system deciding not to act, and it is meant to be watched. */
  if (action !== "verifying"){
    d.classList.add("fading");
    setTimeout(function(){ if (d.parentNode) d.remove(); }, 2500);
  } else {
    setTimeout(function(){
      if (d.parentNode){ d.classList.add("fading");
        setTimeout(function(){ if (d.parentNode) d.remove(); }, 2400); }
    }, 6000);
  }

  while (bay.holdsEl.children.length > 6) bay.holdsEl.firstChild.remove();
}

/* ---- countdown ------------------------------------------------- */

function tick(){
  var now = Date.now();
  for (var id in bays){
    var bay = bays[id];
    if (!bay.deadline) continue;
    var left = Math.max(0, (bay.deadline - now) / 1000);
    bay.timeEl.textContent = left.toFixed(1) + "s";
    bay.fillEl.style.width = (bay.window ? (left / bay.window) * 100 : 0) + "%";
    if (left <= 0){ bay.deadline = 0; bay.timeEl.textContent = "0.0s"; }
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

/* ---- the rail -------------------------------------------------- */

function addEntry(kind, head, sub, at){
  var el = document.createElement("div");
  el.className = "entry k-" + kind;
  var t = document.createElement("time");
  t.textContent = clockText(at);
  var body = document.createElement("div");
  body.className = "body";
  var h = document.createElement("div");
  h.className = "head"; h.textContent = head;
  var s = document.createElement("div");
  s.className = "sub"; s.textContent = sub || "";
  body.appendChild(h); body.appendChild(s);
  el.appendChild(t); el.appendChild(body);
  log.insertBefore(el, log.firstChild);
  while (log.children.length > 120) log.lastChild.remove();
}

/* ---- events ---------------------------------------------------- */

function conf(v){
  return (v === null || v === undefined) ? "" : "conf " + Number(v).toFixed(2);
}

function handle(e){
  /* The placeholder is only removed by makeBay(), which never runs for
     a bay that was seeded from the URL -- so without this the "waiting"
     text outlives the thing it was waiting for. */
  var placeholder = floor.querySelector(".empty");
  if (placeholder) placeholder.remove();

  var bay = bayFor(e.zone);

  if (e.kind === "decision"){
    if (!bay) return;

    if (bay.el.classList.contains("offline"))
      setState(bay, "monitoring", "Monitoring");

    if (e.action === "verifying"){
      setStat("hold", stats.hold + 1);
      popHold(bay, "verifying");
      if (!bay.el.classList.contains("alarm") &&
          !bay.el.classList.contains("escalated"))
        setState(bay, "holding", "Verifying");
      bay.detailEl.innerHTML = "<b>" + e.violation + "</b> held &mdash; " +
        (e.reason || "awaiting reconfirmation");
      addEntry("decision", "Held — " + e.zone,
               e.violation + " · " + conf(e.confidence) + " · " + (e.reason || ""), e.at);

    } else if (e.action === "suppressed"){
      setStat("sup", stats.sup + 1);
      setStat("hold", Math.max(0, stats.hold - 1));
      popHold(bay, "suppressed");
      if (bay.el.classList.contains("holding"))
        setState(bay, "monitoring", "Monitoring");
      bay.detailEl.innerHTML = "<b>" + e.violation + "</b> suppressed &mdash; " +
        (e.reason || "never reconfirmed");
      addEntry("decision", "Suppressed — " + e.zone,
               e.violation + " · " + conf(e.confidence) + " · " + (e.reason || ""), e.at);

    } else if (e.action === "confirmed"){
      setStat("hold", Math.max(0, stats.hold - 1));
      popHold(bay, "confirmed");
      addEntry("decision", "Reconfirmed — " + e.zone,
               e.violation + " · " + conf(e.confidence) + " · " + (e.reason || ""), e.at);

    } else if (e.action === "fire"){
      if (bay.el.classList.contains("offline"))
        setState(bay, "monitoring", "Monitoring");
      popHold(bay, "confirmed");
      addEntry("decision", "Acting now — " + e.zone,
               e.violation + " · " + conf(e.confidence) + " · " + (e.reason || ""), e.at);

    } else if (e.action === "ignored"){
      addEntry("decision", "Below floor — " + e.zone,
               e.violation + " · " + conf(e.confidence), e.at);
    }
    return;
  }

  if (e.kind === "incident"){
    if (!bay) return;
    setStat("inc", stats.inc + 1);
    setState(bay, "alarm", (e.severity || "high").toUpperCase() + " · incident open");
    bay.incident = e.incident_id;
    bay.detailEl.innerHTML =
      "<b>" + (e.violation || "violation") + "</b>" +
      (e.substance ? " · " + e.substance : "") +
      (e.confidence != null ? " · " + conf(e.confidence) : "") +
      (e.source ? "<br>via " + e.source : "");

    var w = Number(e.ack_window || 0);
    if (w > 0 && live){
      bay.window = w;
      bay.deadline = Date.now() + w * 1000;
      bay.clockEl.classList.remove("hidden");
      bay.ackEl.classList.remove("hidden");
    }
    addEntry("incident", "INCIDENT — " + e.zone,
             (e.violation || "") + " · " + (e.severity || "") +
             (e.spoken ? " · “" + e.spoken + "”" : ""), e.at);
    return;
  }

  if (e.kind === "ack"){
    setStat("ack", stats.ack + 1);
    if (bay){
      stopClock(bay);
      setState(bay, "acked", "Acknowledged");
      bay.detailEl.innerHTML = "Stood down by <b>" + (e.by || "kiosk") + "</b>" +
        (e.elapsed != null ? " after " + Number(e.elapsed).toFixed(1) + "s" : "");
      setTimeout(function(){
        if (bay.el.classList.contains("acked"))
          setState(bay, "monitoring", "Monitoring");
      }, 6000);
    }
    addEntry("ack", "Acknowledged — " + (e.zone || ""),
             "by " + (e.by || "kiosk") + " · no escalation", e.at);
    return;
  }

  if (e.kind === "escalation"){
    setStat("esc", stats.esc + 1);
    if (bay){
      stopClock(bay);
      setState(bay, "escalated", "Escalated · no ack");
      bay.detailEl.innerHTML = "No acknowledgement in " +
        (e.elapsed != null ? Number(e.elapsed).toFixed(0) + "s" : "the window") +
        ".<br>Escalated to <b>" + (e.route || "safety officer") + "</b>.";
    }
    addEntry("escalation", "ESCALATED — " + (e.zone || ""),
             "nobody acknowledged · routed to " + (e.route || "safety officer"), e.at);
    return;
  }

  if (e.kind === "note"){
    addEntry("note", e.text || "", "", e.at);
  }
}

/* ---- transport ------------------------------------------------- */

function post(body){
  fetch("twin/event", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)
  }).catch(function(){});
}

function setConn(ok, text){
  var d = document.getElementById("conn");
  d.className = "dot " + (ok ? "on" : "off");
  document.getElementById("conn-t").textContent = text;
}

SEED.forEach(makeBay);
floor.insertAdjacentHTML("beforeend",
  '<div class="empty">Waiting for the first decision. Start the trigger with ' +
  '<code>--twin http://127.0.0.1:8001</code>, or run <code>python bay_twin.py --demo</code>.</div>');

var src = new EventSource("twin/stream");
src.addEventListener("live", function(){ live = true; setConn(true, "live"); });
src.onopen = function(){ setConn(true, "live"); };
src.onerror = function(){ setConn(false, "reconnecting"); live = false; };
src.onmessage = function(m){
  var e;
  try { e = JSON.parse(m.data); } catch (err) { return; }
  handle(e);
};
})();
</script>
</body>
</html>
"""


# ============================================================
# DEMO
# ============================================================

def demo(port=8010, open_browser=True):
    """
    Serve the twin and drive a scripted stream through it.

    This exists so the page can be shown with no camera, no model and
    no second terminal -- the failure mode that ruins a live demo. The
    script is the sequence the real system produces: borderline
    detections that suppress, one that reconfirms, an incident that is
    acknowledged, and one that is not.
    """

    import uvicorn
    from fastapi import FastAPI

    app = FastAPI(title="Bay Twin (demo)")
    app.include_router(build_router())

    url = "http://127.0.0.1:%d/twin" % port

    def script():
        time.sleep(2.5)
        emit_note("Demo stream -- every event below is the shape the real "
                  "modules emit.")
        time.sleep(1.2)

        # 1. A borderline detection that never reconfirms. The amber dot.
        emit_decision({"zone": "BAY-3", "violation": "NO-Hardhat",
                       "confidence": 0.51, "tier": "borderline",
                       "action": "verifying",
                       "reason": "0.51 is below the 0.75 act threshold -- "
                                 "needs 2 more sightings in 4s"})
        time.sleep(4.5)
        emit_decision({"zone": "BAY-3", "violation": "NO-Hardhat",
                       "confidence": 0.51, "tier": "borderline",
                       "action": "suppressed",
                       "reason": "1 of 2 required sightings in 4.0s -- "
                                 "not reconfirmed, no incident raised"})
        time.sleep(2.0)

        # 2. Another, elsewhere, same outcome.
        emit_decision({"zone": "BAY-7", "violation": "NO-Safety Vest",
                       "confidence": 0.47, "tier": "borderline",
                       "action": "verifying",
                       "reason": "borderline -- holding"})
        time.sleep(4.5)
        emit_decision({"zone": "BAY-7", "violation": "NO-Safety Vest",
                       "confidence": 0.47, "tier": "borderline",
                       "action": "suppressed",
                       "reason": "expired unconfirmed"})
        time.sleep(1.5)

        # 3. One that DOES reconfirm, and fires.
        emit_decision({"zone": "BAY-5", "violation": "NO-Mask",
                       "confidence": 0.62, "tier": "borderline",
                       "action": "verifying",
                       "reason": "borderline -- holding for reconfirmation"})
        time.sleep(2.2)
        emit_decision({"zone": "BAY-5", "violation": "NO-Mask",
                       "confidence": 0.71, "tier": "borderline",
                       "action": "confirmed",
                       "reason": "2 sightings in 2.2s -- promoted to a "
                                 "full response"})
        time.sleep(0.4)
        emit_incident(
            {"bay_id": "BAY-5", "incident_type": "NO-Mask", "source": "camera",
             "confidence": 0.71, "substance_name": "Chlorine",
             "incident_id": "INC-DEMO-1"},
            {"severity": "critical",
             "spoken_alert": "Bay 5. Respirator required. Chlorine present.",
             "contraindication": "Do not mix with ammonia or acids."},
            ack_window=14)
        time.sleep(6.0)
        emit_ack("INC-DEMO-1", by="kiosk", zone="BAY-5", elapsed=6.0)
        time.sleep(3.0)

        # 4. A HIGH-confidence hit that fires at once -- and nobody acks.
        emit_decision({"zone": "BAY-1", "violation": "NO-Hardhat",
                       "confidence": 0.91, "tier": "high",
                       "action": "fire",
                       "reason": "0.91 >= 0.75 -- acting immediately"})
        time.sleep(0.4)
        emit_incident(
            {"bay_id": "BAY-1", "incident_type": "NO-Hardhat", "source": "camera",
             "confidence": 0.91, "substance_name": "Sodium hydroxide",
             "incident_id": "INC-DEMO-2"},
            {"severity": "high",
             "spoken_alert": "Bay 1. Hard hat required. Caustic present.",
             "contraindication": "Do not flush with a pressurised water jet."},
            ack_window=14)
        time.sleep(14.5)
        emit_escalation("INC-DEMO-2", zone="BAY-1", elapsed=14.0,
                        route="#safety-escalation + SMS to duty officer")
        time.sleep(1.0)
        emit_note("Nobody acknowledged BAY-1. The escalation webhook, the "
                  "PDF addendum and the re-spoken alert all fired without "
                  "anyone asking.")

    threading.Thread(target=script, daemon=True).start()

    _banner(url)

    if open_browser:
        _open_when_ready(port, url)

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _banner(url):
    """Print the URL where it cannot be missed, and flush it."""

    print("", flush=True)
    print("  " + "=" * 58, flush=True)
    print("  Bay Twin is at:  %s" % url, flush=True)
    print("  " + "=" * 58, flush=True)
    print("", flush=True)
    print("  Open that FULL address -- the port and the /twin path both",
          flush=True)
    print("  matter. Plain 127.0.0.1 is a different server on port 80,",
          flush=True)
    print("  which is why it shows a blank page.", flush=True)
    print("", flush=True)


def _open_when_ready(port, url, timeout=20.0):
    """
    Open a browser once the port actually accepts a connection.

    A fixed delay races uvicorn's startup: on a slow first import the
    tab opens before the server is listening, the browser shows its own
    error page, and nothing ever retries.
    """

    import socket

    def wait():
        deadline = time.time() + timeout

        while time.time() < deadline:
            probe = socket.socket()
            probe.settimeout(0.4)

            try:
                probe.connect(("127.0.0.1", port))
                break
            except Exception:
                time.sleep(0.25)
            finally:
                try:
                    probe.close()
                except Exception:
                    pass
        else:
            print("  server did not come up in %.0fs -- open %s by hand"
                  % (timeout, url), flush=True)
            return

        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            print("  could not open a browser -- go to %s" % url, flush=True)

    threading.Thread(target=wait, daemon=True).start()


def serve(port=8010):
    """Standalone twin, for when the incident service runs elsewhere."""

    import uvicorn
    from fastapi import FastAPI

    app = FastAPI(title="Bay Twin")
    app.include_router(build_router())

    _banner("http://127.0.0.1:%d/twin" % port)
    print("  Point the trigger at it: --twin http://127.0.0.1:%d" % port,
          flush=True)
    print("", flush=True)

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


# ============================================================
# SELF TEST
# ============================================================

def selftest():
    """No server, no browser, no network."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-46s %s" % ("PASS" if condition else "FAIL", name, detail))

    bus = EventBus(replay=5)

    # -- fan-out -----------------------------------------------------

    a, hist_a = bus.subscribe()
    b, _ = bus.subscribe()

    bus.publish("note", text="one")

    check("both subscribers receive a publish",
          a.qsize() == 1 and b.qsize() == 1, "2 queues, 1 event each")

    check("a fresh subscriber's replay is empty",
          hist_a == [], "nothing had been published yet")

    event = a.get_nowait()
    check("event carries kind, seq and at",
          event["kind"] == "note" and event["seq"] == 1 and "at" in event,
          "seq=%d" % event["seq"])

    bus.unsubscribe(a)
    bus.publish("note", text="two")
    check("unsubscribe stops delivery",
          a.qsize() == 0 and b.qsize() == 2, "a=0, b=2")

    # -- replay ------------------------------------------------------

    for i in range(8):
        bus.publish("note", text="fill-%d" % i)

    _, history = bus.subscribe()
    check("replay is bounded to its limit",
          len(history) == 5, "%d events kept of 10 published" % len(history))

    check("replay is the most recent, in order",
          history[-1]["text"] == "fill-7" and history[0]["seq"] < history[-1]["seq"],
          "ends at fill-7")

    # -- the rule that matters: a stalled reader must not block ------

    small = EventBus()
    slow = queue.Queue(maxsize=2)
    small._subscribers.append(slow)

    started = time.time()

    for i in range(50):
        small.publish("note", text=str(i))

    elapsed = time.time() - started

    check("a full subscriber queue drops instead of blocking",
          small.dropped == 48 and elapsed < 0.5,
          "%d dropped in %.3fs" % (small.dropped, elapsed))

    check("publishing still returns the event when subscribers are full",
          small.publish("note", text="x")["kind"] == "note",
          "publish never raises")

    # -- shaping -----------------------------------------------------

    BUS.reset()

    class FakeDecision(object):
        def as_dict(self):
            return {"zone": "BAY-3", "violation": "NO-Hardhat",
                    "confidence": 0.51, "tier": "borderline",
                    "action": "suppressed", "reason": "never reconfirmed"}

    shaped = emit_decision(FakeDecision())
    check("emit_decision accepts a Decision object",
          shaped["action"] == "suppressed" and shaped["zone"] == "BAY-3",
          "as_dict() honoured")

    shaped = emit_decision({"zone": "BAY-1", "violation": "NO-Mask",
                            "confidence": 0.9, "tier": "high",
                            "action": "fire", "reason": "high"})
    check("emit_decision accepts a plain dict",
          shaped["tier"] == "high", "no import of confidence_router needed")

    inc = emit_incident(
        {"bay_id": "BAY-9", "incident_type": "NO-Hardhat", "source": "kiosk",
         "confidence": 0.88, "substance_name": "Acetone"},
        {"severity": "CRITICAL", "spoken_alert": "Bay 9."},
        incident_id="INC-1", ack_window=20)

    check("emit_incident maps the wire field names",
          inc["zone"] == "BAY-9" and inc["violation"] == "NO-Hardhat",
          "bay_id -> zone, incident_type -> violation")

    check("severity is normalised to lower case",
          inc["severity"] == "critical", "CRITICAL -> critical")

    check("severity defaults rather than going missing",
          emit_incident({"bay_id": "B"}, {})["severity"] == "high",
          "no response -> high")

    # -- ingest ------------------------------------------------------

    got = ingest({"kind": "ack", "incident_id": "INC-1", "by": "kiosk"})
    check("ingest accepts a known kind", got["kind"] == "ack", "ack")

    forged = ingest({"kind": "note", "text": "x", "seq": 999999, "at": 0})
    check("ingest strips a forged seq and at",
          forged["seq"] != 999999 and forged["at"] != 0,
          "seq=%d, ordering stays local" % forged["seq"])

    rejected = False
    try:
        ingest({"kind": "shutdown"})
    except ValueError:
        rejected = True

    check("ingest rejects an unknown kind", rejected, "shutdown -> ValueError")

    rejected = False
    try:
        ingest(["not", "an", "object"])
    except ValueError:
        rejected = True

    check("ingest rejects a non-object body", rejected, "list -> ValueError")

    # -- the emitter -------------------------------------------------

    em = Emitter("http://127.0.0.1:9/unreachable", timeout=0.2)

    check("emitter normalises a base URL",
          em.url.endswith("/twin/event"),
          em.url)

    check("emitter accepts a base that already has the path",
          Emitter("http://x/twin/event").url == "http://x/twin/event",
          "not doubled")

    started = time.time()

    for i in range(20):
        em.send("note", text=str(i))

    elapsed = time.time() - started

    check("send() returns immediately even with no server",
          elapsed < 0.2, "20 sends in %.3fs" % elapsed)

    em.close(timeout=2.0)

    check("emitter survives an unreachable twin",
          em.failed > 0 and em.sent == 0,
          "%d failed, nothing raised" % em.failed)

    # -- the page ----------------------------------------------------

    check("page is self-contained -- no external fetch",
          "cdn" not in PAGE.lower() and "https://" not in PAGE,
          "no CDN, works offline")

    check("page subscribes to the stream",
          'EventSource("twin/stream")' in PAGE, "relative URL")

    for token in ("suppressed", "escalated", "Verifying"):
        check("page renders the %s state" % token, token in PAGE, "")

    passed = sum(checks)
    print("\n  %d/%d twin checks passed" % (passed, len(checks)))
    return passed == len(checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="serve the twin and drive a scripted stream")
    parser.add_argument("--serve", type=int, metavar="PORT",
                        help="serve the twin and wait for real events")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return 0 if selftest() else 1

    if args.serve:
        serve(args.serve)
        return 0

    if args.demo:
        demo(args.port, open_browser=not args.no_open)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
The autonomous half of HazardWatch OS: watch a bay, notice a PPE
violation, and open an incident without anyone touching anything.

Two things in here matter more than the detection itself.

**One trigger path, two front doors.** A camera detection and a kiosk
button press both end up in exactly one function -- fire_incident().
Nothing downstream can tell them apart except the `source` field, which
is recorded rather than branched on. That is deliberate: the kiosk is
the fallback for when the camera is unplugged, badly lit, or pointed at
a wall during the demo, and a fallback that runs different code from
the thing it replaces is not a fallback, it is a second bug surface.

**A detector is not a trigger.** YOLO re-decides the world 30 times a
second, so wiring detections straight to POSTs sends 30 incidents a
second for one person who forgot a hardhat. Two independent gates sit
in between: a violation must hold for N consecutive frames before it
counts (kills single-frame flicker), and once fired, that same
violation in that same zone is muted for a cooldown window (kills the
"still not wearing it" storm). Both live in TriggerGate, which has no
camera and no HTTP in it and is therefore testable on its own.

Run it:

    python yolo_trigger.py camera --zone BAY-3
    python yolo_trigger.py kiosk  --zone BAY-3 --violation NO-Hardhat
    python yolo_trigger.py selftest        # gate logic, no camera, no network

The kiosk line is what a physical button shells out to. It is the same
call the camera makes.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

# Where the teammate's incident service lives. Override per-demo with
# HAZARDWATCH_API rather than editing this.
INCIDENT_API = os.getenv("HAZARDWATCH_API", "http://127.0.0.1:8000").rstrip("/")
INCIDENT_PATH = "/incident"

# Optional shared secret, same convention as the sibling service: unset
# means the endpoint is open, which is right for a demo.
API_KEY = os.getenv("API_KEY") or None

# How long the same violation stays muted in the same zone after firing.
# The brief says 30-60s; 45 sits in the middle. Too short and a person
# who simply has not put the helmet on yet generates a second incident
# for the same event; too long and a genuinely new event gets swallowed.
COOLDOWN_SECONDS = float(os.getenv("HAZARDWATCH_COOLDOWN", "45"))

# Consecutive frames a violation must survive before it counts. At ~30
# fps this is a sixth of a second of agreement -- long enough to kill
# single-frame flicker, short enough that nobody perceives a delay.
CONSECUTIVE_FRAMES = int(os.getenv("HAZARDWATCH_FRAMES", "5"))

# Below this the box is a guess, not a detection. Raise it if the demo
# room produces false hardhat-misses; lower it if real ones are missed.
CONFIDENCE_FLOOR = float(os.getenv("HAZARDWATCH_CONF", "0.45"))

# Ordered candidates. The first that loads wins. The stock yolov8n.pt at
# the end is a COCO model with no PPE classes at all -- it is here so an
# offline laptop still starts, and run_camera() says loudly that the
# camera path cannot fire on it. See _violation_classes().
MODEL_CANDIDATES = [
    os.getenv("HAZARDWATCH_MODEL") or None,
    "best.pt",
    "hf:Hansung-Cho/yolov8-ppe-detection:best.pt",
    "yolov8n.pt",
]

# PPE fine-tunes label the *absence* of equipment, and they agree on the
# prefix even when they disagree on everything else: NO-Hardhat,
# NO-Safety Vest, No_Mask. Deriving violations from this prefix against
# the model's own names dict means swapping in a different fine-tune
# needs no code change -- hardcoding class indices would break silently
# on the first model with a different class order.
VIOLATION_PREFIXES = ("no-", "no_", "no ")

# Read timeout. The incident service may call an LLM, so this is
# generous; a camera thread blocking for 30s is better than an incident
# silently dropped.
HTTP_TIMEOUT = float(os.getenv("HAZARDWATCH_TIMEOUT", "30"))


def log(message):
    print("%s  %s" % (datetime.now().strftime("%H:%M:%S"), message), flush=True)


# ============================================================
# THE GATE -- no camera, no network, therefore testable
# ============================================================

class TriggerGate:
    """
    Decides whether an observed violation is worth an incident.

    Two independent gates, both of which must pass:

      1. Confirmation. The (zone, violation) pair must be seen on
         CONSECUTIVE_FRAMES frames in a row. Any frame that does not
         see it resets that pair's streak to zero.
      2. Cooldown. Having fired, the pair is muted for `cooldown`
         seconds.

    Time is injected rather than read from the clock so the cooldown can
    be tested without a test that sleeps for 45 seconds.
    """

    def __init__(self, cooldown=COOLDOWN_SECONDS, frames=CONSECUTIVE_FRAMES,
                 clock=time.monotonic):
        self.cooldown = cooldown
        self.frames = max(1, frames)
        self.clock = clock
        self._streak = {}    # (zone, violation) -> consecutive frames seen
        self._fired_at = {}  # (zone, violation) -> clock time of last fire

    def observe(self, zone, violations):
        """
        Feed one frame's worth of findings in; get back the subset that
        should fire right now.

        `violations` is the set of violation labels visible in this
        frame. Pairs absent from it have their streak reset, which is
        why this takes a whole frame rather than one violation at a
        time -- "not seen this frame" is information.
        """

        violations = set(violations)
        now = self.clock()
        ready = []

        for violation in sorted(violations):
            key = (zone, violation)
            self._streak[key] = self._streak.get(key, 0) + 1

            if self._streak[key] < self.frames:
                continue

            last = self._fired_at.get(key)

            if last is not None and (now - last) < self.cooldown:
                continue  # muted: same violation, same zone, too soon

            self._fired_at[key] = now
            ready.append(violation)

        # Anything not seen this frame loses its streak. Without this a
        # violation that flickers on for one frame every second would
        # accumulate a streak and eventually fire.
        for key in list(self._streak):
            if key[0] == zone and key[1] not in violations:
                self._streak[key] = 0

        return ready

    def remaining_mute(self, zone, violation):
        """Seconds left on the cooldown, 0.0 if clear. For status/UI."""

        last = self._fired_at.get((zone, violation))

        if last is None:
            return 0.0

        return max(0.0, self.cooldown - (self.clock() - last))


# ============================================================
# THE ONE TRIGGER PATH
# ============================================================

def build_incident_request(violation, zone, source, confidence=None,
                           substance=None, language="en", camera_id=None):
    """
    The hazard event as the incident service expects it.

    Every field the teammate's endpoint sees is built here and nowhere
    else, so when the real contract lands this function is the only
    thing that changes. Note that the API_CONTRACT.md sitting in this
    repo documents the language detect/translate service, NOT this
    endpoint -- do not "reconcile" the two.
    """

    event = {
        "zone": zone,
        "bay": zone,          # same value, both spellings, until the contract picks one
        "hazard_type": violation,
        "violation": violation,
        "source": source,     # "camera" | "kiosk" -- recorded, never branched on
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "language": language,
    }

    if confidence is not None:
        event["confidence"] = round(float(confidence), 3)

    if substance is not None:
        event["substance"] = substance

    if camera_id is not None:
        event["camera_id"] = camera_id

    return event


def post_incident(event, base=None, key=None, timeout=HTTP_TIMEOUT):
    """
    POST the event. Returns (status, parsed_body). Never raises.

    Never raising is the point: a camera loop that dies because the
    incident service blipped is worse than one that logs the blip and
    keeps watching the bay.
    """

    base = (base or INCIDENT_API).rstrip("/")
    url = base + INCIDENT_PATH
    data = json.dumps(event).encode()

    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")

    if key or API_KEY:
        request.add_header("X-API-Key", key or API_KEY)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())

    except urllib.error.HTTPError as e:
        raw = e.read().decode()

        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw

    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def fire_incident(violation, zone, source="kiosk", confidence=None,
                  substance=None, language="en", camera_id=None,
                  base=None, key=None):
    """
    THE trigger path. The camera calls this. The kiosk button calls
    this. Nothing else opens an incident.

    Returns the dict below whether or not the POST succeeded, because
    the caller's job is to carry on either way:

        {"event": {...}, "status": 200|None, "response": {...}|str, "ok": bool}
    """

    event = build_incident_request(
        violation, zone, source,
        confidence=confidence, substance=substance,
        language=language, camera_id=camera_id,
    )

    log("TRIGGER  %-16s zone=%-8s source=%s" % (violation, zone, source))

    status, body = post_incident(event, base=base, key=key)
    ok = status == 200 and isinstance(body, dict)

    if ok:
        log("  incident opened: severity=%s" % body.get("severity"))
    else:
        log("  incident POST failed: %s %s" % (status, str(body)[:160]))

    return {"event": event, "status": status, "response": body, "ok": ok}


def dispatch_downstream(result, speak=True, dossier=True, webhook=True):
    """
    Optional follow-through: speak the alert, write the PDF, fire the
    webhook. Each sibling module is imported lazily and its absence is
    survivable, so this file works standalone and simply does more as
    the other pieces land.
    """

    if not result.get("ok"):
        return {}

    body = result["response"]
    event = result["event"]
    done = {}

    if speak and body.get("spoken_alert"):
        try:
            import tts_alert
            done["spoke"] = tts_alert.speak(
                body["spoken_alert"], event.get("language", "en"))
        except Exception as e:
            log("  tts skipped: %s: %s" % (type(e).__name__, e))

    if dossier:
        try:
            import dossier as dossier_module
            done["pdf"] = dossier_module.build_dossier(event, body)
            log("  dossier: %s" % done["pdf"])
        except Exception as e:
            log("  dossier skipped: %s: %s" % (type(e).__name__, e))

    if webhook:
        try:
            import webhook_dispatch
            done["webhook"] = webhook_dispatch.dispatch(event, body)
        except Exception as e:
            log("  webhook skipped: %s: %s" % (type(e).__name__, e))

    return done


# ============================================================
# THE CAMERA
# ============================================================

def load_model(candidates=None):
    """
    Load the first candidate that works. Returns (model, label).

    Raises RuntimeError if none load -- unlike the runtime paths there
    is nothing sensible to degrade to here, and starting a camera loop
    with no model would fail 30 times a second instead of once.
    """

    # Imported here, not at module scope: ultralytics pulls in torch and
    # costs seconds to import, and the kiosk path never needs it.
    from ultralytics import YOLO

    tried = []

    for candidate in (candidates if candidates is not None else MODEL_CANDIDATES):
        if not candidate:
            continue

        try:
            path = candidate

            if candidate.startswith("hf:"):
                # hf:<repo_id>:<filename> -- downloaded once, then cached.
                _, repo_id, filename = candidate.split(":", 2)
                from huggingface_hub import hf_hub_download
                path = hf_hub_download(repo_id=repo_id, filename=filename)

            model = YOLO(path)
            return model, candidate

        except Exception as e:
            tried.append("%s (%s: %s)" % (candidate, type(e).__name__, e))

    raise RuntimeError("no model would load. Tried:\n  " + "\n  ".join(tried))


def _violation_classes(model):
    """Class ids whose name marks *missing* equipment. May be empty."""

    return {
        class_id: name
        for class_id, name in model.names.items()
        if name.lower().startswith(VIOLATION_PREFIXES)
    }


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")


def frames_from(source, max_frames=None):
    """
    Yield frames from a webcam index, a video file, or a still image.

    Accepting files is not a convenience, it is demo insurance. A laptop
    privacy shutter produces a perfectly valid capture of a perfectly
    flat grey frame -- the camera "works", every read succeeds, and the
    model correctly detects nothing. Being able to point --source at a
    recorded clip or a photo means a covered lens costs the demo
    nothing.

    A still image is yielded repeatedly, because one frame can never
    satisfy the gate's consecutive-frame requirement.
    """

    import cv2

    live = isinstance(source, int) or str(source).isdigit()

    if live:
        source = int(source)

    if not live and str(source).lower().endswith(IMAGE_SUFFIXES):
        image = cv2.imread(str(source))

        if image is None:
            raise RuntimeError("could not read image: %s" % source)

        count = 0

        while max_frames is None or count < max_frames:
            yield image
            count += 1

        return

    capture = cv2.VideoCapture(source)

    if not capture.isOpened():
        raise RuntimeError(
            "could not open source %r.%s" % (
                source,
                "\n  Use a recorded clip or photo:  --source clip.mp4"
                "\n  Or the kiosk path:             python yolo_trigger.py kiosk "
                "--zone BAY-3 --violation NO-Hardhat" if live else ""))

    count = 0

    try:
        while max_frames is None or count < max_frames:
            read_ok, frame = capture.read()

            if not read_ok:
                if not live:
                    return  # end of the file, not a glitch

                log("dropped frame")
                continue

            yield frame
            count += 1

    finally:
        capture.release()


def run_camera(zone, source_index=0, base=None, key=None, substance=None,
               language="en", show=False, gate=None, downstream=True,
               max_frames=None):
    """
    Watch one source, feed every frame through the gate, fire what the
    gate lets through. Ctrl-C to stop.
    """

    import cv2

    model, label = load_model()
    violations = _violation_classes(model)

    log("model: %s" % label)
    log("classes: %d, of which violations: %s"
        % (len(model.names), sorted(violations.values()) or "NONE"))

    if not violations:
        # Honest failure. A COCO model has no NO-Hardhat class, so this
        # loop could watch forever and never legitimately fire. Say so
        # rather than letting it look like a quiet, working camera.
        log("WARNING: this model exposes no violation classes -- the camera")
        log("         path CANNOT fire on it. Point HAZARDWATCH_MODEL at a PPE")
        log("         fine-tune, or use the kiosk path for the demo.")

    gate = gate or TriggerGate()

    log("watching zone=%s on source %r -- Ctrl-C to stop" % (zone, source_index))
    log("gate: %d consecutive frames, %.0fs cooldown, conf>=%.2f"
        % (gate.frames, gate.cooldown, CONFIDENCE_FLOOR))

    blank_frames = 0
    fired_total = 0

    try:
        for frame in frames_from(source_index, max_frames=max_frames):
            # A covered privacy shutter reads as a valid, perfectly flat
            # frame: every read succeeds and the model correctly finds
            # nothing, which looks exactly like a quiet bay. Say it out
            # loud instead of watching a lens cap for the whole demo.
            if float(frame.std()) < 1.0:
                blank_frames += 1

                if blank_frames == 30:
                    log("WARNING: 30 featureless frames -- the lens looks covered.")
                    log("         Nothing can be detected. Check the privacy shutter,")
                    log("         or run with --source clip.mp4 / --source photo.jpg")
            else:
                blank_frames = 0

            results = model(frame, verbose=False, conf=CONFIDENCE_FLOOR)[0]

            seen = {}

            for box in results.boxes:
                class_id = int(box.cls[0])

                if class_id not in violations:
                    continue

                name = violations[class_id]
                confidence = float(box.conf[0])
                # Keep the most confident instance of each violation.
                seen[name] = max(seen.get(name, 0.0), confidence)

            for violation in gate.observe(zone, seen.keys()):
                fired_total += 1
                result = fire_incident(
                    violation, zone, source="camera",
                    confidence=seen[violation], substance=substance,
                    language=language, camera_id=str(source_index),
                    base=base, key=key,
                )

                if downstream:
                    dispatch_downstream(result)

            if show:
                cv2.imshow("HazardWatch %s" % zone, results.plot())

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

    except KeyboardInterrupt:
        log("stopped")

    finally:
        # The frame generator owns the capture and releases it itself.
        if show:
            cv2.destroyAllWindows()

    log("%d incident(s) fired" % fired_total)
    return fired_total


# ============================================================
# DOCTOR -- the pre-demo check
# ============================================================

def doctor(zone="BAY-3", source="0", api=None, language="en", frames=20):
    """
    Check everything the live path needs, before it is needed.

    Ordered so the cheap checks fail first, and every failure names the
    fix rather than just the symptom. FAIL means the demo will not work;
    WARN means it will work but in a degraded way you should know about.

    Returns the number of failures, so this can gate a run.
    """

    results = []

    def record(status, name, detail, fix=None):
        results.append(status)
        print("  %-4s  %-26s %s" % (status, name, detail))

        if fix and status != "PASS":
            print("        -> %s" % fix)

    print("HazardWatch pre-flight\n")

    # -- outputs directory -------------------------------------------
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")

    try:
        os.makedirs(out_dir, exist_ok=True)
        probe = os.path.join(out_dir, ".doctor")

        with open(probe, "w") as handle:
            handle.write("ok")

        os.remove(probe)
        record("PASS", "outputs/ writable", out_dir)

    except Exception as e:
        record("FAIL", "outputs/ writable", "%s: %s" % (type(e).__name__, e),
               "the dossier has nowhere to write")

    # -- camera ------------------------------------------------------
    capture = None
    live_frame = None

    try:
        import cv2
        import numpy

        index = int(source) if str(source).isdigit() else source
        capture = cv2.VideoCapture(index)

        if not capture.isOpened():
            record("FAIL", "camera opens", "source %r would not open" % source,
                   "check the index, or use --source clip.mp4, or run the kiosk path")
        else:
            for _ in range(10):       # let exposure settle
                capture.read()

            read_ok, live_frame = capture.read()

            if not read_ok or live_frame is None:
                record("FAIL", "camera reads", "opened but returned no frame",
                       "another app may be holding the camera")
            else:
                height, width = live_frame.shape[:2]
                spread = float(numpy.std(live_frame))

                record("PASS", "camera reads", "%dx%d" % (width, height))

                # The failure that looks like success: a closed privacy
                # shutter reads as a perfectly valid, perfectly flat frame.
                if spread < 1.0:
                    record("FAIL", "lens is uncovered",
                           "frame is featureless (std %.1f)" % spread,
                           "open the privacy shutter -- nothing can be detected")
                elif spread < 10.0:
                    record("WARN", "lens is uncovered",
                           "very low contrast (std %.1f)" % spread,
                           "check lighting; detection will be unreliable")
                else:
                    record("PASS", "lens is uncovered", "std %.1f" % spread)

                started = time.perf_counter()

                for _ in range(frames):
                    capture.read()

                capture_fps = frames / (time.perf_counter() - started)
                record("PASS", "capture rate", "%.1f fps" % capture_fps)

    except Exception as e:
        record("FAIL", "camera", "%s: %s" % (type(e).__name__, e),
               "opencv is missing or the camera is unavailable")

    finally:
        if capture is not None:
            capture.release()

    # -- model -------------------------------------------------------
    model = None

    try:
        model, label = load_model()
        record("PASS", "model loads", label)

        violations = _violation_classes(model)

        if violations:
            record("PASS", "violation classes",
                   "%d of %d: %s" % (len(violations), len(model.names),
                                     ", ".join(sorted(violations.values()))))
        else:
            record("FAIL", "violation classes",
                   "none -- this model cannot detect a violation",
                   "point HAZARDWATCH_MODEL at a PPE fine-tune")

    except Exception as e:
        record("FAIL", "model loads", "%s: %s" % (type(e).__name__, str(e)[:90]),
               "pip install -r requirements-trigger.txt")

    # -- live inference ----------------------------------------------
    if model is not None and live_frame is not None:
        try:
            model(live_frame, verbose=False, conf=CONFIDENCE_FLOOR)  # warm-up

            started = time.perf_counter()

            for _ in range(5):
                results_obj = model(live_frame, verbose=False, conf=CONFIDENCE_FLOOR)[0]

            infer_fps = 5 / (time.perf_counter() - started)

            seen = {}

            for box in results_obj.boxes:
                name = model.names[int(box.cls[0])]
                seen[name] = max(seen.get(name, 0.0), round(float(box.conf[0]), 2))

            record("PASS", "inference on a live frame", "%.1f fps" % infer_fps)
            record("PASS" if seen else "WARN", "what the camera sees",
                   ", ".join("%s %.2f" % (k, v) for k, v in sorted(seen.items()))
                   or "nothing above conf %.2f" % CONFIDENCE_FLOOR,
                   None if seen else
                   "stand in frame; the gate needs %d consecutive frames to fire"
                   % CONSECUTIVE_FRAMES)

            confirm_seconds = CONSECUTIVE_FRAMES / infer_fps
            record("PASS", "time to fire", "%.1f s (%d frames at %.1f fps)"
                   % (confirm_seconds, CONSECUTIVE_FRAMES, infer_fps))

        except Exception as e:
            record("FAIL", "inference", "%s: %s" % (type(e).__name__, str(e)[:90]))

    # -- incident service --------------------------------------------
    # Reachability only. A probe POST would open a real incident, which
    # is not something a pre-flight check should do.
    base = (api or INCIDENT_API).rstrip("/")

    try:
        request = urllib.request.Request(base, method="GET")

        with urllib.request.urlopen(request, timeout=5) as response:
            record("PASS", "incident service", "%s -> %s" % (base, response.status))

    except urllib.error.HTTPError as e:
        # Any HTTP answer means something is listening, which is the question.
        record("PASS", "incident service", "%s -> %s (reachable)" % (base, e.code))

    except Exception as e:
        record("WARN", "incident service", "%s unreachable (%s)" % (base, type(e).__name__),
               "start it, or pass --api; the rehearsal uses its own mock")

    # -- speech ------------------------------------------------------
    try:
        import tts_alert

        voice = tts_alert._offline_voice_for(language)

        if voice:
            record("PASS", "speech (%s)" % language, "offline voice: %s" % voice.name)
        else:
            cache = tts_alert.CACHE_DIR
            warm = len(os.listdir(cache)) if os.path.isdir(cache) else 0

            record("WARN", "speech (%s)" % language,
                   "no local voice; needs gTTS (network). %d cached clip(s)" % warm,
                   "python tts_alert.py --prefetch alerts.txt --lang %s" % language)

    except Exception as e:
        record("FAIL", "speech", "%s: %s" % (type(e).__name__, str(e)[:90]),
               "pip install -r requirements-trigger.txt")

    failures = results.count("FAIL")
    warnings = results.count("WARN")

    print("\n  %d passed, %d warning(s), %d failure(s)"
          % (results.count("PASS"), warnings, failures))

    if failures:
        print("\n  NOT ready -- fix the failures above.")
    elif warnings:
        print("\n  Ready, with caveats. The warnings are things to know, not blockers.")
    else:
        print("\n  Ready.")

    return failures


# ============================================================
# SELF TEST -- the gate, which is the part that can actually be wrong
# ============================================================

def selftest():
    """Exercise TriggerGate against a fake clock. No camera, no network."""

    now = [1000.0]
    gate = TriggerGate(cooldown=45, frames=3, clock=lambda: now[0])
    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-38s %s" % ("PASS" if condition else "FAIL", name, detail))

    def over(frames, zone, violations):
        """Every fire across `frames` identical frames, flattened.

        Which frame within the window a fire lands on depends on whether
        that pair already had a streak, so the assertions below are about
        what fired across the window, not about one frame's return value.
        """

        fired = []

        for _ in range(frames):
            fired += gate.observe(zone, violations)

        return fired

    first_two = [gate.observe("BAY-3", ["NO-Hardhat"]) for _ in range(2)]
    check("2 frames is not enough", first_two == [[], []], str(first_two))

    third = gate.observe("BAY-3", ["NO-Hardhat"])
    check("3rd consecutive frame fires", third == ["NO-Hardhat"], str(third))

    now[0] += 1
    during = [gate.observe("BAY-3", ["NO-Hardhat"]) for _ in range(30)]
    check("muted during cooldown", all(f == [] for f in during),
          "30 further frames, %d fires" % sum(len(f) for f in during))

    other_zone = over(3, "BAY-7", ["NO-Hardhat"])
    check("cooldown is per-zone", other_zone == ["NO-Hardhat"], "BAY-7 %s" % other_zone)

    both = over(3, "BAY-3", ["NO-Hardhat", "NO-Safety Vest"])
    check("cooldown is per-violation", both == ["NO-Safety Vest"], str(both))

    # A violation that never stopped has a saturated streak, so it is
    # free to fire on the very first frame past the mute window -- there
    # is nothing left to re-confirm.
    now[0] += 46
    again = over(3, "BAY-3", ["NO-Hardhat"])
    check("fires again after cooldown", again == ["NO-Hardhat"], str(again))
    check("and fires exactly once", len(again) == 1, "%d fires in 3 frames" % len(again))

    flicker_gate = TriggerGate(cooldown=45, frames=3, clock=lambda: now[0])
    flickers = []

    for _ in range(10):
        flickers += flicker_gate.observe("BAY-9", ["NO-Mask"])
        flickers += flicker_gate.observe("BAY-9", [])      # gone this frame

    check("flicker never accumulates", flickers == [],
          "%d fires in 20 alternating frames" % len(flickers))

    print("\n%d/%d gate checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


# ============================================================
# CLI
# ============================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="HazardWatch autonomous trigger (camera) and its kiosk fallback.")
    sub = parser.add_subparsers(dest="mode", required=True)

    def common(p):
        p.add_argument("--zone", default="BAY-3", help="zone/bay id, e.g. BAY-3")
        p.add_argument("--substance", default=None, help="substance present in the bay")
        p.add_argument("--language", default="en", help="language code for the spoken alert")
        p.add_argument("--api", default=None, help="incident service base URL")
        p.add_argument("--key", default=None, help="X-API-Key, if the service requires one")
        p.add_argument("--no-downstream", action="store_true",
                       help="open the incident but skip TTS/PDF/webhook")

    camera = sub.add_parser("camera", help="watch a webcam (the autonomous path)")
    common(camera)
    camera.add_argument("--source", default="0",
                        help="camera index (0), or a video/image file to run "
                             "against instead -- useful when the lens is covered")
    camera.add_argument("--show", action="store_true", help="open a preview window")
    camera.add_argument("--cooldown", type=float, default=COOLDOWN_SECONDS)
    camera.add_argument("--frames", type=int, default=CONSECUTIVE_FRAMES)
    camera.add_argument("--max-frames", type=int, default=None,
                        help="stop after N frames (a still image is otherwise endless)")

    kiosk = sub.add_parser(
        "kiosk", help="one trigger, no camera -- what a physical button calls")
    common(kiosk)
    kiosk.add_argument("--violation", default="NO-Hardhat",
                       help="violation label, e.g. NO-Hardhat")

    sub.add_parser("selftest", help="gate logic only: no camera, no network")

    check = sub.add_parser(
        "doctor", help="pre-flight: camera, lens, model, endpoint, speech")
    check.add_argument("--zone", default="BAY-3")
    check.add_argument("--source", default="0", help="camera index or file")
    check.add_argument("--api", default=None, help="incident service base URL")
    check.add_argument("--language", default="en")
    check.add_argument("--frames", type=int, default=20,
                       help="frames to time the capture rate over")

    args = parser.parse_args(argv)

    if args.mode == "selftest":
        return selftest()

    if args.mode == "doctor":
        return 1 if doctor(args.zone, args.source, args.api,
                           args.language, args.frames) else 0

    if args.mode == "kiosk":
        # Deliberately the same call the camera loop makes. The only
        # difference that reaches the service is source="kiosk".
        result = fire_incident(
            args.violation, args.zone, source="kiosk",
            substance=args.substance, language=args.language,
            base=args.api, key=args.key,
        )

        if not args.no_downstream:
            dispatch_downstream(result)

        return 0 if result["ok"] else 1

    run_camera(
        args.zone, source_index=args.source, base=args.api, key=args.key,
        substance=args.substance, language=args.language, show=args.show,
        gate=TriggerGate(cooldown=args.cooldown, frames=args.frames),
        downstream=not args.no_downstream, max_frames=args.max_frames,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

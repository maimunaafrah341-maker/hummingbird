"""
One command that gates the trigger half and measures it.

    python eval_trigger.py

Two jobs, in this order:

**Gate.** Run every check suite in the project as its own process and
exit non-zero if any of them fails. Subprocesses rather than imports on
purpose -- a suite that only passes because a previous one warmed a
cache or left a module imported is not passing, and running them in one
interpreter hides exactly that.

**Measure.** Then produce the numbers that decide whether this thing
can run on the demo hardware: import cost, model load, inference rate,
how much the gate actually suppresses, and the latency of each output
stage. Written to EVAL-TRIGGER.md in the same form as EVAL.md, which is
a document of measured values and says so.

Nothing here estimates. Every number in the report came from running
the code on the machine the report was written on, and the report says
which machine and when. If a measurement cannot be taken -- no
ultralytics, no network, no camera -- it is recorded as "not measured"
rather than filled in with something plausible.

    python eval_trigger.py --quick       # gate only, skip model/inference
    python eval_trigger.py --no-write    # do not touch EVAL-TRIGGER.md
    python eval_trigger.py --audio       # let the rehearsal speak aloud

Exit code is the gate: 0 if every suite passed, 1 otherwise.
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "EVAL-TRIGGER.md")

# Each suite is (label, argv). Exit code is the pass/fail signal; the
# printed counts are parsed for the report only, so a suite that changes
# its output format degrades the report, never the gate.
SUITES = [
    ("trigger gate", ["yolo_trigger.py", "selftest"]),
    ("confidence router", ["confidence_router.py", "--selftest"]),
    ("dossier", ["dossier.py", "--selftest"]),
    ("webhook dispatch", ["webhook_dispatch.py", "--selftest"]),
    ("tts alert", ["tts_alert.py", "--selftest"]),
    ("alert language", ["alert_language.py", "--selftest"]),
    ("incident service", ["incident_api.py", "--selftest"]),
    ("escalation watcher", ["escalation_watcher.py", "--selftest"]),
    ("bay twin", ["bay_twin.py", "--selftest"]),
    ("phrases", ["phrases.py", "--selftest"]),
    ("incident rehearsal", ["smoke_test.py", "incident", "--no-audio", "--no-open"]),
]

# "8/8 gate checks passed" or "11 passed, 0 failed"
COUNT_PATTERNS = [
    re.compile(r"(\d+)\s*/\s*(\d+)\s+\w[\w ]*checks passed"),
    re.compile(r"(\d+)\s+passed,\s+(\d+)\s+failed"),
]


def _python(args, timeout=600):
    """Run a python script in this repo. Returns (exit_code, output)."""

    started = time.perf_counter()

    process = subprocess.run(
        [sys.executable] + args,
        cwd=HERE, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )

    elapsed = time.perf_counter() - started
    return process.returncode, (process.stdout or "") + (process.stderr or ""), elapsed


def _snippet(code, timeout=600):
    """Run a throwaway script in a FRESH interpreter. Returns parsed JSON or None."""

    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=HERE, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )

    for line in (process.stdout or "").splitlines():
        line = line.strip()

        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue

    return None


def _counts(output):
    """Best-effort (passed, total) from a suite's output."""

    for pattern in COUNT_PATTERNS:
        match = pattern.search(output)

        if not match:
            continue

        first, second = int(match.group(1)), int(match.group(2))

        # "8/8 passed" gives (passed, total); "11 passed, 0 failed" gives
        # (passed, failed) -- normalise both to (passed, total).
        return (first, second) if pattern is COUNT_PATTERNS[0] else (first, first + second)

    return None, None


# ============================================================
# GATE
# ============================================================

def run_gate(audio=False):
    """Run every suite. Returns (all_passed, [row, ...])."""

    print("gate\n")
    rows = []
    ok = True

    for label, argv in SUITES:
        if label == "incident rehearsal" and audio:
            argv = [a for a in argv if a != "--no-audio"]

        code, output, elapsed = _python(argv)
        passed, total = _counts(output)
        suite_ok = code == 0
        ok = ok and suite_ok

        detail = "%d/%d checks" % (passed, total) if passed is not None \
            else "exit %d" % code

        print("  %s  %-20s %-16s %6.2fs"
              % ("PASS" if suite_ok else "FAIL", label, detail, elapsed))

        if not suite_ok:
            tail = [l for l in output.splitlines() if l.strip()][-6:]

            for line in tail:
                print("          | %s" % line[:110])

        rows.append({"label": label, "ok": suite_ok, "passed": passed,
                     "total": total, "seconds": elapsed,
                     "command": "python " + " ".join(argv)})

    total_checks = sum(r["total"] or 0 for r in rows)
    total_passed = sum(r["passed"] or 0 for r in rows)

    print("\n  %d/%d checks across %d suites, %.1fs total"
          % (total_passed, total_checks, len(rows), sum(r["seconds"] for r in rows)))

    return ok, rows


# ============================================================
# MEASUREMENTS
# ============================================================

def measure_imports():
    """Import cost per module, each in a fresh interpreter."""

    results = {}

    for module in ("yolo_trigger", "dossier", "webhook_dispatch", "tts_alert",
                   "alert_language", "confidence_router",
                   "escalation_watcher", "bay_twin"):
        data = _snippet(
            "import time,json;"
            "t=time.perf_counter();"
            "import %s;"
            "print(json.dumps({'seconds': time.perf_counter()-t}))" % module)

        results[module] = data["seconds"] if data else None

    return results


def measure_gate():
    """
    What the gate actually suppresses.

    This is the number the whole design exists for, so it is measured
    rather than asserted: feed a continuous violation through the real
    TriggerGate and count how many incidents come out the other side.
    """

    import yolo_trigger as yt

    now = [0.0]
    gate = yt.TriggerGate(cooldown=45, hits=3, window=8, clock=lambda: now[0])

    frames = 1800          # 60 seconds at 30 fps
    fires = 0

    for _ in range(frames):
        now[0] += 1.0 / 30.0
        fires += len(gate.observe("BAY-3", ["NO-Hardhat"]))

    started = time.perf_counter()

    for _ in range(10000):
        gate.observe("BAY-9", ["NO-Mask"])

    per_call = (time.perf_counter() - started) / 10000 * 1e6  # microseconds

    return {"frames": frames, "simulated_seconds": 60, "fires": fires,
            "ungated": frames, "microseconds_per_frame": per_call}


def measure_model():
    """Model load and inference, in a fresh interpreter."""

    code = r"""
import json, os, time
import psutil

process = psutil.Process()
baseline = process.memory_info().rss

t = time.perf_counter()
from ultralytics import YOLO
import_seconds = time.perf_counter() - t
after_import = process.memory_info().rss

import yolo_trigger as yt

t = time.perf_counter()
model, label = yt.load_model()
load_seconds = time.perf_counter() - t
after_load = process.memory_info().rss

import ultralytics, cv2
image_path = os.path.join(os.path.dirname(ultralytics.__file__), "assets", "bus.jpg")
frame = cv2.imread(image_path)

model(frame, verbose=False, conf=yt.CONFIDENCE_FLOOR)   # warm-up, not timed

runs = 15
t = time.perf_counter()
for _ in range(runs):
    results = model(frame, verbose=False, conf=yt.CONFIDENCE_FLOOR)[0]
infer_seconds = (time.perf_counter() - t) / runs
after_infer = process.memory_info().rss

violations = yt._violation_classes(model)

# The whole per-frame loop, not just the model call: frame delivery,
# inference, unpacking the boxes and the gate decision. This is the
# number that describes the autonomous path; inference alone flatters
# it by leaving out everything around the model.
gate = yt.TriggerGate(cooldown=45, hits=3, window=8)
loop_runs = 15
t = time.perf_counter()
for frame_in in yt.frames_from(image_path, max_frames=loop_runs):
    r = model(frame_in, verbose=False, conf=yt.CONFIDENCE_FLOOR)[0]
    seen = {}
    for box in r.boxes:
        class_id = int(box.cls[0])
        if class_id in violations:
            name = violations[class_id]
            seen[name] = max(seen.get(name, 0.0), float(box.conf[0]))
    gate.observe("BAY-EVAL", seen.keys())
loop_seconds = (time.perf_counter() - t) / loop_runs

found = {}
for box in results.boxes:
    name = model.names[int(box.cls[0])]
    found[name] = max(found.get(name, 0.0), round(float(box.conf[0]), 3))

print(json.dumps({
    "model": label,
    "classes": len(model.names),
    "violation_classes": sorted(violations.values()),
    "import_seconds": import_seconds,
    "load_seconds": load_seconds,
    "inference_seconds": infer_seconds,
    "fps": 1.0 / infer_seconds,
    "loop_seconds": loop_seconds,
    "loop_fps": 1.0 / loop_seconds,
    "detections": found,
    "rss_baseline_mb": baseline / 1e6,
    "rss_after_import_mb": after_import / 1e6,
    "rss_after_load_mb": after_load / 1e6,
    "rss_after_inference_mb": after_infer / 1e6,
    "image": os.path.basename(image_path),
    "image_height": int(frame.shape[0]),
    "image_width": int(frame.shape[1]),
}))
"""

    try:
        return _snippet(code, timeout=900)
    except subprocess.TimeoutExpired:
        return None


def measure_camera(index=0, frames=25):
    """
    The live lens-to-incident path, measured rather than assumed.

    Separate from measure_model() because it answers a different
    question: not "how fast is the model" but "how fast is a frame that
    started as light hitting a sensor". Capture cost is real and the
    file-source numbers do not contain it.

    Returns None if there is no usable camera, which is a legitimate
    outcome and is reported as such rather than guessed at.
    """

    code = r"""
import json, time
import cv2, numpy
import yolo_trigger as yt

capture = cv2.VideoCapture(%d)

if not capture.isOpened():
    print(json.dumps({"available": False, "reason": "camera would not open"}))
    raise SystemExit

for _ in range(10):          # let exposure settle
    capture.read()

read_ok, frame = capture.read()

if not read_ok or frame is None:
    capture.release()
    print(json.dumps({"available": False, "reason": "opened but returned no frame"}))
    raise SystemExit

height, width = frame.shape[:2]
spread = float(numpy.std(frame))

FRAMES = %d

# Capture alone.
t = time.perf_counter()
for _ in range(FRAMES):
    capture.read()
capture_seconds = (time.perf_counter() - t) / FRAMES

model, label = yt.load_model()
violations = yt._violation_classes(model)
gate = yt.TriggerGate(cooldown=45, hits=3, window=8)

model(frame, verbose=False, conf=yt.CONFIDENCE_FLOOR)     # warm-up

# The whole live path: capture, infer, unpack, gate.
seen_any = {}
t = time.perf_counter()
for _ in range(FRAMES):
    ok, live = capture.read()
    if not ok:
        continue
    r = model(live, verbose=False, conf=yt.CONFIDENCE_FLOOR)[0]
    seen = {}
    for box in r.boxes:
        class_id = int(box.cls[0])
        name = model.names[class_id]
        seen_any[name] = max(seen_any.get(name, 0.0), round(float(box.conf[0]), 3))
        if class_id in violations:
            seen[violations[class_id]] = max(seen.get(violations[class_id], 0.0),
                                             float(box.conf[0]))
    gate.observe("BAY-EVAL", seen.keys())
loop_seconds = (time.perf_counter() - t) / FRAMES

capture.release()

print(json.dumps({
    "available": True,
    "width": width, "height": height,
    "frame_std": spread,
    "lens_open": spread >= 10.0,
    "capture_seconds": capture_seconds,
    "capture_fps": 1.0 / capture_seconds,
    "loop_seconds": loop_seconds,
    "loop_fps": 1.0 / loop_seconds,
    "detections": seen_any,
    "frames": FRAMES,
}))
""" % (index, frames)

    try:
        return _snippet(code, timeout=900)
    except subprocess.TimeoutExpired:
        return None


def measure_outputs(allow_network=True):
    """Latency of the three output stages."""

    import dossier
    import tts_alert
    import webhook_dispatch

    results = {}

    # -- PDF ---------------------------------------------------------
    event, response = dossier.SAMPLE_EVENT, dossier.SAMPLE_RESPONSE

    dossier.build_dossier(event, response, filename="eval_warmup.pdf")  # warm-up

    runs = 10
    started = time.perf_counter()

    for _ in range(runs):
        path = dossier.build_dossier(event, response, filename="eval_measure.pdf")

    results["pdf_seconds"] = (time.perf_counter() - started) / runs
    results["pdf_bytes"] = os.path.getsize(path)

    # -- webhook -----------------------------------------------------
    url, server = webhook_dispatch._background_stub(quiet=True)

    try:
        webhook_dispatch.dispatch(event, response, url=url, verbose=False)  # warm-up

        runs = 10
        started = time.perf_counter()

        for _ in range(runs):
            sent = webhook_dispatch.dispatch(event, response, url=url, verbose=False)

        results["webhook_seconds"] = (time.perf_counter() - started) / runs
        results["webhook_ok"] = sent["ok"]
        results["sms_chars"] = sent["payload"]["channels"]["sms"]["characters"]

    finally:
        server.shutdown()
        server.server_close()

    # -- TTS ---------------------------------------------------------
    # Cache hit vs cold synthesis. The cache hit is the number that
    # matters, because it is the one a prefetched demo actually pays.
    phrase = response["spoken_alert"]

    try:
        started = time.perf_counter()
        cached_path = tts_alert.synthesize(phrase, "en")
        first = time.perf_counter() - started

        started = time.perf_counter()
        tts_alert.synthesize(phrase, "en")
        results["tts_cache_hit_seconds"] = time.perf_counter() - started
        results["tts_mp3_bytes"] = os.path.getsize(cached_path)
        results["tts_first_call_seconds"] = first

    except Exception as e:
        results["tts_error"] = "%s: %s" % (type(e).__name__, e)

    if allow_network:
        try:
            unique = "%s %d" % (phrase, int(time.time()))
            started = time.perf_counter()
            tts_alert.synthesize(unique, "en")
            results["tts_cold_seconds"] = time.perf_counter() - started
        except Exception as e:
            results["tts_cold_error"] = "%s: %s" % (type(e).__name__, e)

    # -- offline voice coverage --------------------------------------
    coverage = {}

    for code in ("en", "hi", "bn", "te", "ur"):
        try:
            voice = tts_alert._offline_voice_for(code)
            coverage[code] = voice.name if voice else None
        except Exception:
            coverage[code] = None

    results["offline_voices"] = coverage

    return results


# ============================================================
# REPORT
# ============================================================

def _row(label, value):
    return "| %s | %s |" % (label, value)


def write_report(rows, imports, gate, model, camera, outputs, path=REPORT):
    import yolo_trigger as yt      # for the gate constants the prose quotes

    """Write EVAL-TRIGGER.md. Same form and same rules as EVAL.md."""

    stamp = datetime.now().strftime("%Y-%m-%d")
    # platform.release() reports "10" on Windows 11, so use platform(),
    # which carries the build number that actually identifies the machine.
    machine = "%s, Python %s, CPU only" % (
        platform.platform(), platform.python_version())

    out = []
    add = out.append

    add("# Measured behaviour — the trigger half")
    add("")
    add("`yolo_trigger.py`, `tts_alert.py`, `dossier.py`, `webhook_dispatch.py`.")
    add("")
    add("Every number here came from running the code, on %s, on %s." % (stamp, machine))
    add("No number in this file is an estimate, and anything that could not")
    add("be measured says so rather than being filled in. Regenerate with:")
    add("")
    add("```")
    add("python eval_trigger.py")
    add("```")
    add("")
    add("Companion to `EVAL.md`, which measures the language service. The two")
    add("halves share a repo and nothing else — neither file's numbers say")
    add("anything about the other's code.")
    add("")

    # -- gate --------------------------------------------------------
    add("## Check suites")
    add("")
    add("| Suite | Result | Wall time | Command |")
    add("|---|---|---|---|")

    for row in rows:
        result = "%d/%d passed" % (row["passed"], row["total"]) \
            if row["passed"] is not None else ("passed" if row["ok"] else "FAILED")
        add("| %s | %s | %.2f s | `%s` |"
            % (row["label"], result, row["seconds"], row["command"]))

    add("")
    total_checks = sum(r["total"] or 0 for r in rows)
    total_passed = sum(r["passed"] or 0 for r in rows)
    add("**%d of %d checks pass.** Each suite runs as its own process, so none"
        % (total_passed, total_checks))
    add("of them can pass on state another one left behind.")
    add("")

    # -- suppression -------------------------------------------------
    add("## What the gate suppresses")
    add("")
    add("The reason the trigger is not wired straight to the detector. A")
    add("continuous violation fed through the real `TriggerGate`:")
    add("")
    add("| | Incidents |")
    add("|---|---|")
    add(_row("Ungated, %d frames (%ds at 30 fps)"
             % (gate["frames"], gate["simulated_seconds"]), "%d" % gate["ungated"]))
    add(_row("Through the gate, same input", "**%d**" % gate["fires"]))
    add(_row("Suppression", "%.1f%%" % (100.0 * (1 - gate["fires"] / gate["ungated"]))))
    add("")
    add("That is one minute of one person without a hardhat. The gate costs")
    add("**%.1f µs per frame**, so the thing that prevents the flood is far"
        % gate["microseconds_per_frame"])
    add("cheaper than a single inference.")
    add("")

    # -- import cost -------------------------------------------------
    add("## Import cost")
    add("")
    add("| Module | Import |")
    add("|---|---|")

    for module, seconds in imports.items():
        add(_row("`%s`" % module,
                 "%.3f s" % seconds if seconds is not None else "not measured"))

    add("")
    add("`yolo_trigger` does not import ultralytics at module scope — the")
    add("kiosk path, and the whole incident rehearsal, never load torch. That")
    add("is why the rehearsal runs on a machine with no camera and no GPU.")
    add("")

    # -- model -------------------------------------------------------
    add("## Detection")
    add("")

    if not model:
        add("**Not measured.** ultralytics was unavailable or the model would")
        add("not load, so no detection numbers are recorded here. The camera")
        add("path is unverified on this machine; the kiosk path is not affected.")
    else:
        add("Model: `%s`, %d classes, of which %d are violations: %s."
            % (model["model"], model["classes"], len(model["violation_classes"]),
               ", ".join("`%s`" % v for v in model["violation_classes"])))
        add("")
        add("| What | Measured |")
        add("|---|---|")
        add(_row("`from ultralytics import YOLO`", "%.2f s" % model["import_seconds"]))
        add(_row("Model load (cached weights)", "%.2f s" % model["load_seconds"]))
        add(_row("Inference alone, one %d×%d frame in memory"
                 % (model["image_width"], model["image_height"]),
                 "%.0f ms  (%.1f fps)"
                 % (model["inference_seconds"] * 1000, model["fps"])))
        add(_row("Full per-frame loop — frame in, gate decision out",
                 "**%.0f ms  (%.1f fps)**"
                 % (model["loop_seconds"] * 1000, model["loop_fps"])))
        add("")

        spread = abs(model["loop_fps"] - model["fps"]) / max(model["fps"], 1e-9)

        if spread < 0.15:
            add("Those two rows agree to within %.0f%%, which is run-to-run noise"
                % (spread * 100))
            add("on a busy laptop rather than a real difference. That is the")
            add("finding: everything the loop does outside the model — unpacking")
            add("boxes, the gate decision — costs microseconds against ~%.0f ms of"
                % (model["inference_seconds"] * 1000))
            add("inference, so the model is effectively the entire frame budget.")
            add("Neither number is reliably the larger one; quote either.")
        else:
            add("The loop figure is the one to quote. Inference alone is not a")
            add("frame rate, it is one term in it — the gap between the rows is")
            add("everything else a frame costs.")

        add("")
        add("RSS, which is what decides where this can run:")
        add("")
        add("```")
        add("baseline python      : %6.0f MB" % model["rss_baseline_mb"])
        add("+ ultralytics        : %6.0f MB" % model["rss_after_import_mb"])
        add("+ model loaded       : %6.0f MB" % model["rss_after_load_mb"])
        add("+ first inference    : %6.0f MB" % model["rss_after_inference_mb"])
        add("```")
        add("")
        add("Detections on `%s`, the reference image ultralytics ships, so this"
            % model["image"])
        add("row is reproducible on any machine:")
        add("")
        add("| Class | Confidence |")
        add("|---|---|")

        for name, confidence in sorted(model["detections"].items(),
                                       key=lambda kv: -kv[1]):
            add(_row("`%s`" % name, "%.3f" % confidence))

        add("")
        slower = min(model["loop_fps"], model["fps"])

        add("At **%.1f fps** the %d-of-%d confirmation costs **%.1f s** at best"
            % (slower, yt.HITS_REQUIRED, yt.WINDOW_FRAMES, yt.HITS_REQUIRED / slower))
        add("-- longer whenever the detector misses a frame -- before a")
        add("violation fires — the latency of the autonomous path, set by CPU")
        add("inference rather than by the gate.")
        add("")
        add("Both figures come from a decoded frame held in memory, so neither")
        add("includes camera capture. That is measured separately below.")

    add("")

    # -- live camera -------------------------------------------------
    add("## The live camera path")
    add("")

    if not camera:
        add("**Not measured.** No usable camera on this machine, so the")
        add("lens-to-incident path is unverified here. The kiosk trigger is")
        add("unaffected — it exists for exactly this case.")
    elif not camera.get("available"):
        add("**Not measured** — %s. The lens-to-incident path is unverified"
            % camera.get("reason", "no camera"))
        add("here; use the kiosk trigger, which exists for exactly this case.")
    else:
        add("Light hitting the sensor through to a gate decision, on the")
        add("built-in camera:")
        add("")
        add("| What | Measured |")
        add("|---|---|")
        add(_row("Resolution", "%d×%d" % (camera["width"], camera["height"])))
        add(_row("Capture alone", "%.0f ms  (%.1f fps)"
                 % (camera["capture_seconds"] * 1000, camera["capture_fps"])))
        add(_row("Full live path — capture, infer, gate",
                 "**%.0f ms  (%.1f fps)**"
                 % (camera["loop_seconds"] * 1000, camera["loop_fps"])))
        add(_row("Time to fire (%d hits of %d)" % (yt.HITS_REQUIRED, yt.WINDOW_FRAMES),
                 "**%.1f s** at best" % (yt.HITS_REQUIRED / camera["loop_fps"])))
        add("")

        if camera["detections"]:
            add("What the camera actually saw during the run:")
            add("")
            add("| Class | Confidence |")
            add("|---|---|")

            for name, confidence in sorted(camera["detections"].items(),
                                           key=lambda kv: -kv[1]):
                add(_row("`%s`" % name, "%.3f" % confidence))

            add("")
            add("So the lens-to-model path is verified end to end on this")
            add("machine, not inferred from the file-source numbers.")
        else:
            add("The camera detected nothing above the confidence floor during")
            add("the run — an empty room, most likely. The path is measured;")
            add("whether it *fires* was not exercised.")

        if not camera["lens_open"]:
            add("")
            add("**Frame variance was %.1f, which means the lens was covered.**"
                % camera["frame_std"])
            add("These timings are real but nothing could have been detected.")
            add("Open the privacy shutter and re-run before trusting the")
            add("detection row above.")

    add("")

    # -- outputs -----------------------------------------------------
    add("## Output stages")
    add("")
    add("| Stage | Measured |")
    add("|---|---|")
    add(_row("PDF dossier", "%.0f ms (%.1f KB)"
             % (outputs["pdf_seconds"] * 1000, outputs["pdf_bytes"] / 1024)))
    add(_row("Webhook round trip (local stub)",
             "%.1f ms" % (outputs["webhook_seconds"] * 1000)))

    if "tts_cache_hit_seconds" in outputs:
        add(_row("TTS cache hit", "%.2f ms" % (outputs["tts_cache_hit_seconds"] * 1000)))
        add(_row("TTS mp3 size", "%.1f KB" % (outputs["tts_mp3_bytes"] / 1024)))

    if "tts_cold_seconds" in outputs:
        add(_row("TTS cold synthesis (network)",
                 "**%.2f s**" % outputs["tts_cold_seconds"]))
    elif "tts_cold_error" in outputs:
        add(_row("TTS cold synthesis", "not measured (%s)" % outputs["tts_cold_error"][:60]))

    add(_row("SMS body", "%d / %d characters" % (outputs["sms_chars"], 160)))
    add("")

    if "tts_cold_seconds" in outputs and "tts_cache_hit_seconds" in outputs:
        ratio = outputs["tts_cold_seconds"] / max(outputs["tts_cache_hit_seconds"], 1e-9)
        add("Cold synthesis is **%.0f×** slower than a cache hit and needs the"
            % ratio)
        add("network at the moment the alert fires. Prefetch before a demo:")
        add("")
        add("```")
        add("python tts_alert.py --prefetch alerts.txt --lang hi")
        add("```")
        add("")

    # -- voices ------------------------------------------------------
    add("### Offline voice coverage on this machine")
    add("")
    add("| Language | Local voice |")
    add("|---|---|")

    for code, voice in outputs["offline_voices"].items():
        add(_row("`%s`" % code, voice or "**none** — falls back to gTTS (network)"))

    add("")
    add("Only languages with a local voice can be spoken offline. Everything")
    add("else needs either the network or a warm cache, which is the entire")
    add("reason the cache exists.")
    add("")

    # -- limits ------------------------------------------------------
    add("## What this does not measure")
    add("")
    add("Stated so the numbers above are not read as more than they are.")
    add("")
    add("- **Detection accuracy.** The reference-image row shows the model")
    add("  fires on a known input. It is not an accuracy figure: there is no")
    add("  labelled PPE eval set in this repo, so no precision or recall is")
    add("  claimed anywhere.")
    add("- **The real `/incident` service.** Every run here uses a local mock")
    add("  answering the assumed contract shape. Nothing is known about the")
    add("  teammate's endpoint until this is pointed at it.")
    if not (camera and camera.get("available")):
        add("- **A real camera.** Frames come from a file; no usable camera was")
        add("  available, so the lens-to-model path is unverified here.")
    elif not camera.get("detections"):
        add("- **A camera that sees a violation.** The live path is timed, but")
        add("  nothing was detected during the run, so firing is unexercised.")
    add("- **Real SMS, Telegram or Slack delivery.** The dispatch payload is")
    add("  built and sent for real; the recipient is a stub. No message was")
    add("  ever sent to a carrier or a workspace.")
    add("- **Sustained running.** Every measurement is short. Nothing here says")
    add("  what happens after an hour of watching a bay.")
    add("")

    text = "\n".join(out) + "\n"

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    return path


# ============================================================
# CLI
# ============================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Gate and measure the HazardWatch trigger half.")
    parser.add_argument("--quick", action="store_true",
                        help="skip the model/inference measurement (no torch load)")
    parser.add_argument("--no-write", action="store_true",
                        help="print the numbers, leave EVAL-TRIGGER.md alone")
    parser.add_argument("--no-network", action="store_true",
                        help="skip the cold-synthesis measurement")
    parser.add_argument("--audio", action="store_true",
                        help="let the incident rehearsal speak aloud")
    parser.add_argument("--gate-only", action="store_true",
                        help="run the suites and stop")
    parser.add_argument("--no-camera", action="store_true",
                        help="skip the live camera measurement")

    args = parser.parse_args(argv)

    print("HazardWatch trigger -- eval pipeline")
    print("%s, Python %s\n" % (platform.platform(), platform.python_version()))

    ok, rows = run_gate(audio=args.audio)

    if args.gate_only:
        print("\n%s" % ("gate passed" if ok else "GATE FAILED"))
        return 0 if ok else 1

    print("\nmeasure\n")

    print("  imports...", end="", flush=True)
    imports = measure_imports()
    print(" done")

    print("  gate suppression...", end="", flush=True)
    gate = measure_gate()
    print(" %d incidents from %d frames" % (gate["fires"], gate["ungated"]))

    model = None

    if args.quick:
        print("  model... skipped (--quick)")
    else:
        print("  model + inference (this loads torch, ~1 min)...", end="", flush=True)
        model = measure_model()
        print(" %s" % ("%.1f fps" % model["fps"] if model else "NOT MEASURED"))

    camera = None

    if args.quick or args.no_camera:
        print("  live camera... skipped")
    else:
        print("  live camera...", end="", flush=True)
        camera = measure_camera()

        if camera and camera.get("available"):
            print(" %.1f fps end to end" % camera["loop_fps"])
        else:
            print(" not available (%s)"
                  % (camera.get("reason", "unknown") if camera else "no result"))

    print("  output stages...", end="", flush=True)
    outputs = measure_outputs(allow_network=not args.no_network)
    print(" done")

    if args.no_write:
        print("\n(--no-write: EVAL-TRIGGER.md not touched)")
    else:
        path = write_report(rows, imports, gate, model, camera, outputs)
        print("\nwrote %s" % path)

    print("\n%s" % ("gate passed -- safe to push" if ok
                    else "GATE FAILED -- do not push"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

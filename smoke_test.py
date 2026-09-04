"""
Two smoke tests that answer two different questions.

**Is the deployed language service actually reachable and honest?**

    python smoke_test.py https://your-service.onrender.com
    python smoke_test.py https://your-service.onrender.com --key YOUR_API_KEY

**Does the HazardWatch trigger half work end to end?**

    python smoke_test.py incident

**Two scenarios for the autonomous decisions**, each dry-runnable alone:

    python smoke_test.py suppressed     # a borderline detection is held
                                        # and never reconfirms -- nothing fires
    python smoke_test.py escalation     # a HIGH incident nobody acknowledges
                                        # escalates by itself

The first proves the system declines to act and says why; the second
proves it acts without being asked. Both run the real modules against
local stubs. `escalation` takes --window SECONDS and --no-audio.

That second one walks a whole incident through -- fake detection, mock
/incident, spoken alert, PDF dossier, webhook dispatch -- with no
camera, no live backend and no webhook endpoint, then prints and opens
everything it produced. It is the dry run to do before a live demo.
Flags: --no-audio, --no-open, --zone BAY-7, --lang hi, --webhook URL.

Almost nothing in that path is faked. The trigger gate, the HTTP
client, the TTS backend choice, the PDF builder and the webhook payload
all run for real; the two stubs stand in for the two things this half
of the project does not own -- the camera and the teammate's incident
service. A green run proves the trigger half works and claims nothing
about the real endpoint.

The rest of this file is the deployed-service test:

Exists because "the container started" and "the service works" are
different claims, and only the second one is worth making. Running the
app on localhost proves the code runs; it proves nothing about the
image, the platform's port binding, the environment variables, or
whether anything outside your network can reach it.

Exits 0 if every check passed, 1 otherwise, so it can gate a deploy.

Checks are ordered cheapest-first and the expensive one is last: the
first Latin-script request pays the model load (~25s), or returns "en"
with semantic_tier_used false on a deployment built without the model.
Both are passes -- the test asserts the service is HONEST about which
tier it has, not that it has both.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


passed = []
failed = []


def call(base, path, key, method="GET", body=None, timeout=60):
    """Returns (status, parsed_json_or_text). Never raises."""

    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None

    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")

    if key:
        request.add_header("X-API-Key", key)

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


def check(name, condition, detail=""):
    if condition:
        passed.append(name)
        print("  PASS  %-34s %s" % (name, detail))
    else:
        failed.append(name)
        print("  FAIL  %-34s %s" % (name, detail))


# ====================================================================
# INCIDENT REHEARSAL
#
# One command that walks a whole incident through the trigger half
# without a camera, without the teammate's service, and without a
# webhook endpoint:
#
#     fake detection -> mock /incident -> TTS -> PDF -> webhook
#
# The point is that almost nothing here is faked. The gate, the trigger
# path, the HTTP client, the TTS backend selection, the PDF builder and
# the webhook payload are all the real code running for real. Only two
# things are stubs, and both are stubs of things somebody ELSE owns:
# the camera (replaced by scripted frames) and the incident service
# (replaced by a local HTTP server that answers the contract shape).
#
# So a green run here means the trigger half works. It says nothing
# about the teammate's endpoint -- that is what pointing --api at the
# real service is for.
# ====================================================================

# What the mock /incident returns. This is the assumed contract shape
# -- {severity, steps, contraindication, spoken_alert} -- and it is the
# single place to update when the real API_CONTRACT.md lands.
def mock_incident_response(event):
    """Answer a hazard event the way the incident service is expected to."""

    bay = event.get("bay_id") or "the bay"
    substance = (event.get("substance_name") or event.get("substance_code")
                 or "an unidentified substance")

    return {
        "severity": "high",
        "steps": [
            "Stop work in %s and clear personnel to the upwind muster point." % bay,
            "Isolate the %s line at the bay shutoff valve." % substance.split()[0].lower(),
            "Issue a hardhat and face shield before anyone re-enters %s." % bay,
            "Log the exposure window and notify the shift safety officer.",
        ],
        "contraindication": "Do not flush the spill with water under pressure -- "
                            "it will generate heat and spatter caustic solution.",
        "spoken_alert": "Hazard in %s. Caustic spill. Clear the bay upwind and "
                        "wait for the safety officer." % bay.replace("-", " ").lower(),
        "latency_ms": 12.4,
    }


class _IncidentHandler(BaseHTTPRequestHandler):
    """Stands in for the teammate's POST /incident."""

    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)

        try:
            event = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            event = {}

        _IncidentHandler.requests.append((self.path, event))

        if self.path.rstrip("/") != "/incident":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"detail": "not found"}')
            return

        body = json.dumps(mock_incident_response(event)).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-ID", "mock-%s" % len(_IncidentHandler.requests))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start_mock_incident():
    """Mock incident service on a free port. Returns (base_url, server)."""

    _IncidentHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _IncidentHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    return "http://127.0.0.1:%d" % server.server_port, server


def run_incident_rehearsal(argv):
    """The full dry run. Returns a process exit code."""

    zone = "BAY-3"
    substance = "Sodium hydroxide (50% solution)"
    language = "en"
    speak = "--no-audio" not in argv
    open_pdf = "--no-open" not in argv
    webhook_url = None

    if "--zone" in argv:
        zone = argv[argv.index("--zone") + 1]

    if "--lang" in argv:
        language = argv[argv.index("--lang") + 1]

    if "--webhook" in argv:
        webhook_url = argv[argv.index("--webhook") + 1]

    import dossier
    import tts_alert
    import webhook_dispatch
    import yolo_trigger

    print("HazardWatch incident rehearsal -- no camera, no live backend\n")

    # -- 1. fake the detection, through the real gate ----------------
    # 30 frames of a person with no hardhat, i.e. one second of camera.
    # The gate must turn that into exactly one incident.
    print("1. detection")

    gate = yolo_trigger.TriggerGate(cooldown=45, hits=3, window=8)
    fires = []

    # Every third frame is a miss, because the detector misses about
    # half of them in reality (EVAL-ACCURACY.md). A rehearsal that feeds
    # 30 perfect frames tests a detector this project does not have.
    for index in range(30):
        seen = [] if index % 3 == 2 else ["NO-Hardhat"]
        fires += gate.observe(zone, seen)

    check("gate fires once per event", len(fires) == 1,
          "30 frames (1 in 3 missed) -> %d incident(s)" % len(fires))
    check("gate mutes the repeat", gate.remaining_mute(zone, "NO-Hardhat") > 40,
          "%.0fs of cooldown left" % gate.remaining_mute(zone, "NO-Hardhat"))

    # -- 2. the real trigger path against a mock service -------------
    print("\n2. POST /incident (mock service)")

    base, server = _start_mock_incident()

    try:
        result = yolo_trigger.fire_incident(
            "NO-Hardhat", zone, source="camera", confidence=0.91,
            substance=substance, language=language, camera_id="0", base=base,
        )

        check("incident opened", result["ok"], "status %s" % result["status"])

        body = result["response"] if result["ok"] else {}
        event = result["event"]

        check("mock received the event",
              len(_IncidentHandler.requests) == 1
              and _IncidentHandler.requests[0][0] == "/incident",
              "path %s" % (_IncidentHandler.requests[0][0]
                           if _IncidentHandler.requests else "none"))

        sent = _IncidentHandler.requests[0][1] if _IncidentHandler.requests else {}
        check("event carries bay_id + incident_type + source",
              sent.get("bay_id") == zone
              and sent.get("incident_type") == "NO-Hardhat"
              and sent.get("source") == "camera",
              "bay_id=%s incident_type=%s source=%s"
              % (sent.get("bay_id"), sent.get("incident_type"), sent.get("source")))

        check("response has the contract fields",
              all(k in body for k in
                  ("severity", "steps", "contraindication", "spoken_alert")),
              ", ".join(sorted(body)) or "empty")

        # The kiosk fallback must reach the same endpoint the same way.
        kiosk = yolo_trigger.fire_incident(
            "NO-Hardhat", zone, source="kiosk", substance=substance,
            language=language, base=base)

        kiosk_sent = _IncidentHandler.requests[-1][1]
        check("kiosk takes the same path",
              kiosk["ok"] and kiosk_sent.get("source") == "kiosk"
              and kiosk_sent.get("incident_type") == sent.get("incident_type"),
              "same endpoint, only source differs")

    finally:
        server.shutdown()
        server.server_close()

    if not result["ok"]:
        print("\nThe mock service did not answer. Nothing downstream can run.")
        return 1

    # -- 3. speak it -------------------------------------------------
    print("\n3. spoken alert")
    print("   \"%s\"" % body["spoken_alert"])

    if speak:
        spoken = tts_alert.speak(body["spoken_alert"], language)
        check("alert spoken aloud", spoken["ok"],
              "via %s%s" % (spoken["backend"],
                            "" if spoken["ok"] else " -- %s" % spoken["reason"]))
    else:
        print("   (--no-audio: not played)")

    # -- 4. the dossier ----------------------------------------------
    print("\n4. dossier")

    pdf = dossier.build_dossier(event, body)
    size = os.path.getsize(pdf) if os.path.exists(pdf) else 0

    check("pdf generated", size > 1200, "%s (%d bytes)" % (os.path.basename(pdf), size))

    code, title, source = dossier.resolve_citation(event, body)
    check("citation resolved", bool(code), "%s -- %s (%s)" % (code, title, source))

    # -- 5. the webhook ----------------------------------------------
    print("\n5. webhook dispatch")

    stub = None

    if not webhook_url:
        webhook_url, stub = webhook_dispatch._background_stub(quiet=True)
        print("   no --webhook given, using a local stub at %s" % webhook_url)

    try:
        sent_alert = webhook_dispatch.dispatch(event, body, url=webhook_url,
                                               verbose=False)
        check("webhook delivered", sent_alert["ok"],
              "status %s -> %s" % (sent_alert["status"], webhook_url))

        sms = sent_alert["payload"]["channels"]["sms"]
        check("sms fits one segment", sms["characters"] <= webhook_dispatch.SMS_LIMIT,
              "%d chars: %s" % (sms["characters"], sms["body"]))

    finally:
        if stub:
            stub.shutdown()
            stub.server_close()

    # -- outputs -----------------------------------------------------
    print("\noutputs")
    print("   PDF   %s" % pdf)

    if speak and tts_alert.CACHE_DIR and os.path.isdir(tts_alert.CACHE_DIR):
        print("   audio %s" % tts_alert.CACHE_DIR)

    payload_path = os.path.join(os.path.dirname(pdf), "last_webhook_payload.json")

    with open(payload_path, "w", encoding="utf-8") as handle:
        json.dump(sent_alert["payload"], handle, indent=2, ensure_ascii=False)

    print("   alert %s" % payload_path)

    if open_pdf:
        dossier.open_pdf(pdf)

    print("\n%d passed, %d failed" % (len(passed), len(failed)))

    if failed:
        print("Failed: %s" % ", ".join(failed))
        return 1

    print("Full incident path works end to end. The camera and the incident")
    print("service were stubbed; everything between them was the real thing.")
    return 0


# ====================================================================
# SCENARIO: a borderline detection is correctly suppressed
# ====================================================================

def run_suppression_scenario(argv):
    """
    A marginal box appears, never reconfirms, and nothing fires.

    The whole path runs for real -- ConfidenceRouter, TriggerGate,
    fire_incident -- against a mock incident service that counts what
    reaches it. The assertion that matters is that the count stays zero
    while the audit trail explains why.
    """

    import confidence_router
    import yolo_trigger

    print("Scenario: borderline detection, never reconfirmed\n")

    now = [1000.0]
    router = confidence_router.ConfidenceRouter(clock=lambda: now[0])
    gate = yolo_trigger.TriggerGate()
    base, server = _start_mock_incident()

    try:
        # A clear violation first, so the run proves the path is live and
        # a zero at the end means "suppressed", not "nothing was wired".
        for _ in range(4):
            now[0] += 0.1
            for violation in gate.observe("BAY-1", router.acting(
                    "BAY-1", {"NO-Hardhat": 0.91})):
                yolo_trigger.fire_incident(violation, "BAY-1", source="camera",
                                           confidence=0.91, base=base)

        control = len(_IncidentHandler.requests)
        check("control: a high-confidence detection does fire",
              control == 1, "%d incident(s) from conf 0.91" % control)

        # Now the borderline one. Seen once, then gone.
        now[0] += 1.0
        first = router.route("BAY-7", {"NO-Mask": 0.55})

        check("borderline is held, not fired",
              first[0].action == confidence_router.VERIFYING,
              first[0].reason[:62])

        for _ in range(12):
            now[0] += 0.1
            for violation in gate.observe("BAY-7", router.acting("BAY-7", {})):
                yolo_trigger.fire_incident(violation, "BAY-7", source="camera",
                                           base=base)

        # Past the verify window with no reconfirmation.
        now[0] += 4.0
        expired = router.route("BAY-7", {})

        check("unconfirmed borderline is suppressed",
              expired and expired[0].action == confidence_router.SUPPRESSED,
              expired[0].reason[:62] if expired else "nothing recorded")

        check("nothing reached the incident service",
              len(_IncidentHandler.requests) == control,
              "still %d incident(s) -- the marginal box opened none"
              % len(_IncidentHandler.requests))

        check("the suppression is auditable",
              router.summary().get(confidence_router.SUPPRESSED) == 1,
              "summary: %s" % router.summary())

    finally:
        server.shutdown()
        server.server_close()

    print("\ndecision trail")
    router.print_audit()

    print("\n%d passed, %d failed" % (len(passed), len(failed)))

    if failed:
        print("Failed: %s" % ", ".join(failed))
        return 1

    print("A marginal detection was held, never reconfirmed, and suppressed.")
    print("No incident was opened, and the trail above says exactly why.")
    return 0


# ====================================================================
# SCENARIO: a HIGH incident nobody acknowledges
# ====================================================================

def run_escalation_scenario(argv):
    """
    A real incident fires, nobody presses the button, and the system
    escalates on its own.

    Both webhook payloads land on a local stub so the second can be
    compared against the first -- an escalation that is merely a resend
    would pass a weaker test than this one.
    """

    import dossier
    import escalation_watcher
    import webhook_dispatch
    import yolo_trigger

    window = 4.0
    speak = "--no-audio" not in argv

    if "--window" in argv:
        window = float(argv[argv.index("--window") + 1])

    print("Scenario: HIGH incident, nobody acknowledges (%.0fs window)\n" % window)

    base, server = _start_mock_incident()
    hook_url, hook = webhook_dispatch._background_stub(quiet=True)

    try:
        result = yolo_trigger.fire_incident(
            "NO-Hardhat", "BAY-3", source="camera", confidence=0.93,
            substance="Sodium hydroxide (50% solution)", base=base)

        check("incident opened", result["ok"], "status %s" % result["status"])

        event, body = result["event"], result["response"]
        pdf = dossier.build_dossier(event, body)
        pages_before = _pdf_pages(pdf)

        first = webhook_dispatch.dispatch(event, body, url=hook_url, verbose=False)
        check("first alert dispatched", first["ok"], "the routine notification")

        watcher = escalation_watcher.EscalationWatcher(
            window=window, speak=speak, webhook_url=hook_url)
        incident_id = os.path.splitext(os.path.basename(pdf))[0]
        watch = watcher.watch(incident_id, event, body, pdf=pdf)

        check("watcher armed on a high-severity incident",
              watch is not None and watch.state == "waiting",
              "%.0fs to acknowledge" % window)

        check("watcher does not block the caller",
              watcher.pending() == [incident_id],
              "camera would still be running here")

        print("\n   nobody is pressing the button...\n")
        settled = watcher.wait_for_escalations(timeout=window + 45)

        check("escalation completed", settled and watch.state == "escalated",
              "after %.0fs of silence" % watch.elapsed())

        # -- the second payload must not be a resend -------------------
        received = list(webhook_dispatch._StubHandler.received)
        check("stub received both alerts", len(received) == 2,
              "%d payload(s)" % len(received))

        if len(received) == 2:
            routine, escalated = received

            check("second payload is tagged as an escalation",
                  escalated.get("escalation") is True
                  and escalated.get("escalation_tag") == escalation_watcher.ESCALATION_TAG,
                  escalated.get("escalation_tag"))

            check("second payload is not a copy of the first",
                  escalated["channels"]["sms"]["body"]
                  != routine["channels"]["sms"]["body"],
                  "different body, not a retry")

            check("escalation goes to a different channel",
                  escalated["channels"]["slack"]["channel"]
                  != routine["channels"]["slack"]["channel"],
                  "%s -> %s" % (routine["channels"]["slack"]["channel"],
                                escalated["channels"]["slack"]["channel"]))

            check("escalation records the silence",
                  escalated.get("unacknowledged_seconds", 0) > 0,
                  "%.1fs unacknowledged" % escalated.get("unacknowledged_seconds", 0))

            check("payload states nothing real was contacted",
                  "No emergency service" in escalated.get("note", ""),
                  escalated.get("note", "")[:48])

        # -- the dossier gained an addendum ----------------------------
        pages_after = _pdf_pages(pdf)

        if pages_before is None:
            print("  note: pypdf not installed -- page count check skipped")
        else:
            check("dossier gained an addendum page",
                  pages_after == pages_before + 1,
                  "%d page(s) -> %d" % (pages_before, pages_after))

        if speak:
            check("re-alert was spoken",
                  (watch.result or {}).get("spoke", {}).get("ok"),
                  "via %s" % (watch.result or {}).get("spoke", {}).get("backend"))

    finally:
        watcher_cleanup = locals().get("watcher")

        if watcher_cleanup:
            watcher_cleanup.cancel_all()

        hook.shutdown()
        hook.server_close()
        server.shutdown()
        server.server_close()

    print("\noutputs")
    print("   PDF  %s  (open it -- the addendum is the last page)" % pdf)

    print("\n%d passed, %d failed" % (len(passed), len(failed)))

    if failed:
        print("Failed: %s" % ", ".join(failed))
        return 1

    print("Nobody acknowledged. The system escalated on its own: a distinct")
    print("higher-urgency alert, an addendum on the report, and the alert")
    print("spoken again. Demo workflow -- no real responder was contacted.")
    return 0


def _pdf_pages(path):
    """Page count, or None if pypdf is unavailable."""

    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except ImportError:
        return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    if sys.argv[1] in ("incident", "--incident", "rehearsal"):
        return run_incident_rehearsal(sys.argv[2:])

    if sys.argv[1] in ("suppressed", "suppression"):
        return run_suppression_scenario(sys.argv[2:])

    if sys.argv[1] in ("escalation", "escalate"):
        return run_escalation_scenario(sys.argv[2:])

    base = sys.argv[1]
    key = None

    if "--key" in sys.argv:
        key = sys.argv[sys.argv.index("--key") + 1]

    print("Testing %s\n" % base)

    # -- reachability ------------------------------------------------
    started = time.time()
    status, health = call(base, "/health", key)
    reach_ms = (time.time() - started) * 1000

    check("reachable", status == 200, "%s in %.0fms" % (status, reach_ms))

    if status != 200:
        print("\nNot reachable. Nothing else can be tested.")
        print("Response: %r" % (health,))
        return 1

    check(
        "health shape",
        isinstance(health, dict) and "tiers" in health and "languages" in health,
        json.dumps(health),
    )

    semantic = health.get("tiers", {}).get("semantic")

    # -- native script: must never need the model --------------------
    started = time.time()
    status, body = call(base, "/detect", key, "POST", {"text": "मुझे मदद चाहिए"})
    native_ms = (time.time() - started) * 1000

    check(
        "detect native hindi",
        status == 200 and body.get("language") == "hi"
        and body.get("script") == "native" and body.get("method") == "script",
        "%s %.0fms" % (json.dumps(body, ensure_ascii=False), native_ms),
    )

    status, body = call(base, "/detect", key, "POST", {"text": "আমার সাহায্য দরকার"})
    check(
        "detect native bengali",
        status == 200 and body.get("language") == "bn",
        json.dumps(body, ensure_ascii=False),
    )

    # -- error handling ----------------------------------------------
    status, body = call(base, "/detect", key, "POST", {"text": ""})
    check("empty text -> 400", status == 400, str(body))

    status, body = call(base, "/detect", key, "POST", {"text": "a" * 6000})
    check("oversized -> 400", status == 400, str(body))

    status, body = call(base, "/detect", key, "POST", {})
    check("missing field -> 422", status == 422, "status %s" % status)

    status, body = call(base, "/nope", key)
    check("unknown route -> 404", status == 404, "status %s" % status)

    # -- translation (needs a provider key on the server) ------------
    status, body = call(
        base, "/translate", key, "POST",
        {"text": "I need help. Case NHAA-2026-27F9A605.", "target_language": "hi"},
        timeout=90,
    )

    if status == 200 and body.get("translated"):
        check(
            "translate en->hi",
            "NHAA-2026-27F9A605" in (body.get("translation") or ""),
            "identifier preserved: %s" % json.dumps(body, ensure_ascii=False)[:120],
        )
    else:
        check(
            "translate en->hi",
            status == 200 and body.get("reason") == "translation_unavailable",
            "no provider key on the server -- degraded honestly: %s" % (body,),
        )

    # -- semantic tier, last because it may cost ~25s ----------------
    print("\n  (next check may take ~25s -- first model load)")

    started = time.time()
    status, body = call(
        base, "/detect", key, "POST",
        {"text": "Mujhe madad chahiye, mera pati mujhe maarta hai."},
        timeout=120,
    )
    romanized_ms = (time.time() - started) * 1000

    if status == 200 and body.get("semantic_tier_used"):
        check(
            "romanized (semantic tier live)",
            body.get("language") == "hi",
            "%s in %.0fms" % (json.dumps(body), romanized_ms),
        )
    else:
        check(
            "romanized (script tier only)",
            status == 200 and body.get("language") == "en"
            and not body.get("semantic_tier_used"),
            "built without the model, reported honestly: %s" % json.dumps(body),
        )

    status, health_after = call(base, "/health", key)
    check(
        "health reports tier truthfully",
        health_after.get("tiers", {}).get("semantic") is not None,
        "semantic: %s -> %s" % (semantic, health_after.get("tiers", {}).get("semantic")),
    )

    # -- summary -----------------------------------------------------
    print("\n%d passed, %d failed" % (len(passed), len(failed)))

    if failed:
        print("Failed: %s" % ", ".join(failed))
        return 1

    print("Service is live and reachable from this machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

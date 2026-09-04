"""
Nobody acknowledged the alarm. Escalate on your own.

The trigger's job ends when the incident is opened, the alert is spoken
and the dossier is written. That leaves the most dangerous case
unhandled: everything fired correctly and *nobody was there*. A hazard
system that announces into an empty bay and then goes quiet has not done
its job, it has documented its own failure.

So a HIGH or CRITICAL incident starts a watcher. If an acknowledgement
arrives within the window, the watcher stands down and records it. If
the window closes with no ack, the system escalates by itself:

  1. A second webhook payload, tagged ESCALATED - NO ACK, distinct from
     the first in urgency, recipients and content -- not a resend.
  2. An addendum page appended to the existing dossier PDF, recording
     the missed acknowledgement and the elapsed time.
  3. The audio alert again, reworded for urgency.

**It never blocks.** Each watch is a daemon timer thread, so the camera
keeps watching the bay while the clock runs. Acknowledging is a plain
function call -- `acknowledge(incident_id)` -- which is what a kiosk
button, a keypress, or a future HTTP endpoint would call. The stand-in
is deliberate and labelled; nothing here contacts a real responder.

Run it:

    python escalation_watcher.py --selftest      # fast fake clock, silent
    python escalation_watcher.py --demo          # 6s window, watch it escalate
    python escalation_watcher.py --demo --ack 2  # acknowledge after 2s instead
"""

import argparse
import os
import sys
import threading
import time
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

# Seconds to wait for an acknowledgement before escalating.
#
# 45 matches the trigger's cooldown so the two do not fight: a bay that
# re-fires the same violation does so at roughly the moment the previous
# incident escalates, rather than mid-window.
ACK_WINDOW = float(os.getenv("HAZARDWATCH_ACK_WINDOW", "45"))

# Severities that get a watcher. A medium-severity PPE reminder does not
# need somebody to press a button.
WATCHED_SEVERITIES = ("high", "critical")

# Tag on the second payload. The receiving end must be able to tell an
# escalation from a retry without diffing bodies.
ESCALATION_TAG = "ESCALATED - NO ACK"

# Escalation routes, defined locally. Nothing here contacts a real
# service; the route is recorded so the demo can show intent.
ROUTES = ("supervisor review", "site safety officer", "shift controller")


def _now():
    return datetime.now(timezone.utc)


def log(message):
    print("%s  [watcher] %s" % (datetime.now().strftime("%H:%M:%S"), message),
          flush=True)


class Watch(object):
    """One incident being watched for acknowledgement."""

    __slots__ = ("incident_id", "event", "response", "pdf", "started_at",
                 "window", "acked_at", "acked_by", "escalated_at", "timer",
                 "route", "result", "finished")

    def __init__(self, incident_id, event, response, pdf, window, route):
        self.incident_id = incident_id
        self.event = event
        self.response = response
        self.pdf = pdf
        self.window = window
        self.route = route
        self.started_at = _now()
        self.acked_at = None
        self.acked_by = None
        self.escalated_at = None
        self.timer = None
        self.result = None
        # Set once escalation has finished all three actions. Speaking
        # takes seconds; without something to wait on, a caller that
        # exits promptly kills the thread mid-sentence.
        self.finished = threading.Event()

    @property
    def state(self):
        if self.acked_at:
            return "acknowledged"

        if self.escalated_at:
            return "escalated"

        return "waiting"

    def elapsed(self):
        end = self.acked_at or self.escalated_at or _now()
        return (end - self.started_at).total_seconds()


class EscalationWatcher(object):
    """
    Watches fired incidents for acknowledgement and escalates on silence.

    Timers are daemon threads: the process can exit without waiting, and
    the camera loop is never blocked by a pending window.
    """

    def __init__(self, window=ACK_WINDOW, on_escalate=None, speak=True,
                 webhook_url=None, timer_factory=None):
        self.window = window
        self.on_escalate = on_escalate
        self.speak = speak
        self.webhook_url = webhook_url
        # Injected so tests can fire the timeout without sleeping.
        self.timer_factory = timer_factory or threading.Timer
        self.watches = {}
        self._lock = threading.Lock()

    # -- starting and stopping ---------------------------------------

    def watch(self, incident_id, event, response, pdf=None, route=ROUTES[0]):
        """
        Start watching a fired incident. Returns the Watch, or None if
        the severity does not warrant one.

        Non-blocking: the timer runs on its own thread.
        """

        severity = str((response or {}).get("severity") or "").lower()

        if severity not in WATCHED_SEVERITIES:
            return None

        with self._lock:
            if incident_id in self.watches:
                return self.watches[incident_id]

            watch = Watch(incident_id, event or {}, response or {}, pdf,
                          self.window, route)
            watch.timer = self.timer_factory(self.window, self._on_timeout,
                                             args=(incident_id,))
            watch.timer.daemon = True
            self.watches[incident_id] = watch

        watch.timer.start()
        log("watching %s (%s) -- %.0fs to acknowledge"
            % (incident_id, severity, self.window))
        return watch

    def acknowledge(self, incident_id, by="kiosk"):
        """
        Stand down a watch. This is the seam a kiosk button, a keypress,
        or a future HTTP endpoint calls -- it is a plain function on
        purpose, so the stand-in and the real thing are the same call.

        Returns the Watch, or None if there was nothing to acknowledge.
        """

        with self._lock:
            watch = self.watches.get(incident_id)

            if watch is None or watch.state != "waiting":
                return None

            watch.acked_at = _now()
            watch.acked_by = by

            if watch.timer:
                watch.timer.cancel()

            watch.finished.set()

        log("%s acknowledged by %s after %.1fs -- no escalation"
            % (incident_id, by, watch.elapsed()))
        return watch

    def cancel_all(self):
        """Stop every pending timer. For shutdown."""

        with self._lock:
            for watch in self.watches.values():
                if watch.timer and watch.state == "waiting":
                    watch.timer.cancel()

    def pending(self):
        return sorted(i for i, w in self.watches.items() if w.state == "waiting")

    def wait_for_escalations(self, timeout=30.0):
        """
        Block until every watch reaches a terminal state, or `timeout`.

        Escalating speaks aloud, which takes seconds. A caller that exits
        the moment the timer fires kills that on a daemon thread halfway
        through -- the alert is cut off and pyttsx3 complains on the way
        down. Anything that shuts down after an escalation should wait
        here first. Returns True if everything settled.

        It waits on watches that are still `waiting` too, and that is the
        whole point. The first version skipped them, which meant a timer
        that had not fired *yet* was treated as nothing to wait for --
        the call returned instantly and the caller's cancel_all() then
        killed the timer a fraction before it would have escalated. The
        escalation never happened and the report said "waiting".

        A watch whose window has not elapsed will hold this until it
        does, which is why `timeout` exists.
        """

        deadline = time.monotonic() + timeout
        settled = True

        for watch in list(self.watches.values()):
            remaining = deadline - time.monotonic()

            if remaining <= 0 or not watch.finished.wait(remaining):
                settled = False

        return settled

    # -- the escalation ----------------------------------------------

    def _on_timeout(self, incident_id):
        """Fires on the timer thread when the window closes unacknowledged."""

        with self._lock:
            watch = self.watches.get(incident_id)

            if watch is None or watch.state != "waiting":
                return          # acknowledged in the gap; nothing to do

            watch.escalated_at = _now()

        try:
            watch.result = self.escalate(watch)
        except Exception as e:
            # An escalation that raises must not kill the timer thread
            # silently. Say so and keep the recorded state honest.
            watch.result = {"error": "%s: %s" % (type(e).__name__, e)}
            log("escalation for %s FAILED: %s" % (incident_id, watch.result["error"]))

        if self.on_escalate:
            try:
                self.on_escalate(watch)
            except Exception:
                pass

        watch.finished.set()
        return watch.result

    def escalate(self, watch):
        """
        The three escalation actions. Each is independent: one failing
        must not stop the others, because they reach different people.
        """

        elapsed = watch.elapsed()
        log("NO ACK for %s after %.0fs -- escalating via %s"
            % (watch.incident_id, elapsed, watch.route))

        done = {"incident_id": watch.incident_id, "elapsed_seconds": elapsed,
                "route": watch.route}

        # 1. A distinct, higher-urgency webhook.
        try:
            done["webhook"] = self._dispatch(watch, elapsed)
        except Exception as e:
            done["webhook_error"] = "%s: %s" % (type(e).__name__, e)
            log("  webhook failed: %s" % done["webhook_error"])

        # 2. The dossier addendum.
        try:
            done["addendum"] = self._append_addendum(watch, elapsed)
        except Exception as e:
            done["addendum_error"] = "%s: %s" % (type(e).__name__, e)
            log("  addendum failed: %s" % done["addendum_error"])

        # 3. The alert again, louder in wording.
        if self.speak:
            try:
                done["spoke"] = self._speak(watch, elapsed)
            except Exception as e:
                done["speech_error"] = "%s: %s" % (type(e).__name__, e)
                log("  re-alert failed: %s" % done["speech_error"])

        return done

    def escalated_payload(self, watch, elapsed):
        """
        The second payload. Deliberately not a copy of the first.

        Different tag, different urgency, different recipients, and it
        leads with the fact that nobody responded -- which is the new
        information. A receiving system must be able to tell this from a
        retry without comparing bodies.
        """

        import webhook_dispatch

        base = webhook_dispatch.build_alert_payload(watch.event, watch.response)

        bay = watch.event.get("bay_id") or "the bay"
        hazard = watch.event.get("incident_type") or "hazard"

        headline = ("[%s] %s in %s unacknowledged for %.0fs"
                    % (ESCALATION_TAG, hazard, bay, elapsed))

        sms_body, truncated = webhook_dispatch._fit_sms(
            "%s. Escalating to %s. No response at the bay."
            % (headline, watch.route))

        base.update({
            "escalation": True,
            "escalation_tag": ESCALATION_TAG,
            "escalation_route": watch.route,
            "unacknowledged_seconds": round(elapsed, 1),
            "original_incident": watch.incident_id,
            "urgency": "immediate",
            "note": ("Demo workflow. No emergency service has been contacted. "
                     "Escalation is recorded locally only."),
        })

        base["channels"] = {
            "sms": {
                "to": "+1XXXXXXXXXX",
                "body": sms_body,
                "characters": len(sms_body),
                "truncated": truncated,
                "encoding": "GSM-7" if sms_body.isascii() else "UCS-2",
                "segment_limit": webhook_dispatch.sms_limit_for(sms_body),
            },
            "telegram": {
                "chat_id": "@hazardwatch_ops",
                "parse_mode": "Markdown",
                "text": "*%s*\n\nOriginal incident: %s\nRoute: %s\n"
                        "Elapsed without acknowledgement: %.0fs"
                        % (headline, watch.incident_id, watch.route, elapsed),
            },
            "slack": {
                "channel": "#hazardwatch-escalations",   # a different room
                "text": headline,
                "blocks_markdown": "*%s*\nNobody acknowledged the original "
                                   "alert at %s. Escalated to %s."
                                   % (headline, bay, watch.route),
            },
        }

        return base

    def _dispatch(self, watch, elapsed):
        import webhook_dispatch

        payload = self.escalated_payload(watch, elapsed)
        url = self.webhook_url or webhook_dispatch.WEBHOOK_URL

        if not url:
            log("  escalation webhook built, no endpoint configured")
            return {"ok": False, "reason": "no endpoint configured",
                    "payload": payload}

        status, body = webhook_dispatch.post_json(url, payload)
        ok = status is not None and 200 <= status < 300
        log("  escalation webhook -> %s" % (status if ok else "FAILED"))
        return {"ok": ok, "status": status, "detail": str(body)[:120],
                "payload": payload}

    def _append_addendum(self, watch, elapsed):
        """
        Append a page to the existing dossier recording the missed ack.

        Appended rather than regenerated: the original report is the
        record of what was known when the incident fired, and rewriting
        it would erase that. The addendum is a later page saying what
        happened next, which is how an incident file actually works.
        """

        if not watch.pdf or not os.path.exists(watch.pdf):
            log("  no dossier to append to")
            return None

        import dossier

        addendum_response = {
            "severity": watch.response.get("severity"),
            "steps": [
                "Confirm whether anyone is present at %s."
                % (watch.event.get("bay_id") or "the bay"),
                "Escalated to %s after %.0f seconds without acknowledgement."
                % (watch.route, elapsed),
                "Record why the original alert was not acknowledged.",
            ],
            "spoken_alert": watch.response.get("spoken_alert"),
            "regulatory_citation": watch.response.get("regulatory_citation"),
        }

        contraindication = watch.response.get("contraindication")

        if contraindication:
            addendum_response["contraindication"] = contraindication

        addendum_event = dict(watch.event)
        addendum_event["incident_type"] = "%s -- %s" % (
            watch.event.get("incident_type") or "hazard", ESCALATION_TAG)

        page = dossier.build_dossier(
            addendum_event, addendum_response,
            filename="_addendum_%s.pdf" % watch.incident_id)

        merged = self._merge(watch.pdf, page)

        if merged:
            log("  addendum appended to %s" % os.path.basename(watch.pdf))

        try:
            os.remove(page)
        except OSError:
            pass

        return watch.pdf if merged else page

    def _merge(self, original, addendum):
        """Append `addendum` to `original` in place. Returns True on success."""

        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            log("  pypdf not installed -- addendum left as a separate file")
            return False

        writer = PdfWriter()

        for page in PdfReader(original).pages:
            writer.add_page(page)

        for page in PdfReader(addendum).pages:
            writer.add_page(page)

        # Written beside the original and moved into place, so a failure
        # halfway through cannot leave a truncated incident report.
        temporary = original + ".tmp"

        with open(temporary, "wb") as handle:
            writer.write(handle)

        os.replace(temporary, original)
        return True

    def _speak(self, watch, elapsed):
        import tts_alert

        bay = (watch.event.get("bay_id") or "the bay").replace("-", " ")
        text = ("Urgent. No response in %s. The hazard alert has not been "
                "acknowledged for %.0f seconds. Escalating to %s."
                % (bay.lower(), elapsed, watch.route))

        result = tts_alert.speak(text, watch.event.get("language", "en"))
        log("  re-alert spoken via %s" % result.get("backend"))
        return result

    # -- reporting ---------------------------------------------------

    def summary(self):
        counts = {}

        for watch in self.watches.values():
            counts[watch.state] = counts.get(watch.state, 0) + 1

        return counts

    def report(self):
        print("\n  %-22s %-14s %8s  %s" % ("incident", "state", "elapsed", "route"))

        for incident_id in sorted(self.watches):
            watch = self.watches[incident_id]
            print("  %-22s %-14s %7.1fs  %s"
                  % (incident_id, watch.state, watch.elapsed(),
                     watch.route if watch.state == "escalated" else "-"))

        print("\n  %s" % ", ".join("%s=%d" % kv for kv in sorted(self.summary().items())))


# ============================================================
# SELF TEST
# ============================================================

class _InstantTimer(object):
    """A threading.Timer that fires when told, not when time passes."""

    pending = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.cancelled = False
        self.daemon = True

    def start(self):
        _InstantTimer.pending.append(self)

    def cancel(self):
        self.cancelled = True

    @classmethod
    def fire_all(cls):
        due, cls.pending = cls.pending, []

        for timer in due:
            if not timer.cancelled:
                timer.function(*timer.args)


def selftest():
    """No sleeping, no audio, no network."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-44s %s" % ("PASS" if condition else "FAIL", name, detail))

    _InstantTimer.pending = []

    event = {"bay_id": "BAY-3", "incident_type": "NO-Hardhat",
             "substance_name": "Sodium hydroxide", "substance_code": "NAOH",
             "source": "camera", "language": "en"}
    high = {"severity": "high", "steps": ["Stop work."],
            "contraindication": "Do not use water.",
            "spoken_alert": "Hazard in bay 3."}

    watcher = EscalationWatcher(window=45, speak=False,
                                timer_factory=_InstantTimer)

    check("medium severity is not watched",
          watcher.watch("HW-1", event, {"severity": "medium"}) is None,
          "a PPE reminder does not need a button press")

    watch = watcher.watch("HW-2", event, high)
    check("high severity starts a watch",
          watch is not None and watch.state == "waiting", watch.state)

    check("watch does not block", len(_InstantTimer.pending) == 1,
          "timer armed on its own thread, caller returned")

    acked = watcher.acknowledge("HW-2", by="kiosk")
    check("acknowledging stands the watch down",
          acked is not None and acked.state == "acknowledged"
          and acked.acked_by == "kiosk", acked.state)

    _InstantTimer.fire_all()
    check("an acknowledged watch never escalates",
          watcher.watches["HW-2"].escalated_at is None
          and watcher.watches["HW-2"].state == "acknowledged", "still acknowledged")

    check("acknowledging twice is a no-op",
          watcher.acknowledge("HW-2") is None, "already handled")

    check("acknowledging an unknown id is safe",
          watcher.acknowledge("HW-NOPE") is None, "returns None, does not raise")

    # -- the case the module exists for ------------------------------
    _InstantTimer.pending = []
    escalations = []
    silent = EscalationWatcher(window=45, speak=False,
                               timer_factory=_InstantTimer,
                               on_escalate=escalations.append)
    silent.watch("HW-3", event, high)
    _InstantTimer.fire_all()

    unacked = silent.watches["HW-3"]
    check("silence escalates on its own",
          unacked.state == "escalated" and len(escalations) == 1,
          "no ack -> escalated without anyone asking")

    payload = unacked.result.get("webhook", {}).get("payload", {})
    check("escalation payload is tagged",
          payload.get("escalation") is True
          and payload.get("escalation_tag") == ESCALATION_TAG,
          payload.get("escalation_tag"))

    first = None

    try:
        import webhook_dispatch
        first = webhook_dispatch.build_alert_payload(event, high)
    except Exception:
        pass

    if first:
        check("second payload differs from the first",
              payload["channels"]["sms"]["body"] != first["channels"]["sms"]["body"]
              and payload["channels"]["slack"]["channel"]
              != first["channels"]["slack"]["channel"],
              "different body and a different Slack room -- not a resend")

    check("payload records the elapsed silence",
          payload.get("unacknowledged_seconds") is not None
          and payload.get("original_incident") == "HW-3",
          "%ss, original %s" % (payload.get("unacknowledged_seconds"),
                                payload.get("original_incident")))

    check("payload says nothing real was contacted",
          "No emergency service" in payload.get("note", ""),
          payload.get("note", "")[:46])

    check("escalation route is recorded",
          unacked.result.get("route") in ROUTES, unacked.result.get("route"))

    check("no dossier means no addendum, not a crash",
          unacked.result.get("addendum") is None
          and "addendum_error" not in unacked.result,
          "handled, not raised")

    # -- the addendum, against a real PDF ----------------------------
    try:
        import dossier
        from pypdf import PdfReader

        original = dossier.build_dossier(event, high, filename="selftest_ack.pdf")
        before = len(PdfReader(original).pages)

        _InstantTimer.pending = []
        with_pdf = EscalationWatcher(window=45, speak=False,
                                     timer_factory=_InstantTimer)
        with_pdf.watch("HW-4", event, high, pdf=original)
        _InstantTimer.fire_all()

        after = len(PdfReader(original).pages)
        text = "\n".join(p.extract_text() for p in PdfReader(original).pages)

        check("addendum is appended to the same file",
              after == before + 1 and with_pdf.watches["HW-4"].result["addendum"]
              == original, "%d page(s) -> %d" % (before, after))

        check("addendum names the missed acknowledgement",
              ESCALATION_TAG in text and "without acknowledgement" in text,
              "the original pages are untouched above it")

    except ImportError:
        print("  note: pypdf/reportlab missing -- addendum checks skipped")

    check("summary counts every state",
          silent.summary().get("escalated") == 1, str(silent.summary()))

    # Regression guard. wait_for_escalations() once skipped watches that
    # had not fired yet, returned instantly, and let the caller's
    # cancel_all() kill the timer a moment before it would have
    # escalated -- so nothing escalated and the report said "waiting".
    _InstantTimer.pending = []
    racing = EscalationWatcher(window=45, speak=False,
                               timer_factory=_InstantTimer)
    racing.watch("HW-5", event, high)

    check("waiting watches are not skipped by the wait",
          racing.wait_for_escalations(timeout=0.05) is False,
          "an unfired watch must block the wait, not be treated as done")

    _InstantTimer.fire_all()
    check("and the wait clears once it escalates",
          racing.wait_for_escalations(timeout=2.0) is True
          and racing.watches["HW-5"].state == "escalated",
          racing.watches["HW-5"].state)

    print("\n%d/%d watcher checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


def demo(window=6.0, ack_after=None):
    """Real timers, short window, so the escalation is watchable."""

    event = {"bay_id": "BAY-3", "incident_type": "NO-Hardhat",
             "substance_name": "Sodium hydroxide (50% solution)",
             "substance_code": "NAOH", "source": "camera", "language": "en"}
    response = {"severity": "high",
                "steps": ["Stop work in BAY-3.", "Issue a hardhat."],
                "contraindication": "Do not flush with a pressurised water jet.",
                "spoken_alert": "Hazard in bay 3. Clear the bay."}

    import dossier

    pdf = dossier.build_dossier(event, response)
    incident_id = os.path.splitext(os.path.basename(pdf))[0]

    watcher = EscalationWatcher(window=window, speak=True)
    print("\nA HIGH incident just fired. Dossier: %s" % os.path.basename(pdf))
    print("The camera keeps running -- this window does not block it.\n")

    watcher.watch(incident_id, event, response, pdf=pdf)

    if ack_after is not None:
        time.sleep(ack_after)
        print()
        watcher.acknowledge(incident_id, by="kiosk button")
    else:
        for remaining in range(int(window), 0, -1):
            print("   %2ds to acknowledge... (nobody is pressing the button)"
                  % remaining, flush=True)
            time.sleep(1)

        # Wait for it properly rather than guessing. The first version
        # slept 2s, which is shorter than the spoken alert -- the thread
        # was killed mid-sentence and the re-alert never logged.
        print("\n   escalating -- waiting for the spoken alert to finish...")

        if not watcher.wait_for_escalations(timeout=40):
            print("   (escalation did not settle within 40s)")

    watcher.cancel_all()
    watcher.report()
    print("\nNote: demo workflow. Nothing above contacted a real responder.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Escalate a HIGH incident that nobody acknowledged.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="real timers, short window")
    parser.add_argument("--window", type=float, default=6.0,
                        help="demo ack window in seconds")
    parser.add_argument("--ack", type=float, default=None, metavar="SECONDS",
                        help="acknowledge after N seconds instead of letting it escalate")

    args = parser.parse_args(argv)

    if args.demo:
        return demo(window=args.window, ack_after=args.ack)

    return selftest()


if __name__ == "__main__":
    sys.exit(main())

"""
Push the alert outward: the SMS / Telegram / Slack leg of an incident.

Nothing here talks to a real carrier or a real workspace. It builds the
payload each channel would receive and POSTs the lot to one endpoint --
a webhook.site URL, or the stub in this file. That is the honest shape
for a hackathon: the dispatch decision, the per-channel formatting and
the delivery report are all real, and only the last hop is simulated.
Swapping in Twilio or a Slack webhook later means changing the URL and
the transport, not the logic.

Per-channel formatting is not decoration. An SMS is hard-limited to 160
characters and a truncated safety instruction is a dangerous artifact,
so the SMS body is built to fit and says so when it had to cut.

There is a receiver in here too, so the whole path can be exercised
with no external service and no network:

    python webhook_dispatch.py --serve 8899        # terminal 1
    python webhook_dispatch.py --demo --url http://127.0.0.1:8899

Run it:

    python webhook_dispatch.py --demo --url https://webhook.site/<your-uuid>
    python webhook_dispatch.py --selftest          # spins its own stub
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# CONFIGURATION
# ============================================================

# The mock endpoint. A webhook.site URL, or the stub below.
WEBHOOK_URL = os.getenv("HAZARDWATCH_WEBHOOK") or None

# Channels a dispatch fans out to. Each becomes one entry in the
# payload with its own formatted body.
DEFAULT_CHANNELS = ("sms", "telegram", "slack")

# A GSM-7 SMS segment is 160 characters. Going over does not fail, it
# silently bills and delivers as multiple parts that can arrive out of
# order -- so the body is built to fit one.
SMS_LIMIT = 160

HTTP_TIMEOUT = float(os.getenv("HAZARDWATCH_WEBHOOK_TIMEOUT", "10"))

# One retry, because demo wifi drops a single request far more often
# than it stays down. More than one and a failing endpoint stalls the
# camera loop behind it.
RETRIES = 1
RETRY_DELAY = 1.0


# ============================================================
# PAYLOAD
# ============================================================

def _fit_sms(text, limit=SMS_LIMIT):
    """
    Trim to one SMS segment on a word boundary.

    Returns (body, was_truncated). A cut instruction ends in "..." so
    the reader can tell the message is incomplete instead of acting on
    half of it.

    The marker is three ASCII dots, not a "…" character, and that is not
    a style choice: a single non-GSM-7 character anywhere in the body
    re-encodes the whole message as UCS-2, which drops the real segment
    limit from 160 to 70. Using the prettier ellipsis to signal
    truncation would silently cause the truncation it is warning about.
    """

    text = " ".join(text.split())

    if len(text) <= limit:
        return text, False

    cut = text[:limit - 3]

    if " " in cut:
        cut = cut[:cut.rindex(" ")]

    return cut.rstrip(" .,;:") + "...", True


def build_alert_payload(event, response, channels=DEFAULT_CHANNELS):
    """
    The alert as each channel would receive it.

    One dict, so a single POST to the mock endpoint shows every channel
    at once and the differences between them are visible side by side.
    """

    event = dict(event or {})
    response = dict(response or {})

    bay = event.get("bay") or event.get("zone") or "unknown bay"
    hazard = event.get("hazard_type") or event.get("violation") or "hazard"
    severity = str(response.get("severity") or "unrecorded").upper()
    substance = event.get("substance")
    contraindication = response.get("contraindication")

    steps = response.get("steps") or []

    if isinstance(steps, str):
        steps = [line.strip() for line in steps.splitlines() if line.strip()]

    headline = "[%s] %s in %s" % (severity, hazard, bay)

    # SMS: the single most actionable sentence, not a summary of
    # everything. First step if there is one, otherwise the hazard.
    sms_core = headline

    if substance:
        sms_core += " (%s)" % substance

    if contraindication:
        sms_core += ". DO NOT: %s" % contraindication
    elif steps:
        sms_core += ". %s" % steps[0]

    sms_body, truncated = _fit_sms(sms_core)

    rich_lines = ["*%s*" % headline]

    if substance:
        rich_lines.append("Substance: %s" % substance)

    rich_lines.append("Detected: %s (%s)"
                      % (event.get("timestamp") or "unknown time",
                         event.get("source") or "unknown source"))

    if contraindication:
        rich_lines.append("")
        rich_lines.append("*DO NOT:* %s" % contraindication)

    if steps:
        rich_lines.append("")
        rich_lines.append("*Response steps*")
        rich_lines += ["%d. %s" % (i, s) for i, s in enumerate(steps, 1)]

    rich_body = "\n".join(rich_lines)

    bodies = {
        "sms": {
            "to": "+1XXXXXXXXXX",
            "body": sms_body,
            "characters": len(sms_body),
            "truncated": truncated,
        },
        "telegram": {"chat_id": "@hazardwatch_ops", "parse_mode": "Markdown",
                     "text": rich_body},
        "slack": {"channel": "#hazardwatch-alerts", "text": headline,
                  "blocks_markdown": rich_body},
    }

    return {
        "simulated": True,
        "note": "Representative payload. No SMS or chat message was actually sent.",
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
        "severity": response.get("severity"),
        "bay": bay,
        "hazard": hazard,
        "spoken_alert": response.get("spoken_alert"),
        "channels": {name: bodies[name] for name in channels if name in bodies},
        "event": event,
    }


# ============================================================
# DELIVERY
# ============================================================

def post_json(url, payload, timeout=HTTP_TIMEOUT):
    """POST and return (status, body_text). Never raises."""

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "HazardWatch-OS/1.0")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")[:400]

    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]

    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def dispatch(event, response, url=None, channels=DEFAULT_CHANNELS, verbose=True):
    """
    Build the alert and POST it to the mock endpoint. Never raises.

    Returns:

        {"ok": bool, "status": int|None, "url": str, "payload": {...},
         "detail": str, "attempts": int}

    `ok` is False when there is no endpoint configured -- and the
    payload still comes back, so a caller with nowhere to send can
    still log or display it.
    """

    url = url or WEBHOOK_URL
    payload = build_alert_payload(event, response, channels)

    if not url:
        if verbose:
            print("  webhook: no endpoint set "
                  "(pass --url or set HAZARDWATCH_WEBHOOK) -- payload built, not sent")

        return {"ok": False, "status": None, "url": None, "payload": payload,
                "detail": "no endpoint configured", "attempts": 0}

    status, body = None, ""

    for attempt in range(1, RETRIES + 2):
        status, body = post_json(url, payload)

        # 2xx is delivered. Anything else is worth one retry, since the
        # common demo failure is a single dropped request.
        if status is not None and 200 <= status < 300:
            break

        if attempt <= RETRIES:
            time.sleep(RETRY_DELAY)

    ok = status is not None and 200 <= status < 300

    if verbose:
        sms = payload["channels"].get("sms", {})
        print("  webhook: %s -> %s%s"
              % (", ".join(payload["channels"]), status if ok else "FAILED",
                 "" if ok else " (%s)" % str(body)[:120]))

        if sms:
            print("  sms body (%d chars%s): %s"
                  % (sms["characters"], ", truncated" if sms["truncated"] else "",
                     sms["body"]))

    return {"ok": ok, "status": status, "url": url, "payload": payload,
            "detail": body, "attempts": attempt}


# ============================================================
# LOCAL STUB RECEIVER
# ============================================================

class _StubHandler(BaseHTTPRequestHandler):
    """Accepts any POST, prints it, answers 200."""

    received = []
    quiet = False

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")

        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw

        _StubHandler.received.append(parsed)

        if not _StubHandler.quiet:
            print("\n--- webhook received %s ---" % datetime.now().strftime("%H:%M:%S"))
            print(json.dumps(parsed, indent=2, ensure_ascii=False)
                  if isinstance(parsed, dict) else parsed)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"received": true}')

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HazardWatch webhook stub. POST here.")

    def log_message(self, *args):
        pass  # the payload dump above is the useful log


def serve(port=8899, quiet=False):
    """Run the stub receiver in the foreground until Ctrl-C."""

    _StubHandler.quiet = quiet
    server = HTTPServer(("127.0.0.1", port), _StubHandler)
    print("webhook stub listening on http://127.0.0.1:%d -- Ctrl-C to stop" % port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped after %d payload(s)" % len(_StubHandler.received))
    finally:
        server.server_close()

    return 0


def _background_stub(quiet=True):
    """Start a stub on a free port in a daemon thread. Returns (url, server)."""

    _StubHandler.quiet = quiet
    _StubHandler.received = []

    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    return "http://127.0.0.1:%d" % server.server_port, server


# ============================================================
# SAMPLE + SELF TEST
# ============================================================

SAMPLE_EVENT = {
    "zone": "BAY-3", "bay": "BAY-3", "hazard_type": "NO-Hardhat",
    "substance": "Sodium hydroxide (50% solution)", "source": "camera",
    "confidence": 0.91, "timestamp": "2026-09-04T11:42:07+00:00", "language": "en",
}

SAMPLE_RESPONSE = {
    "severity": "high",
    "steps": ["Stop work in BAY-3 and clear personnel to the upwind muster point.",
              "Isolate the sodium hydroxide line at the bay shutoff valve.",
              "Issue a hardhat and face shield before anyone re-enters."],
    "contraindication": "Do not flush the spill with water under pressure.",
    "spoken_alert": "Hazard in bay 3. Caustic spill. Clear the bay upwind.",
}


def selftest():
    """Full round trip against a stub started in this process."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-36s %s" % ("PASS" if condition else "FAIL", name, detail))

    payload = build_alert_payload(SAMPLE_EVENT, SAMPLE_RESPONSE)

    check("all channels present",
          set(payload["channels"]) == {"sms", "telegram", "slack"},
          ", ".join(payload["channels"]))

    sms = payload["channels"]["sms"]
    check("sms fits one segment", sms["characters"] <= SMS_LIMIT,
          "%d/%d chars" % (sms["characters"], SMS_LIMIT))
    check("sms carries the contraindication", "DO NOT" in sms["body"], sms["body"][:70])

    long_body, truncated = _fit_sms("word " * 100)
    check("over-long sms is cut on a word boundary",
          truncated and len(long_body) <= SMS_LIMIT and long_body.endswith("word..."),
          "%d chars, ends %r" % (len(long_body), long_body[-12:]))

    # A non-GSM-7 character re-encodes the segment as UCS-2 and halves
    # the limit, so the body this builds must stay ASCII-clean.
    check("truncated sms stays GSM-7 safe",
          long_body.isascii(), "ascii=%s" % long_body.isascii())

    short, was_cut = _fit_sms("Short message.")
    check("short sms is untouched", short == "Short message." and not was_cut, short)

    check("marked as simulated", payload["simulated"] is True
          and "No SMS" in payload["note"], payload["note"][:40])

    check("telegram gets the full steps",
          all(s[:20] in payload["channels"]["telegram"]["text"]
              for s in SAMPLE_RESPONSE["steps"]),
          "%d chars" % len(payload["channels"]["telegram"]["text"]))

    # -- real round trip, no external service ------------------------
    url, server = _background_stub()

    try:
        result = dispatch(SAMPLE_EVENT, SAMPLE_RESPONSE, url=url, verbose=False)
        check("posted to the stub", result["ok"] and result["status"] == 200,
              "%s %s" % (result["status"], url))
        check("stub received it", len(_StubHandler.received) == 1,
              "%d payload(s)" % len(_StubHandler.received))

        if _StubHandler.received:
            got = _StubHandler.received[0]
            check("payload survived the wire",
                  got["channels"]["sms"]["body"] == sms["body"]
                  and got["bay"] == "BAY-3",
                  "bay=%s severity=%s" % (got["bay"], got["severity"]))

        dead = dispatch(SAMPLE_EVENT, SAMPLE_RESPONSE,
                        url="http://127.0.0.1:9", verbose=False)
        check("dead endpoint fails honestly",
              not dead["ok"] and dead["payload"]["channels"],
              "retried %d time(s), payload still returned" % dead["attempts"])

        none = dispatch(SAMPLE_EVENT, SAMPLE_RESPONSE, url="", verbose=False)
        check("no endpoint still builds the payload",
              not none["ok"] and none["payload"]["channels"] and none["attempts"] == 0,
              none["detail"])

    finally:
        server.shutdown()
        server.server_close()

    print("\n%d/%d webhook checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="POST a simulated SMS/Telegram/Slack alert to a mock endpoint.")
    parser.add_argument("--url", default=None,
                        help="mock endpoint (webhook.site URL or the local stub)")
    parser.add_argument("--demo", action="store_true",
                        help="dispatch the sample incident")
    parser.add_argument("--serve", type=int, metavar="PORT", nargs="?", const=8899,
                        help="run the local stub receiver on PORT (default 8899)")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="print the payload instead of sending it")
    parser.add_argument("--selftest", action="store_true",
                        help="round trip against a stub in this process")

    args = parser.parse_args(argv)

    if args.serve is not None:
        return serve(args.serve)

    if args.selftest:
        return selftest()

    if args.show:
        print(json.dumps(build_alert_payload(SAMPLE_EVENT, SAMPLE_RESPONSE),
                         indent=2, ensure_ascii=False))
        return 0

    if not args.demo and not args.url:
        parser.error("use --demo, --print, --serve, or --selftest")

    result = dispatch(SAMPLE_EVENT, SAMPLE_RESPONSE, url=args.url)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

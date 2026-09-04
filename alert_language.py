"""
Speak the alert in the language of the person standing in the bay.

This is the seam between the two halves of this repo. The trigger half
produces an English `spoken_alert`; `translation.py` and `/translate`
already know how to turn text into hi/te/ur/bn. Until now they were
strangers living in the same folder.

## The one rule that matters

`API_CONTRACT.md` is blunt about it: a null translation is a 200, not
an error, and *"never display an untranslated string labelled as a
translation."* This module extends that rule to audio, where getting it
wrong is worse than on screen.

If translation fails, the text is still English. Handing that English
text to the TTS layer tagged `hi` makes gTTS read it with a Hindi
voice -- English words, Hindi phonetics -- which is unintelligible in
both languages. So `localize()` always returns the language the text is
*actually in*, never the one that was requested. A caller that speaks
`result["language"]` cannot make that mistake.

That is the same failure this project already fixed once, in
tts_alert's voice matching. It is worth stating twice.

## Where the translation comes from

The HTTP service first, because that is the contract the teammate owns
and the thing that will be deployed. If it is unreachable, the local
`translation.py` module is tried directly -- same code, no server. If
neither works the original English is returned, marked untranslated,
with the reason. An alert nobody can understand is still better than no
alert; an alert confidently mislabelled is not.

Run it:

    python alert_language.py "Hazard in bay 3. Clear the bay." --lang hi
    python alert_language.py --selftest
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


# The language service. Separate from HAZARDWATCH_API: the incident
# endpoint and the translate endpoint are two different services that
# happen to default to the same port in local development.
LANGUAGE_API = os.getenv("HAZARDWATCH_LANG_API", "http://127.0.0.1:8000").rstrip("/")

API_KEY = os.getenv("API_KEY") or None

# The contract warns that the first romanized request pays a ~25s model
# load. Translation does not use that tier, but the service may be cold
# for other reasons, and a demo would rather wait than lose the alert.
HTTP_TIMEOUT = float(os.getenv("HAZARDWATCH_LANG_TIMEOUT", "35"))

# Languages the sibling service documents. Anything else is passed
# through to the model, which may well know it -- the contract allows
# "any code the model knows" -- so this list is for reporting, not
# gatekeeping.
KNOWN_LANGUAGES = ("en", "hi", "te", "ur", "bn")


def _post(path, body, base=None, timeout=HTTP_TIMEOUT):
    """POST to the language service. Returns (status, parsed). Never raises."""

    url = (base or LANGUAGE_API).rstrip("/") + path
    data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")

    if API_KEY:
        request.add_header("X-API-Key", API_KEY)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")

        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw

    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def _translate_locally(text, target_language):
    """
    Same translation, no server. Returns the contract's shape or None.

    Used when the HTTP service is unreachable, which on a demo laptop is
    the normal case rather than the exceptional one.
    """

    try:
        import translation
    except Exception:
        return None

    for name in ("translate", "translate_text"):
        function = getattr(translation, name, None)

        if function is None:
            continue

        try:
            result = function(text, target_language)

            # The module may return the contract dict, or just a string.
            if isinstance(result, dict):
                return result

            if isinstance(result, str) and result.strip():
                return {"translation": result, "translated": True, "reason": None}

        except Exception:
            return None

    return None


def localize(text, target_language, base=None, allow_local=True):
    """
    Translate an alert, and say honestly what language came back.

    Returns:

        {"text": str,          # what to actually speak or print
         "language": str,      # the language `text` IS in, not the one asked for
         "translated": bool,
         "reason": str|None,   # why not, when translated is False
         "original": str}

    `language` is the field that matters. On any failure it stays "en",
    so a caller that speaks result["language"] never reads English text
    with a Hindi voice.
    """

    text = (text or "").strip()
    target = (target_language or "en").strip().lower()

    outcome = {"text": text, "language": "en", "translated": False,
               "reason": None, "original": text}

    if not text:
        outcome["reason"] = "empty text"
        return outcome

    if target in ("", "en"):
        outcome["reason"] = "already_in_target_language"
        return outcome

    status, body = _post("/translate", {
        "text": text, "target_language": target, "source_language": "en",
    }, base=base)

    if status == 200 and isinstance(body, dict):
        # Per the contract: translated=False with a reason is a normal
        # 200 outcome, and means show the original.
        if body.get("translated") and body.get("translation"):
            outcome.update(text=body["translation"], language=target,
                           translated=True, reason=None)
            return outcome

        outcome["reason"] = body.get("reason") or "translation_unavailable"

    else:
        outcome["reason"] = "service_unreachable (%s)" % (
            status if status else str(body)[:60])

    if allow_local:
        local = _translate_locally(text, target)

        if local and local.get("translated") and local.get("translation"):
            outcome.update(text=local["translation"], language=target,
                           translated=True, reason=None)
            return outcome

    # Still English. Say so, in the language field as well as the reason.
    return outcome


def announce(text, target_language, base=None, speak=True):
    """
    Localize an alert and say it out loud in whatever language it is
    genuinely in. Returns the localize() result with a "spoken" key.

    This is the function the trigger calls; it exists so the "translate,
    then speak in the language you actually got" pairing lives in one
    place and cannot be got wrong at each call site.
    """

    result = localize(text, target_language, base=base)

    if speak:
        try:
            import tts_alert
            result["spoken"] = tts_alert.speak(result["text"], result["language"])
        except Exception as e:
            result["spoken"] = {"ok": False, "backend": None,
                                "reason": "%s: %s" % (type(e).__name__, e)}
    else:
        result["spoken"] = None

    return result


# ============================================================
# SELF TEST
# ============================================================

def selftest():
    """No server required: the interesting paths are the failures."""

    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print("  %s  %-40s %s" % ("PASS" if condition else "FAIL", name, detail))

    english = localize("Hazard in bay 3. Clear the bay.", "en")
    check("english is a no-op",
          english["language"] == "en" and not english["translated"]
          and english["reason"] == "already_in_target_language",
          english["reason"])

    empty = localize("", "hi")
    check("empty text is handled", empty["reason"] == "empty text", empty["reason"])

    # The whole point. Point it at a dead port and confirm the failure
    # does NOT come back labelled Hindi.
    dead = localize("Hazard in bay 3.", "hi", base="http://127.0.0.1:9",
                    allow_local=False)

    check("unreachable service degrades",
          not dead["translated"] and "unreachable" in (dead["reason"] or ""),
          dead["reason"])

    check("FAILED TRANSLATION IS NOT LABELLED HINDI",
          dead["language"] == "en" and dead["text"] == "Hazard in bay 3.",
          "language=%r text unchanged=%s"
          % (dead["language"], dead["text"] == "Hazard in bay 3."))

    check("original is always preserved",
          dead["original"] == "Hazard in bay 3.", dead["original"])

    # A service that answers 200 with translated=False is an ordinary
    # outcome per the contract, not an error -- and must behave exactly
    # like the unreachable case from the caller's point of view.
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps({"translation": None, "translated": False,
                               "reason": "translation_unavailable",
                               "latency_ms": 0.0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        null = localize("Hazard in bay 3.", "hi",
                        base="http://127.0.0.1:%d" % server.server_port,
                        allow_local=False)

        check("null translation is not an error",
              not null["translated"] and null["reason"] == "translation_unavailable",
              null["reason"])

        check("null translation stays english",
              null["language"] == "en" and null["text"] == "Hazard in bay 3.",
              "language=%r" % null["language"])

    finally:
        server.shutdown()
        server.server_close()

    print("\n%d/%d language checks passed" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Translate a hazard alert and speak it in the language it is in.")
    parser.add_argument("text", nargs="?", help="the alert text (English)")
    parser.add_argument("--lang", default="hi", help="target language: %s"
                        % ", ".join(KNOWN_LANGUAGES))
    parser.add_argument("--api", default=None, help="language service base URL")
    parser.add_argument("--quiet", action="store_true", help="translate, do not speak")
    parser.add_argument("--selftest", action="store_true")

    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.text:
        parser.error("give me an alert to localize")

    result = announce(args.text, args.lang, base=args.api, speak=not args.quiet)

    print("  requested : %s" % args.lang)
    print("  spoken in : %s%s" % (result["language"],
                                  "" if result["translated"]
                                  else "  (NOT translated: %s)" % result["reason"]))
    print("  text      : %s" % result["text"])

    return 0 if result["translated"] or args.lang == "en" else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Walk a live /incident through the cases worth showing someone.

    uvicorn api:app --port 8000        # in another terminal, first
    python demo.py                     # then this
    python demo.py --pause             # stop between cases, for presenting
    python demo.py --base http://127.0.0.1:8001

Four real requests against a real backend. Nothing here is mocked, and
nothing is pre-baked -- if the model or the corpus changes, the output
changes with it, which is the whole point of demonstrating it live.

The four cases are chosen to show the decisions, not just the feature:

  1. A substance the corpus knows -- retrieval cites that substance's
     own safety sheet, and nobody else's.
  2. The same, localized -- the spoken alert comes back in Hindi, and
     the response says whether it was really translated.
  3. A substance the corpus has NO sheet for -- the interesting one.
     It answers, and it deliberately withholds every other substance's
     safety data rather than offering the nearest chemical as an
     analogue.
  4. A substance the detection side could not identify at all -- same
     handling, different cause, and the response distinguishes them.

Exits 0 if every request was answered, 1 otherwise, so it can also be
used as a quick end-to-end check.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


# A Windows console defaults to cp1252 and dies on Devanagari with a
# UnicodeEncodeError -- which would take out the demo at exactly the
# moment it was showing off the multilingual alert. Force UTF-8 rather
# than depending on the operator having set PYTHONIOENCODING.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


WIDTH = 74

# Both providers rate-limit a burst. Four assessments back to back is
# within that limit on a cold start but not reliably so on a second run,
# and the demo being re-run is the normal case, not the exception. A few
# seconds between cases costs nothing while presenting -- the operator
# is talking anyway -- and removes the most likely way this falls over
# in front of an audience.
GAP_SECONDS = 5

# How long to wait before the single retry on a 503.
RETRY_WAIT_SECONDS = 20

CASES = [
    {
        "title": "A substance the corpus knows",
        "notice": "retrieved_sources cites chlorine's own sheet -- not ammonia's, "
                  "not caustic soda's -- plus the regulations.",
        "body": {
            "bay_id": "BAY-04",
            "substance_code": "CL2",
            "substance_name": "Chlorine gas",
            "incident_type": "gas leak detected near the pump pit",
            "target_lang": "en",
        },
    },
    {
        "title": "The same incident, spoken in Hindi",
        "notice": "spoken_alert_translated says whether the alert is REALLY in "
                  "Hindi. False means you are looking at English and must not "
                  "feed it to a Hindi voice.",
        "body": {
            "bay_id": "BAY-07",
            "substance_code": "H2SO4",
            "substance_name": "Sulphuric acid (98%)",
            "incident_type": "acid splash to the eyes",
            "target_lang": "hi",
        },
    },
    {
        "title": "A substance the corpus has NO sheet for",
        "notice": "retrieval_mode is substance_unknown and NOT ONE sds_ file is "
                  "cited. Offering the nearest chemical instead produced sound-"
                  "looking advice sourced from documents that never contained "
                  "it, so the other sheets are withheld on purpose.",
        "body": {
            "bay_id": "BAY-02",
            "substance_code": "TOLUENE",
            "substance_name": "Toluene",
            "incident_type": "drum spill in the loading bay",
            "target_lang": "en",
        },
    },
    {
        "title": "A substance nobody could identify",
        "notice": "substance_code is null -- 'unmapped', which is NOT the same "
                  "as 'no substance'. Same withholding, different cause, and "
                  "retrieval_mode tells them apart.",
        "body": {
            "bay_id": "BAY-11",
            "substance_code": None,
            "substance_name": "Unidentified white crystalline solid",
            "incident_type": "unlabelled drum leaking",
            "target_lang": "en",
        },
    },
]


def rule(char="-"):
    print(char * WIDTH)


def wrap(text, indent=0, width=WIDTH):
    """Word-wrap without pulling in textwrap's paragraph handling."""

    pad = " " * indent
    line = pad
    out = []

    for word in str(text).split():
        if len(line) + len(word) + 1 > width and line.strip():
            out.append(line.rstrip())
            line = pad + word + " "
        else:
            line += word + " "

    if line.strip():
        out.append(line.rstrip())

    return "\n".join(out)


def call(base, path, body=None, timeout=180):
    """Returns (status, parsed, seconds). Never raises."""

    url = base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None

    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")

    started = time.time()

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode()), time.time() - started

    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), time.time() - started
        except ValueError:
            return e.code, raw, time.time() - started

    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e), time.time() - started


def show_health(base):

    status, body, _ = call(base, "/health")

    if status != 200:
        print("Cannot reach %s -- is the backend running?" % base)
        print("  start it with:  uvicorn api:app --port 8000")
        print("  got: %s %s" % (status, body))
        return None

    tiers = body.get("tiers", {})
    generation = body.get("generation", {})

    print("Backend: %s" % base)
    print("  script detection   %s" % tiers.get("script"))
    print("  semantic tier      %s" % tiers.get("semantic"))
    print("  retrieval index    %s" % body.get("retrieval"))
    print("  featherless key    %s" % generation.get("featherless_configured"))
    print("  last provider      %s" % generation.get("last_provider"))
    print("  languages          %s" % ", ".join(body.get("languages", [])))
    print()
    print(wrap("null means nothing has needed it yet -- both the model and "
               "the index load lazily, so the honest answer before the first "
               "request is 'unknown', not 'false'."))

    return body


def show_case(number, case, base):
    """Returns True if the request was answered."""

    rule("=")
    print("CASE %d/%d  %s" % (number, len(CASES), case["title"]))
    rule("=")

    body = case["body"]

    print("REQUEST")
    print("  substance   %s   (code: %s)"
          % (body["substance_name"], body["substance_code"] or "null -- unmapped"))
    print("  incident    %s" % body["incident_type"])
    print("  bay         %s" % body["bay_id"])
    print("  language    %s" % body["target_lang"])
    print()

    status, response, elapsed = call(base, "/incident", body)

    # A 503 here is nearly always the provider rate-limiting a burst of
    # requests, not a broken service -- four assessments in a minute is
    # enough to trip it, and running the demo twice in a row certainly
    # is. Waiting and asking once more is what a person would do, and a
    # demo that dies on a transient throttle teaches the audience the
    # wrong thing about the system. The retry is announced rather than
    # silent: if it is happening, that is worth seeing.
    if status == 503:
        print("  provider busy (%s) -- waiting %ds and trying once more"
              % (str(response).strip()[:80], RETRY_WAIT_SECONDS))
        print()
        time.sleep(RETRY_WAIT_SECONDS)
        status, response, elapsed = call(base, "/incident", body)

    if status != 200:
        print("FAILED  HTTP %s after %.1fs" % (status, elapsed))
        print(wrap(response, indent=2))
        print()
        return False

    print("RESPONSE  (%.1fs)" % elapsed)
    print()
    print("  SEVERITY   %s" % str(response.get("severity", "")).upper())
    print()

    for index, step in enumerate(response.get("steps", []), start=1):
        print(wrap("%d. %s" % (index, step), indent=2))

    print()
    print("  DO NOT:")
    print(wrap(response.get("contraindication", ""), indent=4))
    print()

    translated = response.get("spoken_alert_translated")

    print("  SPOKEN ALERT  (%s)"
          % ("translated into %s" % body["target_lang"] if translated
             else "English -- not translated"))
    print(wrap(response.get("spoken_alert", ""), indent=4))
    print()

    print("  PROVENANCE")
    print("    retrieval_mode        %s" % response.get("retrieval_mode"))
    print("    grounded              %s" % response.get("grounded"))
    print("    generation_provider   %s" % response.get("generation_provider"))
    print("    latency_ms            %s" % response.get("latency_ms"))
    print("    retrieved_sources")

    for source in response.get("retrieved_sources") or ["(none)"]:
        print("      - %s" % source)

    print()
    print("  NOTICE")
    print(wrap(case["notice"], indent=4))
    print()

    return True


def main():

    parser = argparse.ArgumentParser(description="Live /incident walkthrough.")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--pause", action="store_true",
                        help="wait for Enter between cases, for presenting")
    parser.add_argument("--gap", type=float, default=GAP_SECONDS,
                        help="seconds between cases when not pausing")

    args = parser.parse_args()

    rule("=")
    print("HAZARDWATCH OS -- live incident walkthrough")
    rule("=")
    print()

    if show_health(args.base) is None:
        return 1

    print()

    answered = []

    for number, case in enumerate(CASES, start=1):

        if number > 1:
            if args.pause:
                try:
                    input("  [Enter] for the next case ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                print()
            else:
                time.sleep(args.gap)

        answered.append(show_case(number, case, args.base))

    rule("=")
    print("SUMMARY")
    rule("=")

    for case, ok in zip(CASES, answered):
        print("  %-4s %s" % ("ok" if ok else "FAIL", case["title"]))

    print()
    print(wrap("Every response above came from a live model call grounded in "
               "the corpus in corpus/. That corpus is illustrative sample "
               "text written for development -- not real safety data sheets "
               "and not real regulation. Replace it before this is used for "
               "anything real."))

    return 0 if all(answered) else 1


if __name__ == "__main__":
    sys.exit(main())

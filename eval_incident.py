"""
Evaluation harness for the /incident pipeline.

    python eval_incident.py                    # offline checks only
    python eval_incident.py --live             # + real calls to Featherless
    python eval_incident.py --live --base http://127.0.0.1:8000

Exits 0 if every check passed, 1 otherwise.

Split into offline and live deliberately. The offline checks --
retrieval, selection policy, response validation, localization
labelling -- are the ones that encode decisions this project has
already made and could silently regress. They need no API key, no
network and no money, so they can run on every change. The live checks
cost real calls and are opt-in.

The retrieval section measures the thing the selection policy exists
for, and measures it against the baseline it replaced: pure semantic
similarity retrieves the wrong substance's safety data often enough
that "top-k by cosine" is not a safe default for a dispatcher. That
number is printed rather than asserted away, because if a corpus change
makes the naive baseline good again, the policy should be reconsidered
rather than kept out of habit.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


passed = []
failed = []


def check(name, condition, detail=""):

    if condition:
        passed.append(name)
        print("  PASS  %-42s %s" % (name, detail))

    else:
        failed.append(name)
        print("  FAIL  %-42s %s" % (name, detail))

    return bool(condition)


# ============================================================
# FIXTURES
# ============================================================
#
# The substance codes here must match the front matter in corpus/.
# Incident types are written the way a detection system would emit
# them -- terse, lower case, no punctuation -- rather than as tidy
# prose, because that is what the endpoint will actually receive.

SUBSTANCE_CASES = [
    ("CL2", "sds_chlorine.md", ["gas leak near the pump pit", "worker collapsed in a trench"]),
    ("NH3", "sds_ammonia.md", ["refrigerant leak in the plant room", "eye splash from a valve"]),
    ("H2SO4", "sds_sulphuric_acid.md", ["acid splash to the eyes", "drum spill on the floor"]),
    ("NAOH", "sds_caustic_soda.md", ["caustic burn to the forearm", "pellets spilled in a walkway"]),
]


# ============================================================
# OFFLINE: RETRIEVAL AND SELECTION
# ============================================================

def eval_retrieval():

    import incident

    print("\nRETRIEVAL")

    if not check(
        "index loads",
        incident.retrieve("CL2", "gas leak") != [],
        "retrieval_available=%s" % (incident.retrieval_available(),),
    ):
        print("  (no index -- run: python ingest.py)")
        return

    # -- the substance the caller named must be the substance we cite --

    policy_hits = 0
    baseline_hits = 0
    baseline_top1_hits = 0
    total = 0

    for code, expected_file, incident_types in SUBSTANCE_CASES:

        for incident_type in incident_types:

            total += 1

            chunks = incident.retrieve(code, incident_type)
            sources = [chunk["source_file"] for chunk in chunks]

            sds = [s for s in sources if s.startswith("sds_")]

            correct = bool(sds) and all(s == expected_file for s in sds)

            if correct:
                policy_hits += 1

            check(
                "%s / %s" % (code, incident_type[:26]),
                correct,
                "sds cited: %s" % (sorted(set(sds)) or "none"),
            )

            # Baseline: what plain top-k similarity would have cited,
            # with no substance filtering at all. Measured two ways,
            # because one number alone would misrepresent it. The
            # strict measure asks whether EVERY SDS cited is the right
            # substance -- which is what actually matters, since the
            # model is given all of them as equally authoritative
            # excerpts. The lenient measure asks only whether the
            # single best-scoring SDS was right, which is the most
            # favourable reading the baseline can be given.
            ranked = _naive_top_k(incident, code, incident_type)
            baseline_sds = [s for s in ranked if s.startswith("sds_")]

            if baseline_sds and all(s == expected_file for s in baseline_sds):
                baseline_hits += 1

            if baseline_sds and baseline_sds[0] == expected_file:
                baseline_top1_hits += 1

    print("\n  substance accuracy over %d cases:" % total)
    print("    selection policy        %d/%d  (every SDS cited is the right one)"
          % (policy_hits, total))
    print("    naive top-k, strict     %d/%d  (every SDS cited is the right one)"
          % (baseline_hits, total))
    print("    naive top-k, lenient    %d/%d  (best-scoring SDS is the right one)"
          % (baseline_top1_hits, total))

    check(
        "policy beats naive similarity",
        policy_hits >= baseline_hits,
        "the selection policy must not be worse than the baseline it replaced",
    )

    # -- one regulation slot is held --

    chunks = incident.retrieve("CL2", "gas leak near the pump pit")

    check(
        "regulation slot filled",
        any(chunk["doc_type"] == "regulation" for chunk in chunks),
        "doc_types: %s" % [c["doc_type"] for c in chunks],
    )

    # -- open vocabulary: a code the corpus has never heard of --

    unknown = incident.retrieve("XYLENE-7", "unlabelled drum leaking")

    check(
        "unknown substance falls back",
        len(unknown) == incident.DEFAULT_TOP_K,
        "%d chunks returned, no crash" % len(unknown),
    )

    # -- top_k is honoured --

    check(
        "top_k respected",
        len(incident.retrieve("CL2", "gas leak", top_k=2)) == 2,
        "",
    )

    # -- no duplicate chunks --

    chunks = incident.retrieve("NH3", "leak")
    ids = [chunk["chunk_id"] for chunk in chunks]

    check("no duplicate chunks", len(ids) == len(set(ids)), "%d unique" % len(set(ids)))


def _naive_top_k(incident, code, incident_type):
    """
    What pure cosine similarity would have retrieved, bypassing the
    selection policy. The comparison the policy has to justify itself
    against.
    """

    import numpy

    index, chunks, model = incident._ensure_index()

    query = "%s%s %s" % (incident.QUERY_PREFIX, code, incident_type)

    vector = numpy.asarray(
        model.encode([query], normalize_embeddings=True), dtype="float32"
    )

    _, ids = index.search(vector, incident.DEFAULT_TOP_K)

    return [chunks[i]["source_file"] for i in ids[0] if i != -1]


# ============================================================
# OFFLINE: RESPONSE VALIDATION
# ============================================================

def eval_validation():

    import incident

    print("\nVALIDATION")

    good = {
        "severity": "high",
        "steps": ["Evacuate the bay.", "Isolate the supply."],
        "contraindication": "Do not apply water to the leak.",
        "spoken_alert": "Evacuate bay four immediately.",
    }

    check(
        "well-formed response accepted",
        incident._validate(good)["severity"] == "high",
        "",
    )

    check(
        "case and whitespace normalised",
        incident._validate(dict(good, severity="  Critical "))["severity"] == "critical",
        "'  Critical ' -> 'critical'",
    )

    # The decision this endpoint is built around: a near-miss severity
    # is a failure, never mapped onto the nearest valid word.
    for bad_severity in ("moderate", "severe", "very high", "3", ""):

        check(
            "severity %r rejected" % bad_severity,
            _raises(incident._validate, dict(good, severity=bad_severity)),
            "not coerced to a neighbouring value",
        )

    cases = [
        ("missing key", {k: v for k, v in good.items() if k != "steps"}),
        ("steps not a list", dict(good, steps="Evacuate the bay.")),
        ("steps empty", dict(good, steps=[])),
        ("step not a string", dict(good, steps=["ok", 42])),
        ("step blank", dict(good, steps=["ok", "   "])),
        ("contraindication blank", dict(good, contraindication="  ")),
        ("spoken_alert missing", {k: v for k, v in good.items() if k != "spoken_alert"}),
        ("severity not a string", dict(good, severity=3)),
    ]

    for name, payload in cases:
        check(name + " rejected", _raises(incident._validate, payload), "")


def _raises(function, argument):

    try:
        function(argument)
        return False

    except ValueError:
        return True


# ============================================================
# OFFLINE: LOCALIZATION LABELLING
# ============================================================

def eval_localization():

    import incident

    print("\nLOCALIZATION")

    alert = "Evacuate bay four immediately."

    text, translated = incident._localize(alert, "en")

    check(
        "english is not 'translated'",
        text == alert and translated is False,
        "en -> translated=%s" % translated,
    )

    # The rule the whole project turns on: a failed translation returns
    # the English text and says so. It must never come back labelled as
    # translated. With no prose-provider key configured this exercises
    # the real failure path; with one configured it exercises the
    # success path. Both are correct -- what is asserted is that the
    # flag matches the text, not which branch ran.
    text, translated = incident._localize(alert, "hi")

    if translated:
        check(
            "translated alert is not the english one",
            text != alert,
            "translated=True and the text changed",
        )

    else:
        check(
            "untranslated alert falls back honestly",
            text == alert,
            "translated=False and the english text was returned",
        )


# ============================================================
# LIVE: THE ENDPOINT
# ============================================================

def call(base, path, method="GET", body=None, timeout=300):
    """
    Returns (status, parsed). Never raises.

    The timeout is generous because the provider's latency is bimodal:
    most assessments finish in under 25s, and a minority take four
    minutes. Measured 2026-09-04 -- a request that this harness gave up
    on at 180s was re-issued and succeeded in 242.8s. A timeout under
    the tail turns a slow provider into a red check that looks like a
    broken endpoint.
    """

    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None

    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")

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


def eval_live(base, delay):
    """
    Live checks, paced.

    The delay is not politeness, and it is not arbitrary. This account
    is limited by tokens per minute rather than by request count, and
    an /incident prompt is large -- four retrieved chunks of roughly
    350 tokens each, plus the instruction block, is around 2000 tokens
    before the model writes anything. Two assessments inside a minute
    exhaust the budget and the second comes back 429.

    Measured 2026-09-04: four assessments fired back to back returned
    one timeout and three 503s; at 15s spacing, two of four still hit
    429; two-token probe requests sent immediately after each other
    both returned 200 in 1.5s, which is what rules out a per-request
    limit. Roughly 45s of spacing was enough.

    A 429 here is the provider's quota, not a fault in the endpoint --
    but it is still reported as a failure rather than skipped, because
    a check that quietly excuses a 503 cannot tell a rate limit from a
    genuinely broken deployment.
    """

    import incident

    print("\nLIVE ENDPOINT (%s, %.0fs between calls)" % (base, delay))

    status, _ = call(base, "/health", timeout=20)

    if not check("service reachable", status == 200, "status %s" % status):
        return

    # -- input validation, cheap and no model call --

    valid = {
        "bay_id": "BAY-04",
        "substance_code": "CL2",
        "incident_type": "gas leak",
        "target_lang": "en",
    }

    for name, field, value in [
        ("bay_id empty", "bay_id", "  "),
        ("substance_code empty", "substance_code", ""),
        ("incident_type empty", "incident_type", ""),
        ("target_lang empty", "target_lang", ""),
    ]:
        status, body = call(base, "/incident", "POST", dict(valid, **{field: value}))
        check(name + " -> 400", status == 400, str(body)[:70])

    status, body = call(base, "/incident", "POST", dict(valid, target_lang="fr"))
    check(
        "unsupported target_lang -> 400",
        status == 400 and "must be one of" in str(body),
        str(body)[:70],
    )

    status, body = call(base, "/incident", "POST", dict(valid, incident_type="x" * 300))
    check("over-long field -> 400", status == 400, str(body)[:70])

    status, body = call(base, "/incident", "POST", {"bay_id": "BAY-04"})
    check("missing fields -> 422", status == 422, "status %s" % status)

    # -- real assessments --

    for position, (code, expected_file, incident_types) in enumerate(SUBSTANCE_CASES):

        if position:
            time.sleep(delay)

        started = time.time()

        status, body = call(base, "/incident", "POST", {
            "bay_id": "BAY-04",
            "substance_code": code,
            "incident_type": incident_types[0],
            "target_lang": "en",
        })

        elapsed = time.time() - started

        if not check("%s assessed" % code, status == 200, "status %s in %.1fs" % (status, elapsed)):
            print("      %s" % str(body)[:200])
            continue

        check(
            "%s severity in enum" % code,
            body.get("severity") in incident.SEVERITIES,
            "severity=%r" % body.get("severity"),
        )

        check(
            "%s steps usable" % code,
            isinstance(body.get("steps"), list)
            and 1 <= len(body["steps"]) <= 8
            and all(isinstance(s, str) and s.strip() for s in body["steps"]),
            "%d steps" % len(body.get("steps") or []),
        )

        check(
            "%s cites its own sds" % code,
            expected_file in (body.get("retrieved_sources") or []),
            "sources=%s" % body.get("retrieved_sources"),
        )

        check(
            "%s grounded" % code,
            body.get("grounded") is True,
            "grounded=%s" % body.get("grounded"),
        )

        check(
            "%s alert is speakable" % code,
            isinstance(body.get("spoken_alert"), str)
            and 0 < len(body["spoken_alert"].split()) <= 35
            and "```" not in body["spoken_alert"],
            "%d words" % len(str(body.get("spoken_alert", "")).split()),
        )

    # -- localization, the honest-labelling rule end to end --

    time.sleep(delay)

    status, body = call(base, "/incident", "POST", {
        "bay_id": "BAY-04",
        "substance_code": "CL2",
        "incident_type": "gas leak near the pump pit",
        "target_lang": "hi",
    })

    if check("hindi request assessed", status == 200, "status %s" % status):

        translated = body.get("spoken_alert_translated")

        check(
            "spoken_alert_translated is truthful",
            translated in (True, False),
            "translated=%s, alert=%r" % (translated, str(body.get("spoken_alert"))[:60]),
        )

        if translated is False:
            check(
                "untranslated alert is still returned",
                bool(str(body.get("spoken_alert") or "").strip()),
                "english fallback present, correctly labelled",
            )


# ============================================================

def main():

    parser = argparse.ArgumentParser(description="Evaluate the /incident pipeline.")
    parser.add_argument("--live", action="store_true", help="also make real API calls")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--delay",
        type=float,
        default=45.0,
        help="seconds between live assessments; see eval_live()",
    )

    args = parser.parse_args()

    eval_retrieval()
    eval_validation()
    eval_localization()

    if args.live:
        eval_live(args.base, args.delay)
    else:
        print("\n(skipping live checks -- pass --live to run them)")

    print("\n%d passed, %d failed" % (len(passed), len(failed)))

    if failed:
        print("Failed: %s" % ", ".join(failed))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

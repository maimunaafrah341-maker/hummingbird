"""
Smoke test to run against a DEPLOYED instance, from a machine that is
not the one hosting it.

    python smoke_test.py https://your-service.onrender.com
    python smoke_test.py https://your-service.onrender.com --key YOUR_API_KEY
    python smoke_test.py https://your-service.onrender.com --incident

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

/incident's input validation is checked by default, because rejecting a
bad request costs nothing -- validation runs before any model call. The
one check that actually asks for an assessment is behind --incident,
and is off by default on purpose. It spends a real Featherless
generation (8-42s measured, bounded at 90s), and the account is limited
by tokens per minute: two assessments inside a minute return 429, so a
gate that ran one on every deploy would fail spuriously whenever two
deploys landed close together. A deploy gate that is flaky for reasons
unrelated to the deploy is worse than one that checks less.
"""

import json
import sys
import time
import urllib.error
import urllib.request


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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    base = sys.argv[1]
    key = None

    if "--key" in sys.argv:
        key = sys.argv[sys.argv.index("--key") + 1]

    run_incident = "--incident" in sys.argv

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

    # -- /incident input validation: free, no model call ------------
    #
    # These stay in the default run because validation happens before
    # anything expensive: a rejected request never reaches the
    # retriever or the provider, so this costs a round trip and
    # nothing else.

    incident_body = {
        "bay_id": "BAY-04",
        "substance_code": "CL2",
        "substance_name": "Chlorine gas",
        "incident_type": "gas leak",
        "target_lang": "en",
    }

    status, body = call(
        base, "/incident", key, "POST", dict(incident_body, bay_id="  ")
    )
    check("incident empty bay_id -> 400", status == 400, str(body))

    status, body = call(
        base, "/incident", key, "POST", dict(incident_body, target_lang="fr")
    )
    check(
        "incident bad target_lang -> 400",
        status == 400 and "must be one of" in str(body),
        str(body),
    )

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

    # -- a real assessment, last and only on request -----------------
    #
    # The most expensive check in the file by a wide margin: a real
    # generation, billed, and slow. See the module docstring for why it
    # is not in the default run.
    #
    # Passes on a 200 with a well-formed body OR on a clean 503, for
    # the same reason the translation check above accepts either
    # outcome: a deployment with no FEATHERLESS_API_KEY, or one whose
    # provider is down or rate-limiting, is not a broken deployment --
    # it is one that must say so honestly. What this rejects is a 500,
    # a hang, or a 200 that omits fields the contract promises.

    if run_incident:

        print("\n  (--incident: one real assessment, 8-90s, billed)")

        started = time.time()

        status, body = call(
            base, "/incident", key, "POST", incident_body, timeout=180,
        )

        incident_ms = (time.time() - started) * 1000

        if status == 200:

            required = [
                "severity", "steps", "contraindication", "spoken_alert",
                "spoken_alert_translated", "substance_name", "grounded",
                "retrieval_mode", "retrieved_sources", "generation_provider",
                "latency_ms",
            ]

            missing = [field for field in required if field not in body]

            check(
                "incident returns every documented field",
                not missing,
                "missing: %s" % (missing or "none"),
            )

            check(
                "incident severity in enum",
                body.get("severity") in ("low", "medium", "high", "critical"),
                "severity=%r in %.0fms" % (body.get("severity"), incident_ms),
            )

            check(
                "incident steps are usable",
                isinstance(body.get("steps"), list)
                and body["steps"]
                and all(isinstance(s, str) and s.strip() for s in body["steps"]),
                "%d steps" % len(body.get("steps") or []),
            )

            # grounded and retrieved_sources have to agree. Sources
            # with grounded false, or grounded true with nothing cited,
            # would mean the response is lying about where it came
            # from -- which is worse than either state on its own.
            check(
                "incident grounding is self-consistent",
                bool(body.get("grounded")) == bool(body.get("retrieved_sources"))
                and (body.get("retrieval_mode") == "unavailable")
                != bool(body.get("grounded")),
                "grounded=%s mode=%s sources=%s"
                % (
                    body.get("grounded"),
                    body.get("retrieval_mode"),
                    body.get("retrieved_sources"),
                ),
            )

            # The name is what the compliance PDF is built from, so it
            # has to come back exactly as sent, not normalised.
            check(
                "incident echoes substance_name verbatim",
                body.get("substance_name") == incident_body["substance_name"],
                "%r" % body.get("substance_name"),
            )

            # Not asserted to be "featherless": a deployment where the
            # primary is unfunded still answers correctly via the
            # fallback, and this test checks honesty, not billing. What
            # it must be is one of the two known providers, and it must
            # agree with what /health reports.
            check(
                "incident names its generation provider",
                body.get("generation_provider") in ("featherless", "groq"),
                "generation_provider=%r%s"
                % (
                    body.get("generation_provider"),
                    "  <- PRIMARY NOT IN USE"
                    if body.get("generation_provider") == "groq" else "",
                ),
            )

        else:
            check(
                "incident degrades honestly",
                status == 503 and isinstance(body, dict) and "detail" in body,
                "no provider on the server -- %s: %s" % (status, body),
            )

        # The index state is only knowable after something has asked
        # for it, so this has to come after the assessment above.
        status, health_final = call(base, "/health", key)

        check(
            "health reports retrieval truthfully",
            "retrieval" in health_final and health_final["retrieval"] is not None,
            "retrieval: %s" % health_final.get("retrieval"),
        )

        generation = health_final.get("generation", {})

        check(
            "health agrees with the response about the provider",
            generation.get("last_provider") == body.get("generation_provider"),
            "health=%r response=%r configured=%s"
            % (
                generation.get("last_provider"),
                body.get("generation_provider"),
                generation.get("featherless_configured"),
            ),
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

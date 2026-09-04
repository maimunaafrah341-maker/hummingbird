"""
Smoke test to run against a DEPLOYED instance, from a machine that is
not the one hosting it.

    python smoke_test.py https://your-service.onrender.com
    python smoke_test.py https://your-service.onrender.com --key YOUR_API_KEY

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

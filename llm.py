"""
Multi-provider LLM caller with automatic failover.

Extracted from Athena's response_engine.py, minus every prompt: this
module knows how to get a completion out of somebody, and nothing about
what to ask. Prompt construction stays in the application.

    generate_response(prompt) -> str

Tries Groq, then Gemini, then OpenRouter, walking a list of models
within each provider before moving on. The point is not redundancy for
its own sake -- it is that all three have free tiers with different
rate limits, and a demo that dies because one provider is throttling
you is indistinguishable, to whoever is watching, from a demo that is
simply broken.

Configure with any subset of GROQ_API_KEY, GEMINI_API_KEY and
OPENROUTER_API_KEY in the environment. Providers with no key are
skipped, so one key is enough to run.

    generate_structured_response(prompt) -> dict

A separate entry point for callers that need a JSON object back rather
than prose. It goes to Featherless (Qwen2.5-72B-Instruct) and nowhere
else -- no failover chain -- and retries once with a correction prompt
if the model returns something that will not parse. Needs
FEATHERLESS_API_KEY.

The two functions are deliberately independent. generate_response()'s
three-tier behaviour is what /translate is documented against, so
nothing in the structured path is permitted to alter it, and the
structured path's single-provider design is not allowed to leak back
into the prose one.
"""

import json
import os
import re
import threading
import time

import requests
from dotenv import load_dotenv
from google import genai


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Optional as of 2026-08-29 -- Groq is primary now (see
# generate_response() below), Gemini demoted to a fallback tier. A
# missing key here just means that tier is skipped, same as
# GROQ_API_KEY/OPENROUTER_API_KEY -- not fatal, since generation no
# longer depends on Gemini specifically working.
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GEMINI_MODEL = "gemini-3.6-flash"

# If the primary model is overloaded (503 "high demand"), try these in
# order before giving up. Newer models can have much tighter capacity
# right after release than older, more established ones -- confirmed
# empirically 2026-08-21: during a sustained multi-hour gemini-3.6-flash
# outage, both gemini-3.5-flash and gemini-3.1-flash-lite responded
# fine. This is what protects a live demo from one model's capacity
# issue taking down the whole pipeline.
GEMINI_MODEL_FALLBACKS = [
    GEMINI_MODEL,
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]

# ============================================================
# CROSS-PROVIDER FALLBACK (Groq, then OpenRouter)
# ============================================================
#
# GEMINI_MODEL_FALLBACKS above only protects against ONE Gemini model
# being overloaded -- all three models still share the same Google
# Cloud project's quota, so account-level exhaustion (exactly what
# happened 2026-08-27, taking the whole demo down mid-crisis) takes
# out all three at once. Groq and OpenRouter are genuinely separate
# billing/quota pools, so they survive a Gemini-account-wide outage
# that the three Gemini models alone can't. Both optional -- if a key
# isn't set, that tier is silently skipped rather than erroring, same
# pattern as OPENAI_API_KEY in voice_service.py.
#
# Both use an OpenAI-compatible chat-completions shape, verified with
# real calls 2026-08-29 (not guessed at) -- Groq confirmed working
# with openai/gpt-oss-120b; four free OpenRouter models were tried,
# two (google/gemma-4-26b-a4b-it:free, z-ai/glm-5.2:free) were
# rate-limited on OpenRouter's shared free pool at that exact moment,
# two (listed below) responded cleanly -- so OPENROUTER_MODELS tries
# more than one for the same reason GEMINI_MODEL_FALLBACKS does: a
# free shared pool being briefly congested shouldn't take down the
# last-resort tier either.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "openai/gpt-oss-120b"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODELS = [
    "minimax/minimax-m3:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]


def _call_openai_compatible_api(base_url, api_key, model, prompt, timeout=30):
    """
    Shared request shape for Groq and OpenRouter -- both are
    OpenAI-compatible chat-completions endpoints, so one function
    covers both rather than duplicating the same requests.post() call
    twice with different URLs.
    """

    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )

    response.raise_for_status()

    content = response.json()["choices"][0]["message"].get("content")

    if not content:
        raise RuntimeError(f"{model} returned an empty response.")

    return content.strip()

# ============================================================
# ATHENA — RESPONSE ENGINE
# ============================================================

"""
Response generation layer for Athena.

Combines:
    1. Structured incident understanding
    2. Risk assessment
    3. Retrieved verified evidence

The final response is intended to remain in the user's
original language.
"""


# ============================================================
# LANGUAGE NAMES
# ============================================================

LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "te": "Telugu",
}


def generate_response(prompt):
    """
    Send the grounded Athena prompt to a model and return the
    generated user-facing response.

    Three tiers, tried in order, each a separate billing/quota pool
    so an outage or a funding lapse in one doesn't take down
    generation entirely:

      1. Groq -- primary as of 2026-08-29. Fast, own separate
         account/billing.
      2. Gemini (GEMINI_MODEL_FALLBACKS) -- demoted to fallback, not
         removed: three models tried in sequence, kept exactly as
         built and documented even if GEMINI_API_KEY's billing is
         later pulled. If Gemini is unfunded, all three attempts fail
         fast (auth/quota error, no real cost in time) and execution
         falls through to tier 3 -- same "real code, currently
         unfunded, documented honestly rather than deleted" pattern
         already used for OPENAI_API_KEY in voice_service.py.
      3. OpenRouter (OPENROUTER_MODELS) -- last resort, free-tier
         models. Two tried in sequence for the same reason Gemini
         tries three: a free shared pool can be briefly congested.

    Tiers 2/3 are skipped as errors (not fatal) when their API key
    isn't set in .env.
    """

    last_error = None

    if GROQ_API_KEY:

        try:
            return _call_openai_compatible_api(
                "https://api.groq.com/openai/v1",
                GROQ_API_KEY,
                GROQ_MODEL,
                prompt,
            )

        except Exception as e:
            last_error = e
            print(f"[Groq] {GROQ_MODEL} failed: {type(e).__name__}: {e}")

    if client:

        for model in GEMINI_MODEL_FALLBACKS:

            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                )

                if not response.text:
                    raise RuntimeError(
                        "Gemini returned an empty response."
                    )

                return response.text.strip()

            except Exception as e:
                last_error = e
                print(f"[Gemini] {model} failed: {type(e).__name__}: {e}")
                continue

    if OPENROUTER_API_KEY:

        for model in OPENROUTER_MODELS:

            try:
                return _call_openai_compatible_api(
                    "https://openrouter.ai/api/v1",
                    OPENROUTER_API_KEY,
                    model,
                    prompt,
                )

            except Exception as e:
                last_error = e
                print(f"[OpenRouter] {model} failed: {type(e).__name__}: {e}")
                continue

    # No provider was even attempted, because none of the three keys is
    # set. Without this guard the `raise last_error` below raises None,
    # and Python replaces it with "TypeError: exceptions must derive
    # from BaseException" -- an error about the error, naming neither
    # the cause nor the fix, printed under a banner claiming three
    # providers failed when none was tried. Callers that catch
    # Exception (translation.py) degrade correctly either way; the
    # person reading the log to find out why does not.
    if last_error is None:
        raise RuntimeError(
            "No LLM provider is configured -- set at least one of "
            "GROQ_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY."
        )

    print("\n" + "=" * 70)
    print("RESPONSE GENERATION ERROR (Groq, Gemini, and OpenRouter all failed)")
    print("=" * 70)
    print(type(last_error).__name__)
    print(str(last_error))
    print("=" * 70)

    raise last_error


# ============================================================
# STRUCTURED JSON GENERATION (Featherless, then Groq)
# ============================================================
#
# Everything below is additive. Nothing above this line is touched by
# it, which is the point: /translate is documented against
# generate_response()'s exact three-tier behaviour, and a second caller
# with different needs must not be able to change what the first one
# does. This path reuses generate_response()'s GROQ_API_KEY and its
# _call_openai_compatible_api() helper, and changes neither.
#
# Featherless is always attempted first and is never skipped when it is
# configured -- that ordering is a requirement, not a performance
# judgement. Groq exists behind it because Featherless's current plan
# excludes automated API use, so an attempt that fails on
# authorisation is expected rather than exceptional, and a dispatcher
# that stops dispatching because of a billing tier is not acceptable.
#
# This is deliberately narrower than generate_response()'s chain: two
# providers, not four. A structured response has to arrive in an agreed
# shape, and every additional provider is another house style to hold
# to it.

FEATHERLESS_API_KEY = os.getenv("FEATHERLESS_API_KEY")
FEATHERLESS_MODEL = "Qwen/Qwen2.5-72B-Instruct"
FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"

# The Groq fallback. Same key and same helper generate_response() uses
# -- GROQ_MODEL and GROQ_API_KEY are defined once, above, and read from
# here rather than redeclared, so there is no second copy to drift.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# How much of a bad response to quote back to the model when asking it
# again. Enough that it can see what it did wrong, capped so that a
# model which answered with three pages of prose does not produce a
# retry prompt larger than its own context budget.
CORRECTION_ECHO_CHARS = 2000

# Per-provider time budgets, each covering that provider's first
# attempt AND its correction retry. They are separate because the two
# providers are not remotely alike in speed, and because a slow
# Featherless attempt must not eat the time Groq needs to rescue it.
#
# 60s for Featherless: measured 2026-09-04, its median was under 25s
# and its slowest SUCCESSFUL call was 42s. Past 60s we would only be
# buying the pathological tail -- two observed runs of 221s and 242s --
# which is exactly what the watchdog exists to cut off.
#
# 30s for Groq: openai/gpt-oss-120b answers /translate prompts in about
# 1.0s measured. 30s is deliberately generous for a larger structured
# prompt plus a retry.
#
# 60 + 30 = 90, which is the same worst case this function had when it
# only called Featherless. The fallback costs no additional latency
# ceiling; it re-divides the budget that was already there.
FEATHERLESS_DEADLINE = 60
GROQ_DEADLINE = 30

# Do not start a correction retry that cannot finish inside what is
# left of that provider's budget. Beginning a call whose result nobody
# will wait for spends the provider's token quota to produce nothing --
# and on the Featherless account, spending quota is what makes the NEXT
# request fail. When Featherless runs out of budget mid-retry the right
# move is to fall through to Groq, which is an order of magnitude
# faster than the retry would have been anyway.
MIN_RETRY_SECONDS = 15

# Which provider answered the most recent successful structured call:
# None until one has, then "featherless" or "groq". Exposed so /health
# can show it, for the same reason language.py exposes its tier state.
#
# It exists because "Featherless is configured" and "Featherless is
# actually answering" are different claims, and only the second one is
# the point. A deployment whose Featherless key is silently rejected on
# every request would otherwise look completely healthy while never
# once using the provider it is required to use.
_last_provider = None


def last_generation_provider():
    """
    The provider that answered the last successful structured call, or
    None if none has succeeded yet in this process.
    """

    return _last_provider


def featherless_configured():
    """
    Whether a Featherless key is present. Says nothing about whether it
    works -- only a real call can establish that, which is what
    last_generation_provider() reports.
    """

    return bool(FEATHERLESS_API_KEY)


class StructuredGenerationTimeout(requests.exceptions.Timeout):
    """
    The budget above was exhausted.

    Subclasses requests.exceptions.Timeout so that callers already
    handling transport failures catch it without knowing this class
    exists -- api.py maps it to a 503 through its existing
    RequestException branch, which is the right answer: the provider
    did not respond in time. A bare TimeoutError would match none of
    those handlers and surface as a 500, blaming the service for the
    provider being slow.
    """


def _call_with_deadline(seconds, function, *args, **kwargs):
    """
    Run `function` in a worker thread and give up waiting after
    `seconds`.

    This bounds how long the CALLER waits. It does not cancel the
    work: Python cannot interrupt a thread blocked in a socket read,
    so the abandoned request keeps running until the provider answers
    or FEATHERLESS_TIMEOUT fires, and it still spends the account's
    token budget on the way. That is a real cost and it is the reason
    MIN_RETRY_SECONDS exists -- what this buys is a predictable
    failure for whoever is waiting, not a lighter load on the
    provider.

    The thread is a daemon so that a hung provider can never keep the
    process from exiting.

    Any exception raised inside the worker is re-raised here, so every
    existing error path -- RuntimeError, HTTPError, JSONDecodeError --
    reaches the caller exactly as it would without the watchdog.
    """

    holder = {}

    def worker():

        try:
            holder["value"] = function(*args, **kwargs)

        except Exception as e:
            holder["error"] = e

    thread = threading.Thread(target=worker, daemon=True)

    thread.start()
    thread.join(seconds)

    if thread.is_alive():
        raise StructuredGenerationTimeout(
            "%s did not respond within %.0fs (the request is still running "
            "and will still consume quota)" % (FEATHERLESS_MODEL, seconds)
        )

    if "error" in holder:
        raise holder["error"]

    return holder["value"]


def _remaining(deadline):
    """Seconds left before `deadline`, never negative."""

    return max(0.0, deadline - time.monotonic())


def _extract_json(text):
    """
    Parse a model's response into a dict, or raise ValueError.

    Models that have been told to return only JSON return only JSON
    most of the time. The rest of the time they wrap it in ```json
    fences, or open with "Here is the JSON you requested:", or add a
    sentence of commentary after the closing brace. None of that is
    worth a retry when the JSON itself is sitting right there, so this
    strips the two common wrappers before giving up.

    Raises rather than returning None so the caller can distinguish
    "the model produced something unusable" -- worth one more attempt
    -- from a transport failure, which is not.
    """

    if not text or not text.strip():
        raise ValueError("empty response")

    candidate = text.strip()

    # ```json ... ``` or a bare ``` ... ``` fence.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)

    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)

    except ValueError:

        # Second attempt: slice from the first brace to the last one,
        # which recovers the object from surrounding prose.
        start = candidate.find("{")
        end = candidate.rfind("}")

        if start == -1 or end == -1 or end < start:
            raise ValueError("no JSON object found in the response")

        try:
            parsed = json.loads(candidate[start:end + 1])

        except ValueError as e:
            raise ValueError("response was not valid JSON (%s)" % e)

    # A bare list or string is valid JSON and still not what any caller
    # of this function asked for.
    if not isinstance(parsed, dict):
        raise ValueError(
            "expected a JSON object, got %s" % type(parsed).__name__
        )

    return parsed


def _attempt_provider(base_url, api_key, model, prompt, budget, label):
    """
    One provider's full attempt at a JSON answer: a call, and one
    correction retry if it comes back unparseable and the budget allows.

    Returns the parsed dict. Raises on anything else -- which is the
    point, because the caller distinguishes providers by catching that.

    `budget` covers this provider's attempt AND its retry, so a slow
    first call shortens the retry rather than doubling the ceiling.
    Monotonic clock, because an adjustment mid-request must not extend
    or collapse it.

    The read timeout is derived from the budget rather than fixed: a
    socket timeout longer than the watchdog it sits inside can never
    fire, and one much shorter would abandon a generation that was
    nearly finished.
    """

    deadline = time.monotonic() + budget
    read_timeout = max(5, int(budget))

    raw = _call_with_deadline(
        _remaining(deadline),
        _call_openai_compatible_api,
        base_url,
        api_key,
        model,
        prompt,
        timeout=read_timeout,
    )

    try:
        return _extract_json(raw)

    except ValueError as e:
        print(
            "[%s] %s failed to return JSON (%s) -- retrying once with a "
            "correction prompt" % (label, model, e),
            flush=True,
        )

    remaining = _remaining(deadline)

    if remaining < MIN_RETRY_SECONDS:
        raise ValueError(
            "%s did not return valid JSON, and only %.0fs of its %ds budget "
            "remained -- too little to retry"
            % (model, remaining, budget)
        )

    corrected = (
        "%s\n\n"
        "---\n\n"
        "Your previous output was NOT valid JSON. This is what you "
        "returned:\n\n"
        "%s\n\n"
        "Return ONLY a single valid JSON object. No markdown fences, no "
        "commentary before or after it, no explanation, no apology. The "
        "first character of your response must be { and the last must "
        "be }."
        % (prompt, raw[:CORRECTION_ECHO_CHARS])
    )

    retried = _call_with_deadline(
        remaining,
        _call_openai_compatible_api,
        base_url,
        api_key,
        model,
        corrected,
        timeout=read_timeout,
    )

    try:
        return _extract_json(retried)

    except ValueError as e:
        print("\n" + "=" * 70)
        print("STRUCTURED GENERATION ERROR (%s returned invalid JSON twice)"
              % label)
        print("=" * 70)
        print(retried[:CORRECTION_ECHO_CHARS])
        print("=" * 70)

        raise ValueError(
            "%s did not return valid JSON after a correction retry (%s)"
            % (model, e)
        )


def generate_structured_response(prompt):
    """
    Get a JSON object out of a model. Returns (payload, provider).

    Featherless first, always, whenever it is configured -- that
    ordering is required, not chosen on merit. Groq picks up if
    Featherless fails for ANY reason: rejected key, rate limit,
    watchdog timeout, or two unparseable answers in a row. Both
    providers get identical treatment, including the one correction
    retry.

    The provider name is returned alongside the payload rather than
    inserted into it. The payload is the model's own JSON, and writing
    our own key into it risks colliding with one the model actually
    produced -- and would make the caller unable to tell the difference.

    Guarantees a dict. Says nothing about which keys are in it: this
    module does not know what was asked for, and the caller that wrote
    the prompt is the only thing that can validate the answer against
    it.

    Raises RuntimeError when no provider is configured at all, and
    otherwise re-raises the LAST provider's failure -- the earlier one
    is printed rather than raised, because the caller needs one error
    to act on and the most recent is the one that decided the outcome.
    """

    global _last_provider

    attempts = []

    if FEATHERLESS_API_KEY:
        attempts.append((
            "featherless", FEATHERLESS_BASE_URL, FEATHERLESS_API_KEY,
            FEATHERLESS_MODEL, FEATHERLESS_DEADLINE,
        ))

    else:
        # Not fatal any more, but not quiet either. Featherless being
        # unconfigured means every response silently comes from the
        # fallback, which is a deployment that looks healthy while
        # never using the provider it is supposed to use.
        print(
            "[Featherless] FEATHERLESS_API_KEY is not set -- skipping the "
            "primary provider entirely and going straight to Groq",
            flush=True,
        )

    if GROQ_API_KEY:
        attempts.append((
            "groq", GROQ_BASE_URL, GROQ_API_KEY,
            GROQ_MODEL, GROQ_DEADLINE,
        ))

    if not attempts:
        raise RuntimeError(
            "No structured-generation provider is configured -- set "
            "FEATHERLESS_API_KEY or GROQ_API_KEY."
        )

    last_error = None

    for index, (label, base_url, api_key, model, budget) in enumerate(attempts):

        try:
            payload = _attempt_provider(
                base_url, api_key, model, prompt, budget, label,
            )

            _last_provider = label

            return payload, label

        except Exception as e:

            last_error = e

            remaining = len(attempts) - index - 1

            print(
                "[%s] %s failed: %s: %s%s"
                % (
                    label, model, type(e).__name__, e,
                    " -- falling back" if remaining else " -- no providers left",
                ),
                flush=True,
            )

    raise last_error

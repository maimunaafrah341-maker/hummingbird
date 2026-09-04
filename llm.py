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
# STRUCTURED JSON GENERATION (Featherless)
# ============================================================
#
# Everything below is additive. Nothing above this line is touched by
# it, which is the point: /translate is documented against
# generate_response()'s exact three-tier behaviour, and a second caller
# with different needs must not be able to change what the first one
# does.
#
# Unlike generate_response(), this path has no failover. That is a
# deliberate decision rather than an oversight: a single provider
# answering in a known JSON dialect is easier to hold to a strict
# output contract than four providers with four house styles, and a
# structured response that arrives in the wrong shape is not more
# useful than one that does not arrive. The tradeoff is real and it
# belongs in the API contract, not hidden here -- a Featherless outage
# takes the structured endpoint down, while /translate keeps running on
# its three tiers.

FEATHERLESS_API_KEY = os.getenv("FEATHERLESS_API_KEY")
FEATHERLESS_MODEL = "Qwen/Qwen2.5-72B-Instruct"
FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"

# Longer than the 30s default used above. A 72B model on a shared
# serverless tier is slower to first token than the small chat models
# the prose path uses, and a timeout that fires while the model is
# still writing produces the same user-visible outcome as an outage
# while wasting the generation that was nearly finished.
FEATHERLESS_TIMEOUT = 60

# How much of a bad response to quote back to the model when asking it
# again. Enough that it can see what it did wrong, capped so that a
# model which answered with three pages of prose does not produce a
# retry prompt larger than its own context budget.
CORRECTION_ECHO_CHARS = 2000


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


def generate_structured_response(prompt):
    """
    Send a prompt to Featherless and return the parsed JSON object it
    replied with.

    One retry, and only for the one failure mode a retry can fix. If
    the response does not parse, the model is asked again with its own
    invalid output quoted back to it and a blunter instruction --
    which works often enough to be worth a second call, because the
    usual cause is a model adding conversational framing rather than a
    model that cannot produce the shape at all.

    A transport failure is NOT retried here. A timeout or a 5xx means
    the provider is unwell, and immediately asking it again is how a
    slow outage becomes a slower one; that decision belongs to the
    caller, which knows whether anyone is still waiting.

    Guarantees a dict. Says nothing about which keys are in it --
    this module does not know what was asked for, and the caller that
    wrote the prompt is the only thing that can validate the answer
    against it.

    Raises RuntimeError if no key is configured, ValueError if the
    model failed to produce JSON twice, and whatever requests raises
    on a transport failure.
    """

    if not FEATHERLESS_API_KEY:
        raise RuntimeError(
            "FEATHERLESS_API_KEY is not set -- structured generation is "
            "unavailable."
        )

    raw = _call_openai_compatible_api(
        FEATHERLESS_BASE_URL,
        FEATHERLESS_API_KEY,
        FEATHERLESS_MODEL,
        prompt,
        timeout=FEATHERLESS_TIMEOUT,
    )

    try:
        return _extract_json(raw)

    except ValueError as e:
        print(
            "[Featherless] %s failed to return JSON (%s) -- retrying once "
            "with a correction prompt" % (FEATHERLESS_MODEL, e),
            flush=True,
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

    retried = _call_openai_compatible_api(
        FEATHERLESS_BASE_URL,
        FEATHERLESS_API_KEY,
        FEATHERLESS_MODEL,
        corrected,
        timeout=FEATHERLESS_TIMEOUT,
    )

    try:
        return _extract_json(retried)

    except ValueError as e:
        print("\n" + "=" * 70)
        print("STRUCTURED GENERATION ERROR (Featherless returned invalid "
              "JSON twice)")
        print("=" * 70)
        print(retried[:CORRECTION_ECHO_CHARS])
        print("=" * 70)

        raise ValueError(
            "%s did not return valid JSON after a correction retry (%s)"
            % (FEATHERLESS_MODEL, e)
        )

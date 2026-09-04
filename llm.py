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
"""

import os

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

    print("\n" + "=" * 70)
    print("RESPONSE GENERATION ERROR (Groq, Gemini, and OpenRouter all failed)")
    print("=" * 70)
    print(type(last_error).__name__)
    print(str(last_error))
    print("=" * 70)

    raise last_error

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

# Featherless. OpenAI-compatible chat-completions, so it goes through
# the same _call_openai_compatible_api as Groq and OpenRouter.
#
# Tried FIRST when its key is set, because it is the provider this
# project is required to use -- the others stay behind it as failover so
# a throttle or an outage still leaves something answering.
#
# **Read this before pointing automated traffic at it.** Featherless
# sells two different things. The Chat/Premium plans are licensed for
# human-driven interactive use and exclude automation; the Developer
# plans are the credit-based, API-driven ones. A server calling this on
# every incident is automation by definition. So the one call this
# project makes is operator-initiated -- see /incident/brief in
# incident_api.py -- and the autonomous safety path never touches it.
# That is a licensing decision, not a performance one.
#
# A new account gets 100,000 trial tokens, which at roughly 600 tokens
# a brief is over 150 presses of the button.
FEATHERLESS_API_KEY = os.getenv("FEATHERLESS_API_KEY")
FEATHERLESS_BASE_URL = os.getenv(
    "FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1")

# Several, tried in order, for the same reason OPENROUTER_MODELS is a
# list: which models a key can reach depends on the plan tier, so a
# single hardcoded name turns a tier difference into a hard failure.
FEATHERLESS_MODELS = [
    m.strip() for m in os.getenv(
        "FEATHERLESS_MODELS",
        # Qwen first, measured against the live key on 2026-09-05:
        # it returned 537 clean structured characters. Mistral-7B-v0.3
        # was first and degenerated -- a wall of one repeated letter,
        # then stray Korean -- and Featherless returned 200 for it, so
        # nothing upstream noticed. Llama-3.1-8B answers 403: gated
        # behind a HuggingFace org connection, not a plan tier.
        "Qwen/Qwen2.5-7B-Instruct,"
        "Qwen/Qwen2.5-14B-Instruct,"
        "mistralai/Mistral-7B-Instruct-v0.3",
    ).split(",") if m.strip()
]


def _looks_degenerate(text):
    """
    Did the model fall into a repetition loop?

    Two signals, both cheap. A long run of one character is the classic
    loop; one character dominating the whole response catches the
    slower version that alternates a little. Normal prose peaks at
    roughly 18% for the space character, so half is a wide margin.
    """

    if len(text) < 40:
        return False

    if re.search(r"(.){29,}", text):
        return True

    commonest = max(text.count(c) for c in set(text))
    return commonest / len(text) > 0.5


def _call_openai_compatible_api(base_url, api_key, model, prompt, timeout=30,
                                system=None, temperature=None, max_tokens=None):
    """
    Shared request shape for Featherless, Groq and OpenRouter -- all
    three are OpenAI-compatible chat-completions endpoints, so one
    function covers them rather than duplicating the same
    requests.post() call with different URLs.

    `system` is a real system message rather than text glued onto the
    front of the prompt. The difference matters for the copilot, whose
    system message is a set of prohibitions -- never issue orders,
    never claim an action was taken -- and a model weights those more
    heavily in the role they belong to than in the middle of a user
    turn where they read as suggestions.
    """

    messages = []

    if system:
        messages.append({"role": "system", "content": system})

    messages.append({"role": "user", "content": prompt})

    body = {"model": model, "messages": messages}

    if temperature is not None:
        body["temperature"] = temperature

    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=timeout,
    )

    response.raise_for_status()

    content = response.json()["choices"][0]["message"].get("content")

    if not content:
        raise RuntimeError(f"{model} returned an empty response.")

    content = content.strip()

    # A 200 is not the same as an answer. A small model can fall into a
    # repetition loop and return a wall of one character, and the
    # provider reports that as success -- so without this check it
    # reaches the operator as advice. Raising here lets the caller fall
    # through to the next model, which is what the list is for.
    if _looks_degenerate(content):
        raise RuntimeError(
            f"{model} returned degenerate output "
            f"({len(content)} chars, repeating).")

    return content

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


def generate_response(prompt, system=None, temperature=None,
                      max_tokens=None, want_provider=False):
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

    if FEATHERLESS_API_KEY:

        for model in FEATHERLESS_MODELS:

            try:
                answer = _call_openai_compatible_api(
                    FEATHERLESS_BASE_URL,
                    FEATHERLESS_API_KEY,
                    model,
                    prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return (answer, "featherless:%s" % model) if want_provider else answer

            except Exception as e:
                last_error = e
                # A 429 here is worth reading rather than retrying past.
                # On a Chat/Premium plan it is not "too fast", it is the
                # plan refusing automated traffic it is not licensed for
                # -- a bigger Chat tier will not fix it, a Developer plan
                # or fewer, human-initiated calls will.
                print(f"[Featherless] {model} failed: {type(e).__name__}: {e}")
                continue

    if GROQ_API_KEY:

        try:
            answer = _call_openai_compatible_api(
                "https://api.groq.com/openai/v1",
                GROQ_API_KEY,
                GROQ_MODEL,
                prompt,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (answer, "groq:%s" % GROQ_MODEL) if want_provider else answer

        except Exception as e:
            last_error = e
            print(f"[Groq] {GROQ_MODEL} failed: {type(e).__name__}: {e}")

    if client:

        for model in GEMINI_MODEL_FALLBACKS:

            try:
                response = client.models.generate_content(
                    model=model,
                    # This client call takes no system role, so the
                    # instructions go in front of the prompt. Said here
                    # rather than silently dropping `system`, which
                    # would quietly relax the copilot's prohibitions on
                    # exactly the tier nobody is watching.
                    contents=("%s\n\n%s" % (system, prompt))
                             if system else prompt,
                )

                if not response.text:
                    raise RuntimeError(
                        "Gemini returned an empty response."
                    )

                text = response.text.strip()
                return (text, "gemini:%s" % model) if want_provider else text

            except Exception as e:
                last_error = e
                print(f"[Gemini] {model} failed: {type(e).__name__}: {e}")
                continue

    if OPENROUTER_API_KEY:

        for model in OPENROUTER_MODELS:

            try:
                answer = _call_openai_compatible_api(
                    "https://openrouter.ai/api/v1",
                    OPENROUTER_API_KEY,
                    model,
                    prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return (answer, "openrouter:%s" % model) if want_provider else answer

            except Exception as e:
                last_error = e
                print(f"[OpenRouter] {model} failed: {type(e).__name__}: {e}")
                continue

    # last_error is still None when NO provider was configured at all --
    # every tier above was skipped rather than attempted, so nothing ever
    # assigned to it. `raise None` is a TypeError about exceptions not
    # deriving from BaseException, which tells the caller nothing about
    # the actual problem: there are no API keys in .env.
    #
    # This is the default state of a fresh clone and of a demo laptop
    # whose .env was never filled in, so it is the error most likely to
    # be seen and the one that most needs to say what to do.
    if last_error is None:
        last_error = RuntimeError(
            "no LLM provider is configured -- set FEATHERLESS_API_KEY "
            "(tried first), or GROQ_API_KEY / GEMINI_API_KEY / "
            "OPENROUTER_API_KEY in .env (see env.example)")

    print("\n" + "=" * 70)
    print("RESPONSE GENERATION ERROR (Groq, Gemini, and OpenRouter all failed)")
    print("=" * 70)
    print(type(last_error).__name__)
    print(str(last_error))
    print("=" * 70)

    raise last_error

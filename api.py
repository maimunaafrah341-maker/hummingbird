"""
HTTP layer over language.py and translation.py.

Deliberately thin. Every real decision -- how detection tiers work,
what a failed translation returns, when the model loads -- lives in the
modules; this file only turns those into requests and responses and
makes the failures legible. If you find yourself adding logic here,
it probably belongs in the module instead.

Run it:

    uvicorn api:app --host 0.0.0.0 --port 8000

Endpoints are documented in API_CONTRACT.md. That file is the thing to
hand someone building against this; this docstring is for whoever edits
the file itself.
"""

import logging
import os
import time
import uuid

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import incident
import language
import llm
import translation


# ============================================================
# CONFIGURATION
# ============================================================

# Longer than any single utterance a person types, short enough that a
# hostile caller cannot make the process do 300 ms of embedding work
# per request. Rejected with 400, not silently truncated -- truncating
# would return a language detected from half the input while reporting
# success.
MAX_TEXT_CHARS = 5000

# /incident's fields are identifiers and short labels, not prose: a bay
# name, a substance code, a few words describing what happened. The
# 5000-character limit above would be meaningless here -- these go into
# a prompt, and a caller who can put 5000 characters into
# incident_type can rewrite the instruction the model is following.
MAX_FIELD_CHARS = 200

# Optional shared-secret gate. Unset (the default) means the API is
# open, which is correct for a public demo. Set it and every endpoint
# except /health requires a matching X-API-Key header.
API_KEY = os.getenv("API_KEY") or None

# Load the embedding model at startup instead of on the first romanized
# request. Off by default: the load is ~22s and ~800MB, and paying it
# before the port opens is what gets a container killed on a small
# host. See EVAL.md.
WARM_UP = os.getenv("WARM_UP", "").strip() == "1"

# Browser origins allowed to call this API. Comma-separated; "*" allows
# any origin.
#
# This exists because a browser, not the caller, enforces the rule: a
# page served from one origin cannot read a response from another
# unless the server says it may. No amount of frontend work can get
# around that, so it has to be decided here.
#
# The default names the deployed console so a fresh deployment works
# without configuration, but it is only a default -- CORS_ORIGINS
# replaces it entirely. Vercel issues a new hostname for every preview
# deployment, so a preview build that is not listed here will be
# blocked; add it to the env var, or set "*" for a demo where the API
# is open anyway.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "https://hummingbird-j1dpu9frm-humming-bird1.vercel.app",
    ).split(",")
    if origin.strip()
]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("api")


app = FastAPI(
    title="Multilingual detection and translation",
    description=(
        "Language detection, script detection, and translation. "
        "See API_CONTRACT.md."
    ),
    version="1.0.0",
)


# ============================================================
# MIDDLEWARE
# ============================================================

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    One log line per request, with a correlation id echoed back in the
    X-Request-ID header.

    The id exists so a report of "it returned the wrong language at
    about 4pm" can be matched to an actual line in the logs. Without
    one, every debugging conversation starts by trying to guess which
    of a hundred identical-looking requests was the one that failed --
    which is exactly the position this project spent three days in
    with a background task that logged nothing.
    """

    request_id = uuid.uuid4().hex[:8]
    started = time.time()

    try:
        response = await call_next(request)

    except Exception:
        # An unhandled exception must still produce a logged,
        # correlated, well-formed response -- not a bare 500 from the
        # server with nothing on either side to tie it to.
        log.exception(
            "%s %s %s -> unhandled exception (%.0fms)",
            request_id, request.method, request.url.path,
            (time.time() - started) * 1000,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    elapsed_ms = (time.time() - started) * 1000

    log.info(
        "%s %s %s -> %d (%.0fms)",
        request_id, request.method, request.url.path,
        response.status_code, elapsed_ms,
    )

    response.headers["X-Request-ID"] = request_id

    return response


# Added AFTER log_requests deliberately: Starlette makes the
# last-registered middleware the outermost one, so this wraps the
# logger rather than sitting inside it. That ordering is what puts CORS
# headers on error responses too -- including the 500 that log_requests
# builds when something throws. Inside, a browser would be told nothing
# about why a failed request failed, which is the exact case somebody
# is debugging when they need the header most.
#
# allow_credentials stays False: this API authenticates with an
# X-API-Key header, not cookies, so it never needs credentialed
# requests -- and "*" plus credentials is a combination browsers reject
# outright.
#
# X-Request-ID is exposed because API_CONTRACT.md tells callers to
# quote it when reporting a problem, and by default a browser will not
# let page JavaScript read a response header at all.
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )


def require_key(x_api_key):
    """
    Reject a request when API_KEY is configured and the header does not
    match. A no-op when API_KEY is unset.
    """

    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def validate_text(text):
    """
    Shared input guard. Returns the text, or raises the 400 that
    describes precisely what was wrong with it.
    """

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"text exceeds {MAX_TEXT_CHARS} characters (got {len(text)})",
        )

    return text


def validate_field(value, name, max_chars=MAX_FIELD_CHARS):
    """
    Input guard for /incident's short string fields. Same contract as
    validate_text(): returns the cleaned value, or raises the 400 that
    says exactly what was wrong.

    Returns the stripped value, because these are compared and
    embedded downstream and " CL2 " and "CL2" are the same substance.
    """

    if not value or not value.strip():
        raise HTTPException(status_code=400, detail=f"{name} must not be empty")

    if len(value) > max_chars:
        raise HTTPException(
            status_code=400,
            detail=f"{name} exceeds {max_chars} characters (got {len(value)})",
        )

    return value.strip()


# ============================================================
# SCHEMAS
# ============================================================

class DetectRequest(BaseModel):
    text: str = Field(..., description="Text to identify the language of.")


class DetectResponse(BaseModel):
    language: str
    script: str
    method: str
    semantic_tier_used: bool
    latency_ms: float


class TranslateRequest(BaseModel):
    text: str = Field(..., description="Text to translate.")
    target_language: str = Field(..., description="Target language code, e.g. 'hi'.")
    source_language: str | None = Field(
        None,
        description="Source language code. Omit to let the model infer it.",
    )


class TranslateResponse(BaseModel):
    translation: str | None
    translated: bool
    reason: str | None
    latency_ms: float


class IncidentRequest(BaseModel):
    bay_id: str = Field(..., description="Where the incident is, e.g. 'BAY-04'.")
    substance_code: str | None = Field(
        None,
        description=(
            "Retrieval key, e.g. 'CL2'. Null when the detecting side "
            "could not map the substance to a known code."
        ),
    )
    substance_name: str = Field(
        ...,
        description="Human-readable substance, e.g. 'Sodium hydroxide (50% solution)'.",
    )
    incident_type: str = Field(..., description="What happened, e.g. 'gas leak'.")
    target_lang: str = Field(..., description="Language for the spoken alert.")


class IncidentResponse(BaseModel):
    severity: str
    steps: list[str]
    contraindication: str
    spoken_alert: str
    spoken_alert_translated: bool
    substance_name: str
    grounded: bool
    retrieval_mode: str
    retrieved_sources: list[str]
    generation_provider: str
    latency_ms: float


# ============================================================
# LIFECYCLE
# ============================================================

@app.on_event("startup")
def startup():
    log.info(
        "starting: semantic_tier=%s warm_up=%s auth=%s",
        language.semantic_tier_available(),
        WARM_UP,
        "on" if API_KEY else "off",
    )

    if WARM_UP:
        log.info("WARM_UP=1, loading the embedding model now (~22s)")
        log.info("semantic tier available: %s", language.warm_up())


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/health")
def health():
    """
    Liveness plus a truthful capability report.

    `semantic` is null until something has actually needed the model --
    the tier is lazy, so before the first Latin-script request there is
    genuinely no answer yet, and claiming either true or false would be
    a guess. Callers that need certainty should set WARM_UP=1.

    `retrieval` reports the same three states about the FAISS index
    behind /incident, for the same reason and with the same caveat.
    Reading it is deliberately passive: it returns what the module has
    already discovered and never triggers a load. Answering a health
    check by loading a model would make the check the most expensive
    request the service serves, and on a cold process it would block
    the very probe that is meant to establish the process is alive.

    `generation` answers two different questions that are easy to
    confuse. `featherless_configured` says a key is present -- known
    instantly, and nothing more than that. `last_provider` says which
    provider actually answered last, which is the question that
    matters: Featherless is attempted first on every request, so a
    deployment whose key is present but rejected would report
    configured=true and last_provider="groq" forever, working
    perfectly while never once using its primary provider. Null until
    something has generated, for the same reason the tiers are.
    """

    return {
        "status": "ok",
        "tiers": {
            "script": True,
            "semantic": language.semantic_tier_available(),
        },
        "retrieval": incident.retrieval_available(),
        "generation": {
            "featherless_configured": llm.featherless_configured(),
            "last_provider": llm.last_generation_provider(),
        },
        "languages": sorted(language.SUPPORTED_LANGUAGES),
    }


@app.post("/detect", response_model=DetectResponse)
def detect(body: DetectRequest, x_api_key: str | None = Header(None)):
    """
    Identify the language of a piece of text, and whether it was
    written in that language's own script or romanized.
    """

    require_key(x_api_key)

    text = validate_text(body.text)

    started = time.time()

    detected = language.detect_language(text)
    script = language.detect_script(text, detected)

    # Which tier answered. Native-script text never touches the model,
    # so reporting "semantic" for it would overstate both the cost and
    # the uncertainty of the answer.
    used_semantic = script == "latin" or script == "romanized"

    return DetectResponse(
        language=detected,
        script=script,
        method="semantic" if used_semantic else "script",
        semantic_tier_used=used_semantic and bool(language.semantic_tier_available()),
        latency_ms=round((time.time() - started) * 1000, 2),
    )


@app.post("/translate", response_model=TranslateResponse)
def translate(body: TranslateRequest, x_api_key: str | None = Header(None)):
    """
    Translate text into a target language.

    A null translation is a 200, not an error: "nothing to translate"
    and "the provider is down" are both ordinary outcomes the caller
    must handle by showing the original. `reason` says which happened
    so the caller can tell the user something true.
    """

    require_key(x_api_key)

    text = validate_text(body.text)

    if not body.target_language or not body.target_language.strip():
        raise HTTPException(status_code=400, detail="target_language must not be empty")

    started = time.time()

    result = translation.translate(
        text,
        body.target_language,
        body.source_language,
    )

    elapsed_ms = round((time.time() - started) * 1000, 2)

    if result is not None:
        return TranslateResponse(
            translation=result,
            translated=True,
            reason=None,
            latency_ms=elapsed_ms,
        )

    # Distinguish the two null cases. They look identical to the module
    # -- both are None -- but they are completely different to a user:
    # one means "your text was already in that language", the other
    # means "we could not reach a provider, try again".
    same_language = (
        body.source_language
        and body.source_language.strip().lower()
        == body.target_language.strip().lower()
    )

    return TranslateResponse(
        translation=None,
        translated=False,
        reason="already_in_target_language" if same_language else "translation_unavailable",
        latency_ms=elapsed_ms,
    )


@app.post("/incident", response_model=IncidentResponse)
def incident_response(body: IncidentRequest, x_api_key: str | None = Header(None)):
    """
    Assess a hazard and return the response to carry out.

    Retrieval degrades but generation does not. A missing corpus still
    produces an answer, marked grounded=false so the caller knows it
    was not sourced from the site's documents; a missing model produces
    an error, because there is nothing truthful to return.

    A 502 here means the model answered in a shape this endpoint will
    not vouch for -- most often a severity outside the enum. That is
    deliberately not repaired into the nearest valid value: guessing
    what an unusable hazard rating was supposed to mean is the failure
    this endpoint is built to avoid.
    """

    require_key(x_api_key)

    bay_id = validate_field(body.bay_id, "bay_id")
    substance_name = validate_field(body.substance_name, "substance_name")
    incident_type = validate_field(body.incident_type, "incident_type")
    target_lang = validate_field(body.target_lang, "target_lang")

    # Null means "the detecting side could not map this substance to a
    # code" -- a real, expected state with its own retrieval path. An
    # empty string means neither that nor a code, so it is rejected
    # rather than quietly read as null: coercing "" to unmapped would
    # hide a caller emitting empty strings where it meant to emit
    # nulls, and the symptom would be assessments silently skipping
    # substance-aware retrieval for substances that do have documents.
    substance_code = body.substance_code

    if substance_code is not None:

        if not substance_code.strip():
            raise HTTPException(
                status_code=400,
                detail="substance_code must be a non-empty string or null",
            )

        substance_code = validate_field(substance_code, "substance_code")

    # Checked against the module's own list rather than a copy kept
    # here, so adding a language in one place adds it everywhere.
    if target_lang.lower() not in language.SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail="target_lang must be one of: %s"
            % ", ".join(sorted(language.SUPPORTED_LANGUAGES)),
        )

    started = time.time()

    try:
        result = incident.assess(
            bay_id,
            substance_code,
            substance_name,
            incident_type,
            target_lang.lower(),
        )

    except RuntimeError as e:
        # No provider configured. A deployment problem, not a caller
        # problem, and the message says which key is missing.
        log.error("incident: %s", e)
        raise HTTPException(status_code=503, detail=str(e))

    except requests.RequestException as e:
        # Timeout, connection failure, rate limit, upstream 5xx. All
        # retryable, and all distinct from the model answering badly.
        #
        # The upstream status is put in the detail rather than dropped.
        # "provider unreachable (HTTPError)" and "provider returned
        # 429" send a caller to completely different places -- the
        # first reads as an outage to wait out, the second as a quota
        # to slow down against -- and the difference is already in the
        # exception. Discarding it costs whoever is debugging the
        # kiosk an hour for nothing.
        upstream = getattr(getattr(e, "response", None), "status_code", None)

        log.error(
            "incident: provider unreachable: %s: %s", type(e).__name__, e
        )

        raise HTTPException(
            status_code=503,
            detail=(
                "structured generation provider returned %d" % upstream
                if upstream
                else "structured generation provider unreachable (%s)"
                % type(e).__name__
            ),
        )

    except ValueError as e:
        # The model replied, and what it said cannot be used -- invalid
        # JSON twice over, or a shape that failed validation.
        log.error("incident: unusable model response: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    return IncidentResponse(
        latency_ms=round((time.time() - started) * 1000, 2),
        **result,
    )

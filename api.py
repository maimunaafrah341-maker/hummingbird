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

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import language
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

# Optional shared-secret gate. Unset (the default) means the API is
# open, which is correct for a public demo. Set it and every endpoint
# except /health requires a matching X-API-Key header.
API_KEY = os.getenv("API_KEY") or None

# Load the embedding model at startup instead of on the first romanized
# request. Off by default: the load is ~22s and ~800MB, and paying it
# before the port opens is what gets a container killed on a small
# host. See EVAL.md.
WARM_UP = os.getenv("WARM_UP", "").strip() == "1"


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
    """

    return {
        "status": "ok",
        "tiers": {
            "script": True,
            "semantic": language.semantic_tier_available(),
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

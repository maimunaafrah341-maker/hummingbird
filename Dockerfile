# Builds the script-detection tier only by default: ~17 MB of runtime
# memory, no torch, no model weights, boots in milliseconds. That fits
# a 512 MB free instance with room to spare, and correctly detects
# Hindi, Telugu, Urdu and Bengali in their own scripts.
#
# To add the semantic tier (romanized Hindi/Telugu), build with:
#     docker build --build-arg SEMANTIC_TIER=1 .
# That needs ~1 GB of RAM at runtime -- see EVAL.md. Without it the
# service still starts and still answers; Latin-script text just comes
# back as "en" and /health reports semantic: false.

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-semantic.txt ./

ARG SEMANTIC_TIER=0

RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$SEMANTIC_TIER" = "1" ]; then \
        pip install --no-cache-dir -r requirements-semantic.txt; \
    fi

COPY . .

# Shell form, not exec-array form, so ${PORT} actually expands. Hosts
# that inject PORT (Render, Cloud Run, Railway) are honoured; anything
# that does not gets 8000.
EXPOSE 8000

# Defaults to the INCIDENT service, not the language one.
#
# This used to default to api:app, and that cost a deployment day. A
# host that runs this image unmodified came up green, passed its health
# check -- both apps serve /health -- and returned 404 on every single
# /incident, because api:app does not have that route. Nothing in the
# logs said so. The frontend showed DEMO FALLBACK and looked like the
# frontend's fault.
#
# The incident service is what the console calls, so it is what an
# unconfigured deploy should get. The language service is still one env
# var away:
#
#     HAZARDWATCH_APP=api:app
#
# and it is the deploy that wants the unusual thing that should have to
# say so.
CMD uvicorn ${HAZARDWATCH_APP:-incident_api:app} --host 0.0.0.0 --port ${PORT:-8000}

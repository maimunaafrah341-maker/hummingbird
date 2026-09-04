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
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}

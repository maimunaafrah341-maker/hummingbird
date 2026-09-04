# Builds the whole service: the API, the multilingual-e5-small
# embedding model, and the FAISS index behind /incident.
#
# There is no lightweight variant any more. Until 2026-09-04 this file
# had a SEMANTIC_TIER build arg that installed sentence-transformers
# only when asked, so the default image carried no torch and ran in
# ~17 MB. That is gone because its premise is gone: faiss-cpu and
# sentence-transformers are base dependencies now, since retrieval is
# what /incident IS rather than an enhancement it can manage without.
# A build arg that no longer changes anything is worse than no build
# arg, because someone will believe it.
#
# MEMORY, measured in this image on 2026-09-04 with the model and the
# index both loaded and one /incident served:
#
#     ~733 MiB   charged to the container (docker stats)
#     ~955 MB    VmRSS of the uvicorn process
#
# The two differ because they count different things, and the smaller
# one is the one to size against: docker stats reports what the cgroup
# is charged, which is what the OOM killer acts on, while VmRSS also
# counts shared, file-backed pages mapped from the image layers. Quoted
# together because quoting only the lower number would flatter the
# image, and only the higher one would overstate the requirement.
#
# The 512 MB instance the old version of this file recommended will be
# OOM-killed either way. Treat 1 GB as the floor and 2 GB as
# comfortable. For reference, EVAL.md measured 821 MB peak for the
# embedding model alone, without FAISS or the API around it.
#
# IMAGE SIZE: 2.97 GB, of which roughly 470 MB is the baked model. That
# is with the CPU-only torch below; the default CUDA build would add
# several gigabytes more.

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./

# Torch from PyPI on Linux bundles the NVIDIA CUDA libraries -- several
# gigabytes of them -- into an image that will never see a GPU. The CPU
# index serves the same torch without them. Done as its own step ahead
# of requirements.txt so that pip already has torch satisfied by the
# time sentence-transformers asks for it, rather than resolving the
# default CUDA build first and discarding it.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# A fixed, explicit cache location for the model weights. Without this
# the cache lands in the build user's home directory, and an image
# whose runtime user differs from its build user silently misses it and
# downloads all over again -- the failure looking exactly like the one
# baking the weights was meant to prevent.
ENV HF_HOME=/opt/huggingface

# Bake the ~470 MB of model weights into the image.
#
# Without this they download from HuggingFace on the first request that
# needs them, which in a container means on every fresh container
# start, and means the service quietly depends on outbound network to
# huggingface.co at runtime. A first request that blocks for a minute
# on a download nobody can see -- or fails outright because the host
# has no egress -- is precisely the "boots but doesn't work" failure
# this rewrite exists to remove. The price is image size; what it buys
# is that a container which has started is actually ready.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small')"

# Make the baked cache authoritative. This line must come AFTER the
# bake above, or the bake itself would be forbidden from downloading.
#
# Baking the weights in is not on its own enough to remove the runtime
# network dependency, which was the entire point of doing it.
# huggingface_hub still HEAD-checks the hub for a newer revision of
# every file in the model on each cold start, and when it cannot reach
# the network it retries each one five times with exponential backoff
# before falling back to the cache it already has. Measured 2026-09-04
# in this image with --network none: 197.5 seconds to load weights that
# were already on disk. With this set, the same load is immediate.
#
# The tradeoff is deliberate: if the cache were ever missing, the
# service now fails loudly instead of quietly pulling 470 MB from the
# internet in production. For an image that ships its own weights,
# failing fast is the correct behaviour -- a silent download is how you
# get a container that passed its health check and then blocks a
# safety dispatcher's first real request.
ENV HF_HUB_OFFLINE=1

# Brings in vectorstore/ (the prebuilt FAISS index) so /incident is
# grounded from the first request without running ingest.py at boot,
# and corpus/ (its source) so the index CAN be rebuilt in-container
# when the corpus changes. See .dockerignore for what is left out.
COPY . .

# Shell form, not exec-array form, so ${PORT} actually expands. Hosts
# that inject PORT (Render, Cloud Run, Railway) are honoured; anything
# that does not gets 8000.
EXPOSE 8000
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}

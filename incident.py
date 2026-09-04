"""
Incident assessment: retrieve the relevant corpus material, then get a
structured judgement from the model that is grounded in it.

    assess(bay_id, substance_code, incident_type, target_lang) -> dict
    retrieval_available() -> True | False | None

Same division of labour as the rest of the project. api.py turns this
into HTTP and validates what a caller sent; this module decides what to
retrieve, what to ask, what counts as a usable answer, and what to do
when a piece of the pipeline is missing.

Two failure modes are handled here rather than raised, because both
have a truthful degraded answer:

  * The retriever cannot load (no index, no model, wrong model). The
    assessment still runs, ungrounded, and the caller is told so via
    grounded=False and an empty retrieved_sources. An unavailable
    corpus is not a reason to leave a bay with a chlorine leak in it
    without an answer.
  * The spoken alert cannot be translated. The English text is
    returned with spoken_alert_translated=False. Never label an
    untranslated string as translated -- the same discipline
    translation.py enforces by returning None.

Everything else -- no Featherless key, provider unreachable, a model
that will not produce the agreed shape -- raises, because there is no
answer to give and pretending otherwise would be worse than an error.
"""

import json
import os

import language
import translation
from llm import generate_structured_response


# ============================================================
# CONFIGURATION
# ============================================================

VECTORSTORE_DIR = os.getenv("VECTORSTORE_DIR", "vectorstore")

INDEX_FILENAME = "index.faiss"
METADATA_FILENAME = "metadata.json"

DEFAULT_TOP_K = 4

# The other half of the e5 asymmetric pair. ingest.py stores chunks
# with "passage: "; queries must use "query: " or the two sides are
# encoded for different tasks and the similarities mean less than they
# appear to. See the PASSAGE_PREFIX comment in ingest.py.
QUERY_PREFIX = "query: "

# Reserve one of the top-k slots for a regulation chunk. An SDS says
# what the chemical does; the regulation says what the site is obliged
# to do about it, and an answer built only from the first tends to be
# competent chemistry with no instruction to raise an alarm or account
# for people.
REGULATION_SLOTS = 1

# The response contract. Not a suggestion to the model -- validated on
# the way back, and a near miss is a failure rather than something to
# map onto the nearest member. Inventing a severity the model did not
# assert is the specific failure this endpoint exists to avoid.
SEVERITIES = ("low", "medium", "high", "critical")

REQUIRED_KEYS = ("severity", "steps", "contraindication", "spoken_alert")


# None = not tried yet, True = loaded, False = tried and cannot.
# Caching the failure is the same reasoning as language.py's
# _semantic_tier: without it, every request re-attempts a load that has
# already been proven impossible.
_index = None
_chunks = None
_retrieval = None


def retrieval_available():
    """
    Whether the FAISS index is usable: True, False, or None if nothing
    has needed it yet. Exposed for the same reason
    language.semantic_tier_available() is -- so a health report can say
    what is actually live instead of guessing.
    """

    return _retrieval


# ============================================================
# INDEX
# ============================================================

def _ensure_index():
    """
    Load the FAISS index and its metadata sidecar once, on first use.
    Returns (index, chunks, model), or (None, None, None) if retrieval
    cannot run here.

    Lazy for the same reason language.py is lazy: importing this module
    must stay cheap, and a web process that loads a model before
    binding its port fails its platform's health check.

    The manifest's model is checked against language.MODEL_NAME. An
    index built with a different embedding model does not announce
    itself -- the dimensions may even match -- it just returns
    confident nonsense, ranked and sourced. Refusing to use it is the
    only way that failure becomes visible.
    """

    global _index, _chunks, _retrieval

    if _retrieval is False:
        return None, None, None

    if _index is None:

        try:

            import faiss

            index_path = os.path.join(VECTORSTORE_DIR, INDEX_FILENAME)
            metadata_path = os.path.join(VECTORSTORE_DIR, METADATA_FILENAME)

            with open(metadata_path, encoding="utf-8") as handle:
                sidecar = json.load(handle)

            chunks = sidecar["chunks"]
            manifest = sidecar.get("manifest", {})

            built_with = manifest.get("model")

            if built_with != language.MODEL_NAME:
                raise RuntimeError(
                    "index was built with %r but this process embeds with "
                    "%r -- rerun ingest.py"
                    % (built_with, language.MODEL_NAME)
                )

            index = faiss.read_index(index_path)

            if index.ntotal != len(chunks):
                raise RuntimeError(
                    "index has %d vectors but the sidecar describes %d "
                    "chunks -- rerun ingest.py"
                    % (index.ntotal, len(chunks))
                )

            # Shared with detection deliberately: one model, loaded
            # once, whichever subsystem needs it first.
            if language.get_embedding_model() is None:
                raise RuntimeError(
                    "the embedding model could not be loaded on this host"
                )

            _index = index
            _chunks = chunks
            _retrieval = True

        except Exception as e:

            # Bare Exception on purpose, same as language.py: a missing
            # file, an unreadable sidecar, faiss not installed, a host
            # too small for the model and a stale index are different
            # exception types that all mean one thing to a caller --
            # there is no retrieval here -- and none of them should
            # take the process down.
            _retrieval = False
            _index = None
            _chunks = None

            print(
                "[incident] retrieval unavailable (%s: %s) -- responses "
                "will be ungrounded and will report grounded=false"
                % (type(e).__name__, e),
                flush=True,
            )

            return None, None, None

    return _index, _chunks, language.get_embedding_model()


# ============================================================
# RETRIEVAL
# ============================================================

def _select(ranked, substance_code, top_k):
    """
    Choose which of the ranked chunks to actually use.

    Pure similarity is not safe here, and the reason is worth stating.
    Every SDS in the corpus discusses corrosive burns, eye irrigation
    and evacuation in closely related language, so a query about
    sulphuric acid retrieves the caustic soda first-aid section at a
    higher score than the sulphuric acid one -- measured, not
    hypothetical. For a chatbot that is a mediocre answer. For a
    dispatcher it is a confident instruction about the wrong chemical:
    caustic soda calls for an hour of irrigation, sulphuric acid does
    not, and neither should ever be neutralised on tissue.

    So the substance code, which the caller has already told us,
    decides which SDS may fill the SDS slots, and similarity only
    orders what is left. One slot is held for a regulation chunk.

    A code with no matching chunks is expected rather than exceptional
    -- substance_code is open vocabulary and a site will name things
    this corpus has never heard of. That falls back to unfiltered
    similarity, and retrieved_sources then shows the caller exactly
    what was used instead.
    """

    code = (substance_code or "").strip().upper()

    matching = [
        chunk for chunk in ranked
        if (chunk.get("substance_code") or "").strip().upper() == code
    ]

    regulations = [
        chunk for chunk in ranked if chunk.get("doc_type") == "regulation"
    ]

    selected = []
    seen = set()

    def add(chunk):

        if len(selected) >= top_k or chunk["chunk_id"] in seen:
            return

        seen.add(chunk["chunk_id"])
        selected.append(chunk)

    if matching:

        # Always leave at least one SDS slot, however small top_k is.
        sds_slots = max(1, top_k - REGULATION_SLOTS)

        for chunk in matching[:sds_slots]:
            add(chunk)

        for chunk in regulations[:REGULATION_SLOTS]:
            add(chunk)

    # Backfill, in similarity order. Covers both the unknown-substance
    # case (nothing added yet, so this is a plain top-k) and the case
    # where the matching substance had fewer chunks than its slots.
    for chunk in ranked:
        add(chunk)

    return selected


def retrieve(substance_code, incident_type, top_k=DEFAULT_TOP_K):
    """
    Top-k corpus chunks for this incident. Returns [] when retrieval is
    unavailable, which the caller must report rather than paper over.
    """

    index, chunks, model = _ensure_index()

    if index is None or model is None:
        return []

    import numpy

    query = "%s%s %s" % (QUERY_PREFIX, substance_code, incident_type)

    vector = numpy.asarray(
        model.encode([query], normalize_embeddings=True),
        dtype="float32",
    )

    # Rank the whole corpus rather than asking FAISS for top_k
    # directly: the selection policy below filters by substance, and a
    # pre-truncated list can easily contain none of the right one. At
    # this corpus size an exhaustive search is microseconds.
    scores, ids = index.search(vector, index.ntotal)

    ranked = [chunks[i] for i in ids[0] if i != -1]

    return _select(ranked, substance_code, top_k)


# ============================================================
# PROMPT
# ============================================================

def _format_excerpts(chunks):

    blocks = []

    for position, chunk in enumerate(chunks, start=1):

        blocks.append(
            "[%d] %s -- %s\n%s"
            % (
                position,
                chunk["source_file"],
                chunk["section_title"],
                chunk["text"],
            )
        )

    return "\n\n".join(blocks)


def _build_prompt(bay_id, substance_code, incident_type, chunks):
    """
    A JSON-only instruction, grounded in the retrieved excerpts.

    Every rule here is load-bearing. A general model asked about a
    chemical incident will otherwise answer from its own training --
    fluently, plausibly, and without any way for the caller to tell
    which parts came from the site's own documents and which were
    recalled from somewhere else. The excerpts are the whole point:
    they are what makes the answer auditable back to a source file.

    The steps are capped and ordered because this is read aloud to
    someone standing in front of the incident. A twelve-step procedure
    is not a better answer than a five-step one; it is an answer nobody
    finishes listening to.
    """

    if chunks:
        grounding = (
            "Reference excerpts from the site's safety documents:\n\n"
            "%s\n\n" % _format_excerpts(chunks)
        )

        sourcing_rule = (
            "- Base every instruction ONLY on the excerpts above. Do not "
            "add hazard information, procedures or contraindications "
            "from your own knowledge, even if you are confident they are "
            "correct.\n"
            "- If the excerpts do not cover something the responder "
            "would need, say so in a step rather than filling the gap.\n"
        )

    else:
        # Ungrounded. Say so plainly rather than letting the model
        # believe excerpts were omitted by accident.
        grounding = (
            "No reference excerpts are available for this incident -- "
            "the document retrieval system could not be reached.\n\n"
        )

        sourcing_rule = (
            "- No site documents are available, so give only "
            "conservative, general-purpose emergency guidance, and keep "
            "it cautious.\n"
        )

    return (
        "You are the reasoning core of an industrial safety dispatch "
        "system. A hazard has been detected in a chemical handling bay "
        "and a response must be issued immediately.\n\n"
        "Incident:\n"
        "- Bay: %s\n"
        "- Substance code: %s\n"
        "- Incident type: %s\n\n"
        "%s"
        "Return ONLY a single JSON object with exactly these four keys:\n\n"
        "{\n"
        '  "severity": one of "low", "medium", "high", "critical",\n'
        '  "steps": an array of 3 to 6 short imperative instructions,\n'
        '  "contraindication": one short sentence naming the single most '
        "dangerous thing NOT to do,\n"
        '  "spoken_alert": one sentence to be read aloud over a public '
        "address system\n"
        "}\n\n"
        "Rules:\n"
        "%s"
        '- "severity" must be exactly one of the four words listed. Not '
        '"moderate", not "severe", not a phrase.\n'
        '- "steps" must be ordered by what to do first. One action per '
        "step, short enough to follow while standing in front of the "
        "hazard.\n"
        '- "contraindication" is the single most dangerous mistake a '
        "responder could make here -- the thing that turns an incident "
        "into a casualty. Draw it from the excerpts.\n"
        '- "spoken_alert" must be in English, under 25 words, '
        "imperative, and readable aloud: no markdown, no abbreviations "
        "that do not read well, no chemical formulae spelled as symbols.\n"
        "- Output the JSON object and nothing else. No markdown fences, "
        "no commentary before or after it.\n"
        % (bay_id, substance_code, incident_type, grounding, sourcing_rule)
    )


# ============================================================
# VALIDATION
# ============================================================

def _validate(payload):
    """
    Check the model's answer against the shape that was asked for, and
    raise ValueError if it does not match.

    Deliberately strict about severity. Whitespace and letter case are
    normalised, because "Medium " and "medium" are the same answer
    written untidily. Anything else -- "moderate", "severe", "high-ish"
    -- is a different answer, and mapping it onto the nearest member of
    the enum would mean this system asserting a hazard rating that no
    model actually produced, at the exact moment the model has
    demonstrated it is not following the contract. A 502 is a failure
    the caller can see and retry. A coerced severity is a wrong answer
    wearing the costume of a right one.
    """

    missing = [key for key in REQUIRED_KEYS if key not in payload]

    if missing:
        raise ValueError(
            "model response is missing required key(s): %s"
            % ", ".join(missing)
        )

    severity = payload["severity"]

    if not isinstance(severity, str):
        raise ValueError(
            "severity must be a string, got %s" % type(severity).__name__
        )

    normalized = severity.strip().lower()

    if normalized not in SEVERITIES:
        raise ValueError(
            "severity was %r; expected exactly one of %s"
            % (severity, ", ".join(SEVERITIES))
        )

    steps = payload["steps"]

    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty array")

    cleaned_steps = []

    for step in steps:

        if not isinstance(step, str) or not step.strip():
            raise ValueError("every step must be a non-empty string")

        cleaned_steps.append(step.strip())

    text_fields = {}

    for key in ("contraindication", "spoken_alert"):

        value = payload[key]

        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s must be a non-empty string" % key)

        text_fields[key] = value.strip()

    return {
        "severity": normalized,
        "steps": cleaned_steps,
        "contraindication": text_fields["contraindication"],
        "spoken_alert": text_fields["spoken_alert"],
    }


# ============================================================
# ASSESSMENT
# ============================================================

def assess(bay_id, substance_code, incident_type, target_lang, top_k=DEFAULT_TOP_K):
    """
    Full pipeline: retrieve, generate, validate, localize.

    Returns a dict with severity, steps, contraindication,
    spoken_alert, spoken_alert_translated, grounded and
    retrieved_sources. Raises RuntimeError when no provider is
    configured, ValueError when the model would not produce a usable
    answer, and whatever requests raises on a transport failure --
    api.py maps those onto status codes.
    """

    chunks = retrieve(substance_code, incident_type, top_k)

    prompt = _build_prompt(bay_id, substance_code, incident_type, chunks)

    assessment = _validate(generate_structured_response(prompt))

    spoken_alert, translated = _localize(assessment["spoken_alert"], target_lang)

    # Sorted and de-duplicated: this is provenance shown to a human,
    # and the same file appearing three times because three of its
    # sections were retrieved tells them nothing extra.
    sources = sorted({chunk["source_file"] for chunk in chunks})

    return {
        "severity": assessment["severity"],
        "steps": assessment["steps"],
        "contraindication": assessment["contraindication"],
        "spoken_alert": spoken_alert,
        "spoken_alert_translated": translated,
        "grounded": bool(chunks),
        "retrieved_sources": sources,
    }


def _localize(spoken_alert, target_lang):
    """
    Translate the spoken alert, or return the English one and say so.

    Returns (text, translated). No language detection first: the prompt
    asked for English and there is nothing to gain from paying a
    detection round trip -- which on a cold process means loading the
    embedding model -- to confirm what we asked for.

    A failed translation is not an error. translation.translate()
    returns None when there is no provider or the call failed, and the
    honest response is the English text with translated=False. The one
    thing that must never happen is returning English while claiming it
    was translated.
    """

    if (target_lang or "").strip().lower() == "en":
        return spoken_alert, False

    translated = translation.translate(spoken_alert, target_lang, "en")

    if not translated:
        return spoken_alert, False

    return translated, True

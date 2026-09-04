"""
Text translation between languages, via whatever LLM provider is up.

Rewritten from Athena's translation.py to be domain-neutral. The
original was hard-wired to one situation -- a counsellor reading a
distress report filed with a helpline -- and said so in every prompt.
Dropped into an arbitrary project those prompts do not merely read
oddly, they steer the model: telling it the author is "in crisis"
changes the register it translates into.

What survived the rewrite is the part that was never domain-specific:
the discipline. Translate literally, add nothing, invent nothing, and
keep identifiers byte-exact. Those rules matter in a support context
because a softened translation hides severity; they matter in every
other context because a translation that quietly improves its source
is not a translation.

    translate(text, target_language, source_language=None) -> str | None
    translate_to_english(text, source_language=None)       -> str | None

`None` always means "no translation available" and never means "the
translation is the empty string". Callers must fall back to showing the
original, never to showing an untranslated string labelled as
translated.

Never raises. A failed translation costs a convenience; it must not be
able to take down the page that asked for it.
"""

from llm import generate_response


# Text already in the target language needs no translation. Kept as a
# set so the check reads identically at both call sites.
SKIP_LANGUAGES = {"en", "eng", "english"}

LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "te": "Telugu",
    "ur": "Urdu",
    "bn": "Bengali",
}


def _language_name(code):
    """
    Human-readable name for a language code, falling back to the code
    itself. The fallback is deliberate: an unknown code should still
    produce a usable prompt ("translate into pt-BR") rather than an
    error, because the model handles far more languages than this
    module has names for.
    """

    normalized = (code or "").strip().lower()

    return LANGUAGE_NAMES.get(normalized, code)


def _build_prompt(text, target_language, source_language=None):
    """
    A deliberately narrow instruction: translate, and do nothing else.

    Every rule here exists because a general-purpose model will
    otherwise do something helpful and wrong -- summarise a long
    passage, soften language it reads as distressing, resolve an
    ambiguity by picking the likelier reading, or localise a reference
    number into the target script. Each of those silently destroys
    information the caller needed.
    """

    target_name = _language_name(target_language)

    source_hint = (
        f"The source text is written in {_language_name(source_language)}. "
        if source_language else ""
    )

    return (
        f"Translate the text below into {target_name}.\n\n"
        f"{source_hint}"
        "Rules:\n"
        f"- Write the translation in {target_name}, using the script "
        "normally used for that language.\n"
        "- Translate as literally as the target language allows. Keep "
        "the author's own tone, register, hesitation and word choice.\n"
        "- Do NOT soften, summarise, censor, expand or tidy the "
        "content. Do not improve it.\n"
        "- Do NOT add commentary, interpretation, notes, greetings, "
        "closings, warnings or advice.\n"
        "- Do NOT guess at anything the text does not say. If part of "
        "it is unclear or unreadable, render that part as [unclear] "
        "rather than inventing a plausible reading.\n"
        "- Keep numbers, dates, reference IDs, URLs, code, and proper "
        "names exactly as written, in their original characters.\n"
        "- Output the translation and nothing else.\n\n"
        "Text:\n"
        f"{text}"
    )


def translate(text, target_language, source_language=None):
    """
    Translate `text` into `target_language`. Returns the translation,
    or None if there is nothing to do or the attempt failed.

    Returns None -- rather than the original text -- when the source is
    already in the target language, so a caller can always distinguish
    "translated" from "no translation needed" and label its output
    honestly.
    """

    if not text or not text.strip():
        return None

    normalized_target = (target_language or "").strip().lower()

    if not normalized_target:
        return None

    if source_language and source_language.strip().lower() == normalized_target:
        return None

    try:
        translated = generate_response(
            _build_prompt(text, target_language, source_language)
        )

    except Exception as e:
        # Bare Exception on purpose: provider outages, rate limits,
        # timeouts and malformed responses all mean the same thing to a
        # caller -- no translation -- and none of them should propagate.
        print(
            "[translation] failed (%s -> %s): %s: %s"
            % (source_language or "auto", normalized_target, type(e).__name__, e),
            flush=True,
        )
        return None

    if not translated or not translated.strip():
        return None

    return translated.strip()


def translate_to_english(text, source_language=None):
    """
    Convenience wrapper for the common case. Returns None when the text
    is already English, so callers can skip the round trip entirely.
    """

    if source_language and source_language.strip().lower() in SKIP_LANGUAGES:
        return None

    return translate(text, "en", source_language)

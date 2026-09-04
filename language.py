"""
Multilingual language and script detection.

Extracted from Athena (SIH26093, the assessment module for India's
National Helpline Against Atrocities) as a domain-independent piece: it
knows about languages and scripts, and nothing about what the text is
reporting.

Two public functions:

    detect_language(text)          -> "en" | "hi" | "te" | "ur" | "bn"
    detect_script(text, language)  -> "native" | "romanized" | "latin"

Detection runs in two tiers, cheapest first:

  1. Script detection by Unicode range. Free, instant, needs no model,
     and decisive for anything written in Devanagari, Telugu,
     Perso-Arabic or Bengali script. Most real traffic stops here.

  2. Semantic similarity against per-language example anchors, for
     Latin-script text only -- where the real question is "English vs
     romanized Hindi vs romanized Telugu" and script tells you nothing.

The embedding model behind tier 2 loads LAZILY, on the first piece of
Latin-script text that actually needs it, not at import. Athena itself
loads at import (embedding_model.py) because its pipeline needs the
model for other work anyway; here it matters, because importing this
module has to stay cheap. Measured: the model costs ~735 MB of RSS and
~21s to load warm. A web process that pays that before binding its port
fails its platform's health check and gets killed on a small host.
Lazily, the process is answering requests in milliseconds and only the
first romanized request waits.

See EVAL.md for the measured latency and memory numbers, and for the
four known detection edge cases and how each is handled.
"""

MODEL_NAME = "intfloat/multilingual-e5-small"

# How far a non-English score must lead English before the semantic
# tier is allowed to override the English default. Ambiguous, short or
# off-topic Latin text weakly resembles every example set, so a bare
# argmax over the scores picks a confident wrong answer surprisingly
# often. This margin is the guard against that.
NEUTRAL_MARGIN = 0.04

_model = None
_language_embeddings = None

# None = not tried yet, True = loaded, False = tried and cannot.
# Caching the failure matters: without it every single Latin-script
# request re-attempts a load that has already been proven impossible,
# and a module that degrades turns into a module that hangs.
_semantic_tier = None


def semantic_tier_available():
    """
    Whether tier 2 is usable: True, False, or None if nothing has
    needed it yet. Exposed so a /health endpoint can report which
    tiers are actually live rather than guessing.
    """

    return _semantic_tier


def _ensure_model():
    """
    Load the embedding model and pre-encode the language anchors once,
    on first use. Returns (model, language_embeddings), or (None, None)
    if the semantic tier cannot run here.

    (None, None) is a supported deployment, not an error. The model
    needs ~735 MB of RAM and sentence-transformers pulls in torch; a
    small free-tier host has neither. Rather than refuse to start,
    this module runs script-detection-only there -- which still
    handles every non-Latin script correctly -- and returns "en" for
    Latin-script text instead of guessing between English and
    romanized Hindi. Callers can check semantic_tier_available() and
    say so in their own response.

    Deliberately not locked: two racing callers would both build the
    embeddings and one would win harmlessly, which is cheaper than
    holding a lock on every detection for the life of the process.
    """

    global _model, _language_embeddings, _semantic_tier

    if _semantic_tier is False:
        return None, None

    if _model is None:

        try:

            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(MODEL_NAME)

            _language_embeddings = {
                language: _model.encode(
                    ["query: " + example for example in examples],
                    normalize_embeddings=True,
                )
                for language, examples in LANGUAGE_EXAMPLES.items()
            }

            _semantic_tier = True

        except Exception as e:

            # Catching bare Exception on purpose. The ways this fails
            # are not one class: ImportError when torch was left out of
            # the install, OSError/MemoryError when the box is too
            # small, and connection errors when the weights are not
            # cached and the host has no outbound network. All of them
            # mean the same thing to a caller -- tier 2 is not
            # available here -- and none of them should take the
            # process down.
            _semantic_tier = False
            _model = None
            _language_embeddings = None

            print(
                "[language] semantic tier unavailable "
                "(%s: %s) -- running script detection only; "
                "Latin-script text will return 'en'"
                % (type(e).__name__, e),
                flush=True,
            )

            return None, None

    return _model, _language_embeddings


def warm_up():
    """
    Force the lazy load now instead of on the first user request.

    Opt-in, for callers who would rather pay the cost at boot than make
    one unlucky user wait for it. Run it in a thread from a startup hook
    so it does not block the port from opening -- calling it inline at
    import would undo the entire point of loading lazily.

    Returns True if the semantic tier came up, False if this host can
    only do script detection.
    """

    model, _ = _ensure_model()

    return model is not None


# ==========================================================
# SUPPORTED LANGUAGES
# ==========================================================

SUPPORTED_LANGUAGES = {
    "en": "English",
    "hi": "Hindi",
    "te": "Telugu",
    "ur": "Urdu",
    "bn": "Bengali",
}


# ==========================================================
# LANGUAGE ANCHORS FOR THE SEMANTIC TIER
# ==========================================================

LANGUAGE_EXAMPLES = {
    "en": [
        "This is an English incident report about a woman facing violence.",
        "I am in danger and I need help.",
        "Someone is threatening me.",
    ],

    "hi": [
        "यह हिंदी में महिला हिंसा से संबंधित घटना की रिपोर्ट है।",
        "मैं खतरे में हूँ और मुझे मदद चाहिए।",
        "कोई मुझे धमकी दे रहा है।",

        # Romanized Hindi
        "Yeh Hindi mein mahila hinsa se sambandhit ghatna ki report hai.",
        "Main khatre mein hoon aur mujhe madad chahiye.",
        "Koi mujhe dhamki de raha hai.",

        # Wider romanized coverage. The three anchors above were all
        # short and structurally alike, so a longer real sentence with
        # different vocabulary could land closer to the Telugu bank than
        # to these. Found live 2026-09-02: "Mujhe meri jaati ke naam par
        # dhamki di ja rahi hai aur ghar ke bahar bheed khadi hai" --
        # unambiguous romanized Hindi -- was detected as Telugu and
        # answered in Telugu.
        #
        # These lean on the function words that actually separate
        # romanized Hindi from romanized Telugu (hai/hain, raha-rahi-rahe,
        # mujhe/meri, ke-ki-ka, aur, nahi) rather than on topic words,
        # which the two share freely.
        "Mujhe meri jaati ke naam par dhamki di ja rahi hai.",
        "Ghar ke bahar bheed khadi hai aur woh log andar aane ki koshish kar rahe hain.",
        "Mere pati mujhe roz maarte hain aur main kuch nahi kar sakti.",
        "Woh log mujhe school jaane nahi de rahe hain.",
        "Mujhe samajh nahi aa raha hai ki main kya karoon, bahut dar lag raha hai.",
        "Mere saath jo hua hai uske baare mein main kisi se baat nahi kar payi.",
    ],

    "te": [
        "ఇది మహిళపై హింసకు సంబంధించిన తెలుగు ఘటన నివేదిక.",
        "నేను ప్రమాదంలో ఉన్నాను మరియు నాకు సహాయం కావాలి.",
        "ఎవరో నన్ను బెదిరిస్తున్నారు.",

        # Romanized Telugu
        "Idi mahilapai hinsaku sambandhinchina Telugu ghatana nivedika.",
        "Nenu pramadamlo unnanu mariyu naaku sahayam kavali.",
        "Evaro nannu bediristunnaru.",

        # Widened alongside the Hindi bank above, and for the same
        # reason -- adding anchors to only one side would just move the
        # boundary rather than sharpen it, trading Hindi misses for
        # Telugu ones. These lean on the endings that actually mark
        # romanized Telugu (undi, unnanu, unnaru, unnaru, naaku, naa,
        # mariyu, ledu, cheyyandi) rather than on shared topic words.
        "Naa kulam peruto nannu bedirinchutunnaru.",
        "Maa intiki bayata janalu gumigudi unnaru.",
        "Naa bharta rojoo nannu kodutunnadu, nenu emi cheyaleka unnanu.",
        "Vaallu nannu badiki vellanivvatam ledu.",
        "Naaku em cheyalo ardham kavatam ledu, chala bhayam ga undi.",
        "Naaku jarigina daani gurinchi nenu evvarito matladalekapoyanu.",
    ],

    "ur": [
        "یہ ایک اردو رپورٹ ہے جو ایک عورت پر تشدد کے بارے میں ہے۔",
        "میں خطرے میں ہوں اور مجھے مدد چاہیے۔",
        "کوئی مجھے دھمکی دے رہا ہے۔",

        # Romanized Urdu -- linguistically very close to romanized
        # Hindi (same spoken Hindustani base), which is exactly why
        # this LANGUAGE_EXAMPLES bank matters for it: without its own
        # anchors here, romanized Urdu text would very plausibly get
        # semantically pulled toward "hi" instead by the fallback
        # below. Native-script Urdu never reaches this fallback at all
        # (see detect_language()'s script-count branch above) -- these
        # anchors are purely for Latin-script Urdu typing.
        "Yeh Urdu mein aik report hai jo aurat par tashaddud ke baare mein hai.",
        "Main khatre mein hoon aur mujhe madad chahiye.",
        "Koi mujhe dhamki de raha hai.",
    ],

    "bn": [
        "এটি একজন নারীর উপর সহিংসতা সম্পর্কিত একটি বাংলা প্রতিবেদন।",
        "আমি বিপদে আছি এবং আমার সাহায্য দরকার।",
        "কেউ আমাকে হুমকি দিচ্ছে।",

        # Romanized Bengali (Banglish) -- native-script Bengali never
        # reaches this fallback (own script-count branch above); these
        # anchors are for Latin-script Bengali typing.
        "Eta ekjon narir upor sohingshotar bapare ekti bangla report.",
        "Ami bipode achi ebong amar sahajyo dorkar.",
        "Keu amake humki dicche.",
    ],
}


# ==========================================================
# DETECTION
# ==========================================================

def detect_language(text):
    """
    Detect whether the incident is English, Hindi, or Telugu.

    Native scripts are detected directly.
    Romanized Hindi/Telugu and English use multilingual
    semantic similarity as a fallback.
    """

    if not text or not text.strip():
        return "en"

    # --------------------------------------------------------
    # Count characters belonging to each script
    # --------------------------------------------------------

    devanagari_count = 0
    telugu_count = 0
    urdu_count = 0
    bengali_count = 0
    other_script_count = 0

    for char in text:

        code = ord(char)

        # Devanagari: U+0900 - U+097F, excluding U+0964/U+0965 (danda /
        # double danda). The danda is pan-Brahmic sentence-ending
        # punctuation reused by Bengali (and Gujarati, Gurmukhi, Odia)
        # -- counting it as "Devanagari" meant a pure-Bengali sentence
        # like "...দরকার।" registered one Devanagari character from its
        # own full stop and got returned as "hi" before bengali_count
        # was ever consulted, below. Found live 2026-08-29 testing
        # detect_language() against real Bengali sentences.
        if 0x0900 <= code <= 0x097F and code not in (0x0964, 0x0965):
            devanagari_count += 1

        # Telugu: U+0C00 - U+0C7F
        elif 0x0C00 <= code <= 0x0C7F:
            telugu_count += 1

        # Urdu (Perso-Arabic script): U+0600 - U+06FF (Arabic block,
        # which Urdu is written with) plus U+0750-U+077F (Arabic
        # Supplement) and U+FB50-U+FDFF/U+FE70-U+FEFF (Arabic
        # Presentation Forms), both used by Urdu-specific letterforms
        # (e.g. ں, ے) that don't appear in the base block. Known,
        # accepted limitation: standard Arabic text uses the same
        # codepoints and would also land here as "ur" -- same tradeoff
        # already made for Tamil/Kannada/etc. below (a script this
        # project doesn't have real support for shouldn't fall through
        # to the semantic fallback and get a confidently wrong native-
        # script label), and Arabic-script input from an Indian
        # national helpline's reporters is overwhelmingly more likely
        # to be Urdu than Arabic.
        elif (
            0x0600 <= code <= 0x06FF
            or 0x0750 <= code <= 0x077F
            or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF
        ):
            urdu_count += 1

        # Bengali: U+0980 - U+09FF
        elif 0x0980 <= code <= 0x09FF:
            bengali_count += 1

        # Any other non-Latin script this project doesn't support
        # (Tamil, Kannada, Malayalam, Gurmukhi, Gujarati, Odia, etc.)
        # -- see the guard below for why this is checked separately
        # from the semantic fallback.
        elif (
            0x0B80 <= code <= 0x0BFF  # Tamil
            or 0x0C80 <= code <= 0x0CFF  # Kannada
            or 0x0D00 <= code <= 0x0D7F  # Malayalam
            or 0x0A00 <= code <= 0x0A7F  # Gurmukhi (Punjabi)
            or 0x0A80 <= code <= 0x0AFF  # Gujarati
            or 0x0B00 <= code <= 0x0B7F  # Odia
        ):
            other_script_count += 1

    # --------------------------------------------------------
    # Strong native-script detection
    # --------------------------------------------------------

    if devanagari_count > 0:
        return "hi"

    if telugu_count > 0:
        return "te"

    if urdu_count > 0:
        return "ur"

    if bengali_count > 0:
        return "bn"

    # A script this project definitively does not support (confirmed
    # by real character ranges, not a guess) must never reach the
    # semantic fallback below -- that fallback measures "which
    # language's distress-anchors sound semantically similar to this
    # text," not "what script is this," and those are different
    # questions. Live testing 2026-08-22 found it answers the first
    # question in a way that's actively misleading for the second:
    # native Tamil script text was confidently (100%) misdetected as
    # Hindi and the user got a romanized-Hindi response to Tamil
    # input -- a specific wrong language, not a safe default. Any
    # confirmed non-Latin/non-Devanagari/non-Telugu script skips
    # straight to the safe "en" default instead.
    if other_script_count > 0:
        return "en"

    # --------------------------------------------------------
    # Fallback to multilingual semantic detection -- only reachable
    # for Latin-script text now, where its actual job (English vs.
    # romanized Hindi vs. romanized Telugu) is well-posed.
    # --------------------------------------------------------

    model, language_embeddings = _ensure_model()

    # Script detection already ruled out every non-Latin script above,
    # so without the semantic tier there is nothing left to distinguish
    # English from romanized Hindi/Telugu. "en" is the safe answer: it
    # is the most common case, and being wrong means replying in
    # English to someone who wrote romanized Hindi -- recoverable.
    # Guessing wrong the other way sends Devanagari to an English
    # speaker.
    if model is None:
        return "en"

    query_embedding = model.encode(
        ["query: " + text],
        normalize_embeddings=True
    )

    scores = {}

    for language, embeddings in language_embeddings.items():

        similarities = query_embedding @ embeddings.T

        scores[language] = float(similarities.max())

    best_language = max(scores, key=scores.get)

    # Ambiguous/short/off-topic text can weakly resemble the hi/te
    # example sets too. Only override the English default when a
    # non-English match clearly leads the English score — otherwise
    # default to English rather than guessing.
    if best_language != "en" and (scores[best_language] - scores["en"]) < NEUTRAL_MARGIN:
        return "en"

    return best_language


def detect_script(text, language):
    """
    Whether the text was written in the language's native script or
    romanized (Latin letters) -- independent of detect_language(),
    so this also works when the caller passes language explicitly
    instead of relying on auto-detection.

    This exists so a caller can reply in the same script the user wrote
    in. Answering romanized input in native Devanagari or Telugu is
    backwards for anyone who speaks the language but only types and
    reads it in Latin letters -- which is most people on a phone
    keyboard.

    DIVERGES FROM ATHENA (deliberate): Athena's copy only knows the
    Devanagari and Telugu ranges, so Urdu and Bengali -- added to
    detect_language() later -- fall through to "latin" even when the
    text is plainly in native script. Caught by the extraction test on
    2026-09-03. Fixed here rather than there because changing it in
    Athena changes which script its live replies come back in, which is
    a product decision, not a refactor.

    Returns "native", "romanized", or "latin" for a language with no
    non-Latin script of its own (English) or one this module does not
    have a range for.
    """

    NATIVE_RANGES = {
        "hi": [(0x0900, 0x097F)],                      # Devanagari
        "te": [(0x0C00, 0x0C7F)],                      # Telugu
        "bn": [(0x0980, 0x09FF)],                      # Bengali
        "ur": [(0x0600, 0x06FF), (0x0750, 0x077F),     # Perso-Arabic,
               (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],    # + Urdu letterforms
    }

    ranges = NATIVE_RANGES.get(language)

    if not ranges:
        return "latin"

    has_native = any(
        low <= ord(char) <= high
        for char in text
        for low, high in ranges
    )

    return "native" if has_native else "romanized"

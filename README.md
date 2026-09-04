# `_reusable/` — domain-independent pieces extracted from Athena

Two modules, lifted out of Athena so they can be dropped into another
project without dragging the atrocity-reporting domain along. Neither
imports anything from Athena. Verified: the only imports in
`language.py` are one lazy `sentence_transformers` inside the loader,
and `llm.py` needs `os`, `requests`, `dotenv`, `google.genai`.

Copy the folder. There is nothing to un-wire.

| File | What it is | Lines |
|---|---|---|
| `language.py` | Language + script detection, 5 languages, two tiers | ~430 |
| `llm.py` | Multi-provider LLM caller with failover | ~236 |
| `translation.py` | Translation between languages, via `llm.py` | ~160 |
| `api.py` | FastAPI layer over the three modules | ~290 |
| `API_CONTRACT.md` | Endpoints, shapes, errors — build against this | — |
| `EVAL.md` | Measured latency, memory, and every known edge case | — |
| `Dockerfile` | Script tier by default; `--build-arg SEMANTIC_TIER=1` for the model | — |

---

## `language.py`

```python
from language import detect_language, detect_script, semantic_tier_available

detect_language("मुझे मदद चाहिए")        # -> "hi"     (< 0.01 ms, no model)
detect_language("Mujhe madad chahiye")   # -> "hi"     (~22 ms, model)
detect_script("मुझे मदद चाहिए", "hi")     # -> "native"
detect_script("Mujhe madad chahiye","hi")# -> "romanized"
```

| Function | Returns |
|---|---|
| `detect_language(text)` | `"en"` \| `"hi"` \| `"te"` \| `"ur"` \| `"bn"` |
| `detect_script(text, language)` | `"native"` \| `"romanized"` \| `"latin"` |
| `semantic_tier_available()` | `True` \| `False` \| `None` (not needed yet) |
| `warm_up()` | Forces the load early; `True` if tier 2 came up |

**Never raises.** Empty input, emoji, digits, unsupported scripts and a
missing model all resolve to a valid language code. Nothing a caller
passes can turn into a 500.

### The one thing to understand before deploying it

The model costs **821 MB of RAM** and **~22 s** to load. It loads
**lazily**, on the first Latin-script input, so:

- importing the module costs 6 ms
- your process binds its port immediately and passes health checks
- native-script detection never touches the model at all
- if the model can't load, the module logs once and keeps serving tier 1

That last point is what makes it deployable on a small box. Install
without `sentence-transformers` and it runs in ~17 MB, correctly
detecting every non-Latin script, returning `"en"` for romanized text.
Check `semantic_tier_available()` and say so in your response rather
than pretending.

Read `EVAL.md` before quoting any capability claim. It has the real
numbers and the honest accuracy limits.

---

## `llm.py`

```python
from llm import generate_response

generate_response("Summarise this in one line: ...")   # -> str
```

Tries Groq → Gemini → OpenRouter, walking several models within each
provider before moving on. Set any subset of `GROQ_API_KEY`,
`GEMINI_API_KEY`, `OPENROUTER_API_KEY`; providers with no key are
skipped, so **one key is enough to run**.

The point is not redundancy for its own sake. All three have free tiers
with different rate limits, and a demo that dies because one provider is
throttling you looks exactly like a demo that is broken.

Measured 2026-09-03: import 1.5 s, live round trip 0.7 s.

---

## `translation.py`

```python
from translation import translate, translate_to_english

translate("I need help", "hi")              # -> "मुझे मदद चाहिए"
translate_to_english("मुझे मदद चाहिए", "hi")  # -> "I need help"
```

Returns `None` -- never a partial or untranslated string -- when there
is nothing to do (empty text, no target, source already in the target
language) or when the provider failed. `None` means *no translation
available*, so a caller can always fall back to the original and label
it honestly. Never raises.

The prompt forbids the model from softening, summarising, adding
commentary, or guessing: anything unreadable comes back as `[unclear]`
rather than a plausible invention, and numbers, dates, reference IDs,
URLs and proper names are preserved in their original characters.

Verified 2026-09-03: `NHAA-2026-27F9A605` and `14566` both survived a
round trip into Devanagari byte-exact. Live translation ~0.6-0.9 s;
every no-op case returns in under a millisecond without an API call.

Rewritten from Athena's version, whose prompts named a helpline
counsellor and a person in crisis in every instruction -- framing that
does not just read oddly in another project, it steers the register the
model translates into. The discipline survived; the domain did not.

---

## Installing

Tier 1 only — script detection, ~17 MB, runs anywhere:

```
# language.py needs nothing at all for native-script detection
pip install -r requirements.txt      # only for llm.py
```

Add the semantic tier (romanized Hindi/Telugu), needs ~1 GB RAM:

```
pip install -r requirements-semantic.txt
```

Split deliberately. `sentence-transformers` pulls in torch, which is
most of a gigabyte before the weights, and a host that cannot afford it
should not be forced to install it to get the other 80% of the
behaviour.

---

## Running it

```
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8000
```

Or in Docker, script tier only (~17 MB, fits a 512 MB free instance):

```
docker build -t multilingual .
docker run -p 8000:8000 --env-file .env multilingual
```

With the semantic tier (romanized Hindi/Telugu, needs ~1 GB):

```
docker build --build-arg SEMANTIC_TIER=1 -t multilingual .
```

Three endpoints: `GET /health`, `POST /detect`, `POST /translate`.
Full shapes, every error status, and the two things a client author
will get wrong are in **`API_CONTRACT.md`** — hand that over, not this
file.

Optional `API_KEY` in the environment gates everything except
`/health` behind an `X-API-Key` header. Unset means open, which is
right for a public demo.

## What is *not* here

No database, no auth beyond the single shared key, no rate limiting,
no per-caller identity. Add those where they belong in the consuming
project rather than here — this stays a library plus a thin HTTP
wrapper, and every one of those decisions depends on the project.

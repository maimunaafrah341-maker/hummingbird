# API contract

Build against this. Everything below was verified by running the
service — `/health`, `/detect` and `/translate` on 2026-09-03,
`/incident` on 2026-09-04 — and the example responses are copied from
real output, not written by hand.

Base URL: whatever you deploy to. Locally, `http://127.0.0.1:8000`.

## Auth

If the `API_KEY` environment variable is set, every endpoint except
`/health` requires a matching `X-API-Key` header, and returns **401**
without it. If `API_KEY` is unset — the default — the API is open.

## Conventions

- All request bodies are JSON. `Content-Type: application/json`.
- Every response carries an `X-Request-ID` header. Quote it when
  reporting a problem; it appears in the server logs on the same line
  as the path and latency.
- `latency_ms` in a response body is *server-side work only*, not
  round-trip time.

---

## `GET /health`

No auth, no body.

```json
{
  "status": "ok",
  "tiers": { "script": true, "semantic": null },
  "retrieval": null,
  "languages": ["bn", "en", "hi", "te", "ur"]
}
```

| Field | Meaning |
|---|---|
| `tiers.script` | Always `true`. Unicode-range detection needs nothing. |
| `tiers.semantic` | `true` loaded, `false` cannot load here, **`null` nothing has needed it yet** |
| `retrieval` | The FAISS index behind `/incident`: `true` loaded, `false` cannot load here, **`null` nothing has needed it yet** |

`null` is not a bug. The embedding model loads lazily, so before the
first Latin-script request there is genuinely no answer — reporting
`true` or `false` would be a guess. Set `WARM_UP=1` if you need
certainty at boot.

`retrieval` follows the same rule and is read the same way. After one
`/incident` request on a healthy deployment:

```json
{
  "status": "ok",
  "tiers": { "script": true, "semantic": true },
  "retrieval": true,
  "languages": ["bn", "en", "hi", "te", "ur"]
}
```

and on a deployment whose index is missing or unreadable, after the
first request has tried it:

```json
{
  "status": "ok",
  "tiers": { "script": true, "semantic": null },
  "retrieval": false,
  "languages": ["bn", "en", "hi", "te", "ur"]
}
```

**`retrieval: false` does not make `/incident` fail** — it still
answers, with `grounded: false`, from the model's general knowledge
rather than the corpus. Reading this field is how you find that out
before an operator does. Note that checking `/health` never *triggers*
a load; it reports what the service has already discovered, so on a
freshly started process the honest answer is `null` until something
has actually asked for an assessment.

---

## `POST /detect`

```json
{ "text": "Mujhe madad chahiye" }
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | 1–5000 characters |

**200:**

```json
{
  "language": "hi",
  "script": "romanized",
  "method": "semantic",
  "semantic_tier_used": true,
  "latency_ms": 24744.29
}
```

| Field | Values |
|---|---|
| `language` | `en` `hi` `te` `ur` `bn` |
| `script` | `native` `romanized` `latin` |
| `method` | `script` (Unicode ranges) or `semantic` (embeddings) |
| `semantic_tier_used` | whether the model actually answered |

Native script is decided without touching the model:

```json
{"language":"hi","script":"native","method":"script",
 "semantic_tier_used":false,"latency_ms":0.0}
```

**That 24744 ms is real and it happens once.** The first Latin-script
request of a process's life pays the model load. Every request after it
is ~25 ms. Native-script requests never pay it at all. If a 25-second
first request is unacceptable, set `WARM_UP=1` and pay it at boot
instead — but read `EVAL.md` first, because on a small host that is
what gets the container killed before it opens its port.

If the semantic tier cannot load, this endpoint still returns 200.
Latin-script text comes back as `"en"` with `semantic_tier_used:
false`, and `/health` reports `semantic: false`. **Check that field
before claiming romanized support to a user.**

---

## `POST /translate`

```json
{
  "text": "I need help. Case NHAA-2026-27F9A605, call 14566.",
  "target_language": "hi",
  "source_language": null
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | 1–5000 characters |
| `target_language` | string | yes | `hi`, `te`, `ur`, `bn`, `en`, or any code the model knows |
| `source_language` | string \| null | no | Omit to let the model infer |

**200, translated:**

```json
{
  "translation": "मुझे मदद चाहिए। केस NHAA-2026-27F9A605, कॉल 14566।",
  "translated": true,
  "reason": null,
  "latency_ms": 944.92
}
```

Note that `NHAA-2026-27F9A605` and `14566` came through byte-exact.
Identifiers, numbers, URLs and proper names are preserved in their
original characters by design.

**200, not translated:**

```json
{"translation": null, "translated": false,
 "reason": "already_in_target_language", "latency_ms": 0.0}
```

| `reason` | Meaning |
|---|---|
| `already_in_target_language` | `source_language` equals `target_language`. Show the original. |
| `translation_unavailable` | No provider reachable. Show the original, offer a retry. |

**A null translation is a 200, not an error.** Both cases are ordinary
outcomes you must handle by showing the original text. Never display an
untranslated string labelled as a translation.

---

## `POST /incident`

Assess a hazard and return the response to carry out. Retrieves the
relevant safety-document excerpts, asks a model for a structured
judgement grounded in them, and localizes the spoken alert.

```json
{
  "bay_id": "BAY-04",
  "substance_code": "CL2",
  "incident_type": "gas leak detected near the pump pit",
  "target_lang": "en"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `bay_id` | string | yes | 1–200 characters. Free text; echoed into the response for context |
| `substance_code` | string | yes | 1–200 characters. **Open vocabulary** — any string. `CL2`, `NH3`, `H2SO4`, `NAOH` have documents in the corpus |
| `incident_type` | string | yes | 1–200 characters. Open vocabulary, e.g. `gas leak`, `acid splash to the eyes` |
| `target_lang` | string | yes | One of `bn` `en` `hi` `te` `ur`. Anything else is a **400** |

**200:**

```json
{
  "severity": "critical",
  "steps": [
    "Raise the alarm and evacuate BAY-04 immediately, moving crosswind first, then upwind.",
    "Account for all personnel by name, ensuring no one is in low-lying areas.",
    "Establish a 100-metre isolation zone, extending downwind, and deny entry to anyone without breathing apparatus.",
    "Trained personnel wearing appropriate PPE should attempt to shut off the supply at the source if safe to do so."
  ],
  "contraindication": "Do not attempt repairs on a pressurised system.",
  "spoken_alert": "Evacuate BAY-04 immediately. Move crosswind, then upwind. Account for all personnel by name.",
  "spoken_alert_translated": false,
  "grounded": true,
  "retrieved_sources": [
    "reg_factories_act_excerpts.md",
    "sds_chlorine.md"
  ],
  "latency_ms": 28549.06
}
```

| Field | Meaning |
|---|---|
| `severity` | `low` `medium` `high` `critical`. Exactly one of these four, always |
| `steps` | Ordered imperative instructions, first action first. Typically 3–6 |
| `contraindication` | The single most dangerous thing **not** to do here |
| `spoken_alert` | One sentence written to be read aloud. Under ~25 words, no markdown |
| `spoken_alert_translated` | Whether `spoken_alert` is actually in `target_lang`. **Read this** |
| `grounded` | Whether the answer was built from retrieved documents. **Read this** |
| `retrieved_sources` | The corpus files the answer was drawn from. Empty when `grounded` is false |
| `latency_ms` | Server-side work only. Dominated by the model call |

### The corpus is illustrative, not real

The documents in `corpus/` are **hand-drafted samples written for
development**. They are not real safety data sheets, they are not
quoted from OSHA or the Factories Act, and the regulatory paraphrases
are simplified to the point of being wrong as law. Every file says so
at the top.

A deployment that handles real incidents must replace the corpus with
the site's actual SDS documents and the applicable regulations, then
rerun `python ingest.py`. Nothing in this API will tell you that you
forgot — the responses look exactly the same either way.

### `grounded: false` means the answer is not sourced

If the FAISS index is missing, unreadable, or was built with a
different embedding model, the endpoint **still returns 200** and still
answers, from the model's general knowledge instead of the corpus:

```json
{
  "severity": "high",
  "steps": [
    "Evacuate all personnel from BAY-04 immediately.",
    "Activate the emergency alarm and notify the safety team.",
    "Close the valves to the affected area if safe to do so.",
    "Do not approach the leak without proper PPE."
  ],
  "contraindication": "Do not attempt to seal the leak without proper respiratory protection.",
  "spoken_alert": "All personnel evacuate BAY-04 immediately due to a chlorine gas leak. Do not re-enter until cleared by safety.",
  "spoken_alert_translated": false,
  "grounded": false,
  "retrieved_sources": [],
  "latency_ms": 10406.9
}
```

This is deliberate — an unreachable corpus is not a reason to leave a
bay with a chlorine leak in it without an answer — but a `grounded:
false` response is **generic advice, not site guidance**, and it should
not be presented to an operator as though it came from the site's
documents. The server logs the reason on the first affected request.

### `spoken_alert_translated` is not decorative

When `target_lang` is not `en`, the alert is translated through the
same provider chain `/translate` uses.

**200, translated** (`target_lang: "hi"`, captured 2026-09-04):

```json
{
  "severity": "critical",
  "steps": [
    "Immediately flush eyes with clean water for at least 30 minutes, holding eyelids apart.",
    "Continue irrigation during transport to medical care.",
    "Do not attempt to neutralize acid in the eyes with any substance.",
    "Check for and remove any contaminated clothing, being careful not to spread the acid."
  ],
  "contraindication": "Do not attempt to neutralize acid in the eyes with any substance.",
  "spoken_alert": "BAY-07 में तत्काल नेत्र धुलाई आवश्यक है। 30 मिनट के लिए पानी से धोएँ। तत्काल चिकित्सा सहायता प्राप्त करें।",
  "spoken_alert_translated": true,
  "grounded": true,
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_sulphuric_acid.md"
  ],
  "latency_ms": 11030.47
}
```

Note that `BAY-07` came through byte-exact, the same way `/translate`
preserves identifiers.

If the translation chain is unavailable, the **English** alert is
returned instead, with `spoken_alert_translated: false`:

```json
{
  "severity": "critical",
  "steps": [
    "Immediately flush affected eyes with clean water for at least 30 minutes.",
    "Continue irrigation during transport to medical care.",
    "Ensure the casualty does not rub their eyes or attempt to neutralize the acid.",
    "Check for and remove any contaminated clothing, footwear, and accessories."
  ],
  "contraindication": "Do not attempt to neutralise acid in the eye with any alkali.",
  "spoken_alert": "Immediate eye wash required for acid exposure in BAY-07. Flush eyes continuously with water.",
  "spoken_alert_translated": false,
  "grounded": true,
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_sulphuric_acid.md"
  ],
  "latency_ms": 41960.89
}
```

That one was captured with the same `target_lang: "hi"` on a
deployment with no `GROQ_API_KEY`/`GEMINI_API_KEY`/`OPENROUTER_API_KEY`
set — the two responses above are the same request against a funded and
an unfunded translation chain. The alert is in English and the flag
says so. **Never feed a
`spoken_alert_translated: false` string to a text-to-speech voice
configured for the target language** — you will get English words read
with Hindi phonemes, which is worse than English read as English.

`steps` and `contraindication` are **always English**, in every case.
Only `spoken_alert` is localized.

### Severity is never guessed

If the model returns a severity outside the four allowed values —
`"moderate"`, `"severe"`, a phrase — the request fails with **502**. It
is not mapped onto the nearest valid value. A dispatcher inventing a
hazard rating that no model actually produced is the failure this
endpoint is built to avoid, and a visible 502 you can retry is the
better outcome. Whitespace and letter case are normalised, because
`"  Critical "` is the same answer written untidily.

---

## Errors

| Status | When | Body |
|---|---|---|
| **400** | `text` empty or whitespace | `{"detail": "text must not be empty"}` |
| **400** | `text` over 5000 chars | `{"detail": "text exceeds 5000 characters (got 6000)"}` |
| **400** | `target_language` empty | `{"detail": "target_language must not be empty"}` |
| **400** | `/incident` field empty | `{"detail": "bay_id must not be empty"}` |
| **400** | `/incident` field over 200 chars | `{"detail": "incident_type exceeds 200 characters (got 300)"}` |
| **400** | `target_lang` not supported | `{"detail": "target_lang must be one of: bn, en, hi, te, ur"}` |
| **401** | `API_KEY` set, header wrong/missing | `{"detail": "invalid or missing X-API-Key"}` |
| **404** | Unknown path | FastAPI default |
| **405** | Wrong method (e.g. `GET /detect`) | FastAPI default |
| **422** | Missing/mistyped field, malformed JSON | FastAPI validation detail |
| **502** | `/incident`: model returned unusable output | `{"detail": "severity was 'moderate'; expected exactly one of low, medium, high, critical"}` |
| **503** | `/incident`: no `FEATHERLESS_API_KEY` | `{"detail": "FEATHERLESS_API_KEY is not set -- structured generation is unavailable."}` |
| **503** | `/incident`: provider unreachable or rate limited | `{"detail": "structured generation provider returned 429"}` |
| **503** | `/incident`: provider sent a malformed body | `{"detail": "structured generation provider unreachable (JSONDecodeError)"}` |
| **500** | Unhandled server error | `{"error": "internal_error", "request_id": "..."}` |

The 502 body above is the message the validator produces, exercised by
`eval_incident.py`; the others were captured over HTTP. **502 and 503
are not caller-triggered** — they mean the model answered unusably or
the provider is unavailable, so retrying the identical request is
reasonable, which is not true of any 4xx above.

Oversized input is **rejected, never truncated** — truncating would
return a language detected from half the input while reporting success.

All of the above were verified by request. 400/401/404/405/422 are the
only statuses user input can produce; nothing a caller sends should be
able to cause a 500.

---

## Notes for whoever builds the client

**Set a timeout above 30 seconds, or warm the service first.** The
default client timeout in most HTTP libraries is shorter than a cold
model load, so the very first romanized request will look like a
network failure when it is actually working.

**Read `semantic_tier_used`, don't assume it.** A deployment without
the model still answers every request successfully — it just answers
`"en"` for anything in Latin script. Silently trusting the language
field there means confidently mislabelling romanized Hindi as English.

**Romanized detection is weaker than native, unevenly.** Native script
is a range check and effectively exact. Romanized Hindi scores ~97,
romanized Telugu ~77.7 on the parent project's eval set. The honest
claim is *five languages in native script, two also in romanized form,
one of those two well* — see `EVAL.md`.

**Budget about 2 minutes for `/incident`, and set a client timeout well
above 90 seconds.** Normal measured latencies on 2026-09-04 are **8–42
seconds**, plus a one-time ~22 s embedding-model load on the first
request of a process. A 30-second client default will time out on
ordinary successful requests.

The model call is bounded at **90 seconds total** by a watchdog, and
that bound covers both the first attempt and the correction retry
together — a slow first attempt shortens the retry rather than doubling
the ceiling. If the budget runs out you get a **503**, not a hang.

That watchdog exists because the underlying timeout does not do what it
looks like it does: the provider call is configured with a 60-second
timeout, and that value bounds *how long the connection may go silent*,
not how long the request may take. Measured 2026-09-04 before the
watchdog, both on that 60-second setting: one request ran **221
seconds** and failed, and an identical one **succeeded at 242
seconds**, against a median under 25. The latency distribution is
bimodal, not a smooth tail.

**One call in the pipeline is still unbounded: the translation of
`spoken_alert`.** The 90-second watchdog covers the model call only.
Localization goes through the same provider chain `/translate` uses,
which has no equivalent bound for exactly the reason above — so a
non-English `/incident` request can exceed 90 seconds by however long
that chain takes, and there is no server-side ceiling on it. This is
invisible on a deployment with no `GROQ_API_KEY`/`GEMINI_API_KEY`/
`OPENROUTER_API_KEY`, because translation then fails instantly and
falls back to English; it becomes live the moment one of those keys is
set. **Apply your own deadline in the kiosk regardless of the
watchdog**, and show the operator something truthful while waiting
rather than blocking on the response.

**`/incident` will 429 under back-to-back calls.** The provider bills
this account by tokens per minute, and an incident prompt is large —
four retrieved excerpts plus instructions is around 2000 tokens before
the model writes anything. Measured: two assessments inside a minute
exhausted the budget and the second came back as `503` with `provider
returned 429`; roughly 45 seconds of spacing was reliable. If the kiosk
can issue assessments faster than that, it needs to queue them or back
off on 503 — this is a quota, so an immediate retry makes it worse.

**`/incident` has no failover; `/translate` does.** Translation walks
Groq → Gemini → OpenRouter and survives any one of them being down.
Structured generation goes to Featherless and nowhere else, by design —
one provider in a known JSON dialect is easier to hold to a strict
output contract than four in four house styles. The tradeoff is that a
Featherless outage takes `/incident` down completely while the rest of
the service keeps working. Degrade the kiosk accordingly rather than
assuming the whole API is down.

**Read `grounded` on every response, not `/health` once at startup.**
`/health` does report the index state as `retrieval`, and it is worth
checking — but it is lazy, so a freshly started process answers `null`
until something has actually asked for an assessment, and a `true` from
five minutes ago is not a promise about this request. `grounded` on the
response you are holding is the only field that describes that
response.

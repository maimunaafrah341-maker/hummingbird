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
  "substance_code": "NAOH",
  "substance_name": "Sodium hydroxide (50% solution)",
  "incident_type": "caustic burn to the forearm",
  "target_lang": "en"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `bay_id` | string | yes | 1–200 characters. Free text; echoed into the response for context |
| `substance_code` | string \| **null** | yes (may be null) | 1–200 characters. The retrieval key. **Null means "could not be mapped to a known code"** — see below. Empty string is a **400**, not a synonym for null |
| `substance_name` | string | yes | 1–200 characters. Human-readable, for display. Echoed back verbatim; never used for filtering |
| `incident_type` | string | yes | 1–200 characters. Open vocabulary, e.g. `gas leak`, `acid splash to the eyes` |
| `target_lang` | string | yes | One of `bn` `en` `hi` `te` `ur`. Anything else is a **400** |

**The two substance fields do different jobs.** `substance_code` selects
which documents are retrieved; `substance_name` is display text that
travels through to the compliance PDF unchanged. Sending a name in the
code field will not break anything, but it will not match a document
either — it will be reported as `substance_unknown` below.

**Null and empty string are not the same.** `null` is the documented way
to say "the detecting side could not map this substance", and it takes
a defined retrieval path. `""` is rejected with a 400, deliberately: if
a caller emits empty strings where it means to emit nulls, that is a bug
worth surfacing rather than silently reinterpreting.

**200:**

```json
{
  "severity": "high",
  "steps": [
    "Flush affected area with large quantities of running water for at least 30 minutes.",
    "Remove all contaminated clothing, footwear, and jewellery while flushing.",
    "Continue flushing until the slippery, soapy feel of the skin has gone.",
    "Seek immediate medical attention."
  ],
  "contraindication": "Do not attempt to neutralise the caustic with acidic solutions.",
  "spoken_alert": "Immediate water rinse required for sodium hydroxide burn in BAY-04. Seek medical help now.",
  "spoken_alert_translated": false,
  "substance_name": "Sodium hydroxide (50% solution)",
  "grounded": true,
  "retrieval_mode": "substance_matched",
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_caustic_soda.md"
  ],
  "latency_ms": 16432.1
}
```

| Field | Meaning |
|---|---|
| `severity` | `low` `medium` `high` `critical`. Exactly one of these four, always |
| `steps` | Ordered imperative instructions, first action first. Typically 3–6 |
| `contraindication` | The single most dangerous thing **not** to do here |
| `spoken_alert` | One sentence written to be read aloud. Under ~25 words, no markdown |
| `spoken_alert_translated` | Whether `spoken_alert` is actually in `target_lang`. **Read this** |
| `substance_name` | Echoed back exactly as sent, for display and for the compliance PDF |
| `grounded` | Whether the answer was built from retrieved documents. **Read this** |
| `retrieval_mode` | How the corpus was searched, and how far to trust the sources. **Read this** |
| `retrieved_sources` | The corpus files the answer was drawn from. Empty when `grounded` is false |
| `latency_ms` | Server-side work only. Dominated by the model call |

### `retrieval_mode`: how much the sources are worth

Four values. Three of them produce a grounded answer, and they are
**not equally trustworthy**.

| Value | Meaning | `grounded` |
|---|---|---|
| `substance_matched` | A code was given and the corpus has documents for it. The SDS excerpts are about the substance you named. | `true` |
| `substance_unknown` | A code was given and the corpus has **nothing** under it. Fell back to unfiltered semantic search. | `true` |
| `substance_unmapped` | `substance_code` was `null`. Same fallback, different cause. | `true` |
| `unavailable` | No index. Answer comes from the model's general knowledge. | `false` |

`grounded` is exactly `retrieval_mode != "unavailable"`. It is kept as a
separate boolean so existing callers do not break; new callers should
read `retrieval_mode`, which distinguishes cases `grounded` cannot.

**`substance_unknown` is the one that will hurt you.** The response looks
completely normal — 200, `grounded: true`, real sources, confident
steps — and the safety data is about a *different chemical*. This is a
real captured response for `substance_code: "TOLUENE"`, which the
corpus has no sheet for:

```json
{
  "severity": "high",
  "steps": [
    "Evacuate the immediate area and isolate it.",
    "Wear alkali-resistant suit, gloves, boots, and full face protection.",
    "Contain the spill with dry inert material — dry sand, earth, or a proprietary absorbent.",
    "Prevent entry to drains, sewers, and watercourses."
  ],
  "contraindication": "Do not wash a bulk solid spill down with water as the first action.",
  "spoken_alert": "Evacuate the chemical handling bay immediately and report to the muster point.",
  "spoken_alert_translated": false,
  "substance_name": "Toluene",
  "grounded": true,
  "retrieval_mode": "substance_unknown",
  "retrieved_sources": [
    "sds_ammonia.md",
    "sds_caustic_soda.md",
    "sds_chlorine.md",
    "sds_sulphuric_acid.md"
  ],
  "latency_ms": 9777.17
}
```

Toluene is a flammable liquid. That answer prescribes **alkali-resistant
PPE** and warns about a **bulk solid spill** — it is caustic soda
procedure, retrieved because caustic soda was the nearest thing in the
corpus, and it says nothing about ignition sources or vapour. Nothing in
the body flags this except `retrieval_mode`. **Do not present
`substance_unknown` output as substance-specific guidance.**

`substance_unmapped` carries the same caveat with a different cause —
nobody claimed a code and got it wrong, the claim was never made:

```json
{
  "severity": "high",
  "steps": [
    "Raise the alarm and evacuate crosswind then upwind.",
    "Account for personnel by name.",
    "Isolate the area for at least 100 metres in all directions.",
    "Do not enter the area to attempt identification or containment."
  ],
  "contraindication": "Do not allow water to come into contact with the unidentified substance.",
  "spoken_alert": "Evacuate crosswind and upwind immediately. Account for all personnel. Stay clear of the chemical handling bay.",
  "spoken_alert_translated": false,
  "substance_name": "Unidentified white crystalline solid",
  "grounded": true,
  "retrieval_mode": "substance_unmapped",
  "retrieved_sources": [
    "sds_ammonia.md",
    "sds_caustic_soda.md",
    "sds_chlorine.md"
  ],
  "latency_ms": 7672.07
}
```

### Which codes the corpus can actually ground

The detecting side currently knows eleven codes. **This corpus has
documents for four of them.**

| Grounded (`substance_matched`) | No documents (`substance_unknown`) |
|---|---|
| `CL2` `NH3` `H2SO4` `NAOH` | `HCL` `ACETONE` `TOLUENE` `METHANOL` `LPG` `DIESEL` `PETROL` |

Note the shape of that gap: six of the seven unsupported codes are
**flammables**, and all four supported sheets are toxics and
corrosives. A petrol or LPG incident will retrieve eye-irrigation and
evacuation procedure, because that is the closest thing present.

This is a property of the sample corpus, not a defect to be worked
around in a client. It resolves when real source documents replace the
illustrative set — until then, seven of eleven codes retrieve by
unfiltered fallback and say so in `retrieval_mode`.

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
  "severity": "critical",
  "steps": [
    "Evacuate all personnel from BAY-04 immediately.",
    "Seal off BAY-04 and adjacent areas to prevent gas spread.",
    "Notify emergency services and the on-site HSE team.",
    "Activate the emergency ventilation system if safe to do so."
  ],
  "contraindication": "Do not attempt to manually stop the leak without proper protective equipment.",
  "spoken_alert": "All personnel evacuate BAY-04 immediately due to chlorine gas leak. Seal off area and call emergency services now.",
  "spoken_alert_translated": false,
  "substance_name": "Chlorine gas",
  "grounded": false,
  "retrieval_mode": "unavailable",
  "retrieved_sources": [],
  "latency_ms": 8976.44
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
    "Immediately flush affected eye with large quantities of clean water for at least 30 minutes.",
    "Hold eyelids apart to ensure thorough irrigation.",
    "Continue irrigation during transport to medical care.",
    "Remove contaminated clothing, footwear, and watch straps if safe to do so."
  ],
  "contraindication": "Do not attempt to neutralise acid in the eye with any alkali.",
  "spoken_alert": "BAY-07 में तुरंत आँख धोना आवश्यक है। 30 मिनट तक पानी से धोएँ। अभी चिकित्सा सहायता प्राप्त करें।",
  "spoken_alert_translated": true,
  "substance_name": "Sulphuric acid (98%)",
  "grounded": true,
  "retrieval_mode": "substance_matched",
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_sulphuric_acid.md"
  ],
  "latency_ms": 8337.98
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
    "Hold eyelids apart during irrigation to ensure thorough flushing.",
    "Continue irrigation during transport to medical care.",
    "Remove contaminated clothing, footwear, and accessories after starting irrigation."
  ],
  "contraindication": "Do not attempt to neutralize the acid with any alkali.",
  "spoken_alert": "Immediate eye irrigation required in BAY-07 for acid exposure. Seek medical help now.",
  "spoken_alert_translated": false,
  "substance_name": "Sulphuric acid (98%)",
  "grounded": true,
  "retrieval_mode": "substance_matched",
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_sulphuric_acid.md"
  ],
  "latency_ms": 6892.94
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
| **400** | `substance_code` is `""` | `{"detail": "substance_code must be a non-empty string or null"}` |
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

**A 200 guarantees shape, not correctness — this matters most if you
are printing the response onto a document.** Validation checks that
`severity` is one of the four allowed values and that `steps`,
`contraindication` and `spoken_alert` are non-empty strings in the
right places. It cannot check that what those strings *say* is sound,
and no field in the response asserts that they are. Observed
2026-09-04: a request that passed validation returned
`"contraindication": "Do not attempt to neutral001"` — truncated and
corrupted mid-word by the model, structurally valid, and useless. It is
rare, and it is not something this API can promise never happens; a
"does this look sensible" heuristic would be a guess wearing the
costume of a check, and would fail in the other direction by rejecting
sound text. If a human signs off on a document built from these
responses, that sign-off is doing real work — treat it as the control,
not as a formality. `retrieval_mode` and `grounded` tell you where the
content came from; nothing tells you it is right.

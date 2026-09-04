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
  "generation": { "featherless_configured": true, "last_provider": null },
  "languages": ["bn", "en", "hi", "te", "ur"]
}
```

| Field | Meaning |
|---|---|
| `tiers.script` | Always `true`. Unicode-range detection needs nothing. |
| `tiers.semantic` | `true` loaded, `false` cannot load here, **`null` nothing has needed it yet** |
| `retrieval` | The FAISS index behind `/incident`: `true` loaded, `false` cannot load here, **`null` nothing has needed it yet** |
| `generation.featherless_configured` | Whether a Featherless key is present. Says nothing about whether it works |
| `generation.last_provider` | Which provider actually answered last: `"featherless"`, `"groq"`, or **`null` nothing has generated yet** |

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

**`generation` answers two different questions, and the second is the
one that matters.** `featherless_configured` only says a key is
present. `last_provider` says who actually answered. Since 2026-09-04
Groq is attempted first, so:

```json
{
  "status": "ok",
  "tiers": { "script": true, "semantic": true },
  "retrieval": true,
  "generation": { "featherless_configured": true, "last_provider": "featherless" },
  "languages": ["bn", "en", "hi", "te", "ur"]
}
```

means Groq was tried and **failed**, and the fallback carried the
request. The steady state is `last_provider: "groq"`; a run of
`"featherless"` means the primary is in trouble. Either state is
invisible in an individual response unless you read
`generation_provider`, and invisible at a glance unless you read this
field, which is why both exist.

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
    "Put on full chemical protective clothing and a self-contained breathing apparatus.",
    "Remove the casualty's contaminated clothing, footwear and jewellery.",
    "Irrigate the forearm with a large, continuous flow of water for at least 30 minutes, continuing until the skin no longer feels slippery.",
    "Keep the area wet, monitor the casualty, and arrange immediate medical transport."
  ],
  "contraindication": "Do not attempt to neutralise the burn with vinegar or any acidic solution.",
  "spoken_alert": "Emergency! Caustic soda spill on forearm - remove clothing, flood with water, do not apply acid, seek medical help now.",
  "spoken_alert_translated": false,
  "substance_name": "Sodium hydroxide (50% solution)",
  "grounded": true,
  "retrieval_mode": "substance_matched",
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_caustic_soda.md"
  ],
  "generation_provider": "groq",
  "latency_ms": 76486.75
}
```

That example was captured on 2026-09-04 under the **previous** provider
order, when Featherless was attempted first: it hit its 60 s budget
without responding and Groq answered in the remaining ~16 s, hence
`generation_provider: "groq"` and the 76-second latency together. Under
the current order Groq is tried first, so a comparable request now
returns `"groq"` in **8–25 seconds**. The body shape is unchanged; only
the timing and the reason for the provider name differ. See
`generation_provider` below.

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
| `generation_provider` | Which model answered: `"featherless"` or `"groq"`. See below |
| `latency_ms` | Server-side work only. Dominated by the model call |

### `generation_provider`: Groq first, Featherless behind it

Structured generation tries **Groq (`openai/gpt-oss-120b`) first on
every request**, and falls back to **Featherless
(`Qwen/Qwen2.5-72B-Instruct`)** if that attempt fails for any reason — a
rejected key, a rate limit, a watchdog timeout, or two unparseable
answers in a row. Both providers get identical treatment, including one
correction retry on malformed JSON.

**This order was reversed on 2026-09-04.** Featherless was primary
because using it is a project requirement, but measured under
conditions that excluded every cause on the caller's side — five
identical requests, five minutes fully idle between each, strictly
sequential, zero rate limiting — it answered **1 of 5**, and all four
failures were flat 60-second timeouts. Every request was paying a
60-second penalty before the fallback began. Featherless remains in the
chain and still answers, quickly when it does (7.7 s, 9.8 s, 10.2 s
measured); it is now the second attempt rather than a tax on the first.

So `generation_provider: "featherless"` now means **Groq was tried and
failed** — the inverse of what it meant before. Captured on a
deployment with a deliberately invalid Groq key:

```json
{
  "severity": "critical",
  "steps": [
    "Raise the alarm and evacuate personnel, moving crosswind first then upwind from the leak",
    "Establish a 100 m isolation zone and deny entry to anyone without self-contained breathing apparatus; evacuate all low-lying areas",
    "Account for all personnel by name and report the headcount",
    "Only chlorine-trained responders in SCBA and protective clothing may approach; if safe, shut off the supply valve at the source, do not attempt a repair",
    "Prevent the gas from entering drains or sewers and keep the release contained"
  ],
  "contraindication": "Do not enter the leak area without self-contained breathing apparatus",
  "spoken_alert": "Evacuate immediately, move crosswind then upwind, stay out of the leak area, and await further instructions.",
  "spoken_alert_translated": false,
  "substance_name": "Chlorine gas",
  "grounded": true,
  "retrieval_mode": "substance_matched",
  "retrieved_sources": [
    "reg_factories_act_excerpts.md",
    "sds_chlorine.md"
  ],
  "generation_provider": "groq",
  "latency_ms": 16977.3
}
```

Nothing else in that body differs from a Featherless-generated one. If
it matters to you which model answered — and for this project it does —
`generation_provider` is the only field that says so, with
`/health`'s `generation.last_provider` as the at-a-glance equivalent.

**503 now means no provider at all.** A missing or rejected Featherless
key no longer fails the request; it falls through to Groq. You will
only see a 503 when neither `FEATHERLESS_API_KEY` nor `GROQ_API_KEY` is
usable.

### `retrieval_mode`: how much the sources are worth

Four values. Three of them produce a grounded answer, and they are
**not equally trustworthy**.

| Value | Meaning | `grounded` |
|---|---|---|
| `substance_matched` | A code was given and the corpus has documents for it. The SDS excerpts are about the substance you named. | `true` |
| `substance_unknown` | A code was given and the corpus has **nothing** under it. **Only regulations are retrieved** — no other substance's SDS. | `true` |
| `substance_unmapped` | `substance_code` was `null`. Same handling, different cause. | `true` |
| `unavailable` | No index. Answer comes from the model's general knowledge. | `false` |

**`grounded` is exactly `retrieved_sources` being non-empty.** That is
the guarantee to rely on, and it is checkable directly from the body.

Its relationship to `retrieval_mode` is one-directional: `unavailable`
always means `grounded: false`, but `grounded: false` does **not** imply
`unavailable`. A corpus containing no regulation documents would answer
an unknown substance with no chunks at all — correctly reporting
`substance_unknown` and `grounded: false` together.

**`substance_unknown` deliberately gives you less.** No other
substance's safety data is offered as an analogue — only the general
regulations, which hold whatever the chemical is. A real captured
response for `substance_code: "TOLUENE"`, which the corpus has no sheet
for:

```json
{
  "severity": "critical",
  "steps": [
    "Declare that no safety data sheet is available and treat the substance as unknown",
    "Activate the emergency alarm and notify the response team",
    "Evacuate all personnel from Bay 02 using clear, unobstructed exits",
    "Isolate the area by closing ventilation and securing doors without locking them from the inside",
    "Permit entry only for trained responders equipped with full-face pressure-demand SCBA",
    "Await arrival of qualified hazmat personnel before any cleanup"
  ],
  "contraindication": "Do not enter the spill area without proper self-contained breathing apparatus and training.",
  "spoken_alert": "Attention all personnel: unknown chemical spill in Bay 02, evacuate immediately, avoid entry, await emergency instructions.",
  "spoken_alert_translated": false,
  "substance_name": "Toluene",
  "grounded": true,
  "retrieval_mode": "substance_unknown",
  "retrieved_sources": [
    "reg_factories_act_excerpts.md",
    "reg_osha_excerpts.md"
  ],
  "generation_provider": "groq",
  "latency_ms": 75853.59
}
```

Note what it does not contain: no hazard chemistry, no first-aid
procedure, no reactivity claim. The first step says outright that no
safety data sheet is available. `retrieved_sources` lists only the two
regulation files, which really are what the answer was built from.

### Why the nearest analogous chemical is deliberately withheld

Until 2026-09-04 this mode retrieved the semantically closest SDS
instead, on the theory that a similar chemical is better than nothing.
Measured, it was worse. The same toluene request returned confident,
correct-sounding advice about eliminating ignition sources while citing
the ammonia, caustic soda, chlorine and sulphuric acid sheets — **not
one of which mentions ignition sources.** The model had answered from
its own training and the response attributed it to four documents that
did not contain it.

That is worse than an obviously wrong answer. Nothing in the body looks
wrong; catching it means opening all four cited files and checking them
against every claim. And the failure runs the other way too: on a
different substance the model may instead *use* those excerpts, and
advise treating a flammable liquid as a corrosive.

The prompt already forbade exactly this, and was ignored — so the fix
is structural rather than another instruction. There is now no
wrong-substance document available to mis-cite. The cost is real: a
caustic-soda irrigation procedure is sometimes reasonable for an
unknown corrosive, and that context is gone. It was given up because
the response cannot tell you which of those two cases you are in, and a
citation to a document that does not describe the substance is worse
than no citation at all.

**Still do not present `substance_unknown` output as
substance-specific guidance.** `retrieved_sources` is now honest about
what was supplied to the model, but it remains no proof of where any
particular sentence came from.

`substance_unmapped` carries the same caveat with a different cause —
nobody claimed a code and got it wrong, the claim was never made:

```json
{
  "severity": "high",
  "steps": [
    "Acknowledge that no safety data sheet is available; treat the white solid as an unknown hazardous material.",
    "Evacuate all personnel from Bay-11 and establish a safe perimeter; keep untrained responders away.",
    "Notify the supervisor or person in charge and contact the nearest Inspector immediately.",
    "Activate the facility alarm and commence building evacuation following fire-escape procedures.",
    "If anyone is exposed, use the nearest eyewash or emergency drench station within ten seconds."
  ],
  "contraindication": "Do not attempt to open or handle the leaking drum without qualified hazardous-material personnel.",
  "spoken_alert": "Danger: unknown chemical leak in Bay Eleven, evacuate immediately and stay clear of the drum!",
  "spoken_alert_translated": false,
  "substance_name": "Unidentified white crystalline solid",
  "grounded": true,
  "retrieval_mode": "substance_unmapped",
  "retrieved_sources": [
    "reg_factories_act_excerpts.md",
    "reg_osha_excerpts.md"
  ],
  "generation_provider": "groq",
  "latency_ms": 61655.45
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
corrosives. A petrol or LPG incident therefore gets no substance
guidance at all — evacuation and duty-of-care procedure only — rather
than corrosive-handling advice dressed up as fire advice.

This is a property of the sample corpus, not a defect to be worked
around in a client. It resolves when real source documents replace the
illustrative set — until then, seven of eleven codes come back as
`substance_unknown`, grounded in the regulations only, with no
substance-specific hazard information at all.

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
    "Evacuate all personnel from Bay four and establish a safe perimeter.",
    "Shut off the pump and isolate the chlorine source only if it can be done without entering the leak zone.",
    "Don self-contained breathing apparatus before any entry into the affected area.",
    "Activate emergency ventilation fans to disperse the gas.",
    "Notify the emergency response team and begin decontamination after the leak is controlled."
  ],
  "contraindication": "Do not enter the leak area without a self-contained breathing apparatus.",
  "spoken_alert": "All personnel, evacuate BAY four immediately, avoid the leak, and await further instructions.",
  "spoken_alert_translated": false,
  "substance_name": "Chlorine gas",
  "grounded": false,
  "retrieval_mode": "unavailable",
  "retrieved_sources": [],
  "generation_provider": "groq",
  "latency_ms": 3139.07
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
    "Activate the nearest emergency eyewash station immediately.",
    "Irrigate the eyes with a strong, continuous flow of clean water, holding the eyelids apart, for at least thirty minutes.",
    "Maintain full-flow irrigation while transporting the casualty to medical care.",
    "Do not apply any neutralising chemicals or alkali to the eyes."
  ],
  "contraindication": "Do not attempt to neutralise the acid with any alkali or other chemical.",
  "spoken_alert": "आपातकाल! आँख में अम्ल लग गया – तुरंत आँखा धुलाई सक्रिय करें, आँखों को लगातार तीस मिनट तक धोते रहें, और तुरंत चिकित्सा सहायता प्राप्त करें!",
  "spoken_alert_translated": true,
  "substance_name": "Sulphuric acid (98%)",
  "grounded": true,
  "retrieval_mode": "substance_matched",
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_sulphuric_acid.md"
  ],
  "generation_provider": "groq",
  "latency_ms": 4081.28
}
```

If the translation chain is unavailable, the **English** alert is
returned instead, with `spoken_alert_translated: false`:

```json
{
  "severity": "critical",
  "steps": [
    "Immediately flush affected eye with large quantities of water for at least 30 minutes.",
    "Hold eyelids apart during irrigation to ensure thorough flushing.",
    "Continue irrigation during transport to medical care.",
    "Check for and remove any remaining acid residue from the face and hands."
  ],
  "contraindication": "Do not attempt to neutralise the acid in the eye with any alkali.",
  "spoken_alert": "Immediate eye irrigation required in BAY-07 for acid splash. Flush continuously and seek medical help.",
  "spoken_alert_translated": false,
  "substance_name": "Sulphuric acid (98%)",
  "grounded": true,
  "retrieval_mode": "substance_matched",
  "retrieved_sources": [
    "reg_osha_excerpts.md",
    "sds_sulphuric_acid.md"
  ],
  "generation_provider": "featherless",
  "latency_ms": 10179.19
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
| **503** | `/incident`: no provider configured at all | `{"detail": "No structured-generation provider is configured -- set FEATHERLESS_API_KEY or GROQ_API_KEY."}` |
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

Model calls are bounded at **60 seconds total** by a watchdog, split
evenly: **30 s for Groq, 30 s for Featherless**. Each provider's budget
covers its first attempt *and* its correction retry together, so a slow
first attempt shortens that provider's retry rather than doubling the
ceiling — and a stalled primary cannot eat the time the fallback needs
to rescue the request. If the whole budget runs out you get a **503**,
not a hang.

The fallback budget is 30 s rather than 60 s on evidence: every
Featherless success measured landed under 15 s, while every failure was
a flat 60 s timeout that never converted into an answer. 30 s keeps
each observed success and halves each observed failure.

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

**`/incident` has a two-provider fallback; `/translate` has three.**
Structured generation tries Groq then Featherless. Translation walks
Groq → Gemini → OpenRouter and is unchanged. The two paths are
deliberately independent: `/translate`'s behaviour is documented against
the three-tier chain and nothing in the structured path alters it. A
Groq outage no longer takes `/incident` down — it shows up as
`generation_provider: "featherless"`, which you should be watching for.

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

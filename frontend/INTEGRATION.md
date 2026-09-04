# Wiring this console to the real backend

Everything the frontend needs in order to talk to the incident service,
and four things that do not currently line up. Nothing here has been
silently adapted on either side — each mismatch is written down so
whoever owns that side can decide.

Checked on 2026-09-04 against `frontend/client/src/pages/Home.tsx` and
the incident service in this repo.

---

## Pointing at the backend

```bash
# frontend/.env.local   (gitignored)
VITE_API_BASE_URL=https://the-deployed-backend.example.com
```

Then **rebuild**. Vite inlines `import.meta.env.*` at build time, so
setting this on the server after the bundle exists does nothing.

Unset, calls stay same-origin relative paths, which is what local dev
wants. All API URLs come from `client/src/api.ts`.

---

## 1. CORS — cannot be fixed from this side

Once the frontend and backend are on different origins, the **browser**
will block the response unless the backend sends the header. No frontend
change can work around it; this is a server-side decision by design.

**The backend must send:**

```
Access-Control-Allow-Origin: https://<this-frontend-origin>
```

And because the request sets `Content-Type: application/json`, the
browser sends a `OPTIONS` preflight first, which the backend must also
answer:

```
Access-Control-Allow-Methods: POST, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

For FastAPI that is `CORSMiddleware`; for Express, the `cors` package.

**Symptom if it is missing:** the request appears to fail from the
frontend with no status code, and the console shows a CORS error. The
console will show the red `DEMO FALLBACK` badge, so at least it will not
be mistaken for a real answer.

**Alternative that avoids CORS in development only:** add a `server.proxy`
entry to `vite.config.ts` so the dev server forwards `/incident` to the
backend, making it same-origin from the browser's point of view. Not done
here — `vite.config.ts` is shared and this is a one-line change somebody
should make deliberately.

---

## 2. `API_CONTRACT.md` does not document `/incident`

The task asked whether the response shape matches `API_CONTRACT.md`
exactly. It cannot be checked, because that file documents a **different
service**. Its endpoints are:

```
GET  /health
POST /detect
POST /translate
```

There is no `/incident` section anywhere in it. The shape below is
therefore taken from the incident service implementation in this repo
(`incident_api.py`), not from a published contract. **Somebody should
write the `/incident` contract down** — right now two teams are building
against an assumption.

---

## 3. Response mismatch: `severity` case (breaks the UI)

**The backend sends lowercase. The frontend compares uppercase.**

```python
# backend
{"severity": "critical", ...}
```

```tsx
// Home.tsx:172
const isCritical = response.severity === "CRITICAL";
```

`"critical" === "CRITICAL"` is `false`, so **a critical incident renders
with the non-critical styling** (`lockout-high` instead of
`lockout-critical`). Line 252 also prints the value raw, so the operator
sees a lowercase `high` where the design expects `HIGH`.

The TypeScript type says `"HIGH" | "CRITICAL" | "MEDIUM"`, but nothing
validates it at runtime — the guard only checks the field is truthy, so
the wrong-case value is accepted and typed as if it were right.

**Not adapted here, because either side is a defensible fix and it is not
this side's call:**

- backend uppercases `severity` in its response, or
- frontend normalises with `.toUpperCase()` on receipt, or
- the contract states the casing and both sides conform

Whoever decides, do it in one place.

---

## 4. Response mismatch: `contraindication` is dropped

The backend returns it:

```json
"contraindication": "Do not flush with a pressurised water jet -- sodium
hydroxide reacts exothermically with water and will spatter caustic
solution."
```

`IncidentResponse` in `Home.tsx` has three fields — `severity`, `steps`,
`spoken_alert` — and no `contraindication`. It is fetched and thrown
away.

This is the field most likely to hurt somebody. It is the *"do not do
this"* instruction, and it is the one thing the backend deliberately
never generates with an LLM. Right now it never reaches the operator.

**Recommended:** add it to the type and render it above the steps, the
way the PDF dossier does — a contraindication read after step 1 is read
too late. Left to whoever owns this screen.

---

## 5. Request mismatch: field names and value vocabularies

What the frontend posts today:

```json
{
  "location": "Bay-3, Reactor B",
  "substance": "Caustic Soda",
  "incident_type": "Spill",
  "language": "Telugu",
  "media": { "camera": true, "microphone": true }
}
```

What the incident service expects:

| Frontend sends | Backend expects | Status |
|---|---|---|
| `location` | `bay_id` | **name mismatch** |
| `substance` | `substance_code` + `substance_name` | **name mismatch** |
| `incident_type` | `incident_type` | name matches, **values differ** |
| `language` | `language` | name matches, **values differ** |
| `media` | — | extra, ignored |
| — | `source`, `timestamp` | not sent |

**The value vocabularies differ too, which is the harder half:**

- **`incident_type`** — the frontend sends event types (`Spill`,
  `Vapor Release`, `Gas Leak`). The camera trigger sends PPE violations
  (`NO-Hardhat`, `NO-Mask`). These are two different taxonomies arriving
  at one field. The backend's rules match on PPE keywords, so `Spill`
  falls through to its generic branch.
- **`language`** — the frontend sends `Telugu` / `Hindi` / `Bengali` /
  `English`; the backend and the translation service use ISO codes
  `te` / `hi` / `bn` / `en`.
- **`substance`** — the frontend sends display names (`Caustic Soda`).
  The backend accepts `substance_name` and matches aliases, so this one
  works today, but it will not produce a `substance_code` unless the
  frontend sends one or the backend maps it.

**Not adapted here.** Mapping on the frontend would hide the
disagreement rather than resolve it, and the incident service already
owns a substance alias table that does most of this. Worth ten minutes
between the two owners.

---

## What works right now

With `VITE_API_BASE_URL` unset and no backend running, the console is
fully usable and shows the red `DEMO FALLBACK` badge on the locked
screen. Nothing invented is presented as real.

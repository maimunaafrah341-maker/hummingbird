# Deploying HazardWatch

Written after a Render deploy failed in 6.2 seconds. Three things were
wrong; only the first one produced an error message.

---

## The failure: Root Directory

The instructions being followed said:

> Root Directory: `_reusable`

**That folder does not exist in this repository.** It exists in
*Athena*, the other project — `_reusable/` is the folder that was
extracted *out of* Athena to create this repo. Here, everything is at
the top level.

Render looked in `_reusable/`, found no Dockerfile, and failed
immediately. That is the 6.2 seconds.

**Fix: leave Root Directory blank.**

---

## The one nobody would have caught: it deploys the wrong app

The `Dockerfile` ends with:

```dockerfile
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}
```

`api:app` is the **language service**. Its endpoints are `/health`,
`/detect` and `/translate`. It does **not** serve `/incident`.

So even with the root directory fixed, the deploy would have come up
green and the frontend would still have got a 404 on every incident —
which the console now reports as `DEMO FALLBACK` rather than pretending
it worked, but it would still not be a working demo.

`/incident` lives in **`incident_api.py`**.

### Pick one

**A. Deploy the incident service** — what the frontend needs.

On Render, set **Docker Command** (Settings → Build & Deploy) to:

```
uvicorn incident_api:app --host 0.0.0.0 --port $PORT
```

Gives you `/health` and `POST /incident`. Needs no API keys — the rules
tier is deterministic and offline.

**B. Deploy both**, as two Render services from the same repo, each
with its own Docker Command. Only do this if the demo actually uses
`/translate`.

---

## Backend — Render

| Field | Value |
|---|---|
| Repository | `maimunaafrah341-maker/hummingbird` |
| Branch | `main` |
| **Root Directory** | **(leave blank)** |
| Language | Docker |
| Region | Singapore |
| Instance Type | Free |
| **Docker Command** | *(leave blank — the image now defaults to the incident service)* |

**Environment variables:**

| Key | Value | Needed? |
|---|---|---|
| `CORS_ORIGINS` | the frontend URL, e.g. `https://hazardwatch.vercel.app` | once the frontend is up |
| `INCIDENT_LLM` | `1` | optional — only to reword steps via an LLM |
| `GROQ_API_KEY` | your key | only if `INCIDENT_LLM=1` |

**The image defaults to `incident_api:app` as of the Dockerfile change.**
It used to default to `api:app`, which is why `hummingbird-2` came up
green, passed its health check — both apps serve `/health` — and
returned **404 on every `/incident`**. Nothing in the logs said so, and
the console showed `DEMO FALLBACK` as though the frontend were at fault.

If a service was created before that change, either **redeploy it from
`main`** or set the Docker Command above explicitly. To serve the
*language* service instead, set `HAZARDWATCH_APP=api:app`.

`GROQ_API_KEY` is **not** required. The incident service answers from a
deterministic rules table with no network calls; the LLM tier only
rewords the steps and falls back to the rules if it fails. Severity and
the contraindication are never LLM-generated.

**Verify it before touching the frontend:**

```bash
curl https://YOUR-SERVICE.onrender.com/health

curl -X POST https://YOUR-SERVICE.onrender.com/incident \
  -H "Content-Type: application/json" \
  -d '{"bay_id":"Bay-3","incident_type":"Spill","substance_code":"NAOH"}'
```

The second should return `severity`, `steps`, `spoken_alert` and
`contraindication`.

**Read `/health` carefully — it tells you which app answered.** The
incident service returns `tiers: {rules, llm}` with `substances` and
`violations`. The language service returns `tiers: {script, semantic}`
with `languages`. Seeing the second one means `/incident` will 404, and
a health check alone will never tell you, because both return 200.

---

## Vercel said "No FastAPI entrypoint found"

```
Error: No FastAPI entrypoint found in default locations, but found
potential entrypoints:
  api.py (variable: app)
  incident_api.py (variable: app)
```

**This project is the frontend. That error means its Root Directory is
blank.**

Vercel only looked for Python because a blank root directory sent it to
the repo root, where `requirements.txt` and two FastAPI apps live. It
never reached `frontend/`.

**Fix: set Root Directory to `frontend`.** Nothing else. The entrypoint
question then never comes up, because Vercel is building a Vite app.

### Do NOT add the pyproject.toml Vercel suggests

Vercel's own error offers a fix:

> Add this to your pyproject.toml: `entrypoint = "api:app"`

Do not take it, for two separate reasons.

**It names the wrong app.** `api:app` is the language service --
`/health`, `/detect`, `/translate`. It has no `/incident`.

**And even with the right app, it makes things worse.** Pinning any
entrypoint turns this loud failure into a quiet success: Vercel would
cheerfully deploy the *backend* to a project meant to serve the
frontend, and the build log would be green. A deploy that fails and
tells you the root directory is wrong is more useful than one that
succeeds at the wrong job.

The error is the diagnostic. Do not silence it.

`pyproject.toml` **is** currently in the repo, pinning `api:app`, and it
should be deleted once whoever added it has read this:

```
git rm pyproject.toml && git commit -m "deploy: the error is the diagnostic"
```

It is left in place here only because removing a teammate's file is
their call, not this document's.

### And the backend could not run there anyway

Bay Twin's `GET /twin/stream` is a long-lived server-sent-events
connection, and the event bus behind it is in-process memory.
Serverless functions hold neither: the connection is cut at the
function timeout, and two requests can land on two instances that share
no state. The page would load and then sit empty.

**Vercel for the frontend, Render for the backend.** That is the split
this document assumes throughout.


## Deployment Protection: the one that hides until demo day

`https://<project>-<hash>-<team>.vercel.app` redirects to
`vercel.com/login`.

That is **Vercel Deployment Protection**, on by default for preview
deployments. The page loads fine for whoever is signed in to the Vercel
team and shows a login wall to everyone else -- so it looks perfect
while you build it and fails for every judge, every phone, and every
person you send the link to.

Two separate things to fix, and you need both.

**1. Use the production URL, not the deployment URL.** A hostname with
a random hash in the middle -- `hummingbird-ma6xsncod-humming-bird1` --
is a single immutable deployment, not the project. The production alias
is the short one, and it is what should be on the slide.

**2. Turn the protection off.**

> Vercel &rarr; Project &rarr; Settings &rarr; Deployment Protection &rarr;
> **Vercel Authentication: Disabled** &rarr; Save, then redeploy.

**Verify it from outside your own session**, because signed in you
cannot tell the difference:

```bash
curl -sI https://YOUR-URL.vercel.app | head -1
```

`HTTP/2 200` is right. `HTTP/2 307` or `302` with a `Location:` pointing
at `vercel.com/sso-api` means it is still gated. A phone on mobile data,
or any private window, tests the same thing.

---

## Frontend — Vercel or Netlify

| Field | Value |
|---|---|
| Root Directory | `frontend` |
| Build Command | `pnpm build` |
| **Publish / Output Directory** | **`dist/public`** |

**The publish directory was missing from the plan and it matters.**
`vite.config.ts` sets `outDir: dist/public`, not the default `dist`.
Point the host at `dist` and it serves an empty directory — a
successful build and a blank page.

**Environment variable, set BEFORE the first build:**

```
VITE_API_BASE_URL = https://YOUR-SERVICE.onrender.com
```

Vite inlines `import.meta.env.*` at **build time**. Adding this
variable after a build changes nothing — you have to redeploy. This is
the single most common way an hour disappears on deployment day.

**Use pnpm, not npm.** `npm install` **fails** on this project:
`@builder.io/vite-plugin-jsx-loc@0.1.1` requires `vite ^4 || ^5` and
`package.json` declares `vite ^7`. The repo ships `pnpm-lock.yaml`, and
both Vercel and Netlify auto-detect it. If a build log shows
`ERESOLVE unable to resolve dependency tree`, the host chose npm —
force pnpm, or add `--legacy-peer-deps`.

---

## CORS

Handled on the backend, in `incident_api.py`. Unset, `CORS_ORIGINS`
allows any origin, which is right while the frontend URL is still
unknown. Set it to the real URL once you have one.

The frontend posts `Content-Type: application/json`, so the browser
sends an `OPTIONS` preflight first. That is already allowed. Verified
locally: preflight returns 200 with `Allow-Methods: GET, POST, OPTIONS`.

If the browser console shows a CORS error, the backend is not running
the current code — redeploy it.

---

## Cold starts

A free Render instance sleeps after ~15 minutes idle, and the first
request after that takes 30–60 seconds. During a live demo that looks
exactly like a hang.

Keep a cron-job.org ping hitting `/health` every 10 minutes. **And hit
the endpoint yourself two minutes before presenting** — a ping schedule
you have not verified is not a warm instance.

---

## Order

1. Backend on Render with the blank root directory and the incident
   Docker Command. Verify with `curl`.
2. Frontend on Vercel/Netlify with `VITE_API_BASE_URL` already set,
   root `frontend`, publish `dist/public`.
3. Set `CORS_ORIGINS` on Render to the frontend URL, redeploy backend.
4. Open the frontend, trigger a protocol. **If the red `DEMO FALLBACK`
   badge appears on the locked screen, the frontend is not talking to
   the backend** — check the browser console, which names the failure.

That badge is the fastest diagnostic you have. No badge means the
response is genuinely coming from the deployed service.

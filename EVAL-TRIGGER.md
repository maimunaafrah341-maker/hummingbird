# Measured behaviour — the trigger half

`yolo_trigger.py`, `tts_alert.py`, `dossier.py`, `webhook_dispatch.py`.

Every number here came from running the code, on 2026-09-04, on Windows-10-10.0.26200-SP0, Python 3.11.0, CPU only.
No number in this file is an estimate, and anything that could not
be measured says so rather than being filled in. Regenerate with:

```
python eval_trigger.py
```

Companion to `EVAL.md`, which measures the language service. The two
halves share a repo and nothing else — neither file's numbers say
anything about the other's code.

## Check suites

| Suite | Result | Wall time | Command |
|---|---|---|---|
| trigger gate | 20/20 passed | 0.24 s | `python yolo_trigger.py selftest` |
| confidence router | 14/14 passed | 0.12 s | `python confidence_router.py --selftest` |
| dossier | 15/15 passed | 0.81 s | `python dossier.py --selftest` |
| webhook dispatch | 17/17 passed | 5.85 s | `python webhook_dispatch.py --selftest` |
| tts alert | 8/8 passed | 1.17 s | `python tts_alert.py --selftest` |
| alert language | 7/7 passed | 2.82 s | `python alert_language.py --selftest` |
| incident service | 12/12 passed | 1.10 s | `python incident_api.py --selftest` |
| escalation watcher | 19/19 passed | 0.74 s | `python escalation_watcher.py --selftest` |
| incident rehearsal | 11/11 passed | 1.51 s | `python smoke_test.py incident --no-audio --no-open` |

**123 of 123 checks pass.** Each suite runs as its own process, so none
of them can pass on state another one left behind.

## What the gate suppresses

The reason the trigger is not wired straight to the detector. A
continuous violation fed through the real `TriggerGate`:

| | Incidents |
|---|---|
| Ungated, 1800 frames (60s at 30 fps) | 1800 |
| Through the gate, same input | **2** |
| Suppression | 99.9% |

That is one minute of one person without a hardhat. The gate costs
**2.2 µs per frame**, so the thing that prevents the flood is far
cheaper than a single inference.

## Import cost

| Module | Import |
|---|---|
| `yolo_trigger` | 0.088 s |
| `dossier` | 0.295 s |
| `webhook_dispatch` | 0.121 s |
| `tts_alert` | 0.029 s |
| `alert_language` | 0.094 s |
| `confidence_router` | 0.008 s |
| `escalation_watcher` | 0.009 s |

`yolo_trigger` does not import ultralytics at module scope — the
kiosk path, and the whole incident rehearsal, never load torch. That
is why the rehearsal runs on a machine with no camera and no GPU.

## Detection

**Not measured.** ultralytics was unavailable or the model would
not load, so no detection numbers are recorded here. The camera
path is unverified on this machine; the kiosk path is not affected.

## The live camera path

**Not measured.** No usable camera on this machine, so the
lens-to-incident path is unverified here. The kiosk trigger is
unaffected — it exists for exactly this case.

## Output stages

| Stage | Measured |
|---|---|
| PDF dossier | 15 ms (3.2 KB) |
| Webhook round trip (local stub) | 2.5 ms |
| TTS cache hit | 0.71 ms |
| TTS mp3 size | 52.1 KB |
| TTS cold synthesis (network) | **0.60 s** |
| SMS body | 158 / 160 characters |

Cold synthesis is **851×** slower than a cache hit and needs the
network at the moment the alert fires. Prefetch before a demo:

```
python tts_alert.py --prefetch alerts.txt --lang hi
```

### Offline voice coverage on this machine

| Language | Local voice |
|---|---|
| `en` | Microsoft David Desktop - English (United States) |
| `hi` | **none** — falls back to gTTS (network) |
| `bn` | **none** — falls back to gTTS (network) |
| `te` | **none** — falls back to gTTS (network) |
| `ur` | **none** — falls back to gTTS (network) |

Only languages with a local voice can be spoken offline. Everything
else needs either the network or a warm cache, which is the entire
reason the cache exists.

## What this does not measure

Stated so the numbers above are not read as more than they are.

- **Detection accuracy.** The reference-image row shows the model
  fires on a known input. It is not an accuracy figure: there is no
  labelled PPE eval set in this repo, so no precision or recall is
  claimed anywhere.
- **The real `/incident` service.** Every run here uses a local mock
  answering the assumed contract shape. Nothing is known about the
  teammate's endpoint until this is pointed at it.
- **A real camera.** Frames come from a file; no usable camera was
  available, so the lens-to-model path is unverified here.
- **Real SMS, Telegram or Slack delivery.** The dispatch payload is
  built and sent for real; the recipient is a stub. No message was
  ever sent to a carrier or a workspace.
- **Sustained running.** Every measurement is short. Nothing here says
  what happens after an hour of watching a bay.


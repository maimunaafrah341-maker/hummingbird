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
| trigger gate | 20/20 passed | 0.23 s | `python yolo_trigger.py selftest` |
| confidence router | 14/14 passed | 0.12 s | `python confidence_router.py --selftest` |
| dossier | 15/15 passed | 0.83 s | `python dossier.py --selftest` |
| webhook dispatch | 17/17 passed | 5.40 s | `python webhook_dispatch.py --selftest` |
| tts alert | 8/8 passed | 1.20 s | `python tts_alert.py --selftest` |
| alert language | 7/7 passed | 2.82 s | `python alert_language.py --selftest` |
| incident service | 12/12 passed | 1.12 s | `python incident_api.py --selftest` |
| escalation watcher | 19/19 passed | 0.85 s | `python escalation_watcher.py --selftest` |
| incident rehearsal | 11/11 passed | 1.09 s | `python smoke_test.py incident --no-audio --no-open` |

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
**3.3 µs per frame**, so the thing that prevents the flood is far
cheaper than a single inference.

## Import cost

| Module | Import |
|---|---|
| `yolo_trigger` | 0.103 s |
| `dossier` | 0.357 s |
| `webhook_dispatch` | 0.118 s |
| `tts_alert` | 0.029 s |
| `alert_language` | 0.103 s |
| `confidence_router` | 0.008 s |
| `escalation_watcher` | 0.012 s |

`yolo_trigger` does not import ultralytics at module scope — the
kiosk path, and the whole incident rehearsal, never load torch. That
is why the rehearsal runs on a machine with no camera and no GPU.

## Detection

Model: `hf:Hansung-Cho/yolov8-ppe-detection:best.pt`, 10 classes, of which 3 are violations: `NO-Hardhat`, `NO-Mask`, `NO-Safety Vest`.

| What | Measured |
|---|---|
| `from ultralytics import YOLO` | 4.30 s |
| Model load (cached weights) | 2.46 s |
| Inference alone, one 810×1080 frame in memory | 64 ms  (15.7 fps) |
| Full per-frame loop — frame in, gate decision out | **67 ms  (14.9 fps)** |

Those two rows agree to within 5%, which is run-to-run noise
on a busy laptop rather than a real difference. That is the
finding: everything the loop does outside the model — unpacking
boxes, the gate decision — costs microseconds against ~64 ms of
inference, so the model is effectively the entire frame budget.
Neither number is reliably the larger one; quote either.

RSS, which is what decides where this can run:

```
baseline python      :     18 MB
+ ultralytics        :    219 MB
+ model loaded       :    256 MB
+ first inference    :    407 MB
```

Detections on `bus.jpg`, the reference image ultralytics ships, so this
row is reproducible on any machine:

| Class | Confidence |
|---|---|
| `Person` | 0.860 |
| `NO-Hardhat` | 0.804 |
| `NO-Mask` | 0.601 |

At **14.9 fps** the 3-of-8 confirmation costs **0.2 s** at best
-- longer whenever the detector misses a frame -- before a
violation fires — the latency of the autonomous path, set by CPU
inference rather than by the gate.

Both figures come from a decoded frame held in memory, so neither
includes camera capture. That is measured separately below.

## The live camera path

Light hitting the sensor through to a gate decision, on the
built-in camera:

| What | Measured |
|---|---|
| Resolution | 1280×720 |
| Capture alone | 33 ms  (30.7 fps) |
| Full live path — capture, infer, gate | **60 ms  (16.7 fps)** |
| Time to fire (3 hits of 8) | **0.2 s** at best |

What the camera actually saw during the run:

| Class | Confidence |
|---|---|
| `NO-Hardhat` | 0.906 |
| `Person` | 0.837 |
| `NO-Safety Vest` | 0.745 |
| `NO-Mask` | 0.687 |

So the lens-to-model path is verified end to end on this
machine, not inferred from the file-source numbers.

## Output stages

| Stage | Measured |
|---|---|
| PDF dossier | 19 ms (3.2 KB) |
| Webhook round trip (local stub) | 3.4 ms |
| TTS cache hit | 0.77 ms |
| TTS mp3 size | 52.1 KB |
| TTS cold synthesis (network) | **1.12 s** |
| SMS body | 158 / 160 characters |

Cold synthesis is **1453×** slower than a cache hit and needs the
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
- **Real SMS, Telegram or Slack delivery.** The dispatch payload is
  built and sent for real; the recipient is a stub. No message was
  ever sent to a carrier or a workspace.
- **Sustained running.** Every measurement is short. Nothing here says
  what happens after an hour of watching a bay.


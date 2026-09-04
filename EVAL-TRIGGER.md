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
| trigger gate | 8/8 passed | 0.19 s | `python yolo_trigger.py selftest` |
| dossier | 10/10 passed | 0.51 s | `python dossier.py --selftest` |
| webhook dispatch | 13/13 passed | 5.84 s | `python webhook_dispatch.py --selftest` |
| tts alert | 5/5 passed | 1.14 s | `python tts_alert.py --selftest` |
| incident rehearsal | 11/11 passed | 1.49 s | `python smoke_test.py incident --no-audio --no-open` |

**47 of 47 checks pass.** Each suite runs as its own process, so none
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
**1.5 µs per frame**, so the thing that prevents the flood is far
cheaper than a single inference.

## Import cost

| Module | Import |
|---|---|
| `yolo_trigger` | 0.097 s |
| `dossier` | 0.318 s |
| `webhook_dispatch` | 0.099 s |
| `tts_alert` | 0.012 s |

`yolo_trigger` does not import ultralytics at module scope — the
kiosk path, and the whole incident rehearsal, never load torch. That
is why the rehearsal runs on a machine with no camera and no GPU.

## Detection

Model: `hf:Hansung-Cho/yolov8-ppe-detection:best.pt`, 10 classes, of which 3 are violations: `NO-Hardhat`, `NO-Mask`, `NO-Safety Vest`.

| What | Measured |
|---|---|
| `from ultralytics import YOLO` | 3.47 s |
| Model load (cached weights) | 2.42 s |
| Inference alone, one 810×1080 frame in memory | 51 ms  (19.5 fps) |
| Full per-frame loop — frame in, gate decision out | **52 ms  (19.3 fps)** |

Those two rows agree to within 1%, which is run-to-run noise
on a busy laptop rather than a real difference. That is the
finding: everything the loop does outside the model — unpacking
boxes, the gate decision — costs microseconds against ~51 ms of
inference, so the model is effectively the entire frame budget.
Neither number is reliably the larger one; quote either.

RSS, which is what decides where this can run:

```
baseline python      :     18 MB
+ ultralytics        :    221 MB
+ model loaded       :    254 MB
+ first inference    :    389 MB
```

Detections on `bus.jpg`, the reference image ultralytics ships, so this
row is reproducible on any machine:

| Class | Confidence |
|---|---|
| `Person` | 0.860 |
| `NO-Hardhat` | 0.804 |
| `NO-Mask` | 0.601 |

At **19.3 fps** the 5-frame confirmation costs **0.3 s** before a
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
| Capture alone | 32 ms  (31.2 fps) |
| Full live path — capture, infer, gate | **60 ms  (16.7 fps)** |
| Time to fire (5 frames) | **0.3 s** |

The camera detected nothing above the confidence floor during
the run — an empty room, most likely. The path is measured;
whether it *fires* was not exercised.

## Output stages

| Stage | Measured |
|---|---|
| PDF dossier | 16 ms (3.2 KB) |
| Webhook round trip (local stub) | 1.3 ms |
| TTS cache hit | 0.85 ms |
| TTS mp3 size | 52.1 KB |
| TTS cold synthesis (network) | **1.09 s** |
| SMS body | 158 / 160 characters |

Cold synthesis is **1284×** slower than a cache hit and needs the
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
- **A camera that sees a violation.** The live path is timed, but
  nothing was detected during the run, so firing is unexercised.
- **Real SMS, Telegram or Slack delivery.** The dispatch payload is
  built and sent for real; the recipient is a stub. No message was
  ever sent to a carrier or a workspace.
- **Sustained running.** Every measurement is short. Nothing here says
  what happens after an hour of watching a bay.


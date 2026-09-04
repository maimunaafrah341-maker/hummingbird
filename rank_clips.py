"""
Rank candidate demo clips by whether they would actually fire the
trigger.

Picking demo footage by eye does not work. Plenty of construction video
shows everyone correctly wearing a hardhat, which the model detects as
`Hardhat` -- a compliance, not a violation -- and the trigger stays
silent. Watching the clip tells you there are workers in it; it does not
tell you the model will see a violation.

So this samples frames across each clip, runs the real detector, feeds
the results through the real TriggerGate, and reports how many incidents
each clip would open. Sampled rather than exhaustive: a 4K clip decoded
frame by frame takes minutes, and the question here is comparative.

    python rank_clips.py                 # every video beside this file
    python rank_clips.py clip.mp4 ...    # specific ones
    python rank_clips.py --frames 30     # sample harder

The number that matters is the last column. It is a comparative score,
not an incident count -- the cooldown is switched off so clips can be
ranked by how readily they clear the confirmation gate. In a real run
the 45s mute turns any of these into one or two incidents. A clip
scoring zero is not a demo clip, however good it looks.
"""

import argparse
import glob
import os
import sys
import time

VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def sample_clip(path, model, violations, samples=20, conf=None):
    """Sample frames evenly across a clip. Returns a summary dict."""

    import cv2

    import yolo_trigger as yt

    conf = conf if conf is not None else yt.CONFIDENCE_FLOOR
    capture = cv2.VideoCapture(path)

    if not capture.isOpened():
        return {"path": path, "error": "could not open"}

    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration = total / fps if fps else 0.0

    # Evenly spaced samples. Seeking is far cheaper than decoding every
    # frame of 4K footage, and gives a fair read across the whole clip
    # rather than judging it by its first second.
    indices = ([int(total * i / float(samples)) for i in range(samples)]
               if total > samples else list(range(max(total, 1))))

    # Cooldown deliberately disabled: this is comparing how readily each
    # clip clears the confirmation gate, not predicting incident counts.
    # With the real 45s mute any of these yields one or two incidents.
    gate = yt.TriggerGate(cooldown=0.0, hits=yt.HITS_REQUIRED,
                          window=yt.WINDOW_FRAMES)

    seen = {}
    violation_frames = 0
    fires = 0
    read = 0
    started = time.time()

    for index in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()

        if not ok or frame is None:
            continue

        read += 1
        results = model(frame, verbose=False, conf=conf)[0]
        present = {}

        for box in results.boxes:
            class_id = int(box.cls[0])
            name = model.names[class_id]
            score = round(float(box.conf[0]), 3)
            seen[name] = max(seen.get(name, 0.0), score)

            if class_id in violations:
                present[violations[class_id]] = max(
                    present.get(violations[class_id], 0.0), score)

        if present:
            violation_frames += 1

        fires += len(gate.observe("RANK", present.keys()))

    capture.release()

    return {
        "path": path,
        "width": width, "height": height, "fps": fps, "duration": duration,
        "sampled": read,
        "violation_frames": violation_frames,
        "fires": fires,
        "detections": seen,
        "violations": {k: v for k, v in seen.items() if yt.is_violation_name(k)},
        "seconds": time.time() - started,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rank demo clips by whether they would fire the trigger.")
    parser.add_argument("clips", nargs="*", help="video files (default: ./*.mp4 etc)")
    parser.add_argument("--frames", type=int, default=20,
                        help="frames sampled per clip")
    parser.add_argument("--conf", type=float, default=None,
                        help="confidence floor (default: the trigger's)")

    args = parser.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    clips = args.clips

    if not clips:
        for suffix in VIDEO_SUFFIXES:
            clips += sorted(glob.glob(os.path.join(here, "*" + suffix)))

    if not clips:
        print("no video files found beside %s" % here)
        return 1

    import yolo_trigger as yt

    model, label = yt.load_model()
    violations = yt._violation_classes(model)

    print("model: %s" % label)
    print("violation classes: %s\n" % ", ".join(sorted(violations.values())))
    print("sampling %d frames per clip, conf>=%.2f -- this takes a minute\n"
          % (args.frames, args.conf if args.conf is not None else yt.CONFIDENCE_FLOOR))

    results = []

    for clip in clips:
        summary = sample_clip(clip, model, violations, args.frames, args.conf)
        results.append(summary)

        name = os.path.basename(clip)[:34]

        if summary.get("error"):
            print("  %-34s  %s" % (name, summary["error"]))
            continue

        print("  %-34s %5.1fs %9dx%-5d  violations in %2d/%2d frames  -> score %d"
              % (name, summary["duration"], summary["width"], summary["height"],
                 summary["violation_frames"], summary["sampled"], summary["fires"]))

    usable = [r for r in results if r.get("fires")]
    usable.sort(key=lambda r: (-r["fires"], -r["violation_frames"]))

    print("\n" + "=" * 70)

    if not usable:
        print("NO CLIP WOULD FIRE THE TRIGGER.")
        print()
        print("Everything detected across all clips:")

        everything = {}

        for r in results:
            for k, v in (r.get("detections") or {}).items():
                everything[k] = max(everything.get(k, 0.0), v)

        for name in sorted(everything, key=lambda n: -everything[n]):
            print("  %-18s %.3f" % (name, everything[name]))

        print()
        print("If only `Hardhat`/`Safety Vest` appear, the workers in these")
        print("clips are wearing their PPE correctly -- which is a compliance,")
        print("not a violation. You need footage of people WITHOUT it.")
        return 1

    print("USABLE DEMO CLIPS, best first:\n")

    for r in usable:
        print("  %s" % os.path.basename(r["path"]))
        print("     score %d (gate clears, cooldown off), violations in %d of %d frames"
              % (r["fires"], r["violation_frames"], r["sampled"]))
        print("     detected: %s"
              % ", ".join("%s %.2f" % (k, v)
                          for k, v in sorted(r["violations"].items(),
                                             key=lambda kv: -kv[1])))
        print("     python yolo_trigger.py camera --zone BAY-3 --source \"%s\" --show"
              % os.path.basename(r["path"]))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())

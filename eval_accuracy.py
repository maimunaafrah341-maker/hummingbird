"""
Precision and recall for the detector, against a real labelled dataset.

The rest of the project measures speed and behaviour. This measures
whether the thing is *right*, which is the only question a safety claim
rests on, and it is the one number the project refused to invent.

## Read this before trusting any number it prints

**Ultralytics matches classes by index, not by name.** `model.val()`
compares the model's class 0 against the dataset's class 0 and never
checks that both are called "Hardhat". Point this at a PPE dataset with
a different class order and it returns a confident, precise,
meaningless mAP -- the worst possible output, because it looks exactly
like a real result.

That is not hypothetical. `construction-ppe.yaml`, the dataset
ultralytics auto-downloads in one line, has 11 classes beginning
`helmet, gloves, vest, boots, goggles`. This project's model has 10
beginning `Hardhat, Mask, NO-Hardhat`. Validating one against the other
compares `vest` to `NO-Hardhat` and reports a number for it.

So class alignment is a hard gate here. If the names do not match
index-for-index, this refuses to run and prints the mismatch. There is
no --force.

## Getting a dataset whose classes match

The Roboflow "Construction Site Safety" set is the matching one:
Hardhat, Mask, NO-Hardhat, NO-Mask, NO-Safety Vest, Person, Safety
Cone, Safety Vest, machinery, vehicle -- same ten, same order.

    pip install roboflow
    # free key from https://app.roboflow.com/settings/api
    python eval_accuracy.py --fetch --roboflow-key YOUR_KEY

or download it by hand from Roboflow Universe / Kaggle in YOLOv8
format, then point at its data.yaml:

    python eval_accuracy.py --data path/to/data.yaml --split test

## What the numbers mean here

For a hazard trigger the two errors are not equal:

  Recall on a NO-* class      a missed violation is an unflagged hazard
  Precision on a NO-* class   a false alarm that erodes trust in the system

Both are reported per class, and the violation classes are called out
separately, because an aggregate mAP that averages `vehicle` and
`machinery` into the headline hides the only classes this project acts
on.
"""

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "EVAL-ACCURACY.md")

# Where --fetch puts the dataset. Gitignored: it is ~180MB of images.
DATA_DIR = os.path.join(HERE, "datasets")

VIOLATION_PREFIXES = ("no-", "no_", "no ")


def _load_yaml(path):
    """Read a dataset yaml. Uses pyyaml if present, else a minimal parser."""

    try:
        import yaml

        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    except ImportError:
        pass

    # ultralytics depends on pyyaml, so this is a fallback that should
    # never fire; kept so a missing optional dep degrades to a clear
    # error rather than a traceback.
    raise RuntimeError("pyyaml is required to read %s (pip install pyyaml)" % path)


def dataset_names(data_yaml):
    """Class names from a dataset yaml, as {index: name}."""

    config = _load_yaml(data_yaml)
    names = config.get("names")

    if isinstance(names, dict):
        return {int(k): str(v) for k, v in names.items()}

    if isinstance(names, list):
        return {i: str(n) for i, n in enumerate(names)}

    raise RuntimeError("no 'names' in %s" % data_yaml)


def check_alignment(model_names, data_names):
    """
    Return a list of mismatch descriptions. Empty means aligned.

    Compared index by index and case-insensitively, because the same
    dataset exported twice can differ only in capitalisation and that is
    not a real mismatch.
    """

    problems = []

    if len(model_names) != len(data_names):
        problems.append("class COUNT differs: model has %d, dataset has %d"
                        % (len(model_names), len(data_names)))

    for index in sorted(set(model_names) | set(data_names)):
        mine = model_names.get(index)
        theirs = data_names.get(index)

        if mine is None or theirs is None:
            problems.append("index %d: model=%r dataset=%r" % (index, mine, theirs))
        elif mine.strip().lower() != theirs.strip().lower():
            problems.append("index %d: model=%r but dataset=%r" % (index, mine, theirs))

    return problems


def is_violation(name):
    return name.lower().startswith(VIOLATION_PREFIXES)


# ============================================================
# FETCH
# ============================================================

def fetch(api_key=None, out_dir=DATA_DIR):
    """Download the class-matching dataset from Roboflow."""

    api_key = api_key or os.getenv("ROBOFLOW_API_KEY")

    if not api_key:
        print("Need a Roboflow API key (free): https://app.roboflow.com/settings/api")
        print("Then:  python eval_accuracy.py --fetch --roboflow-key YOUR_KEY")
        print("   or: set ROBOFLOW_API_KEY=... and re-run")
        return None

    try:
        from roboflow import Roboflow
    except ImportError:
        print("pip install roboflow")
        return None

    os.makedirs(out_dir, exist_ok=True)
    cwd = os.getcwd()

    try:
        os.chdir(out_dir)
        project = (Roboflow(api_key=api_key)
                   .workspace("roboflow-universe-projects")
                   .project("construction-site-safety"))

        # Version 27 is the YOLOv8 export whose class order matches the
        # model this project loads. If Roboflow retires it, take any
        # version and let the alignment gate below tell you if it fits.
        dataset = project.version(27).download("yolov8")
        print("downloaded to %s" % dataset.location)
        return os.path.join(dataset.location, "data.yaml")

    except Exception as e:
        print("fetch failed: %s: %s" % (type(e).__name__, e))
        return None

    finally:
        os.chdir(cwd)


# ============================================================
# EVALUATE
# ============================================================

def evaluate(data_yaml, split="test", model_path=None, conf=None, imgsz=640):
    """Run validation. Returns a results dict, or None if it could not run."""

    import yolo_trigger as yt

    model, label = (None, None)

    if model_path:
        from ultralytics import YOLO
        model, label = YOLO(model_path), model_path
    else:
        model, label = yt.load_model()

    model_names = {int(k): str(v) for k, v in model.names.items()}
    data_names = dataset_names(data_yaml)

    print("model:   %s" % label)
    print("dataset: %s" % data_yaml)
    print()

    problems = check_alignment(model_names, data_names)

    if problems:
        print("CLASS MISMATCH -- refusing to validate.\n")

        for problem in problems[:15]:
            print("  %s" % problem)

        print("\nUltralytics matches classes by INDEX, not name. Validating")
        print("across this mismatch would produce a precise, confident and")
        print("entirely meaningless number, so it is not done.\n")
        print("model  : %s" % ", ".join(model_names[i] for i in sorted(model_names)))
        print("dataset: %s" % ", ".join(data_names[i] for i in sorted(data_names)))
        print("\nUse a dataset whose classes match, or a model trained on this one.")
        return None

    print("class alignment OK -- %d classes match index for index" % len(model_names))
    print("validating on the %r split, this takes a minute...\n" % split)

    try:
        metrics = model.val(
            data=data_yaml, split=split, imgsz=imgsz,
            conf=conf if conf is not None else 0.001,   # standard mAP protocol
            verbose=False, plots=False,
        )

    except Exception as e:
        # Almost always a missing or misplaced split rather than a bug:
        # say which split and where it looked, not a traceback.
        print("\ncould not validate the %r split: %s: %s"
              % (split, type(e).__name__, str(e)[:200]))
        print("\n  Check that %s points at real images for that split." % data_yaml)
        print("  A Roboflow YOLOv8 export has train/ valid/ test/ beside the yaml;")
        print("  note it names the middle one 'valid', so use --split val only if")
        print("  the yaml maps it. Try --split test, or --split train to sanity check.")
        return None

    box = metrics.box

    if box.ap_class_index is None or len(box.ap_class_index) == 0:
        print("\nvalidation ran but matched no labelled instances -- the split is")
        print("empty, or the label files are not where the yaml says they are.")
        return None
    per_class = {}

    for position, class_index in enumerate(box.ap_class_index):
        name = model_names[int(class_index)]
        per_class[name] = {
            "precision": float(box.p[position]),
            "recall": float(box.r[position]),
            "map50": float(box.ap50[position]),
            "map50_95": float(box.ap[position]),
            "violation": is_violation(name),
        }

    return {
        "model": label,
        "data": data_yaml,
        "split": split,
        "imgsz": imgsz,
        "classes": len(model_names),
        "map50": float(box.map50),
        "map50_95": float(box.map),
        "precision": float(box.mp),
        "recall": float(box.mr),
        "per_class": per_class,
    }


def print_results(results):
    violations = {k: v for k, v in results["per_class"].items() if v["violation"]}

    print("overall")
    print("  mAP@50      %.3f" % results["map50"])
    print("  mAP@50-95   %.3f" % results["map50_95"])
    print("  precision   %.3f" % results["precision"])
    print("  recall      %.3f" % results["recall"])

    print("\nper class")
    print("  %-18s %9s %9s %9s" % ("", "precision", "recall", "mAP@50"))

    for name in sorted(results["per_class"]):
        row = results["per_class"][name]
        print("  %-18s %9.3f %9.3f %9.3f%s"
              % (name, row["precision"], row["recall"], row["map50"],
                 "   <- fires the trigger" if row["violation"] else ""))

    if violations:
        mean_recall = sum(v["recall"] for v in violations.values()) / len(violations)
        mean_precision = sum(v["precision"] for v in violations.values()) / len(violations)

        print("\nviolation classes only -- the ones the trigger acts on")
        print("  mean recall     %.3f   (a miss is an unflagged hazard)" % mean_recall)
        print("  mean precision  %.3f   (a false positive is alert fatigue)" % mean_precision)


# ============================================================
# REPORT
# ============================================================

def write_report(results, path=REPORT):
    out = []
    add = out.append

    violations = {k: v for k, v in results["per_class"].items() if v["violation"]}

    add("# Measured accuracy — PPE detection")
    add("")
    add("Every number here came from running `model.val()` on %s."
        % datetime.now().strftime("%Y-%m-%d"))
    add("Regenerate with:")
    add("")
    add("```")
    add("python eval_accuracy.py --data %s --split %s"
        % (results["data"], results["split"]))
    add("```")
    add("")
    add("| | |")
    add("|---|---|")
    add("| Model | `%s` |" % results["model"])
    add("| Dataset | `%s` |" % results["data"])
    add("| Split | `%s`, %d classes |" % (results["split"], results["classes"]))
    add("| Image size | %d |" % results["imgsz"])
    add("")
    add("Class names were verified to match the model's index for index before")
    add("validating. Ultralytics compares classes by index and never checks the")
    add("names, so without that gate a mismatched dataset returns a confident")
    add("and meaningless number.")
    add("")

    add("## Overall")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add("| mAP@50 | **%.3f** |" % results["map50"])
    add("| mAP@50-95 | %.3f |" % results["map50_95"])
    add("| Precision | %.3f |" % results["precision"])
    add("| Recall | %.3f |" % results["recall"])
    add("")

    add("## Per class")
    add("")
    add("| Class | Precision | Recall | mAP@50 | Trigger acts on it |")
    add("|---|---|---|---|---|")

    for name in sorted(results["per_class"]):
        row = results["per_class"][name]
        add("| `%s` | %.3f | %.3f | %.3f | %s |"
            % (name, row["precision"], row["recall"], row["map50"],
               "**yes**" if row["violation"] else "—"))

    add("")

    if violations:
        mean_recall = sum(v["recall"] for v in violations.values()) / len(violations)
        mean_precision = sum(v["precision"] for v in violations.values()) / len(violations)

        add("## The classes that actually matter")
        add("")
        add("The trigger fires on `NO-*` classes and nothing else, so the")
        add("headline mAP — which averages in `vehicle` and `machinery` — is not")
        add("the number to judge this system by.")
        add("")
        add("| | Value | Why it matters |")
        add("|---|---|---|")
        add("| Mean recall, violation classes | **%.3f** | A miss is a hazard nobody was told about |"
            % mean_recall)
        add("| Mean precision, violation classes | **%.3f** | A false positive is alert fatigue |"
            % mean_precision)
        add("")

        worst = min(violations.items(), key=lambda kv: kv[1]["recall"])
        add("Weakest violation class by recall: `%s` at **%.3f** — it misses"
            % (worst[0], worst[1]["recall"]))
        add("roughly %.0f%% of true instances. Say that out loud rather than"
            % ((1 - worst[1]["recall"]) * 100))
        add("quoting the aggregate.")
        add("")

    add("## Limits")
    add("")
    add("- **The model's training data is not known to us.** These weights came")
    add("  from a public checkpoint. If it was trained on this dataset's train")
    add("  split then the test split is a fair held-out measure; if it was")
    add("  trained on something overlapping this split, these numbers are")
    add("  optimistic. This cannot be verified from the checkpoint alone.")
    add("- **This is not your bay.** The dataset is construction sites. Lighting,")
    add("  camera angle, and what people wear in the room you demo in are a")
    add("  different distribution. Treat these as an upper bound.")
    add("- **Detection is not the whole trigger.** These figures are per frame.")
    add("  The gate requires the same violation on several consecutive frames,")
    add("  which suppresses isolated false positives and delays true ones — so")
    add("  end-to-end behaviour is better on precision and slightly worse on")
    add("  latency than the per-frame numbers suggest.")
    add("")

    text = "\n".join(out) + "\n"

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    return path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Precision/recall for the PPE detector on a labelled dataset.")
    parser.add_argument("--data", default=None, help="path to the dataset data.yaml")
    parser.add_argument("--split", default="test", choices=["test", "val", "train"])
    parser.add_argument("--model", default=None,
                        help="weights to evaluate (default: what yolo_trigger loads)")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--fetch", action="store_true",
                        help="download the class-matching Roboflow dataset")
    parser.add_argument("--roboflow-key", default=None)
    parser.add_argument("--no-write", action="store_true",
                        help="do not write EVAL-ACCURACY.md")
    parser.add_argument("--json", action="store_true", help="dump raw results as JSON")

    args = parser.parse_args(argv)

    data = args.data

    if args.fetch:
        data = fetch(args.roboflow_key) or data

    if not data:
        print(__doc__)
        return 1

    if not os.path.exists(data):
        print("no such dataset yaml: %s" % data)
        return 1

    results = evaluate(data, split=args.split, model_path=args.model, imgsz=args.imgsz)

    if results is None:
        return 1

    print_results(results)

    if args.json:
        print("\n" + json.dumps(results, indent=2))

    if not args.no_write:
        print("\nwrote %s" % write_report(results))

    return 0


if __name__ == "__main__":
    sys.exit(main())

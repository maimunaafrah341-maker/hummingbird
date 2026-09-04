# Measured accuracy — PPE detection

Every number here came from running `model.val()` on 2026-09-04.
Regenerate with:

```
python eval_accuracy.py --data C:\Users\Maimuna Afrah\OneDrive\Desktop\hummingbiird\datasets\Construction-Site-Safety-27\data.yaml --split test
```

| | |
|---|---|
| Model | `hf:Hansung-Cho/yolov8-ppe-detection:best.pt` |
| Dataset | `C:\Users\Maimuna Afrah\OneDrive\Desktop\hummingbiird\datasets\Construction-Site-Safety-27\data.yaml` |
| Split | `test`, 10 classes |
| Image size | 640 |

Class names were verified to match the model's index for index before
validating. Ultralytics compares classes by index and never checks the
names, so without that gate a mismatched dataset returns a confident
and meaningless number.

## Overall

| Metric | Value |
|---|---|
| mAP@50 | **0.708** |
| mAP@50-95 | 0.394 |
| Precision | 0.840 |
| Recall | 0.634 |

## Per class

| Class | Precision | Recall | mAP@50 | Trigger acts on it |
|---|---|---|---|---|
| `Hardhat` | 0.966 | 0.783 | 0.893 | — |
| `Mask` | 0.976 | 0.750 | 0.772 | — |
| `NO-Hardhat` | 0.765 | 0.477 | 0.501 | **yes** |
| `NO-Mask` | 0.839 | 0.506 | 0.605 | **yes** |
| `NO-Safety Vest` | 0.877 | 0.633 | 0.747 | **yes** |
| `Person` | 0.856 | 0.707 | 0.801 | — |
| `Safety Cone` | 0.726 | 0.359 | 0.403 | — |
| `Safety Vest` | 0.828 | 0.820 | 0.886 | — |
| `machinery` | 0.777 | 0.773 | 0.831 | — |
| `vehicle` | 0.789 | 0.537 | 0.637 | — |

## The classes that actually matter

The trigger fires on `NO-*` classes and nothing else, so the
headline mAP — which averages in `vehicle` and `machinery` — is not
the number to judge this system by.

| | Value | Why it matters |
|---|---|---|
| Mean recall, violation classes | **0.539** | A miss is a hazard nobody was told about |
| Mean precision, violation classes | **0.827** | A false positive is alert fatigue |

Weakest violation class by recall: `NO-Hardhat` at **0.477** — it misses
roughly 52% of true instances. Say that out loud rather than
quoting the aggregate.

## Limits

- **The model's training data is not known to us.** These weights came
  from a public checkpoint. If it was trained on this dataset's train
  split then the test split is a fair held-out measure; if it was
  trained on something overlapping this split, these numbers are
  optimistic. This cannot be verified from the checkpoint alone.
- **This is not your bay.** The dataset is construction sites. Lighting,
  camera angle, and what people wear in the room you demo in are a
  different distribution. Treat these as an upper bound.
- **Detection is not the whole trigger.** These figures are per frame.
  The gate requires the same violation on several consecutive frames,
  which suppresses isolated false positives and delays true ones — so
  end-to-end behaviour is better on precision and slightly worse on
  latency than the per-frame numbers suggest.


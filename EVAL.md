# Measured behaviour — `language.py`

Every number here came from running the code, on 2026-09-03, on the
development laptop (Windows 11, CPU only). No number in this file is an
estimate. Re-run the measurements before quoting them on different
hardware — the ratios hold, the absolute values will not.

## Cost

| What | Measured |
|---|---|
| `import language` | **0.006 s**, model not loaded |
| Native-script detection (hi/te/ur/bn) | **< 0.01 ms**, model still not loaded |
| First Latin-script detection (triggers the load) | **22.4 s** warm disk, **62 s** first ever run |
| Steady-state detection, any input | **21–28 ms** |
| Peak RSS with the model resident | **821 MB** |
| Peak RSS without `sentence-transformers` installed | ~17 MB |

The RSS breakdown, since it decides where this can be hosted:

```
baseline python      :   17 MB
+ torch imported     :  189 MB   (3.2s)
+ model loaded       :  734 MB   (20.7s)
+ first encode       :  821 MB   (0.1s)
```

**The model, not the code, is the entire hosting constraint.** 821 MB
does not fit on a 512 MB free instance, and no amount of tuning the
Python changes that.

## The two tiers, and why the split exists

Tier 1 is Unicode range counting. Pure Python, no dependencies, no
model, and *decisive* — a character is in the Devanagari block or it is
not. It resolves Hindi, Telugu, Urdu and Bengali written in their own
scripts, which is most real traffic.

Tier 2 only runs on Latin-script text, where the actual question is
"English, or romanized Hindi, or romanized Telugu?" and script tells you
nothing. That is the only question worth 821 MB.

Because the model loads lazily and only tier 2 needs it, a deployment
that never sees romanized input never pays for it, and a deployment
that cannot afford it at all still answers tier-1 questions correctly.

## Degraded mode

If `sentence-transformers` is missing, the box is out of memory, or the
weights cannot be fetched, `_ensure_model()` catches it, logs **once**,
caches the failure, and the module keeps serving:

```
[language] semantic tier unavailable (ImportError: No module named
sentence_transformers) -- running script detection only; Latin-script
text will return 'en'
```

Verified by blocking the import at runtime:

| Input | Result in degraded mode |
|---|---|
| `मुझे मदद चाहिए` | `hi` — correct, < 0.01 ms |
| `আমার সাহায্য দরকার` | `bn` — correct, < 0.01 ms |
| `Mujhe madad chahiye` | `en` — **degraded**, would be `hi` with tier 2 |
| `""` | `en` |

`semantic_tier_available()` returns `True`, `False`, or `None` (nothing
has needed it yet) so a `/health` endpoint can report which tiers are
actually live instead of asserting both.

## Known edge cases

These are characterised, not hypothetical. Each was found by testing.

| Input | Returns | Why |
|---|---|---|
| `""` or `"   "` | `en` | Explicit guard before any work |
| Tamil / Kannada / Malayalam / Gurmukhi / Gujarati / Odia | `en` | **Deliberate.** These scripts have real Unicode ranges but no support here. Sending them to tier 2 produced a *confident wrong answer* — native Tamil scored 100% Hindi in live testing on 2026-08-22, and the user got a romanized Hindi reply to Tamil input. A safe default beats a confident mistake. |
| Arabic text | `ur` | Accepted tradeoff. Urdu and Arabic share codepoints. For an Indian helpline, Arabic-script input is overwhelmingly more likely to be Urdu. |
| Bengali ending in `।` | `bn` | Was `hi`. The danda (U+0964) is pan-Brahmic punctuation reused by Bengali, so one character of a pure-Bengali sentence registered as Devanagari and short-circuited before `bengali_count` was read. Fixed by excluding U+0964/U+0965 from the Devanagari count. Found live 2026-08-29. |
| `12345`, `😰😰😰` | `en` | Reaches tier 2, no language leads English by `NEUTRAL_MARGIN` |
| `Mera husband hits me every day` | `en` | Code-switched input resolves to the dominant language. Correct here, but genuinely ambiguous input is a known weak spot. |
| Short ambiguous Latin text | `en` | `NEUTRAL_MARGIN = 0.04` — a non-English score must *lead* English by this much to override the default. Without it, argmax picks a confident wrong answer surprisingly often. |

## Accuracy limits — read before claiming coverage

Romanized detection is materially weaker than native-script detection,
and the gap is not uniform across languages. In the parent project's
eval set, romanized Hindi scores around **97.0** and romanized Telugu
around **77.7** on the same measure.

Native script: effectively exact, it is a range check.
Romanized: good for Hindi, noticeably weaker for Telugu.

Say that out loud rather than claiming "5 languages supported" flat.
The honest claim is: *five languages in native script, two of them also
in romanized form, one of those two well.*

## Divergence from the parent project

`detect_script()` here handles Urdu and Bengali native script. Athena's
copy only knows the Devanagari and Telugu ranges, so `ur` and `bn` fall
through to `"latin"` even for plainly native text — those languages were
added to `detect_language()` later and `detect_script()` was not
extended with them. Caught by the extraction test on 2026-09-03.

Fixed here and not there, deliberately: changing it in Athena changes
which script its live replies come back in, which is a product decision
rather than a refactor.

Regression check against the parent implementation: **11 of 13 test
inputs identical**, the 2 differences being exactly this fix.

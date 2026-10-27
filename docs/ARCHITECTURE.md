# LoadMaster architecture

One photo in, rule verdicts out. Every layer has one job, and the
layers that can be wrong are never allowed to be quietly wrong:
they degrade to COULD NOT TELL with the reason attached.

```
phone photo + declared inputs (bed ft, load ft, load type, weight?)
        |
        v
SAM 3.1 segmentation (sam_client.py, prompt_packs.py)
  prompt packs keyed to trailer type x load type x photo role;
  all prompts run in parallel with retry-with-backoff; a timeout
  is never reported as "not found"
        |
        v
detection model (mask_decode.py, detections.py)
  masks decoded to boxes + polygons; wheel boxes are filtered to
  AXLE ESTIMATES (size + bottom-band), never a wheel count;
  every detection is editable (tap-to-correct) and carries its
  provenance: model-found, user-added, user-removed, bonus-only
        |
        +------------------------+------------------------+
        v                                                 v
measurements (measure.py)                    cross-check (crosscheck.py)
  bed-length calibration: pixels to            Muse Spark reads the SAME photo
  inches, with honest refusal when             independently (no SAM results
  the scale cannot be supported;               shown to it) and answers a
  overhang, centroid vs axle with              fixed question set per role
  uncertainty, tarp %, tie-down gaps           (counts, blocked front, lights,
                                               plate, straps over edges)
        |                                                 |
        +------------------------+------------------------+
                                 v
rules engine (rules.py) - deterministic, no LLM inside
  the 8 checks of 49 CFR 393; every verdict carries its evidence
  (mask region, measurement, citation) and a minimum fix;
  cross-check views feed checks 5 and 6 and can only DEMOTE a
  verdict (PASS -> CHECK THIS), never upgrade one
                                 |
                                 v
report (report.py)
  Muse Spark writes the plain-words summary STRICTLY from the
  verdict JSON (grounding prompt forbids invented numbers);
  any model failure falls back to a deterministic template;
  the report layer never raises
                                 |
                                 v
web app (webapp.py, web_ui.html, modal_app.py)
  FastAPI on Modal; /analyze (full pipeline), /verdicts (rules
  only, no SAM re-run, powers tap-to-correct recheck + the
  WHAT CHANGED delta), /gallery (real captured runs), /health
```

## Decisions that shaped the design

**Tie-down counting basis.** Measured during development
(docs/SAM_PROMPT_MATRIX.md, 31 live calls): straps are invisible
to SAM at full-frame distance but read at 0.79 confidence in an
anchor close-up. So the count comes only from close-up detections
plus the hauler's own tap marks. Full-frame strap signals are kept
as bonus_only and can never raise the count. The system
under-counts honestly rather than guessing straps it cannot see;
the first real user run proved the point (6 real straps, 4 tapped,
2 more taps flipped the verdict to PASS).

**Demote-only cross-check.** Two models disagreeing is
information, not a tie to break by confidence. Spark's read can
move PASS to CHECK THIS and can supply the only evidence for
checks segmentation cannot judge (edge protection, lights and
plate); it can never turn a failure into a pass.

**Refusal as a feature.** Calibration refuses when the bed mask
cannot support a scale. Weight placement is labeled a visual
estimate and can never PASS. Checks that need tension, ratings,
or weight return COULD NOT TELL and name the missing input. A
wrong PASS is the worst output this product can produce, so the
engine is built to prefer silence over a guess.

**Facts vs words.** The rules engine owns every fact (counts,
inches, verdicts). Muse Spark owns only the prose and the
independent second read. The report prompt is grounded in the
verdict JSON, and grading the report's numbers against the
verdicts is covered by tests with Spark stubbed.

## Testing

142 pytest tests: parser and decoding contracts, measurement
refusals, every rules branch (including the 5 ft / 1,100 lb
tie-down exception and demote-only cross-check behavior), report
grounding and fallback, the web layer's cross-check wiring, and
the gallery's captured-run shape. Live behavior is verified
against the deployed app, not only locally.

## Photo sets (Oct 26)

A run may carry several photos of the same load. The web layer picks
the primary set (first side view) for measurements, the overlay, and
tap-to-correct; every set goes to the rules engine, which is where
the per-role evidence rules live (close-ups ground the tie-down
count, the rear view grounds lights and plate). Recheck requests
carry all sets plus the cross-check records, so corrections are
judged against the same evidence as the original run.

## Intake and abuse guard (Oct 26)

Uploads are validated, EXIF-transposed (phone photos are often
stored sideways; SAM and the measurement layer must see the photo
upright), downscaled to a 2048 px edge, and capped at 25 MB. The
analyze endpoint is rate limited per client IP (30 runs/hour,
in-memory): the public judging URL spends from a shared, capped API
credit pool, and the limit is the difference between a judge trying
the app and a script draining it.

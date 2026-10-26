# Build log

Newest first. Every entry is work done inside the hackathon window,
Oct 26 - Nov 4, 2026.

## Oct 26, 2026 - Day 2: calibrated measurement layer + overlay fix

(Pulled forward from the Oct 28 plan slot.)

Built:
- `loadmaster/measure.py` - the Tier 1.2 measurement layer.
  Calibration comes from the user-supplied bed length (once per run)
  and the trailer_bed mask's tight extent along x, giving
  pixels-per-inch. On top of that: rear overhang in inches (rearmost
  cargo bitmap edge vs the bed's rear edge), load centroid vs axle
  (bitmap-weighted cargo centroid against the axle estimate, in
  inches and % of bed length, with an uncertainty range of one
  bitmap-weighted standard deviation of the cargo pixels, floored at
  2 in), tarp coverage for loose-material loads (tarp/canvas bitmap
  union overlapping the cargo union, % covered and % exposed), and
  tie-down coverage gaps (anchor centres mapped onto the bed axis,
  spans between anchors and the bed ends, largest gap in inches).
  Every measurement object carries value, unit, method, source
  detection ids, and a confidence basis, in a JSON shape consistent
  with the Day 1 detection records. Bitmap statistics are computed
  run by run from the RLE, never per-pixel in Python and never from
  polygons.
- `loadmaster/pipeline.py` - optional measurement stage:
  `--bed-length-ft N` writes `<photo>_measurements.json` alongside
  the detections and records the length plus a per-measurement
  status summary in the run record.

Honesty rules, enforced in code (each is a test case):
- No bed length or no trailer_bed detection -> calibration fails
  and every inch measurement reports "could_not_measure" with the
  reason named. A bed that is taller than wide in frame is also
  refused (the side-view x-axis mapping does not apply).
- The centroid figure is labeled a visual estimate with the flags
  `visual_estimate` and `not_a_weight_measurement` wherever it
  appears. A photo shows where the bulk sits, not how heavy it is.
- Tarp coverage carries `proxy_only` (matrix finding: tarp prompts
  are a proxy, not yet validated on a real tarp-over-load photo).
  With no tarp detected on a loose load it reports 0% covered as an
  ESTIMATE, with the basis stating that a missed detection is not
  proof of absence.
- Tie-down gaps degrade when anchor positions cannot be placed on
  the bed axis - the normal case, since counting anchors live in
  separate close-up photos whose coordinates cannot be mapped onto
  the side photo. The measurement says exactly that.

Overlay / polygon fix (the Day 1 firewood overlay was misleading):
- Root cause 1, traced wrong: `MaskRaster.polygon()` traced
  whichever component held the first set pixel in scan order and
  stopped at the first re-entry of the start pixel. The trailer
  bitmap has 3 components (measured): the body, 1,228,045 px (98%),
  plus strays of 20,661 px and 4,889 px. The shipped polygon traced
  the 4,889 px stray - a 230x38 px sliver at the mask's top edge.
  Fix: trace the LARGEST component, with a proper backtrack state
  (current pixel + direction to the last background pixel examined)
  and stop when a state repeats (Jacob's criterion in state form).
  A first attempt that compared arrival directions failed the
  L-shape recall test at 71% (the walk looped and the stride
  sampler cut chords through the interior); the state-based walk
  passes at >=95% recall. Hair-thin components fall back to their
  tight bbox instead of a 2-point "polygon" (the Day 1 degenerate
  log polygon). The stored trailer polygon now spans
  x 229-3263, y 1208-2135, against the bitmap body's tight bbox
  x 226-3263, y 1208-2138.
- Root cause 2, fills never rendered: the Day 1 overlay pasted an
  alpha-0 fill through an L mask, which leaves the layer's alpha at
  0 - proven by sampling the shipped PNG: at the trailer bitmap's
  centroid the overlay pixel was identical to the source photo, and
  only ~109k pixels differed in the whole image (boxes, labels,
  outlines). What read as a colour wash was the broken outline
  path doing all the visual work. Fix: the bitmap is the fill
  tile's alpha channel (110 where set, 0 elsewhere), composited
  with alpha_composite, so fills hug the bitmap exactly.
  Verification on the re-rendered firewood overlay: 0 changed
  pixels in rows 0-816 (the sky and treetops), 1.18M changed pixels
  concentrated in the load band, and a visual pass confirms the log
  tints follow the log shapes with the sky clean. All three stored
  example detection sets had their polygons recomputed from the
  stored bitmaps and their overlays re-rendered with the fixed
  code (no new SAM calls for those two).

Tests: 36 passed (pytest, foreground run) - the 16 Day 1 tests plus
20 new: calibration math and its three refusal paths, overhang past
the bed edge and at zero, centroid behind/ahead of the axle and its
degradation without an axle, tarp at 50% / absent / not-applicable,
tie-down gap spans and their degradation, report JSON provenance,
polygon largest-component + recall + non-degeneracy, and an overlay
test asserting the fill tints bitmap pixels only.

Proof run (live pipeline, firewood photo, `--bed-length-ft 8` -
an ASSUMED bed length for this dev photo, recorded in the run
record, not measured ground truth): detections reproduced Day 1
exactly (trailer 0.863, 7 log masks, 1 axle estimate, full-frame
strap empty). Measurements: rear overhang 0.0 in (no log reaches
the bed's rear edge); centroid estimate 22.7 in behind the axle
+/- 19.7 in, centroid at 50.9% of bed vs the axle at 27.2%; tarp
not applicable (firewood); tie-down gaps could_not_measure (no
in-frame anchors, as designed). One caveat recorded honestly:
SAM's trailer mask on this photo runs to the frame's right edge
(it takes in the pickup bed behind the trailer), so the calibrated
extent is the mask's extent, not a tape-measured bed - the layer
measures the mask it is given, and tap-to-correct is the remedy
when a mask overreaches.

## Oct 26, 2026 - Day 1: SAM pipeline, overlay, editable detections

(Pulled forward from the Oct 27 plan slot.)

Built:
- `loadmaster/sam_client.py` - SAM 3.1 client on the Day 0 call
  pattern (Responses API, one noun phrase per call, image resized to
  1024 px, connector credential via dynamic_credentials; the key is
  never read or stored). Retry with backoff, max 3 attempts: a
  failure after retries returns status "error", never an empty
  result. A whole prompt pack runs in parallel (thread pool).
- `loadmaster/prompt_packs.py` - prompt packs keyed by trailer type,
  load type, and photo role (side / rear three-quarter / anchor
  close-up), each entry citing its matrix finding. Full-frame strap
  prompts are flagged bonus-only; the close-up pack is the tie-down
  counting source.
- `loadmaster/mask_decode.py` - output text -> boxes, bitmaps,
  polygons, RLE.
- `loadmaster/detections.py` - the editable detection model
  (tap-to-correct): add / remove / move / set-bbox, JSON round-trip,
  provenance on every detection, and the wheel filter that collapses
  filtered wheel boxes into axle estimates without ever exposing a
  raw wheel count.
- `loadmaster/overlay.py` - annotated overlay renderer (Pillow):
  translucent masks, boxes, labels.
- `loadmaster/pipeline.py` - `python -m loadmaster.pipeline --photo
  ... --trailer-type ... --load-type ... --role ... --out-dir ...`
  writes detections JSON, overlay PNG, and a per-prompt run record.

Mask decode route: Meta's `meta-sam-parser` package, NOT a
hand-rolled decoder. The one_bit payload is base85 over an
arithmetic-coded bitstream; the parser is the sane way to read it.
Verified against the Day 0 proof output before use: 1 box, 1 mask at
0.863, raster 953x324, 123,346 set pixels. Note: the parser
normalises box coordinates to exclusive bounds (the raw token's
x2=1023 comes back as 1024), which matches the mask raster extent.
Fallback if the package is missing: box tokens still parse by regex
and masks are carried as undecodable payloads, never dropped.

Tests: 16 passed (pytest, foreground run) - synthetic mask
bitmap/polygon/RLE, the Day 0 fixture decode, detection
add/remove/move/round-trip, and the wheel filter including
fragment/high-box rejection. No live API in tests.

Proof runs (10 live SAM calls, outputs in examples/output/):
- Utility trailer + firewood, side view: "trailer" 1 mask at 0.863
  (identical to Day 0), "logs" 7 masks at 0.59-0.70, wheel filtered
  to 1 axle estimate at 0.918, full-frame "ratchet strap" EMPTY,
  exactly as the matrix predicts (bonus signal only at this
  distance). One small log mask produced a degenerate 2-point
  polygon; its bitmap/RLE is intact and is the source of truth.
- Ratchet strap anchor close-up: 1 tie-down detection at 0.797 -
  the close-up counting path works (matrix: 0.79).
- Lowboy + excavator, rear view: "flatbed trailer" 1 mask at 0.675
  (the prompt keyed to trailer type did its job), "excavator" 0.610,
  "chain" 1 presence hint at 0.540, 7 raw wheel boxes filtered to 2
  axle estimates. "ratchet strap" returned 2 boxes at this rear
  distance (0.77 / 0.52); they are recorded as bonus-only and never
  counted, per the matrix design rule.
- No prompt errored in any run; every call succeeded on attempt 1.
  Latency was 2-6 s per call in parallel, well under the matrix's
  15-20 s sequential figures.

## Oct 26, 2026 - Day 0 (continued)

- SAM prompt matrix: 31 live calls over 5 development photos
  (docs/SAM_PROMPT_MATRIX.md). Headlines: trailer, wheel, and specific
  cargo prompts work; straps are invisible to SAM at full-frame
  distance but read at 0.79 in an anchor close-up, so tie-down counting
  is designed around close-ups + tap-to-correct; 1 timeout in 31 calls,
  recovered on retry, so the pipeline retries with backoff.

## Oct 26, 2026 - Day 0

- Repository created (public, MIT) under littlestjames82-sys.
- Meta Model API access proven before any product code:
  - Models list returned Muse Spark 1.3 and SAM 3.1 (live model id
    spelling: sam-3.1).
  - First SAM 3.1 segmentation call (Responses API, prompt "trailer")
    on a development photo of a loaded utility trailer: HTTP 200,
    1 bounding box, 1 mask, confidence 0.863.
  - Muse Spark test call answered as expected.
- Scope set: SAM geometry + deterministic 49 CFR 393 rules engine +
  Muse Spark report, with evidence-linked verdicts, calibrated
  measurements, fix-and-recheck with a before/after delta, and
  tap-to-correct detections. Full plan in the commit history from
  Day 1 on.

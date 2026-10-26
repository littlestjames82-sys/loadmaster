# Build log

Newest first. Every entry is work done inside the hackathon window,
Oct 26 - Nov 4, 2026.

## Oct 26, 2026 - Working app: hosted service + mobile UI (Day 4, pulled forward)

Ryan rejected the static phone preview as a mockup and asked for
the real app on his phone, so the hosted app was built the same day:

- `loadmaster/webapp.py` - a FastAPI service around the existing
  pipeline (nothing in the pipeline was changed to serve it):
  - `GET /` serves the mobile UI. `GET /health` returns {"ok": true}.
  - `POST /analyze` (multipart photo + trailer_type, load_type,
    photo_role, optional bed_length_ft / load_length_ft / weight_lb)
    runs the real pipeline via pipeline.run_pipeline: SAM prompt
    pack in parallel, decoded masks, detections, annotated overlay
    (returned as base64 JPEG), Day 2 measurements when a bed length
    is given, Day 3 verdicts when a load length is given. SAM prompt
    failures surface per prompt in run.prompts and in warnings -
    never as a silent empty result.
  - `POST /verdicts` (JSON: detections + measurements + inputs)
    recomputes ONLY the rules verdicts - no SAM calls. This is the
    tap-to-correct + instant recheck path.
  - The rules engine is imported lazily through an adapter: the web
    layer passes it a list of detection sets (its native shape),
    maps the UI's cargo load types (firewood, steel, ...) onto the
    engine's load classes (general / heavy_wheeled_tracked /
    loose_material), and normalizes verdicts for the UI. When the
    engine is absent or fails, verdicts are omitted and the warnings
    say why.
- `loadmaster/web_ui.html` - the mobile UI, evolved from the
  preview's design: photo picker with camera capture, trailer /
  load / view pickers, bed length + load length + optional weight,
  progress state, overlay with findings list (tap to highlight),
  verdict cards (status, CFR citation, finding, minimum fix),
  measurements panel, and tap-to-correct - add a missed tie-down by
  tapping the photo, or remove a finding - which calls /verdicts and
  updates the verdict cards without re-running the analysis. The
  honesty line is on the page. Vanilla JS, no CDN.
- `loadmaster/sam_client.py` - the one credential change: when the
  META_API_KEY environment variable is set it is sent as a Bearer
  header; otherwise the connector dynamic_credentials path is used
  exactly as before (and its import is now optional so the module
  loads where no connector exists). The key is never printed or
  stored.
- `modal_app.py` (repo root) - the Modal deployment: app
  "loadmaster", image built from requirements.txt with the
  loadmaster package included, the FastAPI app exposed via
  @modal.asgi_app(), 300 s timeout, and the API key supplied by the
  Modal secret named "meta-model-api" (env var META_API_KEY).
- requirements.txt gained fastapi, uvicorn, python-multipart, modal.

Local live proof (this VM, real API credential via the connector;
server run with uvicorn on 127.0.0.1, then shut down):
- POST /analyze, firewood dev photo, utility / firewood / side,
  bed 8 ft, load 6 ft: HTTP 200 in 13.1 s total. 9 detections
  (trailer bed, 7 log cargo masks, 1 axle estimate); all 4 SAM
  prompts ok or honestly empty on attempt 1 (full-frame "ratchet
  strap" empty, as the Day 0 matrix says it must be); 4
  measurements returned; 6 verdicts returned (heavy equipment and
  tarp coverage correctly not applicable to this load) including
  forward_blocking CHECK_THIS and weight_placement CHECK_THIS with
  CFR citations; overlay returned; zero warnings.
- POST /verdicts with two user tie-down marks injected into that
  detection set: HTTP 200, 0.02 s, no SAM calls. tiedown_count
  flipped from COULD_NOT_TELL to PASS ("2 tie-downs counted;
  393.110 requires 2 for a 6 ft load"); aggregate_wll stayed
  COULD_NOT_TELL, honestly, for want of ratings and weight.
- GET / returned the UI with all key element ids present.
- Full pytest suite re-run after these changes: 89 passed.

Deploy readiness: modal_app.py imports cleanly against modal
1.6.2 and is deploy-ready, but NO deploy was run - this VM has no
Modal CLI auth yet. Remaining human steps: `modal setup` (or token)
on an authenticated machine, Ryan creates the "meta-model-api"
secret with his Meta API key (`modal secret create meta-model-api
META_API_KEY=...`; the key never passes through Muse), then
`modal deploy modal_app.py`.

## Oct 26, 2026 - Day 3: deterministic rules engine + dual-model cross-check

(Pulled forward from the Oct 29 plan slot.)

Built:
- `loadmaster/rules.py` - the deterministic 49 CFR 393 rules
  engine. No LLM calls, no randomness, no network: the same
  detections + measurements + user inputs always produce the same
  verdicts. Inputs: Day 1 detection sets (one per photo), the Day 2
  measurements, and the user's declared inputs (load length, load
  type in the three spec categories, optional weight, optional
  tie-down WLL ratings as one uniform number or a per-tie-down
  list). Output: one verdict object per applicable check -
  check_id, CFR citation, status (PASS / CHECK_THIS /
  COULD_NOT_TELL), plain-words finding, evidence (detection ids +
  measurement names), minimum fix for every CHECK_THIS,
  confidence basis, and a risk_rank.
- `loadmaster/crosscheck.py` - the Tier 2.5 dual-model
  cross-check. Muse Spark reads a photo INDEPENDENTLY (it is never
  told what SAM found) and answers a small structured question set
  per photo role: close-up (how many distinct tie-downs; any strap
  over a sharp edge without a protector), rear (tail lights
  visible? plate visible? tarp covering?), side (rear-heavy?
  blocked at the front?). Answers are parsed into plain fields;
  the raw answer is kept on the result for audit. Same Responses
  endpoint and connector credential path as the SAM client.
- `loadmaster/pipeline.py` - optional Day 3 stage: `--rules` with
  the user inputs on the command line writes
  `<photo>_verdicts.json`; `--crosscheck` runs Spark's independent
  read of the photo first and feeds its view into the verdicts.

Engine design choices, recorded so they are auditable:
- Not-applicable handling: a check that does not apply to the load
  at all (heavy equipment on a general load, tarp on a non-loose
  load, 393.130 when the machine is under its 10,000 lb trigger)
  produces NO verdict object; it is listed with its reason under
  "not_applicable". COULD_NOT_TELL is reserved for checks that
  apply but cannot be decided from the evidence - marking a
  not-applicable check COULD_NOT_TELL would fake an evidence gap.
- Tie-down count basis: anchor close-up detections plus user-added
  (tap-to-correct) tie-down marks ONLY. Full-frame strap detections
  are bonus_only and never count (matrix finding). If no close-up
  set and no user marks were supplied, the check is COULD_NOT_TELL
  with the reason, even though the required number is known.
- 393.110 at <=5 ft branches on weight (1 tie-down at <=1,100 lb,
  2 above). With the weight un-supplied the verdict follows the
  observed count honestly: 2+ observed passes either way, 1
  observed is COULD_NOT_TELL with the assumption path stated, 0
  observed fails both branches. Above 5 ft the required count
  never depends on weight, so no weight is needed there.
- Aggregate WLL (393.106/393.108) is computable only from
  user-supplied weight AND ratings; a direct tie-down counts half
  its rating, an over-the-top tie-down counts in full. The finding
  says in so many words that a photo cannot verify ratings or
  weight.
- Forward blocking is derived deterministically in the engine
  from the Day 2 calibration (bed front edge, pixels-per-inch)
  and the frontmost cargo mask edge: within 6 in of the bed front
  passes (threshold constant in rules.py), a larger open gap is
  CHECK_THIS. Without calibration it degrades to COULD_NOT_TELL.
- Overhang and lights/plate are ONE verdict whose finding always
  separates the two halves: overhang from the Day 2 measurement,
  lights/plate only from the cross-check's rear read (SAM has no
  such signal). The combined status is the worse of the two
  halves, honestly.
- Weight placement can NEVER pass: it is a visual estimate only.
  Bulk behind the axle by more than the estimate's own
  uncertainty is CHECK_THIS; anything else is COULD_NOT_TELL with
  the estimate stated in the finding.
- Risk ranking (1 = fix first): forward blocking, tie-down
  count, heavy equipment, overhang/lights-plate, weight
  placement, tarp, edge protection, aggregate WLL.
- Cross-check application: Spark's view is attached to a verdict
  as a cross_check record {spark_view, sam_view, agreement}. A
  disagreement over something Spark can see demotes a PASS to
  CHECK_THIS with both views in the finding. Nothing is ever
  upgraded: an agreeing Spark cannot turn a failure into a pass.

Cross-check live smoke (2 Spark image calls, Oct 26; image input
works on the Responses endpoint with the same data-URL shape as
SAM - the Day 0 Spark proof was text-only, this is the first live
image read):
- Ratchet-strap anchor close-up: HTTP 200, one attempt, 12.1 s.
  Raw answer: {"tiedown_count": 2, "straps_over_sharp_edge_
  without_protection": "unclear"}. Note the honest disagreement:
  SAM's close-up segmentation found 1 tie-down in this photo
  (0.797, Day 1); Spark counted 2. Neither is ground truth - this
  is exactly the disagreement the cross-check exists to surface.
- Lowboy rear view: HTTP 200, one attempt, 10.4 s. Raw answer:
  {"tail_lights_visible": true, "license_plate_visible": false,
  "tarp_covering_load": "no"}. Spark reports the plate not
  visible on this rear shot of a semi rig.
- A third live read ran inside the wired pipeline example below:
  the same close-up again counted 2 tie-downs, and the edge
  question came back "yes" this time (it was "unclear" in the
  smoke call) - real run-to-run variance in the vision read,
  recorded here rather than smoothed over. The engine treats a
  "yes" as CHECK_THIS evidence and an "unclear" as no evidence,
  so variance moves a verdict only toward caution, never toward
  a pass.

Tests: 89 passed (pytest, foreground run) - the 36 Day 1+2 tests
plus 53 new: the 393.110 required-count table (boundary cases at
5 ft / 1,100 lb / 10 ft fractions), every check's pass, fail, and
degradation branches, bonus-only straps never counting, user
marks counting (and a user cargo mark not counting), direct vs
indirect WLL arithmetic, the not-applicable paths, the
weight-placement never-pass rule, cross-check parsing
(fences/prose/loose values/garbage), stubbed-transport
ask_spark (payload shape, independence of the prompt, retry,
unparseable answer = error, never an empty view), and the
disagreement demotion (PASS -> CHECK_THIS) plus the
never-upgrade rule.

Proof runs (live pipeline, outputs in examples/output/):
- Firewood side view, assumed 8 ft bed, 8 ft load, `--rules`:
  0 PASS / 2 CHECK_THIS / 4 COULD_NOT_TELL, heavy equipment and
  tarp not-applicable. Forward blocking CHECK_THIS (load front
  8.6 in behind the bed front - computed against SAM's bed mask,
  which on this photo overreaches into the pickup bed; the Day 2
  caveat stands and tap-to-correct is the remedy). Tie-down
  count COULD_NOT_TELL (no close-ups supplied; the full-frame
  strap prompt was empty, as the matrix predicts). Weight
  placement CHECK_THIS on the Day 2 estimate (bulk 22.7 in
  behind the axle vs +/-19.7 in uncertainty). Overhang half
  passes at 0 in inside a COULD_NOT_TELL whose lights/plate half
  is honestly unchecked. Aggregate WLL COULD_NOT_TELL (no
  weight or ratings supplied).
- Ratchet-strap close-up, 6 ft load, `--rules --crosscheck`:
  tie-down count CHECK_THIS - SAM counted 1 of the 2 required,
  and the attached cross_check record shows Spark's independent
  count of 2 with agreement=false (both views preserved).
  Edge protection CHECK_THIS from Spark's "yes" on the
  sharp-edge question. Everything else COULD_NOT_TELL or
  not-applicable, honestly.

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

## Working app — LIVE on Modal (Oct 26, 2026, evening)

- Hosted deployment is up: the FastAPI app (`loadmaster/webapp.py`) is
  deployed on Modal via GitHub Actions (`modal deploy modal_app.py`,
  repository secrets for the Modal token; the Meta API key lives in a
  Modal secret, never in the repo).
- Verified end to end through the public URL: `GET /health` returns
  `{"ok": true}`; `POST /analyze` on the firewood dev photo returned
  HTTP 200 with 9 real SAM detections (trailer bed 0.863, seven log
  masks 0.60–0.70, axle estimate 0.918) and verdicts identical to the
  local pipeline run for the same photo: forward blocking CHECK THIS,
  weight placement CHECK THIS, tie-down count / overhang / edge
  protection / aggregate WLL COULD NOT TELL (with reasons).
- This is the judges' live demo link; the same URL serves the mobile
  web UI (photo in, verdict cards out, tap-to-correct recheck through
  `/verdicts` without re-running SAM).

## Spark report + recheck delta in the app (Oct 26, 2026, evening)

- `loadmaster/report.py`: Muse Spark turns the deterministic
  verdicts into a short plain-words, fix-first summary. Strict
  grounding: the prompt carries only the verdict facts (findings,
  minimum fixes, citations), forbids invented numbers, and keeps the
  honesty line (a photo cannot verify tension, ratings, or weight).
  A deterministic template report covers any Spark failure, so the
  app never ships a blank summary. Verdicts themselves never come
  from the model; the engine owns the facts, Spark owns the words.
- `/analyze` now returns `report` alongside verdicts; the UI shows it
  as a "What to fix first" card above the verdict cards. Verdict
  card titles now use plain names (Tie-down count, not tiedown_count).
- Recheck delta: after tap-to-correct rechecks, a "WHAT CHANGED"
  box lists each verdict that flipped (old chip -> new chip) plus
  how many checks stayed the same; if nothing flipped it says so.
- Proof: 98 pytest tests pass (9 new for the report writer, Spark
  stubbed). Local live run on the hay-bale photo returned a Spark
  report (source "spark") whose every number traces to a verdict:
  10.9 in front gap, 0.6 in rear overhang, 6 tie-downs required.

## Guided capture, quality gates, printable report card (Oct 26, 2026, evening)

- Guided capture: the setup card now shows a framing guide that
  follows the selected photo view (side: whole bed + wheels + front
  edge; rear: lights + plate + overhang; close-up: one anchor fills
  the frame, with the counting basis explained).
- Instant quality gates: the moment a photo is picked, the phone
  itself checks size, light, and sharpness on a small canvas copy
  and shows plain chips (Photo size OK / Looks very dark / Looks
  very soft...). Advisory only; Analyze is never blocked.
- After analysis, a second set of gates reports what the camera
  actually found (Trailer found / No axle found - weight placement
  cannot be measured / Load found (19 pieces) / no countable
  tie-downs yet) so a bad photo gets a retake hint, not silence.
- Printable report card: one tap builds a clean one-page card
  (inputs, Spark headline + summary, verdict table with findings
  and minimum fixes, measurements, and the not-a-DOT-inspection
  honesty footer) and opens the print sheet; on the phone that is
  also Save-as-PDF / share.

## Judges' sample gallery (Oct 26, 2026, evening)

- `/gallery` on the live app: three REAL pipeline runs captured
  from this service's own `/analyze` (firewood side view, excavator
  lowboy rear view, ratchet-strap anchor close-up), each with its
  overlay image, detection chips, Spark headline + summary, and
  the full verdict table. Nothing hand-written; the runs are
  pinned by tests (tests/test_gallery.py) so the page can never
  silently go empty. Linked from the app header ("See sample
  runs") as the no-upload path for judges and the backup if a
  live run is slow.
- Gallery photos are the openly licensed development set, labeled
  as such on the page; the scored test set remains Ryan's own
  staged loads.

## Cross-check wired into the hosted app (Oct 26, 2026, evening)

- Honest gap found and closed: the dual-model cross-check shipped
  in the CLI on Day 3 but the hosted app never ran it, while the
  README and submission draft described it as product behavior.
  Now /analyze runs Muse Spark's independent read on every photo
  (the pipeline runs it whenever asked, not only when the pipeline
  itself computes verdicts), the records feed evaluate_rules, and
  the app returns them. The UI gained a "Second opinion" card
  showing Spark's own answers in plain words.
- Proof (strap close-up): SAM counted 1 tie-down, Spark counted 2;
  edge protection moved from COULD NOT TELL to CHECK THIS purely
  on Spark's close-up read, with the dual-model basis recorded on
  the verdict. Gallery captures regenerated so the samples match
  the live behavior, second-opinion line included.
- Modal function now keeps a warm container for 5 minutes after a
  request (scaledown_window), cutting judge-facing cold starts.

## Full dev-set QA sweep (Oct 26, 2026, evening)

- All 8 remaining development photos ran through the app end to
  end (top-down beams, skip-bin rear, steel-rack rear and side,
  wheel-loader side and distant, canvas-tarp truck, farm hay):
  every run HTTP 200, zero warnings, cross-check ok, Spark report
  present. Harder views (top-down, distant, rear) degraded to
  COULD NOT TELL instead of inventing verdicts; the clean farm
  hay side view produced real CHECK THIS verdicts. No crashes,
  no fake passes across the set.
- docs/ARCHITECTURE.md added: pipeline diagram, the counting-basis
  and demote-only cross-check decisions, refusal-as-a-feature.

## Quick wins: Spanish report, voice readout, rubric README (Oct 26, 2026, evening)

- From the judges-value research (see repo docs and the research
  notes): Spanish report option (Report language select in the
  app; Muse Spark writes the summary in Spanish, and the template
  fallback is a real Spanish template, not a relabel; check names
  and rule citations stay verbatim) and a Listen button that reads
  the report aloud in the browser in the report's language.
  Evidence: Hispanic drivers are 25.7 percent of US truck drivers
  (EPI/CPS 2025) and 28 percent of Hispanic adults are
  smartphone-dependent for internet (Pew 2025).
- README reorganized as a rubric map: a "Why it matters, in
  numbers" section (CVSA Roadcheck 2026 focus area, #4 vehicle
  out-of-service, 1,724 violations; FMCSA 2025 counts; AAA
  Foundation debris crash totals), a "For the judges" section
  mapping each criterion to its evidence, and a "How it compares"
  section naming CargoPro head-on (checklist that asks the driver
  to count vs a system that sees and measures).
- Submission draft updated with the same numbers, the competitor
  line, and a re-cut demo structure (magic moment in the first
  45 seconds, technology named at 2:20, live link close).

## Latency hardening (Oct 26, 2026, evening)

- First live Spanish run exposed a real risk: a cold container
  plus a slow-upstream evening pushed /analyze past the old
  300 s function timeout. Two fixes: the cross-check now runs
  CONCURRENTLY with the SAM prompts (its latency hides inside
  segmentation instead of stacking after it; a 180 s join cap
  keeps a hung read from stalling verdicts), and the Modal
  function timeout is 600 s so a slow run finishes instead of
  dying. Local proof under the same slow conditions: full
  close-up run with cross-check and a Spanish Spark report in
  55.8 s.
- Root cause of the two dead live runs found in the retry math:
  each SAM call allowed a 240 s timeout x 3 attempts, so a sick
  upstream that hangs connections instead of erroring could park
  the parallel stage past 700 s, beyond any function timeout.
  Bounds tightened everywhere: SAM 90 s x 2 attempts, cross-check
  75 s x 2, report 45 s x 2. Worst case for a full analysis is
  now roughly 280 s even if every call hangs to its limit, and a
  call that still fails surfaces as an honest error status in
  the results instead of a request that never answers.
- Second root cause, in the hosting shape: the ASGI function ran
  one input at a time per container, so a single wedged analysis
  queue-blocked every later request behind it (three verification
  probes in a row received zero bytes, each behind the previous
  corpse, while /health stayed green). Fix: max_containers=3 and
  @modal.concurrent(max_inputs=12), so one slow run can never
  block the next judge.

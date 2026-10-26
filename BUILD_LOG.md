# Build log

Newest first. Every entry is work done inside the hackathon window,
Oct 26 - Nov 4, 2026.

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

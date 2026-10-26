# Build log

Newest first. Every entry is work done inside the hackathon window,
Oct 26 - Nov 4, 2026.

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

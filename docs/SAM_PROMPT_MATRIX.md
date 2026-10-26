# SAM 3.1 prompt matrix - what SAM can reliably see (Oct 26, 2026)

31 live SAM 3.1 calls (Meta Model API, one noun phrase per call, images
resized to 1024 px) across 5 openly licensed development photos. This
experiment fixes what the geometry layer is allowed to measure. Raw
outputs are kept with the build records; counts and confidences below
are from those outputs, spot-checked by hand against the photos.

## What works

- **Trailer / bed:** "trailer" found the utility trailer (0.86), the
  steel-rack rig (2 boxes, 0.74 / 0.62), and the hay rig (0.61). It
  missed the lowboy semi entirely; "flatbed trailer" found that one
  (0.68). Lesson: prompt packs keyed to trailer type, never one
  generic prompt.
- **Wheels:** found on every photo. Clean single wheel on the utility
  trailer (0.92). On busy scenes it over-fires (8 boxes on the steel
  rack rig including tiny fragments; 7 on the hay and lowboy rigs,
  where tractors and multi-axle trailers genuinely have many wheels).
  Usable for axle position only with size and bottom-band filtering.
  Never a raw count.
- **Cargo, by specific noun:** "logs" segmented 7 individual logs
  (0.60-0.70); "excavator" found the excavator (0.61); "load" found the
  hay load (0.63) but missed on two other rigs; "steel frame" missed a
  rusty steel rack entirely. Lesson: the user's load-type selection
  drives a specific prompt pack, and tap-to-correct covers the misses.
- **Tarp / cover (proxy test):** on a canvas-canopy truck bed, "tarp"
  scored 0.71 and "canvas cover" 0.75-0.76. No openly licensed photo of
  a tarp wrapped over a trailer load exists in the dev set, so the real
  tarp check is validated on Ryan's own test photos later.
- **Chain:** one thin box on the chained excavator (0.54). A presence
  hint only, never a count.

## What does not work

- **Tie-down straps at full-frame distance.** On the steel-rack photo,
  where yellow ratchet straps are plainly visible, four prompt variants
  ("ratchet strap", "strap", "yellow strap", plus round 1's "ratchet
  strap" on two other rigs) returned zero. But in the anchor close-up
  photo, "ratchet strap" scored 0.79. Straps are a scale problem, not
  a prompt problem.
  Design consequence, now load-bearing for the whole product:
  tie-down counting comes from the anchor close-ups that guided
  capture already requires, plus tap-to-correct on the full frame.
  Full-frame strap detection is a bonus signal only.
- **Reliability:** 1 of 31 calls timed out and succeeded on retry with
  the same result as the Day 0 proof (0.86). The pipeline retries with
  backoff; a timeout is never reported as "not found."
- **Latency:** calls averaged roughly 15-20 seconds wall time in
  sequence. A full check runs its prompts in parallel; the report
  target is about 30 seconds end to end.

## Geometry inputs this licenses

Trailer/bed box (per-type prompts), filtered wheel boxes for axle
position, cargo masks by specific noun, tarp/cover masks. Those four
feed overhang, centroid-vs-axle balance, and tarp coverage. Tie-downs
come from close-ups + user correction, per above.

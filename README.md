# LoadMaster

LoadMaster checks a loaded trailer against the federal cargo
securement rules (49 CFR Part 393, Subpart I) and tells the hauler, in
plain words, what to fix before the trailer rolls.

It is a rules system, not a picture describer. SAM 3.1 (Meta Model API)
segments the load, the trailer bed, the wheels and axles, and the
tie-downs, and turns those masks into measurements: tie-down count and
coverage, rear overhang, load balance against the axle position, tarp
coverage. A deterministic rules engine applies the actual securement
rules to those measurements. Muse Spark (Meta Model API) turns the
rule results into a plain-words report: PASS, CHECK THIS, or COULD NOT
TELL FROM THE PHOTO, with one specific fix for every failure and a
fix-and-recheck loop that shows what changed.

Built by Ryan Cotten (Ghost Developer Studio, Oakdale, Tennessee) for
the Meta Global AI Developer Hackathon, Indie Developer track, theme:
Build for Your World. Ryan hauls firewood, equipment, and trailers in
East Tennessee. This is a real problem from his own work.

## Honesty line

A photo check can count visible tie-downs and flag placement. It cannot
verify strap tension, actual working load limit ratings, or load
weight. LoadMaster says so in every report, and any check that depends
on something a photo cannot show comes back COULD NOT TELL, with the
reason named, never a guess.

## The checks (49 CFR 393)

1. Tie-down count by load length (393.110)
2. Tie-down strength: aggregate working load limit at least 50 percent
   of load weight (393.106, 393.108); photo-limited, see honesty line
3. Heavy equipment, 10,000 lb or more: at least 4 tie-downs, accessories
   lowered and secured (393.130)
4. Forward blocking against sliding under braking
5. Edge protection where straps or chains cross sharp edges
6. Overhang, and lights or plate obscured
7. Tarp coverage of loose material
8. Weight placement: centered, heavier end forward of the axles
   (reported as a visual estimate only)

## Built during the hackathon (Oct 26 - Nov 4, 2026)

Everything in this repository was created inside the hackathon window.
Pre-window work was research only: a written spec grounded in 49 CFR
393, with no code and no repository.

- Oct 26: repository created; SAM 3.1 and Muse Spark access proven on
  the Meta Model API (first SAM call: one trailer mask, 0.863
  confidence, on a development photo).

Build progress is logged in BUILD_LOG.md, newest at the top.

## Libraries and services used (not claimed as new work)

- Meta Model API: SAM 3.1 (segmentation) and Muse Spark 1.3 (report
  reasoning), accessed with an API key that is never stored in this
  repository.
- meta-sam-parser (Meta's SAM output parser) for mask decoding, if used
  in the final pipeline; the build log will say.

## License

MIT. See LICENSE.

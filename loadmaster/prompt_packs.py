"""SAM prompt packs, keyed by (trailer_type, load_type, photo_role).

Every choice here encodes a finding from the 31-call SAM prompt matrix
(``docs/SAM_PROMPT_MATRIX.md``, Oct 26, 2026):

- Trailer/bed prompts are keyed to trailer type: "trailer" found the
  utility trailer (0.86) but missed the lowboy semi entirely;
  "flatbed trailer" found the lowboy (0.68).
- Wheels over-fire on busy scenes (8 boxes on the steel-rack rig,
  fragments included). The wheel prompt feeds the axle estimate only,
  through the size + bottom-band filter in detections.py. A raw wheel
  count is never exposed.
- Cargo is prompted by SPECIFIC noun from the user's load-type
  selection: "logs" segmented 7 logs (0.60-0.70), "excavator" hit
  (0.61). The generic "load" missed on 2 of 3 rigs and is deliberately
  NOT used; tap-to-correct covers cargo misses.
- Tarp prompts ("tarp" 0.71, "canvas cover" 0.75-0.76 on a canvas
  canopy) are PROXY signals only; no dev photo of a tarp wrapped over
  a trailer load exists yet.
- Chain scored one thin box (0.54): presence hint only, never a count.
- Straps are a scale problem: four prompt variants returned ZERO at
  full-frame distance on a photo with plainly visible yellow straps,
  while "ratchet strap" hit 0.79 in an anchor close-up. Tie-down
  counting therefore comes from the close-up pack + tap-to-correct;
  full-frame strap prompts are a bonus signal only, never a count.

Second matrix (``files/sam-matrix-2``, Oct 31, 2026; 22 calls on the
barite flatbed case, warehouse pallets, a steel coil, and the scrap
case) added the unitized-load findings:

- A load declared "generic" previously got NO cargo prompt at all, so
  the wrapped barite pallets on a flatbed were never even asked about;
  all six verdicts came back COULD NOT TELL. Generic now runs a small
  unitized sweep (below).
- "shrink wrapped pallet" segmented the barite stack (7 objects,
  0.52-0.84) and "wrapped pallet" hit too (6 objects, up to 0.88), but
  BOTH missed the warehouse water pallets, where plain "pallet" /
  "pallets" hit 3 objects at 0.87-0.94. Pallet packs therefore run the
  plain and wrapped nouns together.
- "stack of bags" hit on both bag-stack photos (0.56-0.77);
  "bags" alone over-fires into individual bags and is not used.
- "roll of steel" hit the steel coil at 0.95, far above "steel coil"
  (0.63); it rides along in the steel pack.
- The scrap heap DOES segment with the right noun: "pile of scrap
  metal" (1 object, 0.79) and "junk pile" (2 objects, 0.73), where
  "scrap metal" and "metal scrap" returned empty. Scrap is its own
  load type now; "load" remains only for loose material.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    prompt: str
    kind: str  # detection kind this prompt feeds (see detections.KINDS)
    note: str = ""
    bonus_only: bool = False  # signal only; never counted (full-frame straps)


# Trailer-type -> bed prompts. Matrix: keyed packs, never one generic.
TRAILER_PROMPTS: dict[str, list[PromptSpec]] = {
    "utility": [PromptSpec("trailer", "trailer_bed", "matrix: 0.86 on utility rig")],
    "flatbed": [
        PromptSpec("flatbed trailer", "trailer_bed", "matrix: names the bed style"),
        PromptSpec("trailer", "trailer_bed", "matrix: secondary"),
    ],
    "lowboy": [
        PromptSpec("flatbed trailer", "trailer_bed", "matrix: 0.68 where 'trailer' missed"),
    ],
    "gooseneck": [
        PromptSpec("trailer", "trailer_bed", "gooseneck reads as a utility-style rig"),
        PromptSpec("flatbed trailer", "trailer_bed", "matrix: secondary"),
    ],
    "dump": [PromptSpec("trailer", "trailer_bed", "utility-style bed")],
    "enclosed": [PromptSpec("trailer", "trailer_bed", "box body; bed geometry limited")],
    "unknown": [
        PromptSpec("trailer", "trailer_bed", "default pack runs both bed prompts"),
        PromptSpec("flatbed trailer", "trailer_bed", "default pack runs both bed prompts"),
    ],
}

# Load-type -> specific cargo nouns (a LIST: several packs run more
# than one noun and the pipeline dedupes near-identical cargo boxes).
# Matrix rule: specific nouns work, generic "load" does not; "load"
# survives only for loose material, which has no object noun.
CARGO_PROMPTS: dict[str, list[PromptSpec]] = {
    "logs": [PromptSpec("logs", "cargo", "matrix: 7 log masks, 0.60-0.70")],
    "firewood": [PromptSpec("logs", "cargo", "firewood reads as logs; matrix: 0.60-0.70")],
    "hay": [PromptSpec("hay bales", "cargo", "specific noun for baled hay")],
    "excavator": [PromptSpec("excavator", "cargo", "matrix: 0.61")],
    "heavy_equipment": [PromptSpec("excavator", "cargo", "nearest validated noun; tap-to-correct covers other machines")],
    "wheel_loader": [PromptSpec("wheel loader", "cargo", "specific noun for the machine")],
    "steel": [
        PromptSpec("steel frame", "cargo", "WEAK: matrix missed a rusty rack entirely; tap-to-correct expected"),
        PromptSpec("roll of steel", "cargo", "matrix-2: steel coil at 0.95, far above 'steel coil' (0.63)"),
    ],
    "vehicle": [PromptSpec("vehicle", "cargo", "specific noun; untested in matrix")],
    "skip_bin": [PromptSpec("container", "cargo", "specific noun for a skip/bin load")],
    "beams": [PromptSpec("beams", "cargo", "specific noun for timber/steel beams")],
    "loose_material": [PromptSpec("load", "cargo", "loose piles have no object noun; expect misses, tap-to-correct")],
    "pallets": [
        PromptSpec("pallet", "cargo", "matrix-2: warehouse pallets 0.87-0.94; barite stack up to 0.80"),
        PromptSpec("shrink wrapped pallet", "cargo", "matrix-2: barite stack 7 objects 0.52-0.84; missed warehouse pallets"),
        PromptSpec("wrapped pallet", "cargo", "matrix-2: barite stack 6 objects up to 0.88; missed warehouse pallets"),
    ],
    "bagged": [
        PromptSpec("stack of bags", "cargo", "matrix-2: bag stacks on both test photos, 0.56-0.77"),
        PromptSpec("shrink wrapped pallet", "cargo", "matrix-2: bagged loads usually ride wrapped on pallets"),
        PromptSpec("pallet", "cargo", "matrix-2: the pallet under the bags"),
    ],
    "scrap": [
        PromptSpec("pile of scrap metal", "cargo", "matrix-2: scrap heap 1 object at 0.79 where 'scrap metal' was empty"),
        PromptSpec("junk pile", "cargo", "matrix-2: scrap heap 2 objects at 0.73"),
    ],
    "generic": [
        PromptSpec("pallet", "cargo", "matrix-2: generic loads get the unitized sweep; previously they got no cargo prompt at all"),
        PromptSpec("shrink wrapped pallet", "cargo", "matrix-2: generic sweep"),
        PromptSpec("stack of bags", "cargo", "matrix-2: generic sweep"),
    ],
}

WHEEL = PromptSpec("wheel", "wheel", "axle position only; filtered, never counted")
TARP = PromptSpec("tarp", "tarp", "proxy signal only; matrix 0.71 on canvas canopy")
CANVAS = PromptSpec("canvas cover", "tarp", "proxy signal only; matrix 0.75-0.76")
CHAIN = PromptSpec("chain", "chain_hint", "presence hint only; matrix 0.54, one thin box")
STRAP_FULLFRAME = PromptSpec(
    "ratchet strap", "tiedown_closeup",
    "BONUS ONLY at full frame: matrix returned zero at this distance; never a count",
    bonus_only=True,
)
STRAP_CLOSEUP = PromptSpec("ratchet strap", "tiedown_closeup", "matrix: 0.79 in anchor close-up; the tie-down counting source")
CHAIN_CLOSEUP = PromptSpec("chain", "chain_hint", "close-up chain presence")

LOOSE_LOAD_TYPES = {"hay", "loose_material", "scrap"}
HEAVY_LOAD_TYPES = {"excavator", "heavy_equipment", "wheel_loader"}


def get_pack(trailer_type: str, load_type: str, photo_role: str) -> list[PromptSpec]:
    """Return the prompt pack for one photo.

    photo_role: "side" (full-frame side view), "rear" (rear
    three-quarter), or "closeup" (tie-down anchor close-up).
    """
    trailer_type = (trailer_type or "unknown").lower()
    load_type = (load_type or "generic").lower()
    photo_role = (photo_role or "side").lower()

    if photo_role == "closeup":
        # Tie-down counting lives here (matrix: straps are a scale
        # problem). Chain close-ups ride along for chained loads.
        pack = [STRAP_CLOSEUP]
        if load_type in HEAVY_LOAD_TYPES:
            pack.append(CHAIN_CLOSEUP)
        return pack

    pack: list[PromptSpec] = []
    pack.extend(TRAILER_PROMPTS.get(trailer_type, TRAILER_PROMPTS["unknown"]))
    pack.append(WHEEL)
    pack.extend(CARGO_PROMPTS.get(load_type, []))
    if load_type in LOOSE_LOAD_TYPES:
        pack.extend([TARP, CANVAS])
    if load_type in HEAVY_LOAD_TYPES:
        pack.append(CHAIN)
    # Full-frame strap prompt: bonus signal only, clearly flagged.
    pack.append(STRAP_FULLFRAME)
    return pack

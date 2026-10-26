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

# Load-type -> specific cargo noun. Matrix: specific nouns work,
# generic "load" does not, so there is deliberately no "generic" entry.
CARGO_PROMPTS: dict[str, PromptSpec] = {
    "logs": PromptSpec("logs", "cargo", "matrix: 7 log masks, 0.60-0.70"),
    "firewood": PromptSpec("logs", "cargo", "firewood reads as logs; matrix: 0.60-0.70"),
    "hay": PromptSpec("hay bales", "cargo", "specific noun for baled hay"),
    "excavator": PromptSpec("excavator", "cargo", "matrix: 0.61"),
    "heavy_equipment": PromptSpec("excavator", "cargo", "nearest validated noun; tap-to-correct covers other machines"),
    "wheel_loader": PromptSpec("wheel loader", "cargo", "specific noun for the machine"),
    "steel": PromptSpec("steel frame", "cargo", "WEAK: matrix missed a rusty rack entirely; tap-to-correct expected"),
    "vehicle": PromptSpec("vehicle", "cargo", "specific noun; untested in matrix"),
    "skip_bin": PromptSpec("container", "cargo", "specific noun for a skip/bin load"),
    "beams": PromptSpec("beams", "cargo", "specific noun for timber/steel beams"),
    "loose_material": PromptSpec("load", "cargo", "loose piles have no object noun; expect misses, tap-to-correct"),
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

LOOSE_LOAD_TYPES = {"hay", "loose_material"}
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
    cargo = CARGO_PROMPTS.get(load_type)
    if cargo is not None:
        pack.append(cargo)
    if load_type in LOOSE_LOAD_TYPES:
        pack.extend([TARP, CANVAS])
    if load_type in HEAVY_LOAD_TYPES:
        pack.append(CHAIN)
    # Full-frame strap prompt: bonus signal only, clearly flagged.
    pack.append(STRAP_FULLFRAME)
    return pack

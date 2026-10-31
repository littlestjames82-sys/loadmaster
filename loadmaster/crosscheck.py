"""Dual-model cross-check (Tier 2.5, Day 3).

Muse Spark reads a photo INDEPENDENTLY of SAM: it is never told
what SAM found, what the rules engine concluded, or what answer
would agree. It answers a small structured question set keyed to the
photo's role, and its answer is parsed into plain fields:

  closeup (anchor close-up):
    tiedown_count                             int | None
    straps_over_sharp_edge_without_protection "yes" | "no" | "unclear"
  rear (rear three-quarter):
    tail_lights_visible                       True | False | None
    license_plate_visible                     True | False | None
    tarp_covering_load                        "yes" | "no" | "unclear"
  side (full side view):
    load_looks_rear_heavy                     "yes" | "no" | "unclear"
    front_blocked                             "yes" | "no" | "unclear"
    loose_or_escaped_items_visible            "yes" | "no" | "unclear"

When SAM finds no cargo at all, a separate locate read (locate_cargo)
asks Spark to name the load and box it; the pipeline then re-prompts
SAM with Spark's noun (Spark points, SAM cuts). Every detection still
comes from SAM or the user: a Spark box alone never becomes a
detection, so the evidence standard is unchanged.

rules.py consumes these views: the rear view is the only source for
the lights/plate half of the overhang check, the close-up edge
answer is the only source for edge protection, and disagreements
with SAM-based verdicts are attached to the verdict as a cross_check
record (a disagreement demotes a PASS to CHECK_THIS; see rules.py).
Spark's raw answer is kept on the result for audit, always.

Transport: same Meta Model API Responses endpoint and the same
registered-connector credential path as sam_client.py (the key is
never read, printed, or stored here). Image input uses the same
input_image data-URL shape the SAM proof proved on Day 0;
muse-spark-1.3 is the answering model (Day 0 verified it answers
text; the Day 3 smoke test verifies image input live and the result
is recorded in BUILD_LOG whatever it shows).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from .sam_client import _extract_output_text, _post, prepare_image

MODEL = "muse-spark-1.3"

ROLE_QUESTIONS: dict[str, str] = {
    "closeup": (
        "This photo is a close-up of a tie-down anchor point on a loaded "
        "trailer. Answer ONLY with a JSON object, no other text, with "
        "exactly these keys:\n"
        '{"tiedown_count": <integer: how many DISTINCT ratchet-strap or '
        'chain tie-downs you can see in this close-up, or null if you '
        'cannot tell>, "straps_over_sharp_edge_without_protection": '
        '"yes" if a strap or chain crosses a sharp edge with no visible '
        'edge protector, "no" if straps cross only protected or rounded '
        'edges, "unclear" if you cannot tell}'
    ),
    "rear": (
        "This photo is a rear view of a loaded trailer. Answer ONLY with "
        "a JSON object, no other text, with exactly these keys:\n"
        '{"tail_lights_visible": true if the trailer tail lights are '
        'fully visible and not obscured by the load, false if they are '
        'obscured or blocked, null if you cannot tell, '
        '"license_plate_visible": true if the license plate is fully '
        'visible and readable, false if it is obscured or blocked, null '
        'if you cannot tell, "tarp_covering_load": "yes" if a tarp or '
        'cover is covering the load, "no" if the load is not covered by '
        'a tarp, "unclear" if you cannot tell}'
    ),
    "side": (
        "This photo is a full side view of a loaded trailer. Answer ONLY "
        "with a JSON object, no other text, with exactly these keys:\n"
        '{"load_looks_rear_heavy": "yes" if the load\'s bulk clearly '
        'sits toward the rear of the trailer behind the axle(s), "no" '
        'if it clearly does not, "unclear" if you cannot tell, '
        '"front_blocked": "yes" if the front of the load is blocked '
        'against forward movement (headboard, bulkhead, or other cargo '
        'touching it), "no" if there is open space in front of the '
        'load, "unclear" if you cannot tell, '
        '"loose_or_escaped_items_visible": "yes" if any item of the '
        'load appears loose, slipped out of its wrapping or '
        'containment, hanging off the load or the trailer, or fallen '
        'from the load, "no" if every item appears contained in the '
        'load, "unclear" if you cannot tell}'
    ),
}

LOCATE_QUESTION = (
    "This photo shows a trailer that may or may not be carrying a "
    "load. Answer ONLY with a JSON object, no other text, with "
    "exactly these keys:\n"
    '{"cargo_present": true if a load or cargo is on the trailer, '
    'false if the trailer is empty, "cargo_noun": a short noun '
    'phrase of 2 to 4 words naming the load itself, for example '
    '"stack of wrapped pallets" or "rusty steel frame" (null if '
    'cargo_present is false), "cargo_box": {"x1": <int>, '
    '"y1": <int>, "x2": <int>, "y2": <int>} bounding the whole load '
    "in 0-1000 image coordinates (null if cargo_present is false)}"
)


@dataclass
class CrossCheckResult:
    photo: str
    photo_role: str
    status: str  # "ok" | "error"
    view: dict = field(default_factory=dict)
    raw_answer: str = ""
    latency_s: float = 0.0
    attempts: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "photo": self.photo,
            "photo_role": self.photo_role,
            "status": self.status,
            "model": MODEL,
            "view": dict(self.view),
            "raw_answer": self.raw_answer,
            "latency_s": round(self.latency_s, 2),
            "attempts": self.attempts,
            "error": self.error,
        }


# ---------------------------------------------------------------------
# Answer parsing. Pure and unit-tested: Spark's answer must survive
# code fences, prose around the JSON, and loose value spellings.
# ---------------------------------------------------------------------

def _tri(value) -> str:
    """Coerce a yes/no/unclear-style answer."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "true"):
            return "yes"
        if v in ("no", "false"):
            return "no"
    return "unclear"


def _bool(value):
    """Coerce a visible/not-visible answer to True | False | None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "true", "visible"):
            return True
        if v in ("no", "false", "obscured", "blocked"):
            return False
    return None


def _count(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def parse_answer(text: str, photo_role: str) -> dict:
    """Parse Spark's raw answer text into the structured view for
    the role. Raises ValueError when no usable JSON object exists."""
    if photo_role not in ROLE_QUESTIONS:
        raise ValueError(f"unknown photo role: {photo_role!r}")
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in Spark's answer")
        candidate = candidate[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("Spark's answer JSON is not an object")
    if photo_role == "closeup":
        return {
            "tiedown_count": _count(data.get("tiedown_count")),
            "straps_over_sharp_edge_without_protection": _tri(
                data.get("straps_over_sharp_edge_without_protection")
            ),
        }
    if photo_role == "rear":
        return {
            "tail_lights_visible": _bool(data.get("tail_lights_visible")),
            "license_plate_visible": _bool(data.get("license_plate_visible")),
            "tarp_covering_load": _tri(data.get("tarp_covering_load")),
        }
    return {
        "load_looks_rear_heavy": _tri(data.get("load_looks_rear_heavy")),
        "front_blocked": _tri(data.get("front_blocked")),
        "loose_or_escaped_items_visible": _tri(
            data.get("loose_or_escaped_items_visible")
        ),
    }


_NOUN_OK = re.compile(r"^[a-z0-9][a-z0-9 \-']{1,48}$")


def _clean_noun(value) -> str | None:
    """Sanitize Spark's cargo noun before it becomes a SAM prompt:
    short, plain words only; anything else is not a prompt."""
    if not isinstance(value, str):
        return None
    noun = " ".join(value.strip().lower().split())
    if not _NOUN_OK.match(noun):
        return None
    if not (2 <= len(noun.split()) <= 5):
        return None
    return noun


def _clean_box(value):
    """Validate a 0-1000 cargo box; returns [x1, y1, x2, y2] or None."""
    if not isinstance(value, dict):
        return None
    try:
        box = [float(value[k]) for k in ("x1", "y1", "x2", "y2")]
    except (KeyError, TypeError, ValueError):
        return None
    if any(not (0.0 <= c <= 1000.0) for c in box):
        return None
    if box[2] - box[0] < 30 or box[3] - box[1] < 30:
        return None
    return box


def parse_locate(text: str) -> dict:
    """Parse Spark's locate answer into {cargo_present, cargo_noun,
    cargo_box}. Raises ValueError when no usable JSON object exists.
    An absent load is a first-class answer, not an error."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in Spark's answer")
        candidate = candidate[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("Spark's answer JSON is not an object")
    present = data.get("cargo_present")
    if not isinstance(present, bool):
        raise ValueError("cargo_present missing or not a boolean")
    if not present:
        return {"cargo_present": False, "cargo_noun": None, "cargo_box": None}
    return {
        "cargo_present": True,
        "cargo_noun": _clean_noun(data.get("cargo_noun")),
        "cargo_box": _clean_box(data.get("cargo_box")),
    }


# ---------------------------------------------------------------------
# The live call. Errors surface as status "error" with the raw state
# preserved - never as an empty view that reads as "all clear".
# ---------------------------------------------------------------------

def ask_spark(
    image_path: Path,
    photo_role: str,
    timeout: float = 75.0,
    max_attempts: int = 2,
    backoff_s: float = 2.0,
    _poster=None,
) -> CrossCheckResult:
    """One independent Spark read of one photo.

    _poster is injectable for tests (same payload -> response-dict
    contract as sam_client._post); production uses sam_client._post,
    which carries the connector credential.
    """
    if photo_role not in ROLE_QUESTIONS:
        return CrossCheckResult(
            photo=str(image_path), photo_role=photo_role, status="error",
            error=f"unknown photo role: {photo_role!r}",
        )
    poster = _poster or _post
    data_url, _sw, _sh, _ow, _oh = prepare_image(Path(image_path))
    payload = {
        "model": MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": ROLE_QUESTIONS[photo_role]},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        "stream": False,
    }
    started = time.monotonic()
    last_error: str | None = None
    raw = ""
    for attempt in range(1, max_attempts + 1):
        try:
            parsed = poster(payload, timeout)
            raw = _extract_output_text(parsed)
            if not raw:
                raise ValueError("Spark returned no output text")
            view = parse_answer(raw, photo_role)
            return CrossCheckResult(
                photo=str(image_path), photo_role=photo_role, status="ok",
                view=view, raw_answer=raw,
                latency_s=time.monotonic() - started, attempts=attempt,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = f"HTTP {exc.code}: {body}"
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_attempts:
            time.sleep(backoff_s * (2 ** (attempt - 1)))
    return CrossCheckResult(
        photo=str(image_path), photo_role=photo_role, status="error",
        raw_answer=raw, latency_s=time.monotonic() - started,
        attempts=max_attempts, error=last_error,
    )


def locate_cargo(
    image_path: Path,
    timeout: float = 75.0,
    max_attempts: int = 2,
    backoff_s: float = 2.0,
    _poster=None,
) -> CrossCheckResult:
    """Ask Spark to name and box the load (the rescue read used when
    SAM finds no cargo). Same transport and failure contract as
    ask_spark: failures arrive as status "error", never as a view
    that reads as "no cargo". photo_role is reported as "locate"."""
    poster = _poster or _post
    data_url, _sw, _sh, _ow, _oh = prepare_image(Path(image_path))
    payload = {
        "model": MODEL,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": LOCATE_QUESTION},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        "stream": False,
    }
    started = time.monotonic()
    last_error: str | None = None
    raw = ""
    for attempt in range(1, max_attempts + 1):
        try:
            parsed = poster(payload, timeout)
            raw = _extract_output_text(parsed)
            if not raw:
                raise ValueError("Spark returned no output text")
            view = parse_locate(raw)
            return CrossCheckResult(
                photo=str(image_path), photo_role="locate", status="ok",
                view=view, raw_answer=raw,
                latency_s=time.monotonic() - started, attempts=attempt,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = f"HTTP {exc.code}: {body}"
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_attempts:
            time.sleep(backoff_s * (2 ** (attempt - 1)))
    return CrossCheckResult(
        photo=str(image_path), photo_role="locate", status="error",
        raw_answer=raw, latency_s=time.monotonic() - started,
        attempts=max_attempts, error=last_error,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LoadMaster dual-model cross-check (one photo)")
    ap.add_argument("--photo", required=True, type=Path)
    ap.add_argument("--role", required=True, choices=sorted(ROLE_QUESTIONS))
    args = ap.parse_args(argv)
    if not args.photo.is_file():
        print(f"photo not found: {args.photo}", file=sys.stderr)
        return 3
    result = ask_spark(args.photo, args.role)
    print(json.dumps(result.to_dict(), indent=1))
    return 0 if result.status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

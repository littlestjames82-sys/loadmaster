"""Editable detection model for LoadMaster.

A DetectionSet is what tap-to-correct edits: the user can add a
missed tie-down, delete a false positive, or drag a box, and every
later layer (measurements, rules) reads the corrected set, never the
raw SAM output. Detections carry their provenance (prompt, raw
confidence, source) so a verdict can always say what it rests on.

Coordinates are in ORIGINAL image pixels.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field

KINDS = [
    "trailer_bed",
    "wheel",
    "axle_estimate",
    "cargo",
    "tarp",
    "tiedown_closeup",
    "chain_hint",
    "user_added",
]

_id_counter = itertools.count(1)


def _new_id(prefix: str = "det") -> str:
    return f"{prefix}-{next(_id_counter):04d}-{uuid.uuid4().hex[:6]}"


@dataclass
class Detection:
    id: str
    kind: str
    bbox: list[float]  # [x1, y1, x2, y2], original image pixels
    confidence: float | None = None
    source: str = "sam"  # "sam" | "user"
    user_corrected: bool = False
    prompt: str | None = None
    polygon: list[list[float]] = field(default_factory=list)
    mask_rle: dict | None = None  # MaskRaster.to_rle() output, original-image scale
    bonus_only: bool = False  # e.g. full-frame strap signal: never counted
    label: str = ""

    def __post_init__(self) -> None:
        # Client-supplied sets arrive here: shape errors must be
        # ValueError (callers handle those), never a bare TypeError
        # from an unhashable kind or a non-sequence bbox.
        if not isinstance(self.kind, str) or self.kind not in KINDS:
            raise ValueError(f"unknown detection kind: {self.kind!r}")
        if not isinstance(self.bbox, (list, tuple)) or len(self.bbox) != 4:
            raise ValueError("bbox must be [x1, y1, x2, y2]")
        if not self.label:
            self.label = self.kind.replace("_", " ")
        # A label is display text (overlay + echoes): coerce and cap
        # it so a hostile or broken client cannot bloat prompts,
        # payloads, or the overlay renderer through it.
        self.label = str(self.label)[:120]

    @property
    def center(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2.0, (self.bbox[1] + self.bbox[3]) / 2.0)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "bbox": [round(float(v), 2) for v in self.bbox],
            "confidence": self.confidence,
            "source": self.source,
            "user_corrected": self.user_corrected,
            "prompt": self.prompt,
            "polygon": [[round(float(x), 1), round(float(y), 1)] for x, y in self.polygon],
            "mask_rle": self.mask_rle,
            "bonus_only": self.bonus_only,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Detection":
        return cls(
            id=d["id"],
            kind=d["kind"],
            bbox=[float(v) for v in d["bbox"]],
            confidence=d.get("confidence"),
            source=d.get("source", "sam"),
            user_corrected=bool(d.get("user_corrected", False)),
            prompt=d.get("prompt"),
            polygon=[[float(x), float(y)] for x, y in d.get("polygon", [])],
            mask_rle=d.get("mask_rle"),
            bonus_only=bool(d.get("bonus_only", False)),
            label=d.get("label", ""),
        )


@dataclass
class DetectionSet:
    image_path: str
    image_width: int
    image_height: int
    trailer_type: str = "unknown"
    load_type: str = "generic"
    photo_role: str = "side"
    detections: list[Detection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- queries ------------------------------------------------------
    def by_kind(self, kind: str) -> list[Detection]:
        return [d for d in self.detections if d.kind == kind]

    def get(self, det_id: str) -> Detection:
        for d in self.detections:
            if d.id == det_id:
                return d
        raise KeyError(det_id)

    # -- tap-to-correct operations ------------------------------------
    def add(
        self,
        kind: str,
        bbox: list[float],
        confidence: float | None = None,
        source: str = "sam",
        prompt: str | None = None,
        polygon: list[list[float]] | None = None,
        mask_rle: dict | None = None,
        bonus_only: bool = False,
        det_id: str | None = None,
    ) -> Detection:
        det = Detection(
            id=det_id or _new_id(),
            kind=kind,
            bbox=[float(v) for v in bbox],
            confidence=confidence,
            source=source,
            user_corrected=(source == "user"),
            prompt=prompt,
            polygon=polygon or [],
            mask_rle=mask_rle,
            bonus_only=bonus_only,
        )
        self.detections.append(det)
        return det

    def add_user(self, kind: str, bbox: list[float]) -> Detection:
        """A detection the user tapped in (tap-to-correct add)."""
        det = self.add(kind, bbox, confidence=None, source="user")
        det.user_corrected = True
        return det

    def remove(self, det_id: str) -> Detection:
        det = self.get(det_id)
        self.detections.remove(det)
        return det

    def move(self, det_id: str, dx: float, dy: float) -> Detection:
        """Drag a detection; bbox, polygon, and mask offset all shift."""
        det = self.get(det_id)
        det.bbox = [det.bbox[0] + dx, det.bbox[1] + dy, det.bbox[2] + dx, det.bbox[3] + dy]
        det.polygon = [[x + dx, y + dy] for x, y in det.polygon]
        if det.mask_rle and "offset" in det.mask_rle:
            ox, oy = det.mask_rle["offset"]
            det.mask_rle = {**det.mask_rle, "offset": [ox + dx, oy + dy]}
        det.user_corrected = True
        return det

    def set_bbox(self, det_id: str, bbox: list[float]) -> Detection:
        det = self.get(det_id)
        det.bbox = [float(v) for v in bbox]
        det.user_corrected = True
        return det

    # -- serialization --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "image_path": self.image_path,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "trailer_type": self.trailer_type,
            "load_type": self.load_type,
            "photo_role": self.photo_role,
            "notes": list(self.notes),
            "detections": [d.to_dict() for d in self.detections],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DetectionSet":
        ds = cls(
            image_path=d["image_path"],
            image_width=int(d["image_width"]),
            image_height=int(d["image_height"]),
            trailer_type=d.get("trailer_type", "unknown"),
            load_type=d.get("load_type", "generic"),
            photo_role=d.get("photo_role", "side"),
            notes=list(d.get("notes", [])),
        )
        ds.detections = [Detection.from_dict(x) for x in d.get("detections", [])]
        return ds


def filter_wheels_to_axle(
    wheel_boxes: list[tuple[list[float], float | None]],
    image_width: int,
    image_height: int,
) -> tuple[list[Detection], dict]:
    """Turn raw SAM wheel boxes into axle estimate(s).

    Matrix finding: wheels over-fire on busy scenes (fragments, plus
    genuinely many wheels on multi-axle rigs and tractors). So wheel
    boxes are filtered by plausible size and bottom-band position,
    clustered along x, and each cluster yields ONE axle_estimate.
    The raw wheel count is never exposed as a finding; the support
    count stays in internal stats for the run record only.
    """
    stats = {"raw": len(wheel_boxes), "kept": 0, "clusters": 0}
    kept: list[tuple[list[float], float | None]] = []
    for bbox, conf in wheel_boxes:
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= 0 or h <= 0:
            continue
        # Plausible wheel size relative to the frame.
        if not (0.02 * image_width <= w <= 0.30 * image_width):
            continue
        if not (0.02 * image_height <= h <= 0.45 * image_height):
            continue
        aspect = w / h
        if not (0.45 <= aspect <= 2.2):
            continue
        # Bottom band: wheels touch the ground line, centres sit low.
        cy = (bbox[1] + bbox[3]) / 2.0
        if cy < 0.42 * image_height:
            continue
        kept.append((bbox, conf))
    stats["kept"] = len(kept)
    if not kept:
        return [], stats

    kept.sort(key=lambda bc: (bc[0][0] + bc[0][2]) / 2.0)
    gap_limit = 0.15 * image_width
    clusters: list[list[tuple[list[float], float | None]]] = [[kept[0]]]
    for item in kept[1:]:
        prev_cx = (clusters[-1][-1][0][0] + clusters[-1][-1][0][2]) / 2.0
        cx = (item[0][0] + item[0][2]) / 2.0
        if cx - prev_cx <= gap_limit:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    stats["clusters"] = len(clusters)

    out: list[Detection] = []
    for cluster in clusters:
        x1 = min(b[0] for b, _ in cluster)
        y1 = min(b[1] for b, _ in cluster)
        x2 = max(b[2] for b, _ in cluster)
        y2 = max(b[3] for b, _ in cluster)
        confs = [c for _, c in cluster if c is not None]
        out.append(
            Detection(
                id=_new_id("axle"),
                kind="axle_estimate",
                bbox=[x1, y1, x2, y2],
                confidence=max(confs) if confs else None,
                source="sam",
                prompt="wheel",
                label="axle (estimate)",
            )
        )
    return out, stats

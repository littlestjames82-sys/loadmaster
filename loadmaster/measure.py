"""Calibrated measurement layer (Tier 1.2, Day 2).

Turns a DetectionSet into measurements in inches. The user supplies
the trailer bed length once per run; the trailer_bed detection's
extent along the frame's x axis gives pixels-per-inch, and every
other number is derived from mask bitmaps (the source of truth),
never from polygons.

Honesty rules baked in:
- No bed detection or no user-supplied length -> calibration fails
  and every dependent measurement degrades to "could_not_measure"
  with the reason named. Nothing here ever guesses a scale.
- The centroid-vs-axle figure is a VISUAL ESTIMATE of where the
  load's bulk sits, labeled as such with an uncertainty range. It is
  not a weight measurement and is never presented as one.
- Tarp coverage is proxy-only (matrix finding: the tarp/canvas
  prompts scored 0.71-0.76 on a canvas canopy; no real
  tarp-over-load photo exists in the dev set yet).
- Tie-down gaps need anchor positions in the SAME image space as
  the bed. Tie-downs are normally counted from separate anchor
  close-up photos, whose coordinates cannot be mapped onto the side
  photo's bed axis; in that case this layer says so instead of
  inventing positions.

Axis convention: the bed runs along the frame's x axis, front at the
bed extent's left edge, rear at its right edge (the guided-capture
convention for side photos). "Behind the axle" means toward the
rear (+x). All coordinates are original-image pixels.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .detections import Detection, DetectionSet
from .mask_decode import MaskRaster

_RUN_RE = re.compile(rb"\x01+")
LOOSE_LOAD_TOKENS = ("loose",)


# ---------------------------------------------------------------------
# Bitmap statistics from runs (no per-pixel Python loops).
# ---------------------------------------------------------------------

@dataclass
class RasterStats:
    count: int
    centroid_x: float
    centroid_y: float
    std_x: float  # bitmap-weighted spread along x, pixels
    min_x: int
    min_y: int
    max_x: int  # inclusive
    max_y: int  # inclusive

    @property
    def rear_x(self) -> float:
        """Exclusive right edge, matching bbox convention."""
        return self.max_x + 1.0


def _sumsq_upto(n: int) -> float:
    """Sum of k^2 for k = 0..n (n may be -1 -> 0)."""
    if n < 0:
        return 0.0
    return n * (n + 1) * (2 * n + 1) / 6.0


def raster_stats(raster: MaskRaster) -> RasterStats | None:
    """Count / centroid / spread / tight bbox of a mask bitmap, in
    original-image coordinates, computed run by run."""
    w, data = raster.width, raster.data
    count = 0
    sx = sy = sx2 = 0.0
    min_x = min_y = 1 << 62
    max_x = max_y = -1
    for y in range(raster.height):
        row = data[y * w : (y + 1) * w]
        if not row.count(1):
            continue
        gy = raster.offset_y + y
        for m in _RUN_RE.finditer(row):
            a = raster.offset_x + m.start()
            b = raster.offset_x + m.end() - 1  # inclusive
            n = b - a + 1
            count += n
            sx += n * (a + b) / 2.0
            sx2 += _sumsq_upto(b) - _sumsq_upto(a - 1)
            sy += n * gy
            if a < min_x:
                min_x = a
            if b > max_x:
                max_x = b
            if gy < min_y:
                min_y = gy
            if gy > max_y:
                max_y = gy
    if count == 0:
        return None
    mean_x = sx / count
    var_x = max(0.0, sx2 / count - mean_x * mean_x)
    return RasterStats(
        count=count,
        centroid_x=mean_x,
        centroid_y=sy / count,
        std_x=math.sqrt(var_x),
        min_x=min_x,
        min_y=min_y,
        max_x=max_x,
        max_y=max_y,
    )


def _det_raster(det: Detection) -> MaskRaster | None:
    if not det.mask_rle:
        return None
    try:
        return MaskRaster.from_rle(det.mask_rle)
    except Exception:
        return None


def _det_stats(det: Detection) -> RasterStats | None:
    raster = _det_raster(det)
    return raster_stats(raster) if raster is not None else None


def _conf_basis(dets: list[Detection]) -> str:
    parts = []
    for d in dets:
        c = f"{d.confidence:.2f}" if d.confidence is not None else "n/a (user)"
        parts.append(f"{d.kind} {d.id} conf {c}")
    return "; ".join(parts) if parts else "no source detections"


# ---------------------------------------------------------------------
# Measurement objects.
# ---------------------------------------------------------------------

STATUSES = ("measured", "estimate", "could_not_measure", "not_applicable")


@dataclass
class Measurement:
    name: str
    status: str
    value: float | dict | None
    unit: str | None
    method: str
    source_detection_ids: list[str] = field(default_factory=list)
    confidence_basis: str = ""
    reason: str | None = None  # why it could not be measured, when it could not
    uncertainty: dict | None = None
    flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown measurement status: {self.status}")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "value": _round_value(self.value),
            "unit": self.unit,
            "method": self.method,
            "source_detection_ids": list(self.source_detection_ids),
            "confidence_basis": self.confidence_basis,
            "reason": self.reason,
            "uncertainty": self.uncertainty,
            "flags": list(self.flags),
        }


def _round_value(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, dict):
        return {k: _round_value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_round_value(x) for x in v]
    return v


@dataclass
class Calibration:
    status: str  # "calibrated" | "could_not_calibrate"
    bed_length_ft: float | None = None
    pixels_per_inch: float | None = None
    bed_detection_id: str | None = None
    bed_extent_px: float | None = None
    bed_min_x: float | None = None
    bed_max_x: float | None = None  # exclusive
    method: str = ""
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "bed_length_ft": self.bed_length_ft,
            "pixels_per_inch": round(self.pixels_per_inch, 4)
            if self.pixels_per_inch is not None
            else None,
            "bed_detection_id": self.bed_detection_id,
            "bed_extent_px": round(self.bed_extent_px, 1)
            if self.bed_extent_px is not None
            else None,
            "bed_min_x": self.bed_min_x,
            "bed_max_x": self.bed_max_x,
            "axis": "x (front at bed_min_x, rear at bed_max_x)",
            "method": self.method,
            "reason": self.reason,
        }


@dataclass
class MeasurementReport:
    image_path: str
    image_width: int
    image_height: int
    load_type: str
    calibration: Calibration
    measurements: list[Measurement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def get(self, name: str) -> Measurement:
        for m in self.measurements:
            if m.name == name:
                return m
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {
            "schema": "loadmaster.measurements/1",
            "image_path": self.image_path,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "load_type": self.load_type,
            "calibration": self.calibration.to_dict(),
            "measurements": [m.to_dict() for m in self.measurements],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------
# Calibration.
# ---------------------------------------------------------------------

def calibrate(ds: DetectionSet, bed_length_ft: float | None) -> Calibration:
    if bed_length_ft is None or bed_length_ft <= 0:
        return Calibration(
            status="could_not_calibrate",
            bed_length_ft=bed_length_ft,
            method="user supplies the trailer bed length once per run",
            reason="no trailer bed length was supplied, so no pixel scale exists",
        )
    beds = ds.by_kind("trailer_bed")
    if not beds:
        return Calibration(
            status="could_not_calibrate",
            bed_length_ft=bed_length_ft,
            method="pixels-per-inch from the trailer_bed detection extent",
            reason="no trailer_bed detection in this photo, so there is no bed extent to scale from",
        )
    # The bed is the largest trailer_bed detection (mask area, else bbox area).
    def _area(d: Detection) -> float:
        st = _det_stats(d)
        return float(st.count) if st else d.width * d.height

    bed = max(beds, key=_area)
    st = _det_stats(bed)
    if st is not None:
        min_x, max_x = float(st.min_x), float(st.max_x + 1)
        extent = max_x - min_x
        height = float(st.max_y - st.min_y + 1)
        src = "mask bitmap tight extent"
    else:
        min_x, max_x = bed.bbox[0], bed.bbox[2]
        extent = bed.width
        height = bed.height
        src = "detection bbox (no mask)"
    if extent <= 0:
        return Calibration(
            status="could_not_calibrate",
            bed_length_ft=bed_length_ft,
            bed_detection_id=bed.id,
            method=f"pixels-per-inch from the trailer_bed {src}",
            reason="the trailer_bed extent has no width along the frame x axis",
        )
    if height > extent:
        return Calibration(
            status="could_not_calibrate",
            bed_length_ft=bed_length_ft,
            bed_detection_id=bed.id,
            method=f"pixels-per-inch from the trailer_bed {src}",
            reason=(
                "the bed extent is taller than it is wide in this frame, so the "
                "side-view x-axis mapping does not apply (photo may be rotated "
                "or a top-down view)"
            ),
        )
    ppi = extent / (bed_length_ft * 12.0)
    return Calibration(
        status="calibrated",
        bed_length_ft=bed_length_ft,
        pixels_per_inch=ppi,
        bed_detection_id=bed.id,
        bed_extent_px=extent,
        bed_min_x=min_x,
        bed_max_x=max_x,
        method=(
            f"pixels-per-inch = trailer_bed {src} along x "
            f"({extent:.0f} px) / user-supplied bed length "
            f"({bed_length_ft} ft = {bed_length_ft * 12:.0f} in)"
        ),
    )


# ---------------------------------------------------------------------
# Individual measurements. Each takes the calibration and degrades
# honestly when its inputs are missing.
# ---------------------------------------------------------------------

def _need_calibration(name: str, cal: Calibration, method: str) -> Measurement | None:
    if cal.status != "calibrated":
        return Measurement(
            name=name,
            status="could_not_measure",
            value=None,
            unit="in",
            method=method,
            reason=f"no calibration: {cal.reason}",
        )
    return None


def measure_rear_overhang(ds: DetectionSet, cal: Calibration) -> Measurement:
    method = (
        "rearmost cargo extent (mask bitmap tight edge, else bbox) minus the "
        "bed rear edge, scaled by the calibration; rear = image-right end of "
        "the bed extent"
    )
    blocked = _need_calibration("rear_overhang", cal, method)
    if blocked:
        return blocked
    cargo = ds.by_kind("cargo")
    if not cargo:
        return Measurement(
            name="rear_overhang", status="could_not_measure", value=None,
            unit="in", method=method,
            reason="no cargo detections, so there is no load extent to compare with the bed",
        )
    rear = -1.0
    contributors: list[Detection] = []
    for d in cargo:
        st = _det_stats(d)
        edge = st.rear_x if st else d.bbox[2]
        if edge > rear:
            rear = edge
            contributors = [d]
        elif edge == rear:
            contributors.append(d)
    overhang_px = max(0.0, rear - float(cal.bed_max_x))
    sources = [d.id for d in contributors] + [str(cal.bed_detection_id)]
    return Measurement(
        name="rear_overhang",
        status="measured",
        value=overhang_px / float(cal.pixels_per_inch),
        unit="in",
        method=method,
        source_detection_ids=sources,
        confidence_basis=_conf_basis(contributors),
    )


def measure_centroid_vs_axle(ds: DetectionSet, cal: Calibration) -> Measurement:
    method = (
        "bitmap-weighted centroid of all cargo masks along the bed axis vs "
        "the axle estimate position (midpoint of axle centres when several), "
        "scaled by the calibration"
    )
    flags = ["visual_estimate", "not_a_weight_measurement"]
    blocked = _need_calibration("load_centroid_vs_axle", cal, method)
    if blocked:
        blocked.flags = flags
        return blocked
    cargo = ds.by_kind("cargo")
    if not cargo:
        return Measurement(
            name="load_centroid_vs_axle", status="could_not_measure", value=None,
            unit="in", method=method, flags=flags,
            reason="no cargo detections, so there is no load centroid to place",
        )
    axles = ds.by_kind("axle_estimate")
    if not axles:
        return Measurement(
            name="load_centroid_vs_axle", status="could_not_measure", value=None,
            unit="in", method=method, flags=flags,
            reason="no axle_estimate detection, so there is no axle position to compare against",
        )

    # Combine cargo evidence: mask stats where masks exist; bbox
    # centres weighted by an estimated fill where they do not.
    total_w = 0.0
    sum_x = 0.0
    sum_x2 = 0.0
    stats_by_id: dict[str, RasterStats | None] = {d.id: _det_stats(d) for d in cargo}
    per_det = [(d, stats_by_id[d.id]) for d in cargo if stats_by_id[d.id] is not None]
    fill_ratio = 0.4  # fallback when nothing is masked
    if per_det:
        mask_area = sum(st.count for _, st in per_det)
        bbox_area = sum(
            (st.max_x - st.min_x + 1) * (st.max_y - st.min_y + 1) for _, st in per_det
        )
        if bbox_area > 0:
            fill_ratio = mask_area / bbox_area
    bbox_only: list[str] = []
    for d, st in per_det:
        w = float(st.count)
        total_w += w
        sum_x += w * st.centroid_x
        sum_x2 += w * (st.std_x**2 + st.centroid_x**2)
    for d in cargo:
        if stats_by_id[d.id] is None:
            bbox_only.append(d.id)
            w = max(1.0, d.width * d.height * fill_ratio)
            cx = d.center[0]
            var = (d.width**2) / 12.0  # uniform-box spread
            total_w += w
            sum_x += w * cx
            sum_x2 += w * (var + cx**2)
    centroid_x = sum_x / total_w
    var_x = max(0.0, sum_x2 / total_w - centroid_x**2)
    std_in = math.sqrt(var_x) / float(cal.pixels_per_inch)

    centers = sorted(d.center[0] for d in axles)
    axle_x = centers[0] if len(centers) == 1 else (centers[0] + centers[-1]) / 2.0
    offset_in = (centroid_x - axle_x) / float(cal.pixels_per_inch)
    bed_pct = (centroid_x - float(cal.bed_min_x)) / float(cal.bed_extent_px) * 100.0
    axle_pct = (axle_x - float(cal.bed_min_x)) / float(cal.bed_extent_px) * 100.0
    direction = "behind the axle (toward the rear)" if offset_in > 0.5 else (
        "ahead of the axle (toward the front)" if offset_in < -0.5 else "at the axle"
    )
    basis = _conf_basis(cargo + axles)
    if bbox_only:
        basis += (
            f"; cargo {', '.join(bbox_only)} had no mask, so bbox centres were "
            f"weighted by an estimated fill ratio of {fill_ratio:.2f}"
        )
    return Measurement(
        name="load_centroid_vs_axle",
        status="estimate",
        value={
            "offset_in": offset_in,
            "direction": direction,
            "centroid_bed_position_pct": bed_pct,
            "axle_bed_position_pct": axle_pct,
        },
        unit="in",
        method=method + "; positive offset = centroid behind the axle",
        source_detection_ids=[d.id for d in cargo] + [d.id for d in axles],
        confidence_basis=basis,
        uncertainty={
            "plus_minus_in": round(max(std_in, 2.0), 1),
            "basis": (
                "one bitmap-weighted standard deviation of the cargo pixels "
                "along the bed axis (the load's own spread), floored at 2 in; "
                "a photo shows where the bulk sits, not how heavy it is"
            ),
        },
        flags=flags,
    )


def _union_canvases(
    width: int, height: int, dets: list[Detection]
) -> bytearray | None:
    """Union of the given detections' bitmaps on an image-size canvas.
    Returns None when none of the detections carries a usable mask."""
    canvas: bytearray | None = None
    for d in dets:
        raster = _det_raster(d)
        if raster is None:
            continue
        if canvas is None:
            canvas = bytearray(width * height)
        w, data = raster.width, raster.data
        for y in range(raster.height):
            gy = raster.offset_y + y
            if gy < 0 or gy >= height:
                continue
            row = data[y * w : (y + 1) * w]
            if not row.count(1):
                continue
            base = gy * width
            for m in _RUN_RE.finditer(row):
                a = raster.offset_x + m.start()
                b = raster.offset_x + m.end()  # exclusive
                a_c, b_c = max(0, a), min(width, b)
                if a_c < b_c:
                    canvas[base + a_c : base + b_c] = b"\x01" * (b_c - a_c)
    return canvas


def _canvas_count(canvas: bytearray, width: int, height: int) -> int:
    total = 0
    for y in range(height):
        row = bytes(canvas[y * width : (y + 1) * width])
        total += int.from_bytes(row, "big").bit_count()
    return total


def _canvas_intersection(a: bytearray, b: bytearray, width: int, height: int) -> int:
    total = 0
    for y in range(height):
        ra = int.from_bytes(bytes(a[y * width : (y + 1) * width]), "big")
        if ra:
            rb = int.from_bytes(bytes(b[y * width : (y + 1) * width]), "big")
            total += (ra & rb).bit_count()
    return total


def measure_tarp_coverage(ds: DetectionSet) -> Measurement:
    method = (
        "tarp/canvas mask area overlapping the cargo area, as a percentage "
        "of the cargo area (bitmap union on the image canvas)"
    )
    flags = ["proxy_only"]
    load_norm = ds.load_type.strip().lower()
    if not any(tok in load_norm for tok in LOOSE_LOAD_TOKENS):
        return Measurement(
            name="tarp_coverage", status="not_applicable", value=None,
            unit="percent", method=method, flags=flags,
            reason=(
                f"load type is {ds.load_type!r}, not loose material; the tarp "
                "check only applies to loose loads"
            ),
        )
    cargo = ds.by_kind("cargo")
    if not cargo:
        return Measurement(
            name="tarp_coverage", status="could_not_measure", value=None,
            unit="percent", method=method, flags=flags,
            reason="no cargo detections, so there is no load area to cover",
        )
    tarps = [d for d in ds.by_kind("tarp") if not d.bonus_only]
    if not tarps:
        return Measurement(
            name="tarp_coverage",
            status="estimate",
            value={"covered_pct": 0.0, "exposed_pct": 100.0},
            unit="percent",
            method=method,
            source_detection_ids=[d.id for d in cargo],
            confidence_basis=(
                "no tarp or canvas cover was detected on this loose load; a "
                "missed detection is not proof that no cover exists, so this "
                "is an estimate from absence, and the cover prompts are a "
                "proxy signal (matrix: 0.71-0.76 on a canvas canopy)"
            ),
            flags=flags,
        )
    cargo_canvas = _union_canvases(ds.image_width, ds.image_height, cargo)
    tarp_canvas = _union_canvases(ds.image_width, ds.image_height, tarps)
    if cargo_canvas is not None and tarp_canvas is not None:
        cargo_area = _canvas_count(cargo_canvas, ds.image_width, ds.image_height)
        covered = _canvas_intersection(
            tarp_canvas, cargo_canvas, ds.image_width, ds.image_height
        )
        used_method = method
    else:
        # Bbox fallback: sum pairwise tarp x cargo bbox intersections,
        # capped at the summed cargo bbox area.
        cargo_area_f = sum(d.width * d.height for d in cargo)
        covered_f = 0.0
        for t in tarps:
            for c in cargo:
                ix = max(0.0, min(t.bbox[2], c.bbox[2]) - max(t.bbox[0], c.bbox[0]))
                iy = max(0.0, min(t.bbox[3], c.bbox[3]) - max(t.bbox[1], c.bbox[1]))
                covered_f += ix * iy
        cargo_area = int(cargo_area_f)
        covered = int(min(covered_f, cargo_area_f))
        used_method = method + "; bbox approximation because a mask was unavailable"
    if cargo_area <= 0:
        return Measurement(
            name="tarp_coverage", status="could_not_measure", value=None,
            unit="percent", method=used_method, flags=flags,
            reason="the cargo detections have no measurable area",
        )
    covered_pct = 100.0 * covered / cargo_area
    return Measurement(
        name="tarp_coverage",
        status="measured",
        value={"covered_pct": covered_pct, "exposed_pct": 100.0 - covered_pct},
        unit="percent",
        method=used_method,
        source_detection_ids=[d.id for d in tarps] + [d.id for d in cargo],
        confidence_basis=_conf_basis(tarps + cargo) + (
            "; proxy signal only: the tarp/canvas prompts scored 0.71-0.76 on "
            "a canvas canopy in the prompt matrix and are not yet validated "
            "on a real tarp-over-load photo"
        ),
        flags=flags,
    )


def measure_tiedown_gaps(ds: DetectionSet, cal: Calibration) -> Measurement:
    method = (
        "tie-down anchor positions mapped onto the bed axis (anchor centre "
        "x in bed coordinates); spans between neighbouring anchors and the "
        "bed ends, scaled by the calibration"
    )
    tiedowns = [
        d for d in ds.by_kind("tiedown_closeup") if not d.bonus_only
    ]
    if not tiedowns:
        return Measurement(
            name="tiedown_coverage_gaps", status="could_not_measure", value=None,
            unit="in", method=method,
            reason=(
                "no tie-down detections in this detection set: tie-downs are "
                "counted from anchor close-up photos plus tap-to-correct, and "
                "close-up coordinates live in the close-up photo's own image "
                "space, so they cannot be placed on this photo's bed axis"
            ),
        )
    blocked = _need_calibration("tiedown_coverage_gaps", cal, method)
    if blocked:
        blocked.reason = (
            "tie-down detections exist but cannot be placed on the bed axis: "
            f"{cal.reason}"
        )
        return blocked
    anchors = sorted(
        (d.center[0] - float(cal.bed_min_x)) / float(cal.bed_extent_px)
        for d in tiedowns
    )
    points = [0.0] + anchors + [1.0]
    spans_in = [
        (points[i + 1] - points[i]) * float(cal.bed_length_ft) * 12.0
        for i in range(len(points) - 1)
    ]
    return Measurement(
        name="tiedown_coverage_gaps",
        status="measured",
        value={
            "largest_gap_in": max(spans_in),
            "anchor_count": len(tiedowns),
            "spans_in": spans_in,
            "anchor_positions_pct_of_bed": [round(a * 100.0, 1) for a in anchors],
        },
        unit="in",
        method=method,
        source_detection_ids=[d.id for d in tiedowns] + [str(cal.bed_detection_id)],
        confidence_basis=_conf_basis(tiedowns),
    )


# ---------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------

def measure_load(ds: DetectionSet, bed_length_ft: float | None) -> MeasurementReport:
    cal = calibrate(ds, bed_length_ft)
    report = MeasurementReport(
        image_path=ds.image_path,
        image_width=ds.image_width,
        image_height=ds.image_height,
        load_type=ds.load_type,
        calibration=cal,
    )
    report.measurements = [
        measure_rear_overhang(ds, cal),
        measure_centroid_vs_axle(ds, cal),
        measure_tarp_coverage(ds),
        measure_tiedown_gaps(ds, cal),
    ]
    if cal.status != "calibrated":
        report.notes.append(
            f"calibration unavailable: {cal.reason}; inch measurements are "
            "reported as could_not_measure rather than guessed"
        )
    report.notes.append(
        "load_centroid_vs_axle is a visual estimate of where the load's bulk "
        "sits; it is not a weight measurement"
    )
    return report


def write_measurements(
    ds: DetectionSet, bed_length_ft: float | None, out_path: Path
) -> MeasurementReport:
    report = measure_load(ds, bed_length_ft)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), indent=1), encoding="utf-8")
    return report

"""Measurement layer tests (Day 2). Synthetic masks with known
geometry only; no live API. Image space is 1000x500 throughout.

Reference rig: bed mask fills x 100..899, y 200..299 (800 px wide).
With a 10 ft bed (120 in) the scale is 800/120 = 6.6667 px/in.
"""

import json

import pytest

from loadmaster.detections import DetectionSet
from loadmaster.mask_decode import MaskRaster
from loadmaster.measure import calibrate, measure_load

BED_FT = 10.0
PPI = 800.0 / 120.0


def rect_mask(ox: int, oy: int, w: int, h: int) -> dict:
    raster = MaskRaster(
        width=w, height=h, offset_x=ox, offset_y=oy, data=bytes([1]) * (w * h)
    )
    return raster.to_rle()


def make_set(load_type: str = "firewood") -> DetectionSet:
    return DetectionSet(
        image_path="/tmp/photo.jpg", image_width=1000, image_height=500,
        trailer_type="utility", load_type=load_type, photo_role="side",
    )


def add_bed(ds: DetectionSet) -> None:
    ds.add("trailer_bed", [100, 200, 900, 300], confidence=0.86,
           mask_rle=rect_mask(100, 200, 800, 100))


def add_axle(ds: DetectionSet, cx: float = 500.0) -> None:
    ds.add("axle_estimate", [cx - 40, 300, cx + 40, 380], confidence=0.92)


# -- calibration ------------------------------------------------------

def test_calibration_math():
    ds = make_set()
    add_bed(ds)
    cal = calibrate(ds, BED_FT)
    assert cal.status == "calibrated"
    assert cal.pixels_per_inch == pytest.approx(PPI)
    assert cal.bed_min_x == 100.0
    assert cal.bed_max_x == 900.0
    assert cal.bed_extent_px == 800.0


def test_calibration_needs_bed_length():
    ds = make_set()
    add_bed(ds)
    cal = calibrate(ds, None)
    assert cal.status == "could_not_calibrate"
    assert "bed length" in cal.reason


def test_calibration_needs_bed_detection():
    ds = make_set()
    cal = calibrate(ds, BED_FT)
    assert cal.status == "could_not_calibrate"
    assert "trailer_bed" in cal.reason


def test_calibration_rejects_vertical_bed():
    ds = make_set()
    ds.add("trailer_bed", [400, 50, 500, 450], confidence=0.8,
           mask_rle=rect_mask(400, 50, 100, 400))
    cal = calibrate(ds, BED_FT)
    assert cal.status == "could_not_calibrate"
    assert "taller than it is wide" in cal.reason


# -- rear overhang ----------------------------------------------------

def test_rear_overhang_past_bed_edge():
    ds = make_set()
    add_bed(ds)
    # Cargo mask ends at x 1049 (rear edge 1050): 150 px past the bed.
    ds.add("cargo", [850, 220, 1050, 280], confidence=0.65,
           mask_rle=rect_mask(850, 220, 200, 60))
    m = measure_load(ds, BED_FT).get("rear_overhang")
    assert m.status == "measured"
    assert m.value == pytest.approx(150.0 / PPI)  # 22.5 in
    assert m.unit == "in"
    assert m.source_detection_ids  # bed + cargo ids recorded


def test_rear_overhang_zero_when_load_inside_bed():
    ds = make_set()
    add_bed(ds)
    ds.add("cargo", [200, 220, 700, 280], confidence=0.65,
           mask_rle=rect_mask(200, 220, 500, 60))
    m = measure_load(ds, BED_FT).get("rear_overhang")
    assert m.status == "measured"
    assert m.value == 0.0


def test_overhang_degrades_without_cargo_or_calibration():
    ds = make_set()
    add_bed(ds)
    m = measure_load(ds, BED_FT).get("rear_overhang")
    assert m.status == "could_not_measure"
    assert "no cargo" in m.reason

    ds2 = make_set()
    add_bed(ds2)
    ds2.add("cargo", [850, 220, 1050, 280], confidence=0.65,
            mask_rle=rect_mask(850, 220, 200, 60))
    m2 = measure_load(ds2, None).get("rear_overhang")
    assert m2.status == "could_not_measure"
    assert "no calibration" in m2.reason


# -- centroid vs axle ---------------------------------------------------

def test_centroid_behind_axle():
    ds = make_set()
    add_bed(ds)
    add_axle(ds, cx=500.0)
    # Cargo rect x 500..699: centroid x = 599.5.
    ds.add("cargo", [500, 220, 700, 270], confidence=0.7,
           mask_rle=rect_mask(500, 220, 200, 50))
    m = measure_load(ds, BED_FT).get("load_centroid_vs_axle")
    assert m.status == "estimate"
    assert m.value["offset_in"] == pytest.approx(99.5 / PPI, abs=0.01)
    assert m.value["direction"].startswith("behind")
    assert m.value["centroid_bed_position_pct"] == pytest.approx(62.44, abs=0.01)
    assert "visual_estimate" in m.flags
    assert "not_a_weight_measurement" in m.flags
    assert m.uncertainty["plus_minus_in"] >= 2.0


def test_centroid_ahead_of_axle():
    ds = make_set()
    add_bed(ds)
    add_axle(ds, cx=500.0)
    ds.add("cargo", [200, 220, 300, 270], confidence=0.7,
           mask_rle=rect_mask(200, 220, 100, 50))
    m = measure_load(ds, BED_FT).get("load_centroid_vs_axle")
    assert m.value["offset_in"] == pytest.approx((249.5 - 500.0) / PPI, abs=0.01)
    assert m.value["direction"].startswith("ahead")


def test_centroid_degrades_without_axle():
    ds = make_set()
    add_bed(ds)
    ds.add("cargo", [500, 220, 700, 270], confidence=0.7,
           mask_rle=rect_mask(500, 220, 200, 50))
    m = measure_load(ds, BED_FT).get("load_centroid_vs_axle")
    assert m.status == "could_not_measure"
    assert "axle" in m.reason


# -- tarp coverage ------------------------------------------------------

def test_tarp_half_covered():
    ds = make_set(load_type="loose material")
    add_bed(ds)
    ds.add("cargo", [100, 200, 300, 300], confidence=0.6,
           mask_rle=rect_mask(100, 200, 200, 100))
    ds.add("tarp", [100, 200, 200, 300], confidence=0.75,
           mask_rle=rect_mask(100, 200, 100, 100))
    m = measure_load(ds, BED_FT).get("tarp_coverage")
    assert m.status == "measured"
    assert m.value["covered_pct"] == pytest.approx(50.0)
    assert m.value["exposed_pct"] == pytest.approx(50.0)
    assert "proxy_only" in m.flags


def test_tarp_absent_is_an_estimate_not_a_proof():
    ds = make_set(load_type="loose material")
    add_bed(ds)
    ds.add("cargo", [100, 200, 300, 300], confidence=0.6,
           mask_rle=rect_mask(100, 200, 200, 100))
    m = measure_load(ds, BED_FT).get("tarp_coverage")
    assert m.status == "estimate"
    assert m.value == {"covered_pct": 0.0, "exposed_pct": 100.0}


def test_tarp_not_applicable_for_solid_loads():
    ds = make_set(load_type="firewood")
    add_bed(ds)
    m = measure_load(ds, BED_FT).get("tarp_coverage")
    assert m.status == "not_applicable"
    assert "loose" in m.reason


# -- tie-down gaps ------------------------------------------------------

def test_tiedown_largest_gap():
    ds = make_set()
    add_bed(ds)
    # Anchors at x centres 300 and 700 -> bed fractions 0.25 / 0.75.
    ds.add("tiedown_closeup", [280, 250, 320, 290], confidence=0.79)
    ds.add("tiedown_closeup", [680, 250, 720, 290], confidence=0.79)
    m = measure_load(ds, BED_FT).get("tiedown_coverage_gaps")
    assert m.status == "measured"
    assert m.value["anchor_count"] == 2
    assert m.value["spans_in"] == pytest.approx([30.0, 60.0, 30.0])
    assert m.value["largest_gap_in"] == pytest.approx(60.0)


def test_tiedown_gaps_degrade_without_inframe_anchors():
    ds = make_set()
    add_bed(ds)
    m = measure_load(ds, BED_FT).get("tiedown_coverage_gaps")
    assert m.status == "could_not_measure"
    assert "close-up" in m.reason


# -- report shape -------------------------------------------------------

def test_report_is_json_with_provenance():
    ds = make_set()
    add_bed(ds)
    add_axle(ds)
    ds.add("cargo", [500, 220, 700, 270], confidence=0.7,
           mask_rle=rect_mask(500, 220, 200, 50))
    report = measure_load(ds, BED_FT)
    json.dumps(report.to_dict())  # must not raise
    assert report.to_dict()["schema"] == "loadmaster.measurements/1"
    for m in report.measurements:
        d = m.to_dict()
        assert d["method"]
        assert "source_detection_ids" in d
        assert "confidence_basis" in d
        if d["status"] == "could_not_measure":
            assert d["reason"]

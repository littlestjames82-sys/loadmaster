"""Rules engine tests (Day 3). Fully synthetic fixtures; no live
API. Image space is 1000x500 throughout; calibration is 800 px of
bed (x 100..900) at 6.6667 px/in unless a fixture says otherwise.
"""

import pytest

from loadmaster.detections import DetectionSet
from loadmaster.mask_decode import MaskRaster
from loadmaster.rules import (
    CHECK_THIS,
    COULD_NOT_TELL,
    PASS,
    UserInputs,
    evaluate_rules,
    required_tiedowns,
)

PPI = 800.0 / 120.0


def rect_mask(ox, oy, w, h):
    raster = MaskRaster(width=w, height=h, offset_x=ox, offset_y=oy,
                        data=bytes([1]) * (w * h))
    return raster.to_rle()


def make_set(role="side", load_type="generic"):
    return DetectionSet(
        image_path="/tmp/photo.jpg", image_width=1000, image_height=500,
        trailer_type="utility", load_type=load_type, photo_role=role,
    )


def closeup_set(n_tiedowns, bonus=0, user_marks=0):
    ds = make_set(role="closeup")
    for i in range(n_tiedowns):
        ds.add("tiedown_closeup", [10 + i * 30, 10, 30 + i * 30, 40],
               confidence=0.79, prompt="ratchet strap")
    for i in range(bonus):
        ds.add("tiedown_closeup", [400 + i * 30, 10, 420 + i * 30, 40],
               confidence=0.5, prompt="ratchet strap", bonus_only=True)
    for i in range(user_marks):
        det = ds.add("user_added", [600 + i * 30, 10, 620 + i * 30, 40],
                     source="user")
        det.label = "tie-down (user)"
    return ds


def side_set(cargo_front_x=110, cargo_back_x=700):
    ds = make_set(role="side")
    ds.add("trailer_bed", [100, 200, 900, 300], confidence=0.86,
           mask_rle=rect_mask(100, 200, 800, 100))
    ds.add("cargo", [cargo_front_x, 150, cargo_back_x, 250], confidence=0.66,
           mask_rle=rect_mask(cargo_front_x, 150, cargo_back_x - cargo_front_x, 100))
    ds.add("axle_estimate", [460, 300, 540, 380], confidence=0.92)
    return ds


def calibration(status="calibrated", reason=None):
    return {
        "status": status, "bed_length_ft": 10.0,
        "pixels_per_inch": PPI if status == "calibrated" else None,
        "bed_detection_id": "bed-1",
        "bed_extent_px": 800.0, "bed_min_x": 100.0, "bed_max_x": 900.0,
        "reason": reason,
    }


def measurements(cal=None, overhang=None, centroid=None, tarp=None):
    ms = []
    if overhang is not None:
        ms.append({"name": "rear_overhang", **overhang})
    if centroid is not None:
        ms.append({"name": "load_centroid_vs_axle", **centroid})
    if tarp is not None:
        ms.append({"name": "tarp_coverage", **tarp})
    return {
        "schema": "loadmaster.measurements/1",
        "calibration": cal if cal is not None else calibration(),
        "measurements": ms,
    }


def overhang_m(value, status="measured"):
    return {"status": status, "value": value, "unit": "in",
            "source_detection_ids": ["cargo-1", "bed-1"],
            "confidence_basis": "cargo conf 0.66", "reason": None, "flags": []}


def centroid_m(offset, unc=10.0, pct=50.0):
    return {"status": "estimate",
            "value": {"offset_in": offset,
                      "direction": "behind the axle (toward the rear)" if offset > 0 else "ahead of the axle (toward the front)",
                      "centroid_bed_position_pct": pct,
                      "axle_bed_position_pct": 40.0},
            "unit": "in", "source_detection_ids": ["cargo-1"],
            "confidence_basis": "cargo conf 0.66",
            "uncertainty": {"plus_minus_in": unc, "basis": "spread"},
            "flags": ["visual_estimate", "not_a_weight_measurement"],
            "reason": None}


def tarp_m(exposed, status="measured"):
    return {"status": status,
            "value": {"covered_pct": 100.0 - exposed, "exposed_pct": exposed},
            "unit": "percent", "source_detection_ids": ["tarp-1"],
            "confidence_basis": "tarp conf 0.71",
            "flags": ["proxy_only"], "reason": None}


def by_id(report):
    return {v["check_id"]: v for v in report["verdicts"]}


def inputs(**kw):
    base = {"load_length_ft": 8.0, "load_type": "general"}
    base.update(kw)
    return UserInputs(**base)


# -- required-count arithmetic (393.110 table) ------------------------

@pytest.mark.parametrize("length,weight,expected", [
    (4.0, 900.0, 1), (5.0, 1100.0, 1), (5.0, 1100.01, 2),
    (4.0, 5000.0, 2), (6.0, None, 2), (10.0, None, 2),
    (10.5, None, 3), (20.0, None, 3), (20.1, None, 4), (30.0, None, 4),
])
def test_required_tiedowns_table(length, weight, expected):
    req, _ = required_tiedowns(length, weight)
    assert req == expected


def test_required_tiedowns_short_load_unknown_weight():
    req, note = required_tiedowns(4.0, None)
    assert req is None and "1,100" in note


# -- check 1: tie-down count ------------------------------------------

def test_count_no_basis_could_not_tell():
    rep = evaluate_rules([side_set()], None, inputs())
    v = by_id(rep)["tiedown_count"]
    assert v["status"] == COULD_NOT_TELL
    assert "close-up" in v["finding"]


def test_count_bonus_only_never_counts():
    ds = closeup_set(0, bonus=3)
    rep = evaluate_rules([ds], None, inputs())
    v = by_id(rep)["tiedown_count"]
    # Close-up set exists (basis present) but every strap hit is
    # bonus-only, so the observed count is 0 -> shortfall.
    assert v["status"] == CHECK_THIS
    assert rep["tiedown_count_basis"]["count"] == 0


def test_count_pass_and_shortfall():
    rep = evaluate_rules([closeup_set(2)], None, inputs())
    assert by_id(rep)["tiedown_count"]["status"] == PASS
    rep = evaluate_rules([closeup_set(1)], None, inputs())
    v = by_id(rep)["tiedown_count"]
    assert v["status"] == CHECK_THIS and "Short by 1" in v["finding"]
    assert v["minimum_fix"] and "1 more" in v["minimum_fix"]


def test_count_user_marks_count():
    rep = evaluate_rules([closeup_set(1, user_marks=1)], None, inputs())
    assert rep["tiedown_count_basis"]["count"] == 2
    assert by_id(rep)["tiedown_count"]["status"] == PASS


def test_count_user_cargo_mark_does_not_count():
    ds = closeup_set(1)
    det = ds.add("user_added", [700, 10, 760, 60], source="user")
    det.label = "cargo (user)"
    rep = evaluate_rules([ds], None, inputs())
    assert rep["tiedown_count_basis"]["count"] == 1


def test_count_no_length_could_not_tell():
    rep = evaluate_rules([closeup_set(4)], None, inputs(load_length_ft=None))
    assert by_id(rep)["tiedown_count"]["status"] == COULD_NOT_TELL


def test_count_short_load_weight_branches():
    # <=5 ft, weight unknown: 2 observed satisfies either branch.
    rep = evaluate_rules([closeup_set(2)], None,
                         inputs(load_length_ft=4.0, weight_lb=None))
    assert by_id(rep)["tiedown_count"]["status"] == PASS
    # 1 observed: depends on the un-supplied weight.
    rep = evaluate_rules([closeup_set(1)], None,
                         inputs(load_length_ft=4.0, weight_lb=None))
    assert by_id(rep)["tiedown_count"]["status"] == COULD_NOT_TELL
    # 0 observed fails both branches.
    rep = evaluate_rules([closeup_set(0)], None,
                         inputs(load_length_ft=4.0, weight_lb=None))
    assert by_id(rep)["tiedown_count"]["status"] == CHECK_THIS
    # Weight supplied: 900 lb -> 1 required; 5,000 lb -> 2 required.
    rep = evaluate_rules([closeup_set(1)], None,
                         inputs(load_length_ft=4.0, weight_lb=900.0))
    assert by_id(rep)["tiedown_count"]["status"] == PASS
    rep = evaluate_rules([closeup_set(1)], None,
                         inputs(load_length_ft=4.0, weight_lb=5000.0))
    assert by_id(rep)["tiedown_count"]["status"] == CHECK_THIS


# -- check 2: aggregate WLL -------------------------------------------

def test_wll_not_computable_paths():
    # No ratings, no weight.
    rep = evaluate_rules([closeup_set(2)], None, inputs())
    v = by_id(rep)["aggregate_wll"]
    assert v["status"] == COULD_NOT_TELL
    assert "cannot verify" in v["finding"]
    # Ratings but no weight.
    rep = evaluate_rules([closeup_set(2)], None, inputs(tiedown_wll_lb=2000.0))
    assert by_id(rep)["aggregate_wll"]["status"] == COULD_NOT_TELL
    # Weight but no ratings.
    rep = evaluate_rules([closeup_set(2)], None, inputs(weight_lb=4000.0))
    assert by_id(rep)["aggregate_wll"]["status"] == COULD_NOT_TELL


def test_wll_direct_counts_half_indirect_full():
    # 2 tie-downs, 2,000 lb each, indirect: aggregate 4,000 >= 50%
    # of 6,000 lb (3,000). PASS.
    rep = evaluate_rules([closeup_set(2)], None,
                         inputs(weight_lb=6000.0, tiedown_wll_lb=2000.0,
                                tiedown_attachment="indirect"))
    assert by_id(rep)["aggregate_wll"]["status"] == PASS
    # Same straps direct: aggregate 2,000 < 3,000. CHECK_THIS.
    rep = evaluate_rules([closeup_set(2)], None,
                         inputs(weight_lb=6000.0, tiedown_wll_lb=2000.0,
                                tiedown_attachment="direct"))
    v = by_id(rep)["aggregate_wll"]
    assert v["status"] == CHECK_THIS and "Short by 1,000" in v["finding"]


def test_wll_per_tiedown_list():
    rep = evaluate_rules(
        [closeup_set(2)], None,
        inputs(weight_lb=4000.0,
               tiedown_wll_lb=[{"wll_lb": 3000.0, "attachment": "direct"},
                               {"wll_lb": 1000.0, "attachment": "indirect"}]))
    # 3000*0.5 + 1000*1 = 2,500 >= 2,000 required.
    assert by_id(rep)["aggregate_wll"]["status"] == PASS


# -- check 3: heavy equipment ------------------------------------------

def test_heavy_not_applicable_general_load():
    rep = evaluate_rules([closeup_set(2)], None, inputs())
    assert "heavy_equipment" not in by_id(rep)
    na = {x["check_id"] for x in rep["not_applicable"]}
    assert "heavy_equipment" in na


def test_heavy_weight_unknown_could_not_tell():
    rep = evaluate_rules([closeup_set(4)], None,
                         inputs(load_type="heavy_wheeled_tracked"))
    assert by_id(rep)["heavy_equipment"]["status"] == COULD_NOT_TELL


def test_heavy_under_trigger_weight_not_applicable():
    rep = evaluate_rules([closeup_set(4)], None,
                         inputs(load_type="heavy_wheeled_tracked",
                                weight_lb=9000.0))
    assert "heavy_equipment" not in by_id(rep)


def test_heavy_four_tiedowns_pass_and_shortfall():
    rep = evaluate_rules([closeup_set(4)], None,
                         inputs(load_type="heavy_wheeled_tracked",
                                weight_lb=14000.0))
    v = by_id(rep)["heavy_equipment"]
    assert v["status"] == PASS
    assert "cannot be verified" in v["finding"]  # accessory honesty note
    rep = evaluate_rules([closeup_set(3)], None,
                         inputs(load_type="heavy_wheeled_tracked",
                                weight_lb=14000.0))
    v = by_id(rep)["heavy_equipment"]
    assert v["status"] == CHECK_THIS and "Short by 1" in v["finding"]
    rep = evaluate_rules([side_set()], None,
                         inputs(load_type="heavy_wheeled_tracked",
                                weight_lb=14000.0))
    assert by_id(rep)["heavy_equipment"]["status"] == COULD_NOT_TELL


# -- check 4: forward blocking ------------------------------------------

def test_forward_blocking_pass_small_gap():
    # Cargo front at x=110, bed front x=100 -> 10 px = 1.5 in gap.
    rep = evaluate_rules([side_set(cargo_front_x=110)],
                         measurements(overhang=overhang_m(0.0)), inputs())
    assert by_id(rep)["forward_blocking"]["status"] == PASS


def test_forward_blocking_check_mid_gap():
    # Cargo front at x=127 -> 27 px = about 4 in gap: too far to call
    # it blocked (nothing in the photo shows what would stop it), so
    # the tightened bar returns CHECK THIS, not PASS.
    rep = evaluate_rules([side_set(cargo_front_x=127)],
                         measurements(overhang=overhang_m(0.0)), inputs())
    v = by_id(rep)["forward_blocking"]
    assert v["status"] == CHECK_THIS and "nothing in the photo shows" in v["finding"]


def test_forward_blocking_check_large_gap():
    # Cargo front at x=400 -> 300 px = 45 in gap.
    rep = evaluate_rules([side_set(cargo_front_x=400)],
                         measurements(overhang=overhang_m(0.0)), inputs())
    v = by_id(rep)["forward_blocking"]
    assert v["status"] == CHECK_THIS and "45.0 in" in v["finding"]


def test_forward_blocking_no_calibration():
    rep = evaluate_rules([side_set()], None, inputs())
    assert by_id(rep)["forward_blocking"]["status"] == COULD_NOT_TELL
    bad = measurements(cal=calibration(status="could_not_calibrate",
                                        reason="no bed length"))
    rep = evaluate_rules([side_set()], bad, inputs())
    assert by_id(rep)["forward_blocking"]["status"] == COULD_NOT_TELL


# -- check 5: edge protection -------------------------------------------

def test_edge_protection_default_could_not_tell():
    rep = evaluate_rules([closeup_set(1)], None, inputs())
    v = by_id(rep)["edge_protection"]
    assert v["status"] == COULD_NOT_TELL
    assert "close-up of each strap" in v["finding"]


def test_edge_protection_from_crosscheck():
    rec_yes = [{"photo_role": "closeup", "status": "ok",
                "view": {"tiedown_count": 1,
                         "straps_over_sharp_edge_without_protection": "yes"}}]
    rep = evaluate_rules([closeup_set(1)], None, inputs(), crosscheck=rec_yes)
    assert by_id(rep)["edge_protection"]["status"] == CHECK_THIS
    rec_no = [{"photo_role": "closeup", "status": "ok",
               "view": {"tiedown_count": 1,
                        "straps_over_sharp_edge_without_protection": "no"}}]
    rep = evaluate_rules([closeup_set(1)], None, inputs(), crosscheck=rec_no)
    assert by_id(rep)["edge_protection"]["status"] == PASS


# -- check 6: overhang + lights/plate -----------------------------------

def test_overhang_zero_pass_part_but_lights_unknown():
    rep = evaluate_rules([side_set()], measurements(overhang=overhang_m(0.0)),
                         inputs())
    v = by_id(rep)["overhang_lights_plate"]
    # Overhang half passes; lights half unknown -> combined CNT.
    assert v["status"] == COULD_NOT_TELL
    assert "0 in overhang" in v["finding"] and "not checked" in v["finding"]


def test_overhang_positive_check_this():
    rep = evaluate_rules([side_set()], measurements(overhang=overhang_m(9.5)),
                         inputs())
    v = by_id(rep)["overhang_lights_plate"]
    assert v["status"] == CHECK_THIS and "9.5 in" in v["finding"]


def test_lights_obscured_and_visible():
    rec_obs = [{"photo_role": "rear", "status": "ok",
                "view": {"tail_lights_visible": False,
                         "license_plate_visible": True,
                         "tarp_covering_load": "unclear"}}]
    rep = evaluate_rules([side_set()], measurements(overhang=overhang_m(0.0)),
                         inputs(), crosscheck=rec_obs)
    v = by_id(rep)["overhang_lights_plate"]
    assert v["status"] == CHECK_THIS and "tail lights" in v["finding"]
    rec_ok = [{"photo_role": "rear", "status": "ok",
               "view": {"tail_lights_visible": True,
                        "license_plate_visible": True,
                        "tarp_covering_load": "unclear"}}]
    rep = evaluate_rules([side_set()], measurements(overhang=overhang_m(0.0)),
                         inputs(), crosscheck=rec_ok)
    assert by_id(rep)["overhang_lights_plate"]["status"] == PASS


def test_overhang_measurement_unavailable():
    m = {"status": "could_not_measure", "value": None, "reason": "no cargo",
         "source_detection_ids": [], "confidence_basis": "", "flags": []}
    rep = evaluate_rules([side_set()], measurements(overhang=m), inputs())
    assert by_id(rep)["overhang_lights_plate"]["status"] == COULD_NOT_TELL


# -- check 7: tarp coverage ----------------------------------------------

def test_tarp_not_applicable_general():
    rep = evaluate_rules([side_set()], measurements(tarp=tarp_m(0.0)), inputs())
    assert "tarp_coverage" not in by_id(rep)
    assert "tarp_coverage" in {x["check_id"] for x in rep["not_applicable"]}


def test_tarp_pass_and_gap():
    loose = inputs(load_type="loose_material")
    rep = evaluate_rules([side_set()], measurements(tarp=tarp_m(0.0)), loose)
    v = by_id(rep)["tarp_coverage"]
    assert v["status"] == PASS and "proxy" in v["confidence_basis"]
    rep = evaluate_rules([side_set()], measurements(tarp=tarp_m(35.0)), loose)
    v = by_id(rep)["tarp_coverage"]
    assert v["status"] == CHECK_THIS and "35%" in v["finding"]


def test_tarp_measurement_missing():
    loose = inputs(load_type="loose_material")
    rep = evaluate_rules([side_set()], measurements(), loose)
    assert by_id(rep)["tarp_coverage"]["status"] == COULD_NOT_TELL
    m = tarp_m(0.0, status="could_not_measure")
    m["value"] = None
    m["reason"] = "no cargo detections"
    rep = evaluate_rules([side_set()], measurements(tarp=m), loose)
    assert by_id(rep)["tarp_coverage"]["status"] == COULD_NOT_TELL


# -- check 8: weight placement -------------------------------------------

def test_weight_placement_never_passes():
    # Bulk well ahead of the axle: still not a PASS.
    rep = evaluate_rules([side_set()],
                         measurements(centroid=centroid_m(-40.0, unc=10.0)),
                         inputs())
    v = by_id(rep)["weight_placement"]
    assert v["status"] == COULD_NOT_TELL
    assert "50%" in v["finding"]  # the estimate is stated
    # Within uncertainty: CNT with the estimate stated.
    rep = evaluate_rules([side_set()],
                         measurements(centroid=centroid_m(5.0, unc=10.0)),
                         inputs())
    assert by_id(rep)["weight_placement"]["status"] == COULD_NOT_TELL


def test_weight_placement_rear_heavy_check_this():
    rep = evaluate_rules([side_set()],
                         measurements(centroid=centroid_m(22.7, unc=19.7)),
                         inputs())
    v = by_id(rep)["weight_placement"]
    assert v["status"] == CHECK_THIS
    assert "22.7 in behind the axle" in v["finding"]
    assert "not a weight measurement" in v["finding"]


def test_weight_placement_no_measurement():
    rep = evaluate_rules([side_set()], None, inputs())
    assert by_id(rep)["weight_placement"]["status"] == COULD_NOT_TELL


# -- cross-check disagreement flips ---------------------------------------

def test_crosscheck_tiedown_disagreement_demotes_pass():
    rec = [{"photo_role": "closeup", "status": "ok",
            "view": {"tiedown_count": 1,
                     "straps_over_sharp_edge_without_protection": "unclear"}}]
    rep = evaluate_rules([closeup_set(2)], None, inputs(), crosscheck=rec)
    v = by_id(rep)["tiedown_count"]
    assert v["status"] == CHECK_THIS  # was PASS at SAM count 2
    assert v["cross_check"]["agreement"] is False
    assert "Spark" in v["cross_check"]["spark_view"]


def test_crosscheck_tiedown_agreement_keeps_pass():
    rec = [{"photo_role": "closeup", "status": "ok",
            "view": {"tiedown_count": 2,
                     "straps_over_sharp_edge_without_protection": "unclear"}}]
    rep = evaluate_rules([closeup_set(2)], None, inputs(), crosscheck=rec)
    v = by_id(rep)["tiedown_count"]
    assert v["status"] == PASS and v["cross_check"]["agreement"] is True


def test_crosscheck_never_upgrades():
    # SAM count short (CHECK_THIS); Spark agrees with the short
    # count. Status must stay CHECK_THIS, never become PASS.
    rec = [{"photo_role": "closeup", "status": "ok",
            "view": {"tiedown_count": 1,
                     "straps_over_sharp_edge_without_protection": "unclear"}}]
    rep = evaluate_rules([closeup_set(1)], None, inputs(), crosscheck=rec)
    assert by_id(rep)["tiedown_count"]["status"] == CHECK_THIS


def test_crosscheck_side_front_blocked_flip():
    rec = [{"photo_role": "side", "status": "ok",
            "view": {"load_looks_rear_heavy": "no", "front_blocked": "no"}}]
    rep = evaluate_rules([side_set(cargo_front_x=110)],
                         measurements(overhang=overhang_m(0.0)), inputs(),
                         crosscheck=rec)
    v = by_id(rep)["forward_blocking"]
    assert v["status"] == CHECK_THIS  # geometry PASS demoted
    assert v["cross_check"]["agreement"] is False


# -- report shape ---------------------------------------------------------

def test_report_shape_and_risk_order():
    rep = evaluate_rules([closeup_set(2)], None, inputs())
    assert rep["schema"] == "loadmaster.verdicts/1"
    ranks = [v["risk_rank"] for v in rep["verdicts"]]
    assert ranks == sorted(ranks)
    first = rep["verdicts"][0]
    assert first["check_id"] == "forward_blocking" and first["risk_rank"] == 1
    for v in rep["verdicts"]:
        assert set(v) >= {"check_id", "cfr_citation", "status", "finding",
                          "evidence", "minimum_fix", "confidence_basis",
                          "risk_rank"}
        assert v["status"] in (PASS, CHECK_THIS, COULD_NOT_TELL)
        if v["status"] == CHECK_THIS:
            assert v["minimum_fix"]
    total = sum(rep["summary"].values())
    assert total == len(rep["verdicts"])

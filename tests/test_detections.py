"""Detection model tests: tap-to-correct operations and the
JSON serialize/deserialize round-trip."""

import json

import pytest

from loadmaster.detections import DetectionSet


def make_set() -> DetectionSet:
    return DetectionSet(
        image_path="/tmp/photo.jpg", image_width=2000, image_height=1000,
        trailer_type="utility", load_type="firewood", photo_role="side",
    )


def test_add_and_by_kind():
    ds = make_set()
    det = ds.add("cargo", [10, 20, 300, 400], confidence=0.66, prompt="logs")
    assert det.source == "sam"
    assert ds.by_kind("cargo") == [det]
    assert ds.by_kind("tarp") == []


def test_add_user_marks_correction():
    ds = make_set()
    det = ds.add_user("tiedown_closeup", [50, 50, 90, 120])
    assert det.source == "user"
    assert det.user_corrected is True
    assert det.confidence is None


def test_remove():
    ds = make_set()
    det = ds.add("cargo", [0, 0, 10, 10])
    removed = ds.remove(det.id)
    assert removed is det
    assert ds.detections == []
    with pytest.raises(KeyError):
        ds.remove(det.id)


def test_move_shifts_everything():
    ds = make_set()
    det = ds.add(
        "cargo", [100, 100, 200, 200],
        polygon=[[100, 100], [200, 200]],
        mask_rle={"width": 4, "height": 4, "offset": [100, 100], "runs": [[1, 16]]},
    )
    ds.move(det.id, 15, -5)
    assert det.bbox == [115, 95, 215, 195]
    assert det.polygon == [[115, 95], [215, 195]]
    assert det.mask_rle["offset"] == [115, 95]
    assert det.user_corrected is True


def test_serialize_round_trip():
    ds = make_set()
    ds.add("trailer_bed", [1, 2, 900, 700], confidence=0.863, prompt="trailer",
           polygon=[[1, 2], [900, 2], [900, 700]],
           mask_rle={"width": 2, "height": 2, "offset": [0, 0], "runs": [[1, 4]]})
    ds.add_user("tiedown_closeup", [40, 40, 60, 90])
    blob = json.dumps(ds.to_dict())
    ds2 = DetectionSet.from_dict(json.loads(blob))
    assert ds2.to_dict() == ds.to_dict()
    assert ds2.image_width == 2000
    assert len(ds2.by_kind("tiedown_closeup")) == 1


def test_unknown_kind_rejected():
    ds = make_set()
    with pytest.raises(ValueError):
        ds.add("spaceship", [0, 0, 5, 5])

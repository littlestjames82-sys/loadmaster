"""Spark-guided cargo rescue (push45) and the containment cross-check.

The rescue: SAM found no cargo for the declared load type, Spark
names the load, SAM re-segments with Spark's noun. Tests stub both
transports; detections must still come only from SAM.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import pipeline as pipeline_mod  # noqa: E402
from loadmaster.crosscheck import locate_cargo, parse_locate  # noqa: E402
from loadmaster.mask_decode import SamObject  # noqa: E402
from loadmaster.rules import TiedownBasis, Verdict, apply_crosscheck  # noqa: E402
from loadmaster.sam_client import SamResult  # noqa: E402

import pytest  # noqa: E402


# -- parse_locate ---------------------------------------------------------

def test_parse_locate_present_fenced():
    text = ('```json\n{"cargo_present": true, "cargo_noun": '
            '"Stack of Wrapped Pallets", "cargo_box": {"x1": 100, '
            '"y1": 200, "x2": 900, "y2": 700}}\n```')
    view = parse_locate(text)
    assert view == {"cargo_present": True,
                    "cargo_noun": "stack of wrapped pallets",
                    "cargo_box": [100.0, 200.0, 900.0, 700.0]}


def test_parse_locate_absent_is_first_class():
    view = parse_locate('{"cargo_present": false, "cargo_noun": null, '
                        '"cargo_box": null}')
    assert view == {"cargo_present": False, "cargo_noun": None,
                    "cargo_box": None}


def test_parse_locate_bad_noun_and_box_become_none():
    view = parse_locate('{"cargo_present": true, "cargo_noun": '
                        '"x", "cargo_box": {"x1": -5, "y1": 0, '
                        '"x2": 10, "y2": 10}}')
    assert view["cargo_present"] is True
    assert view["cargo_noun"] is None
    assert view["cargo_box"] is None


def test_parse_locate_garbage_raises():
    with pytest.raises(ValueError):
        parse_locate("no json here")
    with pytest.raises(ValueError):
        parse_locate('{"cargo_noun": "logs"}')


def test_locate_cargo_stubbed_transport(tmp_path):
    from loadmaster.crosscheck import locate_cargo as _locate

    def poster(payload, timeout):
        assert payload["input"][0]["content"][0]["text"].startswith(
            "This photo shows a trailer")
        return {"output_text": '{"cargo_present": true, "cargo_noun": '
                '"pile of scrap metal", "cargo_box": {"x1": 50, '
                '"y1": 60, "x2": 950, "y2": 800}}'}

    p = _photo(tmp_path)
    res = _locate(p, _poster=poster)
    assert res.status == "ok"
    assert res.photo_role == "locate"
    assert res.view["cargo_noun"] == "pile of scrap metal"
    assert res.view["cargo_box"] == [50.0, 60.0, 950.0, 800.0]


# -- pipeline rescue ------------------------------------------------------

class _FakeLocate:
    def __init__(self, view):
        self.status = "ok"
        self.view = view

    def to_dict(self):
        return {"photo_role": "locate", "status": "ok", "view": self.view}


def _photo(tmp_path) -> Path:
    from PIL import Image

    p = tmp_path / "photo.jpg"
    Image.new("RGB", (120, 80), (80, 70, 60)).save(p, format="JPEG")
    return p


def _cargo_result(prompt):
    obj = SamObject(object_id="o1", bbox=(10, 10, 100, 60),
                    confidence=0.81, mask=None)
    return SamResult(prompt=prompt, status="ok", attempts=1,
                     objects=[obj], sent_width=120, sent_height=80)


def test_rescue_segments_with_sparks_noun(tmp_path, monkeypatch):
    calls = []

    def fake_run_prompts(image_path, prompts):
        calls.append(list(prompts))
        if prompts == ["stack of wrapped pallets"]:
            return [_cargo_result(prompts[0])]
        return [SamResult(prompt=pr, status="empty", attempts=1)
                for pr in prompts]

    monkeypatch.setattr(pipeline_mod, "run_prompts", fake_run_prompts)
    monkeypatch.setattr(
        pipeline_mod, "locate_cargo",
        lambda photo: _FakeLocate({"cargo_present": True,
                                   "cargo_noun": "stack of wrapped pallets",
                                   "cargo_box": [80, 120, 830, 750]}))
    monkeypatch.setattr(
        pipeline_mod, "ask_spark",
        lambda photo, role: _FakeLocate({}))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ds, record = pipeline_mod.run_pipeline(
        _photo(tmp_path), "flatbed", "generic", "side", out_dir,
        crosscheck=True)
    cargo = ds.by_kind("cargo")
    assert len(cargo) == 1
    assert cargo[0].source == "sam"
    assert cargo[0].prompt == "stack of wrapped pallets"
    assert record["cargo_rescue"]["view"]["cargo_noun"] == \
        "stack of wrapped pallets"
    assert any("second pass" in n for n in ds.notes)
    assert calls[-1] == ["stack of wrapped pallets"]


def test_rescue_negative_control_empty_trailer(tmp_path, monkeypatch):
    calls = []

    def fake_run_prompts(image_path, prompts):
        calls.append(list(prompts))
        return [SamResult(prompt=pr, status="empty", attempts=1)
                for pr in prompts]

    monkeypatch.setattr(pipeline_mod, "run_prompts", fake_run_prompts)
    monkeypatch.setattr(
        pipeline_mod, "locate_cargo",
        lambda photo: _FakeLocate({"cargo_present": False,
                                   "cargo_noun": None, "cargo_box": None}))
    monkeypatch.setattr(
        pipeline_mod, "ask_spark",
        lambda photo, role: _FakeLocate({}))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ds, record = pipeline_mod.run_pipeline(
        _photo(tmp_path), "flatbed", "generic", "side", out_dir,
        crosscheck=True)
    assert ds.by_kind("cargo") == []
    assert len(calls) == 1  # no second SAM pass was attempted
    assert any("also saw no cargo" in n for n in ds.notes)


def test_no_rescue_when_sam_already_found_cargo(tmp_path, monkeypatch):
    located = []

    def fake_run_prompts(image_path, prompts):
        return [_cargo_result(pr) if pr == "logs" else
                SamResult(prompt=pr, status="empty", attempts=1)
                for pr in prompts]

    monkeypatch.setattr(pipeline_mod, "run_prompts", fake_run_prompts)
    monkeypatch.setattr(
        pipeline_mod, "locate_cargo",
        lambda photo: located.append(1) or _FakeLocate({}))
    monkeypatch.setattr(
        pipeline_mod, "ask_spark",
        lambda photo, role: _FakeLocate({}))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ds, record = pipeline_mod.run_pipeline(
        _photo(tmp_path), "utility", "firewood", "side", out_dir,
        crosscheck=True)
    assert len(ds.by_kind("cargo")) == 1
    assert located == []
    assert "cargo_rescue" not in record


# -- containment cross-check ----------------------------------------------

def _verdict(status):
    return Verdict(check_id="tiedown_count", cfr_citation="49 CFR 393.110",
                   status=status, finding="Base finding.", evidence={},
                   confidence_basis="test", risk_rank=2)


def _records(loose):
    return [{"photo_role": "side", "status": "ok",
             "view": {"loose_or_escaped_items_visible": loose,
                      "front_blocked": "unclear",
                      "load_looks_rear_heavy": "unclear"}}]


def test_loose_item_demotes_pass():
    v = _verdict("PASS")
    basis = TiedownBasis(count=6, detection_ids=[], basis_present=True,
                         detail="")
    apply_crosscheck([v], _records("yes"), basis)
    assert v.status == "CHECK_THIS"
    assert v.cross_check is not None
    assert v.cross_check["agreement"] is False


def test_loose_item_marks_unsettled_verdict_without_upgrading():
    v = _verdict("COULD_NOT_TELL")
    basis = TiedownBasis(count=0, detection_ids=[], basis_present=False,
                         detail="")
    apply_crosscheck([v], _records("yes"), basis)
    assert v.status == "COULD_NOT_TELL"
    assert "loose or escaped" in v.finding
    assert v.cross_check["agreement"] is True


def test_no_loose_item_no_change():
    v = _verdict("PASS")
    basis = TiedownBasis(count=6, detection_ids=[], basis_present=True,
                         detail="")
    apply_crosscheck([v], _records("no"), basis)
    assert v.status == "PASS"
    assert v.cross_check is None

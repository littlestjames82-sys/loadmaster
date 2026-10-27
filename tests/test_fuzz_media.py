"""Adversarial media paths: the overlay renderer, the stored-mask
decoder, and the report builder, fed garbage on purpose.

- A detection set is client-suppliable (the recheck endpoint), so a
  stored mask claiming a gigantic raster must be refused, not
  allocated.
- The overlay is decoration: if it cannot render, the analysis must
  still return (the pipeline records the failure instead of dying).
- write_report promises to never raise; junk verdict shapes prove
  it.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import detections as det_mod  # noqa: E402
from loadmaster.mask_decode import MaskRaster  # noqa: E402
from loadmaster.overlay import render_overlay  # noqa: E402
from loadmaster.report import write_report  # noqa: E402

KINDS = ["trailer_bed", "wheel", "cargo", "tarp", "tiedown_closeup",
         "chain_hint", "user_added", "axle_estimate"]


def _img(tmp_path) -> Path:
    from PIL import Image

    p = tmp_path / "base.jpg"
    Image.new("RGB", (160, 120), (70, 60, 50)).save(p, format="JPEG")
    return p


def test_overlay_survives_garbage_geometry(tmp_path):
    rng = random.Random(4242)
    img = _img(tmp_path)
    weird = [0.0, -10.0, 1e6, float("nan"), float("inf"), 33.3]
    for trial in range(40):
        ds = det_mod.DetectionSet(
            image_path=str(img), image_width=160, image_height=120,
            trailer_type="utility", load_type="generic", photo_role="side")
        dets = []
        for i in range(rng.randrange(0, 10)):
            d = det_mod.Detection(
                id=f"g{i}", kind=rng.choice(KINDS),
                bbox=[rng.choice(weird) for _ in range(4)],
                confidence=0.5, source="sam")
            if rng.random() < 0.5:
                d.polygon = [[rng.choice(weird), rng.choice(weird)]
                             for _ in range(rng.randrange(0, 6))]
            dets.append(d)
        ds.detections = dets
        out = render_overlay(ds, tmp_path / f"ov{trial}.png")
        assert out.exists()


def test_mask_raster_refuses_oversized_rle():
    with pytest.raises(ValueError):
        MaskRaster.from_rle({
            "width": 20000, "height": 10000,
            "runs": [[1, 200_000_000]],
        })


def test_mask_raster_accepts_normal_rle():
    r = MaskRaster.from_rle({
        "width": 4, "height": 2, "runs": [[0, 3], [1, 2], [0, 3]],
    })
    assert r.set_pixels == 2


def test_write_report_never_raises_on_junk():
    junk_verdicts = [
        None, [], {}, {"check": "tiedown_count"},
        {"check_id": "tiedown_count", "status": "PASS"},
        {"check": "x", "status": None, "finding": 42, "fix": ["a"]},
        "not-a-dict", 42,
    ]
    for jv in junk_verdicts:
        out = write_report(jv if isinstance(jv, list) else [jv],
                           {"load_type": "generic", "trailer_type": "utility"})
        assert isinstance(out, dict)
        assert out.get("headline")

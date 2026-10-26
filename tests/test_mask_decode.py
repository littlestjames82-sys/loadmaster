"""Mask decode tests. No live API: a synthetic bitmap plus the real
Day 0 proof output text saved as a fixture."""

from pathlib import Path

import pytest

from loadmaster.mask_decode import HAVE_PARSER, MaskRaster, parse_output_text

FIXTURE = Path(__file__).parent / "fixtures" / "sam_proof_output_trailer.txt"


def make_rect_raster() -> MaskRaster:
    # 8 wide, 6 high; filled rectangle x in [2,5], y in [1,3], offset (10, 20)
    w, h = 8, 6
    data = bytearray(w * h)
    for y in range(1, 4):
        for x in range(2, 6):
            data[y * w + x] = 1
    return MaskRaster(width=w, height=h, offset_x=10, offset_y=20, data=bytes(data))


def test_synthetic_raster_bbox_and_pixels():
    r = make_rect_raster()
    assert r.set_pixels == 12
    assert r.bbox == (12, 21, 16, 24)


def test_synthetic_raster_rle_round_trip():
    r = make_rect_raster()
    r2 = MaskRaster.from_rle(r.to_rle())
    assert r2.data == r.data
    assert (r2.width, r2.height, r2.offset_x, r2.offset_y) == (8, 6, 10, 20)


def test_synthetic_raster_polygon():
    r = make_rect_raster()
    poly = r.polygon()
    assert len(poly) >= 4
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    assert min(xs) >= 12 and max(xs) <= 15
    assert min(ys) >= 21 and max(ys) <= 23


def test_bad_raster_length_rejected():
    with pytest.raises(ValueError):
        MaskRaster(width=4, height=4, data=b"\x01\x00")


@pytest.mark.skipif(not HAVE_PARSER, reason="meta-sam-parser not installed")
def test_day0_proof_fixture_decodes():
    text = FIXTURE.read_text(encoding="utf-8")
    objects = parse_output_text(text)
    assert len(objects) == 1
    obj = objects[0]
    # Raw token says x2=1023, y2=670; meta-sam-parser normalises to
    # exclusive bounds (x2+1, y2+1), matching the mask raster extent.
    assert obj.bbox == (71, 347, 1024, 671)
    assert obj.confidence == pytest.approx(0.8632, abs=0.001)
    assert obj.mask is not None
    assert (obj.mask.width, obj.mask.height) == (953, 324)
    assert obj.mask.set_pixels == 123346
    poly = obj.mask.polygon()
    assert len(poly) >= 10

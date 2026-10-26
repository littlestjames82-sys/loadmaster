"""Overlay renderer tests (Day 2 fill fix). The mask fill must tint
exactly the bitmap's pixels - no more, no fewer."""

from PIL import Image

from loadmaster.detections import DetectionSet
from loadmaster.mask_decode import MaskRaster
from loadmaster.overlay import KIND_COLORS, render_overlay


def test_mask_fill_tints_bitmap_only(tmp_path):
    photo = tmp_path / "photo.png"
    Image.new("RGB", (120, 90), (10, 10, 10)).save(photo)

    ds = DetectionSet(
        image_path=str(photo), image_width=120, image_height=90,
        trailer_type="utility", load_type="firewood", photo_role="side",
    )
    raster = MaskRaster(
        width=40, height=40, offset_x=10, offset_y=10, data=bytes([1]) * 1600
    )
    ds.add("cargo", [10, 10, 50, 50], confidence=0.9, mask_rle=raster.to_rle())

    out = render_overlay(ds, tmp_path / "overlay.png")
    img = Image.open(out).convert("RGB")

    # Interior of the bitmap: tinted toward the cargo colour.
    r, g, b = img.getpixel((30, 30))
    cr, cg, cb = KIND_COLORS["cargo"]
    assert g > 60 and abs(r - (0.57 * 10 + 0.43 * cr)) < 12
    # Outside the bitmap and away from box/label: untouched photo.
    assert img.getpixel((100, 60)) == (10, 10, 10)
    assert img.getpixel((60, 80)) == (10, 10, 10)

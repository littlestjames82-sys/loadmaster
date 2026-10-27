"""Broad photo-format intake (push43).

Field failure, Oct 27, 2026: the owner's own hay photo arrived
named .avif and the app refused it with "unsupported photo
format". Phone photos are HEIC/HEIF as often as JPEG, and web
downloads are AVIF; testers would hit the same wall. _normalize_upload
now accepts JPEG, PNG, WEBP, HEIC, HEIF, AVIF, BMP, TIFF, GIF
(first frame), and MPO, and re-encodes everything to JPEG.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import webapp  # noqa: E402


def _roundtrip(fmt: str, **save_kw) -> Image.Image:
    img = Image.new("RGB", (120, 80), (180, 60, 40))
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save_kw)
    out = webapp._normalize_upload(buf.getvalue())
    result = Image.open(io.BytesIO(out))
    result.load()
    assert result.format == "JPEG"
    assert result.size == (120, 80)
    return result


def test_jpeg_and_png_still_work():
    _roundtrip("JPEG")
    _roundtrip("PNG")


def test_bmp_accepted():
    _roundtrip("BMP")


def test_tiff_accepted():
    _roundtrip("TIFF")


def test_gif_accepted_first_frame():
    img = Image.new("RGB", (120, 80), (10, 200, 10))
    other = Image.new("RGB", (120, 80), (10, 10, 200))
    buf = io.BytesIO()
    img.save(buf, format="GIF", save_all=True, append_images=[other])
    out = webapp._normalize_upload(buf.getvalue())
    result = Image.open(io.BytesIO(out))
    result.load()
    assert result.format == "JPEG"
    # First frame is the green one, not the blue second frame.
    r, g, b = result.convert("RGB").getpixel((60, 40))
    assert g > 150 and b < 100


def test_avif_accepted_when_encoder_available():
    probe = io.BytesIO()
    try:
        Image.new("RGB", (16, 16)).save(probe, format="AVIF")
    except Exception as exc:  # no AVIF encoder in this Pillow build
        pytest.skip(f"AVIF encoder unavailable: {exc}")
    _roundtrip("AVIF")


def test_heic_accepted_when_library_available():
    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        pytest.skip("pillow-heif not installed in this environment")
    probe = io.BytesIO()
    try:
        Image.new("RGB", (16, 16)).save(probe, format="HEIF")
    except Exception as exc:
        pytest.skip(f"HEIF encoder unavailable: {exc}")
    _roundtrip("HEIF")


def test_non_image_refused_with_supported_list():
    with pytest.raises(ValueError) as ei:
        webapp._normalize_upload(b"this is not an image at all")
    assert "HEIC" in str(ei.value) and "AVIF" in str(ei.value)


def test_unsupported_image_format_refused():
    # A real image in a format outside the supported set (PPM is
    # Pillow-readable but not a photo format we accept).
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="PPM")
    with pytest.raises(ValueError) as ei:
        webapp._normalize_upload(buf.getvalue())
    assert "unsupported photo format" in str(ei.value)

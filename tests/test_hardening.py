"""Intake hardening: EXIF rotation, downscale, format refusal, and
the per-IP rate limit that protects the shared API credits."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import webapp  # noqa: E402


def _jpeg_bytes(size=(200, 100), orientation=None) -> bytes:
    img = Image.new("RGB", size, (120, 80, 40))
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif.tobytes()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", **kwargs)
    return buf.getvalue()


def test_exif_orientation_is_applied():
    out = webapp._normalize_upload(_jpeg_bytes((200, 100), orientation=6))
    img = Image.open(io.BytesIO(out))
    assert img.size == (100, 200)  # stored landscape, displayed portrait


def test_oversized_image_is_downscaled():
    buf = io.BytesIO()
    Image.new("RGB", (3000, 1500), (10, 10, 10)).save(buf, format="PNG")
    out = webapp._normalize_upload(buf.getvalue())
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == webapp.MAX_IMAGE_EDGE
    assert img.format == "JPEG"


def test_garbage_upload_refused():
    with pytest.raises(ValueError, match="not a readable photo"):
        webapp._normalize_upload(b"this is not an image at all")


def test_oversize_upload_refused():
    with pytest.raises(ValueError, match="over 25 MB"):
        webapp._normalize_upload(b"x" * (webapp.MAX_UPLOAD_BYTES + 1))


def test_analyze_rejects_oversized_chunked_body_before_parsing(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(webapp.app.state, "max_analyze_body_bytes", 64, raising=False)
    chunks = iter((b"a" * 40, b"b" * 40))
    resp = client.post(
        "/analyze",
        content=chunks,
        headers={"content-type": "multipart/form-data; boundary=unused"},
    )
    assert resp.status_code == 413
    assert "request body too large" in resp.json()["error"]


def test_analyze_rejects_more_than_four_photos(monkeypatch):
    client = _client(monkeypatch)
    files = [
        ("photos", (f"{i}.jpg", _jpeg_bytes(), "image/jpeg"))
        for i in range(5)
    ]
    resp = client.post("/analyze", files=files, data={"trailer_type": "utility"})
    assert resp.status_code == 400
    assert "no more than 4 photos" in resp.json()["error"]


def _client(monkeypatch):
    from loadmaster import detections as det_mod

    def fake(photo_path, trailer_type, load_type, photo_role, out_dir,
             bed_length_ft=None, crosscheck=False):
        ds = det_mod.DetectionSet(
            image_path="x.jpg", image_width=10, image_height=10,
            trailer_type="utility", load_type="generic", photo_role=photo_role,
        )
        return ds, {"outputs": {}, "prompts": [], "crosscheck": []}

    monkeypatch.setattr(webapp, "run_pipeline", fake)
    webapp._analyze_hits.clear()
    from fastapi.testclient import TestClient

    return TestClient(webapp.app)


def test_analyze_rejects_non_image(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post(
        "/analyze", files={"photo": ("x.txt", b"hello", "text/plain")},
        data={"trailer_type": "utility"},
    )
    assert resp.status_code == 400
    assert "not a readable photo" in resp.json()["error"]


def test_rate_limit_kicks_in(monkeypatch):
    monkeypatch.setattr(webapp, "_ANALYZE_LIMIT", 2)
    client = _client(monkeypatch)
    ok = {"photo": ("a.jpg", _jpeg_bytes(), "image/jpeg")}
    for _ in range(2):
        resp = client.post("/analyze", files=dict(ok), data={"trailer_type": "utility"})
        assert resp.status_code == 200
    resp = client.post("/analyze", files=dict(ok), data={"trailer_type": "utility"})
    assert resp.status_code == 429

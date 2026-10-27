"""Input validation (external review fix): declared numbers that
are negative, zero, NaN, or infinite must never reach the rules
math. The engine drops them to None so checks degrade to COULD NOT
TELL; the analyze endpoint refuses them outright. Plus the daily
global cap behind the per-IP rate limit."""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import webapp  # noqa: E402
from loadmaster.rules import UserInputs  # noqa: E402


def test_negative_and_nan_inputs_are_dropped():
    u = UserInputs(load_length_ft=-6, weight_lb=-100)
    assert u.load_length_ft is None
    assert u.weight_lb is None
    u = UserInputs(load_length_ft=float("nan"), weight_lb=float("inf"))
    assert u.load_length_ft is None
    assert u.weight_lb is None


def test_wll_list_is_sanitized():
    u = UserInputs(tiedown_wll_lb=[-5, {"wll_lb": -1}, 1000,
                                   {"wll_lb": 500, "attachment": "direct"}])
    assert u.tiedown_wll_lb == [1000, {"wll_lb": 500, "attachment": "direct"}]
    assert UserInputs(tiedown_wll_lb=-50).tiedown_wll_lb is None
    assert UserInputs(tiedown_wll_lb=[]).tiedown_wll_lb is None


def test_valid_inputs_survive():
    u = UserInputs(load_length_ft=6, weight_lb=900, tiedown_wll_lb=1000)
    assert (u.load_length_ft, u.weight_lb, u.tiedown_wll_lb) == (6, 900, 1000)


def _jpg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 70, 50)).save(buf, format="JPEG")
    return buf.getvalue()


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
    webapp._day_hits.clear()
    from fastapi.testclient import TestClient

    return TestClient(webapp.app)


def test_analyze_refuses_negative_length(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post(
        "/analyze", files={"photo": ("a.jpg", _jpg(), "image/jpeg")},
        data={"trailer_type": "utility", "bed_length_ft": "-3"},
    )
    assert resp.status_code == 400
    assert "not a usable number" in resp.json()["error"]


def test_verdicts_negative_weight_cannot_pass_wll(monkeypatch):
    client = _client(monkeypatch)
    from loadmaster import detections as det_mod

    ds = det_mod.DetectionSet(
        image_path="x.jpg", image_width=100, image_height=80,
        trailer_type="utility", load_type="generic", photo_role="closeup",
    )
    ds.detections = [
        det_mod.Detection(id="m1", kind="user_added", bbox=[1, 1, 5, 5],
                          source="user", label="tie-down (your mark)")
    ]
    resp = client.post("/verdicts", json={
        "detections": ds.to_dict(),
        "measurements": None,
        "inputs": {"load_length_ft": 6, "load_type": "generic",
                   "weight_lb": -100, "tiedown_wll_lb": 1000},
    })
    assert resp.status_code == 200
    statuses = {v["check"]: v["status"] for v in resp.json()["verdicts"]}
    assert statuses.get("aggregate_wll") == "COULD NOT TELL"


def test_global_daily_cap(monkeypatch):
    monkeypatch.setattr(webapp, "_DAILY_CAP", 1)
    client = _client(monkeypatch)
    files = {"photo": ("a.jpg", _jpg(), "image/jpeg")}
    r1 = client.post("/analyze", files=dict(files), data={"trailer_type": "utility"})
    assert r1.status_code == 200
    r2 = client.post(
        "/analyze", files=dict(files), data={"trailer_type": "utility"},
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert r2.status_code == 429
    assert "daily analysis capacity" in r2.json()["error"]

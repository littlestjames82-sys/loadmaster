"""Multi-photo /analyze: verdicts computed over the whole photo set.

The decisive case: a side view alone cannot ground a tie-down count
(the counting basis is close-up evidence), so tiedown_count must be
COULD NOT TELL with the side photo only, and must resolve once a
close-up carrying the user's strap marks joins the set. The stubbed
pipeline routes on photo_role, exactly like the real one.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import detections as det_mod  # noqa: E402


def _det(kind, source="sam", conf=0.9, label=""):
    return det_mod.Detection(
        id=f"{kind}-{source}-{conf}", kind=kind, bbox=[10, 10, 60, 60],
        confidence=conf, source=source, label=label,
    )


def _ds(role, dets):
    ds = det_mod.DetectionSet(
        image_path="x.jpg", image_width=200, image_height=120,
        trailer_type="utility", load_type="motorcycle", photo_role=role,
    )
    ds.detections = dets
    return ds


def _fake_run_pipeline(photo_path, trailer_type, load_type, photo_role,
                       out_dir, bed_length_ft=None, crosscheck=False):
    if photo_role == "closeup":
        ds = _ds("closeup", [
            _det("user_added", source="user", label="tie-down (your mark)")
            for _ in range(4)
        ])
    else:
        ds = _ds("side", [_det("trailer_bed"), _det("cargo")])
    record = {"outputs": {}, "prompts": [], "crosscheck": []}
    return ds, record


def _client(monkeypatch):
    import loadmaster.webapp as webapp

    monkeypatch.setattr(webapp, "run_pipeline", _fake_run_pipeline)
    from fastapi.testclient import TestClient

    return TestClient(webapp.app)


def _post(client, files):
    return client.post(
        "/analyze", files=files,
        data={"trailer_type": "utility", "load_type": "motorcycle",
              "bed_length_ft": "8", "load_length_ft": "6"},
    )


def _verdict(resp, check):
    for v in resp.json()["verdicts"]:
        if v["check"] == check:
            return v
    raise AssertionError(f"no verdict for {check}")


def test_side_only_cannot_count_tiedowns(monkeypatch):
    client = _client(monkeypatch)
    resp = _post(client, [("photo", ("side.jpg", b"img", "image/jpeg"))])
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["photo_sets"]) == 1
    assert _verdict(resp, "tiedown_count")["status"] == "COULD NOT TELL"


def test_closeup_in_set_grounds_the_count(monkeypatch):
    client = _client(monkeypatch)
    resp = _post(client, [
        ("photos", ("side.jpg", b"img", "image/jpeg")),
        ("photos", ("close.jpg", b"img", "image/jpeg")),
        ("photo_roles", (None, "side")),
        ("photo_roles", (None, "closeup")),
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert [s["role"] for s in body["photo_sets"]] == ["side", "closeup"]
    assert body["run"]["photos"] == 2
    v = _verdict(resp, "tiedown_count")
    assert v["status"] == "PASS", v

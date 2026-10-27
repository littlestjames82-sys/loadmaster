"""Adversarial robustness: the rules engine, measurement layer,
and intake must never crash and never emit an out-of-vocabulary
verdict, no matter how degenerate the geometry or inputs. Seeded
and deterministic: a failure here reproduces forever."""
from __future__ import annotations

import io
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import detections as det_mod  # noqa: E402
from loadmaster.measure import measure_load  # noqa: E402
from loadmaster.rules import (  # noqa: E402
    CHECK_THIS,
    COULD_NOT_TELL,
    PASS,
    UserInputs,
    evaluate_rules,
)

VALID = {PASS, CHECK_THIS, COULD_NOT_TELL}
KINDS = ["trailer_bed", "wheel", "cargo", "tarp", "tiedown_closeup",
         "chain_hint", "user_added", "axle_estimate"]


def _weird_float(rng):
    return rng.choice([
        0.0, -0.0, 1.0, -50.0, 250.0, 1e6, -1e6,
        float("nan"), float("inf"), float("-inf"), 0.001,
    ])


def _random_set(rng, role):
    ds = det_mod.DetectionSet(
        image_path="fuzz.jpg",
        image_width=rng.choice([0, 1, 100, 4000]),
        image_height=rng.choice([0, 1, 80, 3000]),
        trailer_type="utility", load_type="generic", photo_role=role,
    )
    dets = []
    for i in range(rng.randrange(0, 14)):
        x1, y1 = _weird_float(rng), _weird_float(rng)
        x2, y2 = x1 + _weird_float(rng), y1 + _weird_float(rng)
        dets.append(det_mod.Detection(
            id=f"f{i}", kind=rng.choice(KINDS), bbox=[x1, y1, x2, y2],
            confidence=rng.choice([None, 0.0, 0.5, 1.0, float("nan")]),
            source=rng.choice(["sam", "user"]),
            bonus_only=rng.random() < 0.3,
            label=rng.choice(["", "tie-down", "ratchet strap"]),
        ))
    ds.detections = dets
    return ds


def test_rules_engine_never_crashes_on_adversarial_geometry():
    rng = random.Random(20261026)
    for trial in range(240):
        sets = [_random_set(rng, rng.choice(["side", "rear", "closeup"]))
                for _ in range(rng.randrange(1, 4))]
        inputs = UserInputs(
            load_length_ft=rng.choice([None, 0.5, 6, 40, 500]),
            load_type=rng.choice(["general", "loose_material",
                                  "heavy_wheeled_tracked"]),
            weight_lb=rng.choice([None, 100, 5000, 12000, 90000]),
            tiedown_wll_lb=rng.choice([None, 500, [500, 1000], []]),
        )
        measurements = None
        if rng.random() < 0.5:
            try:
                measurements = measure_load(sets[0], 8.0).to_dict()
            except Exception:
                measurements = None
        report = evaluate_rules(sets, measurements, inputs)
        for v in report["verdicts"]:
            assert v["status"] in VALID, (trial, v)
            assert not (isinstance(v.get("finding"), float)
                        and math.isnan(v["finding"]))


def test_measure_layer_never_crashes_on_degenerate_sets():
    rng = random.Random(777)
    for _ in range(120):
        ds = _random_set(rng, "side")
        report = measure_load(ds, rng.choice([0.5, 8.0, 48.0, 1e4]))
        assert report is not None
        for m in report.measurements:
            if isinstance(m.value, (int, float)):
                assert math.isfinite(m.value), m


def _jpeg(size=(64, 48)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (90, 70, 50)).save(buf, format="JPEG")
    return buf.getvalue()


def _stub_client(monkeypatch, run_pipeline=None):
    import loadmaster.webapp as webapp

    if run_pipeline is not None:
        monkeypatch.setattr(webapp, "run_pipeline", run_pipeline)
    webapp._analyze_hits.clear()
    webapp._day_hits.clear()
    from fastapi.testclient import TestClient

    return TestClient(webapp.app), webapp


def test_truncated_image_refused_cleanly(monkeypatch):
    client, _ = _stub_client(monkeypatch)
    raw = _jpeg()
    resp = client.post(
        "/analyze",
        files={"photo": ("a.jpg", raw[: len(raw) // 3], "image/jpeg")},
        data={"trailer_type": "utility"},
    )
    assert resp.status_code == 400
    assert "not a readable photo" in resp.json()["error"]


def test_analyze_error_does_not_leak_internals(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret path /home/hatch/workspace/key.pem exploded")

    client, _ = _stub_client(monkeypatch, run_pipeline=boom)
    resp = client.post(
        "/analyze", files={"photo": ("a.jpg", _jpeg(), "image/jpeg")},
        data={"trailer_type": "utility"},
    )
    assert resp.status_code == 400
    assert "key.pem" not in resp.text and "/home/" not in resp.text
    assert "RuntimeError" in resp.json()["error"]


def test_verdicts_body_size_guard(monkeypatch):
    client, _ = _stub_client(monkeypatch)
    resp = client.post(
        "/verdicts", content=b" " * 2_000_001,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_verdicts_body_size_guard_without_content_length(monkeypatch):
    client, _ = _stub_client(monkeypatch)
    chunks = iter((b" " * 1_000_001, b" " * 1_000_001))
    resp = client.post(
        "/verdicts", content=chunks,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_verdicts_rejects_non_object_json(monkeypatch):
    client, _ = _stub_client(monkeypatch)
    resp = client.post(
        "/verdicts", json=[],
    )
    assert resp.status_code == 400
    assert "JSON object" in resp.json()["error"]


def test_concurrent_requests_stay_well_formed(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from loadmaster import detections as dm

    def fake(photo_path, trailer_type, load_type, photo_role, out_dir,
             bed_length_ft=None, crosscheck=False):
        ds = dm.DetectionSet(
            image_path="x.jpg", image_width=10, image_height=10,
            trailer_type="utility", load_type="generic",
            photo_role=photo_role)
        return ds, {"outputs": {}, "prompts": [], "crosscheck": []}

    client, _ = _stub_client(monkeypatch, run_pipeline=fake)
    files = {"photo": ("a.jpg", _jpeg(), "image/jpeg")}

    def hit(i):
        if i % 2:
            return client.post("/analyze", files=dict(files),
                               data={"trailer_type": "utility"}).status_code
        return client.post("/verdicts", json={
            "detections": _side_dict(), "measurements": None,
            "inputs": {"load_length_ft": 6},
        }).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(hit, range(40)))
    assert all(c in (200, 429) for c in codes), codes


def _side_dict():
    ds = det_mod.DetectionSet(
        image_path="x.jpg", image_width=100, image_height=80,
        trailer_type="utility", load_type="generic", photo_role="side")
    return ds.to_dict()


def test_evaluate_rules_is_deterministic():
    import json

    from loadmaster.webapp import load_gallery_entries

    for entry in load_gallery_entries():
        m = entry.get("measurements") or {}
        ds = {
            "image_path": "gallery",
            "image_width": m.get("image_width", 0),
            "image_height": m.get("image_height", 0),
            "trailer_type": entry["inputs"].get("trailer_type", "unknown"),
            "load_type": "generic",
            "photo_role": "side",
            "detections": entry.get("detections") or [],
            "notes": [],
        }
        from loadmaster.webapp import compute_verdicts

        a, _, _ = compute_verdicts(ds, entry.get("measurements"),
                                   entry["inputs"],
                                   crosscheck=entry.get("crosscheck"))
        b, _, _ = compute_verdicts(ds, entry.get("measurements"),
                                   entry["inputs"],
                                   crosscheck=entry.get("crosscheck"))
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

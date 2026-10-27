"""Failure injection at the seams (external review item): a SAM
prompt erroring after retries, a cross-check that fails or returns
garbage, and oversized recheck payloads must all degrade honestly,
never crash and never fabricate a verdict change."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadmaster import pipeline as pipeline_mod  # noqa: E402
from loadmaster import webapp  # noqa: E402
from loadmaster.rules import UserInputs, evaluate_rules  # noqa: E402
from loadmaster.sam_client import SamResult  # noqa: E402


def _photo(tmp_path) -> Path:
    from PIL import Image

    p = tmp_path / "photo.jpg"
    Image.new("RGB", (120, 80), (80, 70, 60)).save(p, format="JPEG")
    return p


def test_pipeline_prompt_error_is_recorded_not_fatal(tmp_path, monkeypatch):
    def fake_run_prompts(image_path, prompts):
        out = []
        for i, pr in enumerate(prompts):
            if i == 0:
                out.append(SamResult(prompt=pr, status="error", attempts=3,
                                     error="upstream timeout", latency_s=0.1))
            else:
                out.append(SamResult(prompt=pr, status="empty", attempts=1,
                                     latency_s=0.1))
        return out

    monkeypatch.setattr(pipeline_mod, "run_prompts", fake_run_prompts)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ds, record = pipeline_mod.run_pipeline(
        _photo(tmp_path), "utility", "generic", "side", out_dir)
    assert record["prompts"][0]["status"] == "error"
    assert any("ERRORED" in n for n in ds.notes)
    assert ds.detections == []


class _FakeCC:
    def __init__(self, status, error=None):
        self.status = status
        self.error = error

    def to_dict(self):
        return {"status": self.status, "error": self.error, "views": {}}


def test_pipeline_crosscheck_error_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline_mod, "run_prompts",
        lambda p, prompts: [SamResult(prompt=pr, status="empty", attempts=1)
                            for pr in prompts],
    )
    monkeypatch.setattr(
        pipeline_mod, "ask_spark",
        lambda photo, role: _FakeCC("error", "model unavailable"),
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ds, record = pipeline_mod.run_pipeline(
        _photo(tmp_path), "utility", "generic", "side", out_dir,
        crosscheck=True)
    assert record["crosscheck"][0]["status"] == "error"
    assert any("cross-check ERRORED" in n for n in ds.notes)


def _side_ds_dict():
    from loadmaster import detections as det_mod

    ds = det_mod.DetectionSet(
        image_path="x.jpg", image_width=100, image_height=80,
        trailer_type="utility", load_type="generic", photo_role="side")
    return ds.to_dict()


def test_rules_malformed_crosscheck_records_are_ignored():
    from loadmaster import detections as det_mod

    ds = det_mod.DetectionSet.from_dict(_side_ds_dict())
    inputs = UserInputs(load_length_ft=6)
    base = evaluate_rules([ds], None, inputs)
    junk = [{"status": "error"}, {"status": "ok", "views": "garbage"},
            {"bogus": 1}, "not-a-dict"]
    out = evaluate_rules([ds], None, inputs, crosscheck=junk)
    base_status = {v["check_id"]: v["status"] for v in base["verdicts"]}
    out_status = {v["check_id"]: v["status"] for v in out["verdicts"]}
    assert out_status == base_status


def test_webapp_security_headers_and_verdicts_cap():
    from fastapi.testclient import TestClient

    client = TestClient(webapp.app)
    resp = client.get("/")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"

    big = _side_ds_dict()
    big["detections"] = [
        {"id": f"d{i}", "kind": "cargo", "bbox": [0, 0, 1, 1]}
        for i in range(501)
    ]
    resp = client.post("/verdicts", json={
        "detections": big, "measurements": None,
        "inputs": {"load_length_ft": 6},
    })
    assert resp.status_code == 400
    assert "too many detections" in resp.json()["error"]

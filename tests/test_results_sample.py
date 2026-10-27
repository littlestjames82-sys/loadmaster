"""Field results page and the one-tap sample run: the two public
proof surfaces. The results page must serve every captured run with
unedited verdicts, and the sample photo must be a real JPEG the
/analyze path accepts like any upload."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from loadmaster.webapp import app, load_results_entries  # noqa: E402

client = TestClient(app)

KNOWN_STATUSES = {"PASS", "CHECK THIS", "COULD NOT TELL"}


def test_results_entries_complete():
    entries = load_results_entries()
    assert len(entries) == 11
    for entry in entries:
        assert entry.get("slug")
        assert entry.get("title")
        verdicts = entry.get("verdicts") or []
        assert verdicts, entry["slug"]
        for v in verdicts:
            assert v.get("status") in KNOWN_STATUSES
        assert entry.get("overlay_png_base64"), entry["slug"]


def test_results_page_serves_all_runs():
    resp = client.get("/results")
    assert resp.status_code == 200
    assert "field results" in resp.text
    assert resp.text.count('<section class="run">') == 11
    # The honesty spread is printed, not hidden.
    assert "COULD NOT TELL instead of guessing" in resp.text


def test_sample_photo_served():
    resp = client.get("/sample-load.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content[:2] == b"\xff\xd8"  # JPEG magic
    # A real photo, but comfortably under the 25 MB upload cap.
    assert 50_000 < len(resp.content) < 5_000_000


def test_ui_offers_sample_run_and_results():
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="sampleBtn"' in resp.text
    assert 'fetch("/sample-load.jpg")' in resp.text
    assert 'href="/results"' in resp.text


def test_gallery_links_results():
    resp = client.get("/gallery")
    assert resp.status_code == 200
    assert 'href="/results"' in resp.text


def test_version_stamp_served():
    resp = client.get("/version")
    assert resp.status_code == 200
    assert resp.json()["build"]

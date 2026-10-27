"""PWA surface: the manifest and icons the phone's add-to-home-
screen flow depends on must actually be served."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from loadmaster.webapp import app  # noqa: E402

client = TestClient(app)


def test_manifest_served():
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "LoadMaster"
    assert body["display"] == "standalone"
    assert {i["sizes"] for i in body["icons"]} == {"192x192", "512x512"}


def test_icons_served():
    for path in ("/icon-192.png", "/icon-512.png"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_health_route():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_service_worker_served_and_registered():
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "addEventListener" in resp.text
    page = client.get("/")
    assert 'register("/sw.js")' in page.text

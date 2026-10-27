"""Modal deployment for LoadMaster (the working app).

Wraps the FastAPI service in loadmaster/webapp.py as a Modal ASGI app
so Ryan gets a real URL: open it on the phone, take a photo, get live
analysis and verdicts back. The same FastAPI app also runs locally
with uvicorn (uvicorn loadmaster.webapp:app) for testing.

The Meta Model API key never lives in this repo: Modal supplies it
from the secret named "meta-model-api" as the META_API_KEY env var,
which loadmaster/sam_client.py prefers when set.

Deploy (one-time setup, run by a human with the Modal account):
    pip install modal
    modal setup
    modal secret create meta-model-api META_API_KEY=<the key>
    modal deploy modal_app.py

Deployed Oct 26, 2026 via GitHub Actions (this file, using the
repo secrets MODAL_TOKEN_ID / MODAL_TOKEN_SECRET) to
https://littlestjames82-sys--loadmaster-fastapi-app.us-east.modal.run
with the "meta-model-api" secret in place; verified live end to end.
"""

from __future__ import annotations

import sys
from pathlib import Path

import modal

ROOT = Path(__file__).parent

app = modal.App("loadmaster")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements(str(ROOT / "requirements.txt"))
    .add_local_dir(str(ROOT / "loadmaster"), remote_path="/root/loadmaster")
    .env({"LOADMASTER_BUILD": "push40-results-sample"})
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("meta-model-api")],
    timeout=600,  # headroom for a 4-photo run with retries; typical runs finish in 15-110s
    scaledown_window=300,  # keep a warm container 5 min so judges do not eat a cold start
)
@modal.asgi_app()
def fastapi_app():
    if "/root" not in sys.path:
        sys.path.insert(0, "/root")
    from loadmaster.webapp import app as web_app

    return web_app

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

NOT deployed yet: this VM has no Modal CLI auth. The file is
deploy-ready; the remaining steps are the CLI login above and Ryan
creating the "meta-model-api" secret with his key.
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
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("meta-model-api")],
    timeout=300,  # a full analysis runs several SAM calls in parallel
)
@modal.asgi_app()
def fastapi_app():
    if "/root" not in sys.path:
        sys.path.insert(0, "/root")
    from loadmaster.webapp import app as web_app

    return web_app

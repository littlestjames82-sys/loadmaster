"""Modal deployment for the GhostBus live watch demo (app: ghostbus-demo).

Serves a public watch page + the REAL GhostBus relay (v0.6.0, unmodified
source from the ghostbus repo, checked out next to this file as
ghostbus-src/ by the deploy workflow) behind one URL, with a small driver
that runs a legible two-agent work cycle through the relay's REST API so
anyone opening the page sees the bus actually working.

Deploy (via GitHub Actions, using this repo's MODAL_TOKEN_ID /
MODAL_TOKEN_SECRET secrets):
    modal deploy ghostbus-demo/modal_app.py

copy=True is load-bearing (see the LoadMaster note): without it Modal
adds files lazily at container startup and a deploy can report success
while the live app keeps serving the previous build. The demo's /version
endpoint reports DEMO_BUILD + the relay's own version so live-vs-deployed
is checkable in one request.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import modal

ROOT = Path(__file__).parent

app = modal.App("ghostbus-demo")

# Bump on every deploy that should be distinguishable live.
BUILD = "2026-11-26-demo3"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .run_commands(
        "apt-get update && apt-get install -y curl ca-certificates",
        "curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.gz -o /tmp/node.tar.gz",
        "mkdir -p /opt/node && tar -xzf /tmp/node.tar.gz -C /opt/node --strip-components=1",
        "/opt/node/bin/node --version",
    )
    .env(
        {
            "DEMO_BUILD": BUILD,
            "GHOSTBUS_DIR": "/app/ghostbus",
            "PATH": "/opt/node/bin:/usr/local/bin:/usr/bin:/bin",
        }
    )
    .add_local_dir(
        str(ROOT / "ghostbus-src" / "src"), remote_path="/app/ghostbus/src", copy=True
    )
    .add_local_file(
        str(ROOT / "ghostbus-src" / "package.json"),
        remote_path="/app/ghostbus/package.json",
        copy=True,
    )
    .add_local_dir(str(ROOT / "demo"), remote_path="/app/demo", copy=True)
)


@app.function(
    image=image,
    timeout=86400,  # web server lifetime cap; scaledown does the cost control
    scaledown_window=300,  # stay warm 5 min after the last request, then sleep
    max_containers=1,  # ONE workspace: the relay store is in-memory, so a second
    # container would serve a second, empty bus and scatter a watcher's polls.
    cpu=0.5,
    memory=512,
)
@modal.web_server(port=8080, startup_timeout=120)
def serve():
    subprocess.Popen(["/opt/node/bin/node", "/app/demo/demo-server.mjs"])

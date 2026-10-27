"""The app's inline JavaScript must at least parse: a syntax error
there is a dead app that no Python test would ever see. Extract the
script and hand it to node's parser when node is available."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UI = Path(__file__).resolve().parents[1] / "loadmaster" / "web_ui.html"


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_inline_javascript_parses(tmp_path):
    html = UI.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "no inline script found in the UI"
    for i, src in enumerate(scripts):
        f = tmp_path / f"ui_{i}.js"
        f.write_text(src, encoding="utf-8")
        proc = subprocess.run(
            ["node", "--check", str(f)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

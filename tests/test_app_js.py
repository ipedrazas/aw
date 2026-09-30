"""app.js is one file for every page: a syntax error anywhere in it (a merge that drops a
closing line, say) stops every page's script. Parse it, where node is to hand."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "src" / "wf" / "web" / "static" / "app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_app_js_parses():
    r = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

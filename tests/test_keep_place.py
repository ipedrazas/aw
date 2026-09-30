"""After an action reloads the page, you are where you were, not at the top."""

from __future__ import annotations

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "src" / "wf" / "web" / "static" / "app.js"


def test_every_reload_goes_through_the_one_that_keeps_your_place():
    js = JS.read_text()
    assert js.count("location.reload()") == 1, "only reloadHere() itself reloads"
    assert js.count("reloadHere()") > 10

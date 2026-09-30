"""Questions are asked in the page, with buttons that say what they do."""

from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "src" / "wf" / "web" / "static" / "app.js"


def test_no_browser_dialogs_are_left():
    js = JS.read_text()
    assert not re.findall(r"(?<![.\w])(alert|confirm|prompt)\(", js)
    assert "function ask(" in js and "function toast(" in js


def test_the_rename_rule_is_the_workspace_s():
    from wf.schema.loader import _NAME

    js = JS.read_text()
    assert f"/^{_NAME.pattern}$/" in js

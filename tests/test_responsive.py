"""On a phone: the nav folds behind a button, and tables stack."""

from __future__ import annotations

from pathlib import Path

from tests.test_api import client  # noqa: F401 - the fixture

STATIC = Path(__file__).resolve().parents[1] / "src" / "wf" / "web" / "static"


def test_the_nav_folds_behind_a_button(client):  # noqa: F811
    page = client.get("/workflows").text
    assert 'aria-controls="topnav"' in page and 'id="topnav"' in page


def test_narrow_screens_stack_tables_and_keep_a_gutter():
    css = (STATIC / "app.css").read_text()
    narrow = css[css.index("@media(max-width:760px)") :]
    assert (
        ".page{padding:16px 16px 48px}" in narrow
        and "table.stackable thead{display:none}" in narrow
    )
    assert "td.dataset.label" in (STATIC / "app.js").read_text()

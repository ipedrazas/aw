import pytest

from tests.helpers import sample_ws, ws  # noqa: F401  (pytest fixtures)


@pytest.fixture(autouse=True)
def _session_root(tmp_path, monkeypatch):
    """Sessions mirror to WF_SESSION_ROOT; keep tests off the container default
    (/app/var/sessions), which a checkout has no reason to be able to create."""
    monkeypatch.setenv("WF_SESSION_ROOT", str(tmp_path / "sessions"))


@pytest.fixture(autouse=True)
def _recorded_link_checks(monkeypatch):
    """The suite does not touch the network; tests of the live check patch it in."""
    monkeypatch.setenv("WF_LINK_CHECK", "recorded")

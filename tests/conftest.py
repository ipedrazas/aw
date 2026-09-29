import pytest

from tests.helpers import close_dbs, sample_ws, ws  # noqa: F401  (pytest fixtures)


@pytest.fixture(autouse=True)
def _session_root(tmp_path, monkeypatch):
    """Sessions mirror to WF_SESSION_ROOT; keep tests off the container default
    (/app/var/sessions), which a checkout has no reason to be able to create."""
    monkeypatch.setenv("WF_SESSION_ROOT", str(tmp_path / "sessions"))


@pytest.fixture(autouse=True)
def _recorded_tools(monkeypatch):
    """The suite does not touch the network; tests of the live tools patch it in."""
    monkeypatch.setenv("WF_LINK_CHECK", "recorded")
    monkeypatch.setenv("WF_SEARCH", "recorded")


@pytest.fixture(autouse=True)
def _close_dbs():
    """Each test's databases are let go of when it ends."""
    yield
    close_dbs()

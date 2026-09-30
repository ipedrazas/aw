"""A run says it is finished only once everything about it is written, and a test's
teardown lets a run's thread finish before closing the sessions under it."""

from __future__ import annotations

import threading
import time

from tests.helpers import wait_for_background
from tests.test_api import client, wait_for  # noqa: F401 - the fixture
from wf.store.ledger import Ledger
from wf.store.records import Expectation


def test_expectations_are_scored_before_the_run_says_it_is_done(client, monkeypatch):  # noqa: F811
    seen: list[list] = []
    real = Ledger.finish_run

    def finish_run(self, run, **kw):
        rows = self.session.query(Expectation).filter_by(run_id=run.id).all()
        seen.append([r.matched for r in rows])
        return real(self, run, **kw)

    monkeypatch.setattr(Ledger, "finish_run", finish_run)
    r = client.post("/api/workflows/deep-research/runs", json={"case": "durable-execution"})
    run = wait_for(client, r.json()["run_id"])
    assert run["status"] == "done", run["error"]
    assert seen and seen[-1] and None not in seen[-1], "every expectation scored first"
    assert all(e["matched"] is not None for e in run["report"]["expectations"])


def test_teardown_waits_for_a_run_s_thread():
    done = []

    def work():
        time.sleep(0.2)
        done.append(True)

    threading.Thread(target=work, name="run-test", daemon=True).start()
    wait_for_background()
    assert done

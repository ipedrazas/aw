"""A database made before a column was added gets that column when it is opened."""

from __future__ import annotations

from sqlalchemy import text

from wf.store import Database
from wf.store.ledger import Ledger


def test_opening_an_older_database_adds_the_columns_it_is_missing(sample_ws, tmp_path):
    url = f"sqlite:///{tmp_path / 'wf.db'}"
    db = Database(url)
    ledger = Ledger(db)
    wv = ledger.workflow_version(sample_ws.load_definition("deep-research"), "sha256:test")
    run = ledger.start_run(
        wv,
        inputs={},
        mode="live",
        depth=0,
        parent=None,
        parent_step_run=None,
        budget_usd=None,
        title="Old",
        case_name=None,
    )
    run_id = run.id
    ledger.close()
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE run DROP COLUMN pause_requested"))
    db.engine.dispose()

    db = Database(url)
    with db.engine.connect() as conn:
        value = conn.execute(
            text("SELECT pause_requested FROM run WHERE id = :id"), {"id": run_id}
        ).scalar()
    assert value in (0, False), "the rows it already had read as not asked to pause"

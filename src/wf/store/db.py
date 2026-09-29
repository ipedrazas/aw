"""Engine and session factory.

``WF_DATABASE_URL`` selects the database. Postgres in Docker and CI; SQLite when the
variable is unset so a checkout runs with nothing else installed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, literal, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .records import Base

DEFAULT_URL = "sqlite:///var/wf.db"


def database_url() -> str:
    return os.environ.get("WF_DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url.endswith("sqlite://"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        else:
            path = url.split("sqlite:///", 1)[-1]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        # A run holds a ledger on one connection while its sessions are written on
        # another, so a file database is put in write-ahead mode: readers and the
        # second writer do not queue behind each other. Postgres needs none of this.
        wal = ":memory:" not in url and not url.endswith("sqlite://")

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            if wal:
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
                dbapi_conn.execute("PRAGMA busy_timeout=5000")

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine: Engine) -> None:
    """create_all makes missing tables but leaves existing ones as they are, so a column
    added since a database was made is added here, with its default for the rows it
    already has. Only columns that are nullable or have a plain default can be."""
    with engine.begin() as conn:
        insp = inspect(conn)
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                ddl += col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None else None
                if default is not None and not callable(default):
                    value = literal(default, col.type).compile(
                        dialect=engine.dialect, compile_kwargs={"literal_binds": True}
                    )
                    ddl += f" NOT NULL DEFAULT {value}"
                elif not col.nullable:
                    raise RuntimeError(
                        f"{table.name}.{col.name} is missing and cannot be added without "
                        "a default; this database needs making again."
                    )
                conn.execute(text(ddl))


class Database:
    def __init__(self, url: str | None = None):
        self.engine = make_engine(url)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        init_db(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

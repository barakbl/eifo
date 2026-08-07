"""Engine and session construction.

SQLite is the default and is expected to stay that way; the schema is kept
PostgreSQL-compatible but moving is explicitly not a goal
(docs.internal/02-architecture.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from tvil_core.settings import Settings, get_settings

#: Tables that must exist before a service will serve traffic.
_SCHEMA_SENTINEL = "titles"


class DatabaseNotReadyError(RuntimeError):
    """The database exists but has not been migrated."""

    def __init__(self, db_url: str) -> None:
        super().__init__(
            f"Database at {db_url} has no TVIL schema. "
            f"Run `tvil-fetch db upgrade` before starting the service."
        )


def _sqlite_path(db_url: str) -> Path | None:
    """Filesystem path for a file-backed SQLite URL, else None."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return None
    raw = db_url[len(prefix) :]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw)


def ensure_sqlite_parent(db_url: str) -> None:
    """Create the directory a file-backed SQLite database will live in.

    SQLite will not create a missing parent directory: it reports only
    "unable to open database file", which is an unhelpful first experience on a
    fresh install where ``data/`` does not exist yet. Every path that opens a
    database — including migrations, which build their own engine — calls this.
    """
    path = _sqlite_path(db_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def create_engine_from_settings(settings: Settings | None = None, *, echo: bool = False) -> Engine:
    """Build an engine, creating the SQLite parent directory if needed.

    SQLite connections get WAL journalling (so the API reads while the fetcher
    writes), enforced foreign keys, and a busy timeout.
    """
    settings = settings or get_settings()
    db_url = settings.db_url

    ensure_sqlite_parent(db_url)

    engine = create_engine(db_url, echo=echo, future=True)

    if db_url.startswith("sqlite"):
        _register_sqlite_pragmas(engine)

    return engine


def _register_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with autoflush off — writes are explicit in this codebase."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def schema_exists(engine: Engine) -> bool:
    """Whether migrations have been applied to this database."""
    return inspect(engine).has_table(_SCHEMA_SENTINEL)


def require_schema(engine: Engine, db_url: str) -> None:
    """Fail fast when a service is started against an unmigrated database."""
    if not schema_exists(engine):
        raise DatabaseNotReadyError(db_url)

"""Applying the migrations that ship inside this package.

``eifo-fetch db …`` is a thin wrapper around these functions: the package that
owns the schema also owns how it is applied.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from eifo_core.fts import INDEXES

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: The full-text index and the shadow tables SQLite builds around it. They are
#: created by a migration rather than declared as models, so autogenerate would
#: otherwise propose dropping them on every run.
FTS_TABLES = frozenset().union(*(index.shadow_tables for index in INDEXES))


def include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Keep search infrastructure out of autogenerate and the drift check.

    Lives here rather than in ``env.py`` so the drift test can apply the same
    rule; importing ``env.py`` outside Alembic runs the migrations.
    """
    return not (type_ == "table" and name in FTS_TABLES)


def alembic_config(db_url: str) -> Config:
    """Alembic config pointing at the packaged migrations."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # configparser reads '%' as interpolation; database URLs may contain it.
    config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
    return config


def upgrade(db_url: str, revision: str = "head") -> None:
    """Apply migrations up to ``revision``."""
    command.upgrade(alembic_config(db_url), revision)


def downgrade(db_url: str, revision: str) -> None:
    """Revert migrations down to ``revision``."""
    command.downgrade(alembic_config(db_url), revision)


def current_revision(engine: Engine) -> str | None:
    """Revision stamped on the database, or None if it has never been migrated."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    """The newest revision the packaged migrations define."""
    # The URL is irrelevant here: only the script directory is read.
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def is_current(engine: Engine) -> bool:
    """Whether the database carries every migration this build ships."""
    return current_revision(engine) == head_revision()


def ensure_current(engine: Engine, db_url: str) -> str | None:
    """Migrate the database to head if it is behind, and say what was applied.

    Creating the schema counts as being behind, so a first start against an
    empty file works the same way as an upgrade.

    Returns:
        The revision the database now carries, or None if it was already there
        and nothing ran.

    Raises:
        Whatever Alembic raises for a migration that genuinely fails. A second
        process migrating the same database at the same moment is not a
        failure: the database is re-checked before the error is re-raised.
    """
    if is_current(engine):
        return None

    try:
        upgrade(db_url)
    except Exception:
        # Two services can start together (compose brings up api and fetcher
        # at once); the loser of that race finds the work already done.
        if not is_current(engine):
            raise
    return current_revision(engine)

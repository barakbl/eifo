"""Applying the migrations that ship inside this package.

``tvil-fetch db …`` is a thin wrapper around these functions: the package that
owns the schema also owns how it is applied.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


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

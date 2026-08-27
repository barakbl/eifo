"""A TMDB id only identifies a work together with its media type.

TMDB numbers films and series in separate namespaces. Movie 105 is Back to the
Future; series 105 is Sex and the City. ``titles.tmdb_id`` was globally unique
and every lookup went by the number alone, so whichever of the pair arrived
second was silently taken for the first: the film was never created, and its
rent-and-buy offers were filed against the series.

Sex and the City was left holding two Apple TV Store offers - from a films-only
storefront - that belong to Back to the Future.

Three things, in order. The offers filed against the wrong work go first, while
they can still be identified; then the key on titles becomes ``(type,
tmdb_id)``; then the alias table gets the same treatment, since an alias keyed
on the bare number shadows the other namespace exactly as a title did.

The missing films are not recreated here - that needs TMDB, which a migration
must not call. The next sync of the affected sources creates them properly,
which is the same path by which they would have arrived in the first place.

Revision ID: 0020_tmdb_ids_are_per_media_type
Revises: 0019_backfill_on_enable
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import eifo_core.types
from eifo_core.fts import restore_triggers

revision = "0020_tmdb_ids_are_per_media_type"
down_revision = "0019_backfill_on_enable"
branch_labels = None
depends_on = None

#: Offers whose own deep link names a namespace its title does not belong to.
#:
#: The link the provider harvester stores carries the media path it read the
#: offer from - ".../movie/105/watch" - so a row saying "movie" on a series is
#: self-evidently misfiled. No network call and no knowledge of which plugin
#: collects what: the row contradicts itself.
_MISFILED = """
    SELECT a.id FROM availability a JOIN titles t ON t.id = a.title_id
     WHERE (a.deep_link_url LIKE '%themoviedb.org/movie/%' AND t.type = 'series')
        OR (a.deep_link_url LIKE '%themoviedb.org/tv/%'    AND t.type = 'movie')
"""


def _titles_without_the_global_unique() -> sa.Table:
    """``titles`` as it stands, minus the UNIQUE on ``tmdb_id`` alone.

    Written out rather than reflected because reflection is exactly what keeps
    the old constraint alive. Only the shape matters here - types are as the
    live schema has them, and nothing reads these columns during the copy.
    """
    return sa.Table(
        "titles",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("tmdb_id", sa.Integer),
        sa.Column("imdb_id", sa.String(16), unique=True),
        sa.Column("name_en", sa.String(500)),
        sa.Column("name_he", sa.String(500)),
        sa.Column("year", sa.Integer),
        sa.Column("overview_en", sa.Text),
        sa.Column("overview_he", sa.Text),
        sa.Column("poster_path", sa.String(500)),
        sa.Column("backdrop_path", sa.String(500)),
        sa.Column("runtime_minutes", sa.Integer),
        sa.Column("seasons", sa.Integer),
        sa.Column("status", sa.String(50)),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.Column("poster_source_url", sa.String(1000)),
        sa.Column("original_language", sa.String(8)),
        sa.Column("origin_countries", sa.String(100)),
        sa.CheckConstraint(
            "name_en IS NOT NULL OR name_he IS NOT NULL", name="ck_titles_has_a_name"
        ),
        sa.Index("ix_titles_type_year", "type", "year"),
    )


def _alias_columns(*, typed: bool = True) -> list[sa.schema.SchemaItem]:
    """``tmdb_aliases``, with or without the namespace in its key."""
    key = ["type", "tmdb_id"] if typed else ["tmdb_id"]
    columns: list[sa.schema.SchemaItem] = []
    if typed:
        columns.append(sa.Column("type", sa.String(32), nullable=False))
    columns += [
        sa.Column("tmdb_id", sa.Integer, nullable=False),
        sa.Column(
            "title_id",
            sa.Integer,
            sa.ForeignKey("titles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", eifo_core.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint(*key, name="pk_tmdb_aliases"),
    ]
    return columns


def upgrade() -> None:
    op.execute(f"DELETE FROM availability WHERE id IN ({_MISFILED})")

    # Spelled out rather than reflected. The old constraint is a bare column
    # UNIQUE, which SQLite gives no name and so nothing can drop by name; batch
    # mode reflects it and faithfully puts it back on the rebuilt table. Handing
    # it the table as it should end up is how the constraint actually goes.
    with op.batch_alter_table(
        "titles",
        copy_from=_titles_without_the_global_unique(),
        recreate="always",
    ) as batch:
        batch.create_unique_constraint("uq_title_tmdb", ["type", "tmdb_id"])

    # Built beside the old one and swapped in, rather than altered in place:
    # widening a primary key is the one thing batch mode cannot do without
    # reflecting the old key and arguing with itself about it.
    #
    # Every alias belongs to the title it points at, so the namespace it was
    # recorded in is that title's own - which makes the backfill exact rather
    # than a guess, and drops any alias whose title has since gone as a
    # side effect of the join.
    op.create_table("tmdb_aliases_new", *_alias_columns())
    op.execute(
        """
        INSERT INTO tmdb_aliases_new (type, tmdb_id, title_id, created_at)
        SELECT t.type, a.tmdb_id, a.title_id, a.created_at
          FROM tmdb_aliases a JOIN titles t ON t.id = a.title_id
        """
    )
    op.drop_table("tmdb_aliases")
    op.rename_table("tmdb_aliases_new", "tmdb_aliases")
    op.create_index("ix_tmdb_aliases_title_id", "tmdb_aliases", ["title_id"])

    # Rebuilding titles took its FTS triggers with it - SQLite drops a table's
    # triggers along with the table, which is the whole reason 0008 exists.
    # Without this the catalog sits at head with search frozen at whatever it
    # held the moment this ran.
    restore_triggers(op.get_bind())


def downgrade() -> None:
    # Going back means one namespace has to win, so drop the aliases and titles
    # that only coexist because the key was widened. The deleted offers are not
    # restored: they were never true of the titles they sat on.
    op.execute(
        """
        DELETE FROM tmdb_aliases WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM tmdb_aliases GROUP BY tmdb_id
        )
        """
    )
    op.execute(
        """
        DELETE FROM titles WHERE tmdb_id IS NOT NULL AND id NOT IN (
            SELECT MIN(id) FROM titles WHERE tmdb_id IS NOT NULL GROUP BY tmdb_id
        )
        """
    )

    op.create_table("tmdb_aliases_old", *_alias_columns(typed=False))
    op.execute(
        """
        INSERT INTO tmdb_aliases_old (tmdb_id, title_id, created_at)
        SELECT tmdb_id, title_id, created_at FROM tmdb_aliases
        """
    )
    op.drop_table("tmdb_aliases")
    op.rename_table("tmdb_aliases_old", "tmdb_aliases")
    op.create_index("ix_tmdb_aliases_title_id", "tmdb_aliases", ["title_id"])

    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.drop_constraint("uq_title_tmdb", type_="unique")
        batch_op.create_unique_constraint("uq_titles_tmdb_id", ["tmdb_id"])

    restore_triggers(op.get_bind())

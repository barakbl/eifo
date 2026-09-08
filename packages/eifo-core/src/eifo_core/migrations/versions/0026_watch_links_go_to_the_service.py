"""Clear the watch links that only went back to TMDB.

The JustWatch export behind the provider harvester carries no per-provider
deep links - a provider entry is a name, an id and a logo, and nothing else -
so the harvester stored TMDB's own per-title watch page instead. That made a
"Watch" button on a Netflix row open themoviedb.org, which is a detour through
a third site to be told what the row already said.

The harvester stores no URL for those offers now, and a row with no URL gets no
button. That alone would not fix the rows already written: ``record_offer``
only ever overwrites a link with a new one, never clears it, so every one of
these would keep the old value through any number of syncs. Hence a migration.

Matched on the URL, not on the source. Which plugin collects a source is a fact
about today's configuration - the same key could be read directly tomorrow -
whereas a link pointing at themoviedb.org is self-evidently not a link to the
service the row names, whoever wrote it.

Deliberately not reversible in the useful sense: the downgrade cannot put back
a URL that should not have been there, and reconstructing it would mean writing
the bad links a second time. It is a no-op, and says so.
"""

from __future__ import annotations

from alembic import op

revision = "0026_watch_links_go_to_the_service"
down_revision = "0025_invited_members_and_api_tokens"
branch_labels = None
depends_on = None

#: A watch link that goes to TMDB rather than to the service the offer names.
#:
#: Scoped to the watch page specifically. TMDB is also a ratings provider here
#: and its own site URL lives in ``rating_providers``; this must not touch that,
#: and matching the path keeps it to the links the harvester wrote.
_TMDB_WATCH_LINKS = """
    UPDATE availability
       SET deep_link_url = NULL
     WHERE deep_link_url LIKE '%themoviedb.org/%/watch%'
"""


def upgrade() -> None:
    op.execute(_TMDB_WATCH_LINKS)


def downgrade() -> None:
    """Nothing to undo: the links these rows held pointed at the wrong site."""

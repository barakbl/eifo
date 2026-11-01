"""Notes can be formatted: stored as HTML, with room for 10,000 characters.

A note was plain text of up to 2,000 characters. It is now HTML, written only by
``eifo_core.notes``, and the limit a person meets is 10,000 characters of text
however much of it is bold. The column's own ceiling rises to 50,000 so the
markup has somewhere to go.

Every existing note becomes one paragraph per line, escaped, so "Tom & Jerry"
and "a <3 b" still read as they were written. The conversion is spelled out
here rather than borrowed from ``eifo_core.notes``: a migration has to do the
same thing on the day it is run in five years as on the day it was written.

Downgrading turns notes back into plain text and cuts them to 2,000
characters; the formatting, and anything past that, is lost.

Revision ID: 0032_formatted_notes
Revises: 0031_poster_backups
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from html.parser import HTMLParser

import sqlalchemy as sa
from alembic import op

revision = "0032_formatted_notes"
down_revision = "0031_poster_backups"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_user_items_note_length"
OLD_LIMIT = 2000
NEW_LIMIT = 50_000


def upgrade() -> None:
    _limit(NEW_LIMIT)
    _rewrite(_as_html)


def downgrade() -> None:
    _rewrite(lambda note: _as_text(note)[:OLD_LIMIT].strip() or None)
    _limit(OLD_LIMIT)


def _limit(length: int) -> None:
    with op.batch_alter_table("user_items", schema=None) as batch_op:
        batch_op.drop_constraint(CONSTRAINT, type_="check")
        batch_op.create_check_constraint(CONSTRAINT, f"note IS NULL OR length(note) <= {length}")


def _rewrite(convert: Callable[[str], str | None]) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, note FROM user_items WHERE note IS NOT NULL")).all()
    for row_id, note in rows:
        bind.execute(
            sa.text("UPDATE user_items SET note = :note WHERE id = :id"),
            {"note": convert(note), "id": row_id},
        )


def _as_html(note: str) -> str | None:
    lines = [" ".join(line.split()) for line in note.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return None
    return "".join(f"<p>{escape(line, quote=False)}</p>" if line else "<p><br></p>" for line in lines)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = [""]
        self._after_break = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.lines.append("")
            self._after_break = True

    def handle_endtag(self, tag: str) -> None:
        # A break that ends a block only gave it height; the block's end is
        # the one new line.
        if tag in {"p", "h1", "h2", "h3", "h4", "h5", "h6"} and not self._after_break:
            self.lines.append("")
        self._after_break = False

    def handle_data(self, data: str) -> None:
        self.lines[-1] += data
        self._after_break = False


def _as_text(note: str) -> str:
    reader = _Text()
    reader.feed(note)
    reader.close()
    return "\n".join(line.strip() for line in reader.lines).strip()

"""Full-text search over the FTS5 index.

User input reaches SQLite's ``MATCH`` operator, which has its own query language
- bare text containing ``AND``, ``"`` or ``*`` is a syntax error at best. Every
term is therefore quoted rather than passed through, which both removes the
syntax hazard and makes a search for ``fauda "season"`` behave the way a user
expects instead of erroring.
"""

from __future__ import annotations

import re

from sqlalchemy import Select, text

from tvil_core.models import Title

#: Words, in any script. Everything else is punctuation as far as search goes.
_TOKENS = re.compile(r"\w+", re.UNICODE)

#: Guards against a pathological query pinning the database.
MAX_TERMS = 12


def fts_query(query: str) -> str | None:
    """Turn user input into a safe FTS5 MATCH expression.

    The final term gets a prefix wildcard so search-as-you-type matches while
    the user is still typing it.

    Returns None when the input has no searchable content, which callers treat
    as "no text filter" rather than "no results".
    """
    terms = _TOKENS.findall(query)[:MAX_TERMS]
    if not terms:
        return None

    quoted = [f'"{term}"' for term in terms[:-1]]
    quoted.append(f'"{terms[-1]}"*')
    return " ".join(quoted)


def apply_text_search(statement: Select[tuple[int]], query: str) -> Select[tuple[int]]:
    """Restrict a title query to FTS matches, newest-relevance first.

    Falls back to an unfiltered statement when the query has no usable terms.
    """
    match = fts_query(query)
    if match is None:
        return statement

    matching_ids = text("SELECT rowid FROM titles_fts WHERE titles_fts MATCH :fts").bindparams(
        fts=match
    )
    return statement.where(Title.id.in_(matching_ids))

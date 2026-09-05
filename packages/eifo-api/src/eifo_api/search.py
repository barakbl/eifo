"""Full-text search over the FTS5 index, and how its results are ordered.

User input reaches SQLite's ``MATCH`` operator, which has its own query language
- bare text containing ``AND``, ``"`` or ``*`` is a syntax error at best. Every
term is therefore quoted rather than passed through, which both removes the
syntax hazard and makes a search for ``fauda "season"`` behave the way a user
expects instead of erroring.

**FTS decides what matches; it does not decide what comes first.** It used to,
through ``bm25``, and it was wrong in a way that took a while to see: bm25
normalises by whole-*document* length, and the document here is four columns -
both names and both overviews. Restricting the match to the name columns does
not restrict that normalisation. So the order was decided mostly by how long a
title's synopsis was. Searching ``הסנדק`` put "הסנדקית מאמא גראס" first, on the
strength of having no overview at all, and The Godfather - 2.2 million votes,
the exact title typed - eighth and last. In English it did not reach the top
eight.

What replaces it is a ladder: how well the name matches, in ranks anybody can
predict, and popularity only ever inside a rank.
"""

from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ColumnElement, Select, text

from eifo_core.enums import RatingProvider
from eifo_core.models import ExternalRating, Title

#: Words, in any script. Everything else is punctuation as far as search goes.
_TOKENS = re.compile(r"\w+", re.UNICODE)

#: Guards against a pathological query pinning the database.
MAX_TERMS = 12

#: The single letters Hebrew attaches to the front of a word - the definite
#: article and the conjunctions and prepositions that behave like it.
#:
#: Hebrew writes them as part of the word, so "the godfather" is one token,
#: ``הסנדק``. A prefix wildcard only grows a term at the end, which leaves
#: somebody typing ``סנדק`` - the actual noun - matching one unrelated title,
#: while ``הסנדק`` matches eight. Nobody thinks of the ``ה`` as part of the
#: name, and there is no reason the search should.
HEBREW_PREFIXES = ("ה", "ו", "ב", "כ", "ל", "מ", "ש")  # noqa: RUF001 - Hebrew, not Latin

_HEBREW = re.compile(r"[\u0590-\u05ff]")

#: Articles ignored at the front of a name, so "godfather" and "The Godfather"
#: are the same thing. Longest first: "an " must be tried before "a ".
ARTICLES = ("the ", "an ", "a ")

#: What counts as the end of a word when deciding how well a name matches.
#: Punctuation a catalog actually uses, rather than a general definition -
#: colons and dashes separate a title from its subtitle everywhere.
SEPARATORS = (
    " ",
    ":",
    "-",
    "\u2013",  # en dash
    "\u2014",  # em dash
    ",",
    ".",
    "(",
    ")",
    "'",
    '"',
    "!",
    "?",
    "/",
    "&",
)

#: The rungs of the ladder, best first. Numbers rather than an enum because
#: they are compared inside SQL, where they are the whole of the ordering.
EXACT = 0
STARTS_WORD = 1
WHOLE_WORD_LATER = 2
STARTS_MID_WORD = 3
MID_WORD_LATER = 4
NO_SUBSTRING = 5
#: A title with nothing in this language. Never better than a real match, and
#: never worse than one either - it simply has nothing to say.
ABSENT = 9


def fts_query(query: str) -> str | None:
    """Turn user input into a safe FTS5 MATCH expression.

    The final term gets a prefix wildcard so search-as-you-type matches while
    the user is still typing it, and a Hebrew term is also tried with each of
    the letters Hebrew glues to the front of a word (:data:`HEBREW_PREFIXES`).

    That last part costs a wider match and buys the search Hebrew speakers
    actually type. It cannot cost precision: everything it adds matches the
    term somewhere inside a word, which is the fourth rung of the ladder, below
    every title that matches it properly.

    Returns None when the input has no searchable content, which callers treat
    as "no text filter" rather than "no results".
    """
    terms = _TOKENS.findall(query)[:MAX_TERMS]
    if not terms:
        return None

    quoted = [f'"{term}"' for term in terms[:-1]]
    quoted.append(_last_term(terms[-1]))
    # Joined with an explicit AND rather than by sitting next to each other.
    # The two mean the same thing to FTS5 right up until one of them is a
    # bracketed OR - `"a" "b" (c OR d)` is a syntax error, and the prefix
    # expansion below makes exactly that shape out of any Hebrew query with
    # more than one word in it.
    return " AND ".join(quoted)


def _last_term(term: str) -> str:
    """The term still being typed, wildcarded, and grown a Hebrew prefix."""
    if not _HEBREW.match(term) or term[0] in HEBREW_PREFIXES:
        # Already carries one, or is not Hebrew at all. Growing a second prefix
        # onto a word that has one asks for a form nobody writes.
        return f'"{term}"*'
    forms = [f'"{term}"*'] + [f'"{prefix}{term}"*' for prefix in HEBREW_PREFIXES]
    return f"({' OR '.join(forms)})"


def name_match(columns: tuple[str, ...], query: str) -> str | None:
    """A MATCH expression restricted to an index's name columns.

    Search-as-you-type over a whole document offers rubbish: somebody two
    letters into an actor's name does not want the films that mention them in
    passing.
    """
    match = fts_query(query)
    if match is None:
        return None
    # Bracketed, so the column filter covers the whole expression. Without the
    # brackets FTS5 applies it to the first phrase and reads the rest as a
    # search over every column, which quietly turns a name search into one that
    # matches synopses too.
    return f"{{{' '.join(columns)}}} : ({match})"


def apply_text_search(statement: Select[tuple[int]], query: str) -> Select[tuple[int]]:
    """Restrict a title query to FTS matches.

    Ordering is the caller's business - see :func:`apply_relevance` for the
    order this match deserves.

    Falls back to an unfiltered statement when the query has no usable terms.
    """
    match = fts_query(query)
    if match is None:
        return statement

    matching_ids = text("SELECT rowid FROM titles_fts WHERE titles_fts MATCH :fts").bindparams(
        fts=match
    )
    return statement.where(Title.id.in_(matching_ids))


def strip_article(name: str) -> str:
    """A name without its leading article, so two spellings of one title meet.

    "The Godfather" and "godfather" are the same request. Without this the
    first is a match somewhere in the middle of a name and the second is an
    exact title, so an obscure 2022 film called "GodFather" outranked the 1972
    one by two million votes.

    A name that is *only* an article keeps it: "The" is a title somebody may
    have meant, and an empty needle matches every row at position one.
    """
    lowered = name.strip().lower()
    for article in ARTICLES:
        if lowered.startswith(article) and lowered[len(article) :].strip():
            return lowered[len(article) :]
    return lowered


def _normalised(column: Any) -> ColumnElement[str]:
    """:func:`strip_article`, as something SQLite computes per row."""
    lowered = sa.func.lower(column)
    return sa.case(
        *[
            (lowered.like(f"{article}%"), sa.func.substr(lowered, len(article) + 1))
            for article in ARTICLES
        ],
        else_=lowered,
    )


def _tier(column: Any, needle: str) -> ColumnElement[int]:
    """Which rung of the ladder this name reaches for this query.

    The distinction that does the work is between a match that ends where a
    word ends and one that does not: ``הסנדק`` is the whole of the first word
    of "הסנדק: חלק שני" and only the front of "הסנדקית מאמא גראס", and the
    difference between those two is the difference between the sequel to the
    film somebody asked for and a film that merely starts with the same
    letters.
    """
    name = _normalised(column)
    position = sa.func.instr(name, needle)
    after = sa.func.substr(name, position + len(needle), 1)
    before = sa.func.substr(name, position - 1, 1)
    ends_a_word = sa.or_(after == "", after.in_(SEPARATORS))
    follows_a_break = before.in_(SEPARATORS)

    return sa.case(
        (column.is_(None), ABSENT),
        (name == needle, EXACT),
        (sa.and_(position == 1, ends_a_word), STARTS_WORD),
        (sa.and_(position > 1, follows_a_break, ends_a_word), WHOLE_WORD_LATER),
        (position == 1, STARTS_MID_WORD),
        (position > 1, MID_WORD_LATER),
        else_=NO_SUBSTRING,
    )


def match_tier(query: str) -> ColumnElement[int] | None:
    """How well a title's name answers this query, over both languages.

    The better of the two: a title is one work with two names, and somebody who
    typed an English name should not be punished for the Hebrew one being
    different.
    """
    needle = strip_article(query)
    if not needle:
        return None
    return sa.func.min(_tier(Title.name_he, needle), _tier(Title.name_en, needle))


def apply_relevance(
    statement: Select[tuple[int]],
    query: str,
    *,
    best_first: bool = True,
) -> Select[tuple[int]] | None:
    """Order a title query by how well each name answers it, then by fame.

    Strictly in that order. Popularity never lifts a title past one that
    matches better, which is what makes the list predictable: type a title in
    full and it is first, however obscure. Inside a rung it decides everything,
    because "which of these did you mean" is a question about recognition -
    ``הסנדק`` and its two sequels share a rung and 2.2 million, 1.5 million and
    460 thousand votes put them in the order anybody would expect.

    Votes rather than the aggregate score: the score answers "which is better",
    and a dropdown is not asking that.

    Returns None when the query has nothing to rank by, which is the caller's
    cue to fall back to its ordinary sort.
    """
    tier = match_tier(query)
    if tier is None:
        return None

    # Joined rather than looked up per row: (title_id, provider) is unique, so
    # one row at most and no aggregation. Asking for max(vote_count) grouped by
    # title instead - the obvious first way to write it - read every one of the
    # thirty thousand IMDb ratings on every keystroke, and cost three times as
    # much for an answer that was already unique.
    statement = statement.outerjoin(
        ExternalRating,
        sa.and_(
            ExternalRating.title_id == Title.id,
            ExternalRating.provider == RatingProvider.IMDB,
        ),
    )
    votes = sa.func.coalesce(ExternalRating.vote_count, 0)
    # A shorter name is closer to what was typed, and breaks the tie between
    # two unrated titles on the same rung without reaching for the row id.
    length = sa.func.length(sa.func.coalesce(Title.name_he, Title.name_en, ""))

    # Named for what it means rather than for which way the columns point. The
    # ladder counts down - rung zero is the exact title - so "best first" is
    # ascending here and descending for the votes beside it, and a parameter
    # called `descending` was read as one of those and silently meant the
    # other: the grid opened its relevance sort on the least relevant title in
    # the catalog.
    criteria: list[Any] = (
        [tier.asc(), votes.desc(), length.asc()]
        if best_first
        else [tier.desc(), votes.asc(), length.desc()]
    )
    return statement.order_by(*criteria, Title.id)

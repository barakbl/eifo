"""Which column a name belongs in.

The schema has a column for Hebrew names and a column for English ones, and
nothing else. Deciding between them is a property of the schema rather than of
whoever is writing to it, which is why it lives here: the fetcher decides it
when a source hands it a listing, and the API decides it when a reviewer rules
that a parked item is a title of its own. Two answers to that question would
eventually be two different answers.
"""

from __future__ import annotations

import re
import unicodedata

_HEBREW = re.compile(r"[֐-׿]")


def is_hebrew(text: str) -> bool:
    """Whether a string contains Hebrew letters."""
    return bool(_HEBREW.search(text))


def latin_script(text: str) -> bool:
    """Whether every letter in a string is Latin.

    The counterpart to :func:`is_hebrew`, and the reason both exist: anything
    written in a third script is neither, and calling it English because it is
    not Hebrew is how "Spirited Away" came to be stored as "千と千尋の神隠し" -
    unfindable by the name every English speaker knows it by.

    Accented Latin passes: "Amélie" and "Cien años de soledad" are
    English-column names in every sense that matters here.
    """
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    return all(unicodedata.name(char, "").startswith("LATIN") for char in letters)


def split_by_script(*candidates: str | None) -> tuple[str | None, str | None]:
    """Sort names into ``(hebrew, english)`` by the script they are written in.

    A name in a third script is neither, and is returned as neither. Callers
    that must store something decide what to do with that.
    """
    hebrew: str | None = None
    english: str | None = None
    for candidate in candidates:
        if not candidate:
            continue
        if is_hebrew(candidate):
            hebrew = hebrew or candidate
        elif latin_script(candidate):
            english = english or candidate
    return hebrew, english

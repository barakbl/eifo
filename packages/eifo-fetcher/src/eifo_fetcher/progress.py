"""Saying so, while a long phase is still going.

A sync of a scraped catalog can spend half an hour between the line that says it
started and the line that says what it found, and a phase that says nothing is
indistinguishable from a phase that has hung. Whoever is watching the log has no
way to tell the difference, and neither has anything reading the log for them.

So the long loops report as they go. When to report is the only interesting
part, and it needs two triggers rather than one:

* **Every so many items**, because a fast source would otherwise report on a
  clock and say the same thing repeatedly while the count runs away.
* **Every so many seconds**, because a slow one - a site read at one request a
  second, a browser waiting out a challenge - would otherwise go quiet for
  minutes at a time between count-based lines, which is exactly the silence this
  exists to break.

Whichever comes first wins. There is also an early line, well before the first
of either, so that something that is going to take an hour is visibly alive
within the first few seconds rather than the first few minutes.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: Items between progress lines on a source that is moving quickly.
EVERY_ITEMS = 100
#: Seconds between progress lines on a source that is not.
EVERY_SECONDS = 15.0
#: The first line comes here, so a slow phase proves itself alive early.
FIRST_AT = 10


class ProgressTicker:
    """Decides when a loop over an unknown number of items should speak up.

    Ask it once per item; it answers True on the items worth reporting. It keeps
    no counter of its own - the caller has one already, and two would disagree
    the first time an item was skipped.
    """

    def __init__(
        self,
        *,
        every: int = EVERY_ITEMS,
        seconds: float = EVERY_SECONDS,
        first: int = FIRST_AT,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._every = every
        self._seconds = seconds
        self._first = first
        self._now = now
        self._last_count = 0
        self._last_at = now()

    def due(self, count: int) -> bool:
        """Whether ``count`` items in is a moment to report, marking it if so."""
        if count <= self._last_count:
            # A loop that has not moved has nothing new to say, however long it
            # has been in there. Anything that wants to report a stall wants a
            # watchdog, which is a different thing from a progress line.
            return False

        # On the round numbers rather than every Nth item since the last line:
        # after an early line at 10, or one the clock brought forward at 137,
        # the next is at 200 rather than 110 or 237. Nobody reading a log wants
        # to do that arithmetic to work out whether a line is missing.
        crossed = count // self._every > self._last_count // self._every
        early = self._last_count == 0 and count >= self._first
        if not (early or crossed or self._now() - self._last_at >= self._seconds):
            return False

        self._last_count = count
        self._last_at = self._now()
        return True


def tally(**counts: int) -> str:
    """Render named counts as a phrase, leaving out the ones that are zero.

    ``tally(new=3, updated=0)`` is ``"3 new"``, not ``"3 new, 0 updated"``: the
    zeroes are the majority on most runs and they crowd out the numbers somebody
    is actually reading for.
    """
    parts = [f"{value:,} {name.replace('_', ' ')}" for name, value in counts.items() if value]
    return ", ".join(parts) if parts else "nothing yet"


def position(done: int, total: int | None) -> str:
    """ "1,200 of 5,000 (24%)", or just the count when nothing knows the total.

    A sync cannot know - a catalog is however long it turns out to be - but
    enrichment is handed a batch and rescoring a list, and there the share is
    the whole point: it is the difference between a number going up and a
    number going up *towards something*.
    """
    if not total or total <= 0:
        return f"{done:,}"
    return f"{done:,} of {total:,} ({round(done / total * 100)}%)"


def remaining(done: int, total: int | None, elapsed_seconds: float) -> str | None:
    """ "about 8 minutes left", or None when it cannot honestly say.

    Straight-line from the rate so far, which is the right model for these
    loops: every title costs about one round of the same provider calls. It is
    wrong early and wrong at the tail, which is why it says "about" and why the
    count beside it is the number to trust.

    None rather than a guess when there is nothing to extrapolate from - no
    total, no time passed, nothing done - because "0 seconds left" on a run that
    has barely started is worse than saying nothing.
    """
    if not total or total <= 0 or done <= 0 or elapsed_seconds <= 0 or done >= total:
        return None

    left = (total - done) * (elapsed_seconds / done)
    if left < 60:
        # To the nearest five: a run this close to done does not need the
        # precision, and "about 37 seconds left" claims one it does not have.
        return f"about {max(5, round(left / 5) * 5)} seconds left"
    if left < 90 * 60:
        return f"about {round(left / 60)} minutes left"
    return f"about {left / 3600:.1f} hours left"

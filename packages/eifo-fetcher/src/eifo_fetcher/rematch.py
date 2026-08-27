"""Re-asking TMDB about titles that joined the catalog without an identity.

A title with no external id is a title nothing downstream can help: enrichment
has no record to read, so it gets no poster, no ratings, no Hebrew name - it is
in the catalog and looks abandoned. 6,171 titles were in that state when this
was written, and for most of them rightly so: local programming TMDB has never
heard of. But several hundred were films everyone has heard of, unresolved
because their source named them with decoration a whole-string comparison
cannot see past - "Star Wars The Force Awakens Episode VII" scores 81 against
the film it obviously is.

This is the backfill for those. It re-runs the search with the acceptance rule
:func:`eifo_fetcher.match.confident_tmdb_choice`, and acts only where exactly
one record qualifies - measured against 2,059 titles whose right answer was
already known, that refusal to guess was the difference between 99.3% correct
and no errors at all. Everything else is reported and left exactly as it was.

Two outcomes when a match is found:

* nobody holds the TMDB id - the title adopts it, and the next enrichment pass
  fills in everything the id unlocks;
* another title already holds it - the two are one work, and the unmatched row
  folds into the one that has the identity, through the same machinery
  ``eifo-fetch dedupe`` uses.

Plan by default; ``--apply`` is the second asking, exactly like dedupe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from eifo_core.models import EnrichAttempt, Title
from eifo_fetcher.dedupe import MergePlan, MergeTally, apply_merges
from eifo_fetcher.match import _search_years, adopt_tmdb_hit, confident_tmdb_choice
from eifo_fetcher.review import not_a_title
from eifo_fetcher.tmdb import TmdbClient, TmdbTitle

logger = logging.getLogger("eifo.fetch.rematch")

#: How many qualifying records to name when a title is ambiguous, so the plan
#: shows what the choice would have been between.
AMBIGUOUS_SHOWN = 3


@dataclass(slots=True)
class Adoption:
    """A title and the single TMDB record that confidently matches it."""

    title: Title
    hit: TmdbTitle


@dataclass(slots=True)
class Fold:
    """An unmatched title that is a second copy of one we already hold."""

    owner: Title
    duplicate: Title
    hit: TmdbTitle


@dataclass(slots=True)
class RematchPlan:
    """What a rematch pass found, before anything is written."""

    adoptions: list[Adoption] = field(default_factory=list)
    folds: list[Fold] = field(default_factory=list)
    #: Titles where more than one record qualified. Named, never guessed at.
    ambiguous: list[tuple[Title, list[TmdbTitle]]] = field(default_factory=list)
    junk_skipped: int = 0
    unmatched: int = 0
    errors: list[str] = field(default_factory=list)


def plan_rematch(
    session: Session,
    tmdb: TmdbClient,
    *,
    limit: int | None = None,
) -> RematchPlan:
    """Search TMDB for every title that has no identity, deciding nothing yet."""
    query = select(Title).where(Title.tmdb_id.is_(None), Title.imdb_id.is_(None)).order_by(Title.id)
    if limit is not None:
        query = query.limit(limit)
    titles = list(session.scalars(query).all())

    plan = RematchPlan()
    for title in titles:
        display = title.name_en or title.name_he or ""
        if not_a_title(display):
            # A sing-along named after its film matches that film with total
            # confidence, which is precisely the attachment nobody wants.
            plan.junk_skipped += 1
            continue

        pairs: list[tuple[str, TmdbTitle]] = []
        try:
            for name in (title.name_en, title.name_he):
                if not name or not name.strip():
                    continue
                for year in _search_years(title.year):
                    for hit in tmdb.search(title.type, name, year=year):
                        pairs.append((name, hit))
        except Exception as exc:
            logger.warning("could not search TMDB for title %s: %r", title.id, exc)
            plan.errors.append(f"title {title.id}: {type(exc).__name__}: {exc}")
            continue

        verdict, hits = confident_tmdb_choice(pairs, title.year)
        if verdict == "auto":
            hit = hits[0]
            # Within the hit's own namespace: a film and a series can share a
            # number, and folding one into the other is exactly the mistake
            # this pass exists to undo.
            owner = session.scalar(
                select(Title).where(
                    Title.type == hit.kind,
                    Title.tmdb_id == hit.tmdb_id,
                    Title.id != title.id,
                )
            )
            if owner is not None:
                plan.folds.append(Fold(owner=owner, duplicate=title, hit=hit))
            else:
                plan.adoptions.append(Adoption(title=title, hit=hit))
        elif verdict == "ambiguous":
            plan.ambiguous.append((title, hits[:AMBIGUOUS_SHOWN]))
        else:
            plan.unmatched += 1
    return plan


def apply_rematch(session: Session, plan: RematchPlan) -> MergeTally:
    """Write the plan: adoptions in place, folds through the dedupe machinery.

    Every touched title has its enrichment attempt forgotten. The queue backs
    off titles that yielded nothing, and these yielded nothing *because* they
    had no identity - with one, the next pass should visit them first, not in
    however many months the backoff had reached.
    """
    for adoption in plan.adoptions:
        adopt_tmdb_hit(session, adoption.title, adoption.hit)

    tally = apply_merges(
        session, [MergePlan(winner=fold.owner, losers=[fold.duplicate]) for fold in plan.folds]
    )

    touched = [adoption.title.id for adoption in plan.adoptions]
    touched += [fold.owner.id for fold in plan.folds]
    if touched:
        session.execute(delete(EnrichAttempt).where(EnrichAttempt.title_id.in_(touched)))
    session.commit()
    return tally


def describe(plan: RematchPlan) -> list[str]:
    """The plan as lines, in the order an operator reads them."""
    lines = []
    for adoption in plan.adoptions:
        name = adoption.title.name_en or adoption.title.name_he or "?"
        got = adoption.hit.name or adoption.hit.original_name or "?"
        lines.append(
            f"adopt  #{adoption.title.id} {name!r} -> tmdb {adoption.hit.tmdb_id} "
            f"{got!r} ({adoption.hit.year or '-'})"
        )
    for fold in plan.folds:
        name = fold.duplicate.name_en or fold.duplicate.name_he or "?"
        lines.append(
            f"fold   #{fold.duplicate.id} {name!r} into #{fold.owner.id}, "
            f"which already holds tmdb {fold.hit.tmdb_id}"
        )
    for title, hits in plan.ambiguous:
        name = title.name_en or title.name_he or "?"
        offers = "; ".join(
            f"{hit.name or hit.original_name} ({hit.year or '-'}) tmdb {hit.tmdb_id}"
            for hit in hits
        )
        lines.append(f"?      #{title.id} {name!r} could be any of: {offers}")
    return lines

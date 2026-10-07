"""API answers, cut down to what a model reasons with.

An assistant's context is its working memory, and a full title record - every
offer's timestamps, every rating's URL, a poster address - spends it on things
no answer needs. These keep what a recommendation turns on: what it is, how
good, how long, what kind, and where it can be watched tonight.

Text that came from somewhere else - names, overviews, characters - is passed
through as data. It was written by catalogs and TMDB, not by the person asking,
and the server's instructions tell the model to treat it so.
"""

from __future__ import annotations

from typing import Any

#: Where the overview is cut. Enough to tell a thriller from a comedy; the
#: rest is in the app, one link away.
OVERVIEW_CHARS = 400

#: Cast listed on a title. The leads decide a film; the twentieth name does not.
CAST_SHOWN = 6


def title_card(card: dict[str, Any], link: str) -> dict[str, Any]:
    """A grid card: enough to choose between titles."""
    return _drop_empty(
        {
            "id": card.get("id"),
            "type": card.get("type"),
            "name": card.get("name_en") or card.get("name_he"),
            "name_he": card.get("name_he"),
            "year": card.get("year"),
            "score": card.get("score"),
            "score_israeli": card.get("score_israeli"),
            "genres": [genre.get("name_en") for genre in card.get("genres") or []],
            "watch": offers(card.get("availability") or [], current_only=True),
            "member_added": card.get("user_added") or None,
            "link": link,
        }
    )


def title_detail(detail: dict[str, Any], link: str) -> dict[str, Any]:
    """One title in full, as far as a decision needs."""
    card = title_card(detail, link)
    credits = detail.get("credits") or []
    directors = [_credited(credit) for credit in credits if credit.get("role") == "director"]
    cast = [_credited(credit) for credit in credits if credit.get("role") == "cast"]
    gone = [
        offer
        for offer in offers(detail.get("availability") or [], current_only=False)
        if offer.get("left")
    ]
    return _drop_empty(
        {
            **card,
            "runtime_minutes": detail.get("runtime_minutes"),
            "seasons": detail.get("seasons"),
            "original_language": detail.get("original_language"),
            "countries": detail.get("origin_countries"),
            "directors": directors,
            "cast": cast[:CAST_SHOWN],
            "overview": _cut(detail.get("overview_en") or detail.get("overview_he")),
            "ratings": {
                rating.get("provider_name"): rating.get("score_display")
                for rating in detail.get("ratings") or []
            },
            "used_to_be_on": gone,
        }
    )


def offers(availability: list[dict[str, Any]], *, current_only: bool) -> list[dict[str, Any]]:
    """Where it can be watched: one entry per service and kind of offer."""
    shown = []
    for offer in availability:
        current = offer.get("is_current") and offer.get("source_active", True)
        if current_only and not current:
            continue
        shown.append(
            _drop_empty(
                {
                    "service": offer.get("source_name"),
                    "service_key": offer.get("source_key"),
                    "how": offer.get("offer_type"),
                    "price": _price(offer),
                    "left": None if current else (offer.get("gone_since") or True),
                }
            )
        )
    return shown


def item(entry: dict[str, Any], link: str) -> dict[str, Any]:
    """One title on the owner's lists, with what they said about it."""
    title = entry.get("title") or {}
    return _drop_empty(
        {
            **title_card(title, link),
            "watched": entry.get("watched") or None,
            "want_to_watch": entry.get("want_to_watch") or None,
            "my_rating": entry.get("rating"),
            "my_note": entry.get("note"),
        }
    )


def person(detail: dict[str, Any], link_for: Any) -> dict[str, Any]:
    """Someone, and what they made - newest first, as the API lists it."""
    return _drop_empty(
        {
            "id": detail.get("id"),
            "name": _person_name(detail),
            "credits": [
                _drop_empty(
                    {
                        "role": credit.get("role"),
                        "character": credit.get("character"),
                        **title_card(credit["title"], link_for(credit["title"]["id"])),
                    }
                )
                for credit in detail.get("credits") or []
            ],
        }
    )


def _credited(credit: dict[str, Any]) -> dict[str, Any]:
    """A person on a title, with the id that ``get_person`` takes."""
    return {"id": credit["person"]["id"], "name": _person_name(credit["person"])}


def _person_name(person: dict[str, Any]) -> str:
    return str(person.get("name_en") or person.get("name_he") or "")


def _price(offer: dict[str, Any]) -> str | None:
    minor = offer.get("price_minor")
    if not isinstance(minor, int):
        return None
    return f"{minor / 100:.2f} {offer.get('price_currency') or 'ILS'}"


def _cut(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text.split())
    return text if len(text) <= OVERVIEW_CHARS else text[: OVERVIEW_CHARS - 1].rstrip() + "…"


def _drop_empty(record: dict[str, Any]) -> dict[str, Any]:
    """Leave out what is not known, rather than spend tokens saying so."""
    return {key: value for key, value in record.items() if value not in (None, [], {}, "")}

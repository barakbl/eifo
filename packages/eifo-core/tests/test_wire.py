"""The shapes a listing and a finding cross the wire in.

A sync used to be one process and an enrich used to be one process. They are
two each now, and what passes between them is these dictionaries. Both sides
depend on them and neither owns them, which is why they live here and why they
are written out field by field rather than derived from the dataclasses: the
wire format is a contract, and it should change when somebody means it to, not
when a field is renamed.

So the tests are of two kinds. Round trips, which say the contract carries
everything that matters; and refusals, which say that what arrives malformed is
reported with enough detail for whoever sent it to fix it. The second kind is
the one that earns its keep - the sender is on another machine, and "invalid
payload" would leave them reading their own code to find out which field.
"""

from __future__ import annotations

from typing import Any

import pytest

from eifo_core import ingest as wire
from eifo_core.enums import OfferType, RatingProvider, TitleKind
from eifo_core.findings import EnrichResult, Rating, TitleView
from eifo_core.items import RawItem, TmdbTitle


def item(**overrides: Any) -> RawItem:
    values: dict[str, Any] = {
        "source_key": "cellcom_tv",
        "kind": TitleKind.SERIES,
        "name": "פאודה",
        "offer_type": OfferType.STREAM,
    }
    values.update(overrides)
    return RawItem(**values)


class TestListings:
    def test_a_full_listing_survives_the_round_trip(self) -> None:
        original = item(
            name_alt="Fauda",
            year=2015,
            tmdb_id=64111,
            imdb_id="tt4565380",
            deep_link_url="https://cellcomtv.co.il/fauda",
            source_ref="s-991",
            poster_url="https://i.example/p.jpg",
            offer_type=OfferType.RENT,
            price_minor=1990,
            price_currency="ILS",
            credits=({"name": "Lior Raz", "role": "actor"},),
            origin_countries="IL",
            extra={"page": 3},
        )

        assert wire.item_from_wire(wire.item_to_wire(original)) == original

    def test_a_bare_listing_does_too(self) -> None:
        """Most of a scraped catalog is a name and a link and nothing else."""
        assert wire.item_from_wire(wire.item_to_wire(item())) == item()

    def test_the_offer_type_defaults_to_a_stream(self) -> None:
        """An older sender that has not heard of the field is not wrong, only quiet."""
        payload = wire.item_to_wire(item())
        del payload["offer_type"]

        assert wire.item_from_wire(payload).offer_type is OfferType.STREAM

    @pytest.mark.parametrize(
        ("payload", "because"),
        [
            ("not an object", "a listing is not an object"),
            ({}, "not a usable listing"),
            ({"source_key": "x", "kind": "opera", "name": "n"}, "not a usable listing"),
            ({"source_key": "x", "kind": "series", "name": "  "}, "not a usable listing"),
        ],
    )
    def test_something_unusable_is_refused_by_name(self, payload: Any, because: str) -> None:
        """The sender is the only person who can fix it, so they are told which."""
        with pytest.raises(wire.WireError, match=because):
            wire.item_from_wire(payload)

    def test_a_refusal_the_item_makes_of_itself_is_carried_through(self) -> None:
        """RawItem validates itself, and those rules are as much the contract."""
        payload = wire.item_to_wire(item())
        payload["price_minor"] = 1990  # with no currency

        with pytest.raises(wire.WireError):
            wire.item_from_wire(payload)


class TestTmdbHits:
    def test_a_hit_survives_the_round_trip(self) -> None:
        hit = TmdbTitle(
            tmdb_id=64111,
            kind=TitleKind.SERIES,
            name="Fauda",
            original_name="פאודה",
            year=2015,
            overview="A story.",
            poster_path="/x.jpg",
        )

        assert wire.tmdb_from_wire(wire.tmdb_to_wire(hit)) == hit

    def test_one_with_nothing_but_an_identity_does_too(self) -> None:
        hit = TmdbTitle(
            tmdb_id=1,
            kind=TitleKind.MOVIE,
            name="A",
            original_name=None,
            year=None,
            overview=None,
            poster_path=None,
        )

        assert wire.tmdb_from_wire(wire.tmdb_to_wire(hit)) == hit

    @pytest.mark.parametrize("payload", [None, [], "1", {"tmdb_id": "not a number"}, {}])
    def test_something_unusable_is_refused(self, payload: Any) -> None:
        with pytest.raises(wire.WireError):
            wire.tmdb_from_wire(payload)


class TestTitlesToLookUp:
    def test_a_title_survives_the_round_trip(self) -> None:
        view = TitleView(
            id=7,
            kind=TitleKind.MOVIE,
            name_he="פוקסטרוט",
            name_en="Foxtrot",
            year=2017,
            tmdb_id=430351,
            imdb_id="tt6896536",
        )

        assert wire.view_from_wire(wire.view_to_wire(view)) == view

    def test_one_known_by_a_single_name_does_too(self) -> None:
        view = TitleView(
            id=7,
            kind=TitleKind.MOVIE,
            name_he="פוקסטרוט",
            name_en=None,
            year=None,
            tmdb_id=None,
            imdb_id=None,
        )

        assert wire.view_from_wire(wire.view_to_wire(view)) == view

    @pytest.mark.parametrize("payload", [None, "7", {"id": 7}, {"id": "x", "kind": "movie"}])
    def test_something_unusable_is_refused(self, payload: Any) -> None:
        with pytest.raises(wire.WireError):
            wire.view_from_wire(payload)


class TestFindings:
    def test_ratings_survive_the_round_trip(self) -> None:
        found = EnrichResult(
            ratings=[
                Rating(
                    provider=RatingProvider.SERET_VIEWERS,
                    score_raw=8.9,
                    vote_count=120,
                    url="https://www.seret.co.il/movies/s_movies.asp?MID=1",
                ),
                Rating(provider=RatingProvider.IMDB, score_raw=7.1),
            ]
        )

        back = wire.finding_from_wire(wire.finding_to_wire(found))

        assert back.ratings == found.ratings

    def test_a_metadata_patch_does_too(self) -> None:
        found = EnrichResult(
            metadata_patch={"name_en": "Foxtrot", "genres": [{"tmdb_id": 18, "name_en": "Drama"}]}
        )

        back = wire.finding_from_wire(wire.finding_to_wire(found))

        assert back.metadata_patch == found.metadata_patch

    def test_an_enricher_that_found_nothing_is_not_an_error(self) -> None:
        """Ordinary: plenty of Israeli titles are not on Rotten Tomatoes."""
        back = wire.finding_from_wire(wire.finding_to_wire(EnrichResult()))

        assert back.is_empty

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "nothing",
            {"ratings": [{"provider": "who", "score_raw": 1.0}]},
            {"ratings": [{"provider": "imdb"}]},
            {"ratings": [{"provider": "imdb", "score_raw": "high"}]},
            {"metadata_patch": ["not an object"]},
        ],
    )
    def test_something_unusable_is_refused(self, payload: Any) -> None:
        with pytest.raises(wire.WireError):
            wire.finding_from_wire(payload)


class TestOptionalNumbers:
    """``bool`` is an ``int`` in Python, and ``True`` would silently become 1.

    Worth its own test because the failure is invisible: a listing whose year
    arrived as ``true`` would be filed under the year 1 and look like a parsing
    accident at the far end, months later.
    """

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_not_a_number(self, value: bool) -> None:
        payload = wire.item_to_wire(item())
        payload["year"] = value

        assert wire.item_from_wire(payload).year is None

    def test_a_number_written_as_text_still_is_one(self) -> None:
        """Senders serialise differently and this is not worth refusing over."""
        payload = wire.item_to_wire(item())
        payload["year"] = "2015"

        assert wire.item_from_wire(payload).year == 2015


class TestTheChunkSizes:
    """Constants rather than behaviour, and asserted anyway.

    Each pair is a default and the cap the far end enforces on it, and a
    default above its own cap is a configuration that refuses every request -
    which is the kind of thing that is only noticed in production.
    """

    @pytest.mark.parametrize(
        ("default", "cap"),
        [
            (wire.SYNC_CHUNK_SIZE, wire.MAX_SYNC_CHUNK),
            (wire.ENRICH_CHUNK_SIZE, wire.MAX_ENRICH_CHUNK),
            (wire.DUE_PAGE_SIZE, wire.MAX_DUE_PAGE),
            (wire.SERET_WRITE_CHUNK, wire.MAX_SERET_WRITE_CHUNK),
            (wire.IMDB_WRITE_CHUNK, wire.MAX_IMDB_WRITE_CHUNK),
        ],
    )
    def test_a_default_never_exceeds_its_cap(self, default: int, cap: int) -> None:
        assert 0 < default <= cap

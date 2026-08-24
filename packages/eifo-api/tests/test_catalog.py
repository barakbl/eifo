"""The catalog endpoints: filters, search, sorting, paging and detail."""

from __future__ import annotations

from fastapi.testclient import TestClient
from seed import Seeded
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import CreditRole, OfferType, SourceKind, TitleKind
from eifo_core.models import Availability, Credit, Person, Source, Title


def names(payload: dict) -> list[str]:
    """Display names of the returned cards, Hebrew preferred."""
    return [item["name_he"] or item["name_en"] for item in payload["items"]]


def ids(payload: dict) -> list[int]:
    return [item["id"] for item in payload["items"]]


class TestAvailabilityFilter:
    def test_defaults_to_what_can_be_watched_now(self, client: TestClient, catalog: Seeded) -> None:
        """Only titles currently offered by a source we still track."""
        body = client.get("/api/v1/titles").json()

        assert ids(body) == [catalog.fauda]
        assert body["total"] == 1

    def test_a_title_only_on_a_retired_source_is_not_current(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """Leftovers on an untracked source are history, not somewhere to send you."""
        assert catalog.shtisel not in ids(client.get("/api/v1/titles").json())

    def test_any_includes_titles_that_went_away(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"available": "any"}).json()

        assert catalog.foxtrot in ids(body)
        assert catalog.shtisel in ids(body)

    def test_gone_returns_only_lapsed_availability(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"available": "gone"}).json()

        assert ids(body) == [catalog.foxtrot]

    def test_a_title_on_nothing_is_never_current(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"available": "any"}).json()

        assert catalog.orphan not in ids(body)


class TestSourceFilter:
    def test_filters_to_one_service(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"sources": "mako"}).json()

        assert ids(body) == [catalog.fauda]

    def test_a_service_with_nothing_current_returns_nothing(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        assert client.get("/api/v1/titles", params={"sources": "free_tv"}).json()["total"] == 0

    def test_several_services_are_combined(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"sources": "netflix_il,mako"}).json()

        assert ids(body) == [catalog.fauda]

    def test_an_unknown_service_matches_nothing(self, client: TestClient, catalog: Seeded) -> None:
        assert client.get("/api/v1/titles", params={"sources": "nope"}).json()["total"] == 0


class TestTextSearch:
    def test_finds_a_title_by_its_hebrew_name(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"q": "פאודה", "available": "any"}).json()

        assert ids(body) == [catalog.fauda]

    def test_finds_the_same_title_by_its_english_name(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """One index, either language - the S3 acceptance criterion."""
        body = client.get("/api/v1/titles", params={"q": "Fauda", "available": "any"}).json()

        assert ids(body) == [catalog.fauda]

    def test_search_is_case_insensitive(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"q": "fAuDa", "available": "any"}).json()

        assert ids(body) == [catalog.fauda]

    def test_matches_a_prefix_while_still_typing(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"q": "fau", "available": "any"}).json()

        assert ids(body) == [catalog.fauda]

    def test_searches_overviews_too(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"q": "undercover", "available": "any"}).json()

        assert ids(body) == [catalog.fauda]

    def test_a_hebrew_only_title_is_findable(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"q": "שטיסל", "available": "any"}).json()

        assert ids(body) == [catalog.shtisel]

    def test_an_unmatched_query_returns_an_empty_page(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"q": "zzzznothing"}).json()

        assert body["items"] == []
        assert body["total"] == 0

    def test_query_syntax_characters_do_not_break_search(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """FTS5 has its own grammar; raw input must never reach it unquoted."""
        for query in ['"', "AND", "fauda OR", "*", "NEAR(", "^x", '")']:
            response = client.get("/api/v1/titles", params={"q": query, "available": "any"})

            assert response.status_code == 200

    def test_punctuation_only_input_is_treated_as_no_filter(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"q": "!!!", "available": "any"}).json()

        assert body["total"] >= 1

    def test_search_reflects_an_edit(
        self, client: TestClient, catalog: Seeded, session_factory: object
    ) -> None:
        """The index is trigger-maintained, so an update must be searchable."""
        from eifo_core.models import Title

        with session_factory() as session:  # type: ignore[operator]
            title = session.get(Title, catalog.shtisel)
            title.name_en = "Shtisel"
            session.commit()

        body = client.get("/api/v1/titles", params={"q": "Shtisel", "available": "any"}).json()

        assert ids(body) == [catalog.shtisel]


class TestOtherFilters:
    def test_filters_by_kind(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"type": "movie", "available": "any"}).json()

        assert catalog.foxtrot in ids(body)
        assert catalog.fauda not in ids(body)

    def test_filters_by_genre(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get(
            "/api/v1/titles", params={"genres": str(catalog.drama), "available": "any"}
        ).json()

        assert set(ids(body)) == {catalog.fauda, catalog.foxtrot}

    def test_filters_by_year_range(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get(
            "/api/v1/titles",
            params={"year_min": 2016, "year_max": 2018, "available": "any"},
        ).json()

        assert ids(body) == [catalog.foxtrot]

    def test_filters_by_minimum_score(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"score_min": 90, "available": "any"}).json()

        assert ids(body) == [catalog.foxtrot]

    def test_a_malformed_genre_list_is_ignored_not_fatal(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        response = client.get("/api/v1/titles", params={"genres": "abc,,"})

        assert response.status_code == 200

    def test_an_invalid_enum_is_rejected(self, client: TestClient, catalog: Seeded) -> None:
        response = client.get("/api/v1/titles", params={"available": "nonsense"})

        assert response.status_code == 422


class TestSorting:
    def test_sorts_by_score_descending_by_default(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"available": "any"}).json()

        assert ids(body)[:2] == [catalog.foxtrot, catalog.fauda]

    def test_titles_without_a_score_sort_last(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"available": "any"}).json()

        assert ids(body)[-1] == catalog.shtisel

    def test_sorts_by_israeli_score(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get(
            "/api/v1/titles", params={"sort": "score_israeli", "available": "any"}
        ).json()

        assert ids(body)[0] == catalog.fauda

    def test_sorts_by_year(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"sort": "year", "available": "any"}).json()

        assert ids(body)[0] == catalog.foxtrot

    def test_sorts_by_name(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/titles", params={"sort": "name", "available": "any"}).json()

        assert names(body) == sorted(names(body))

    def test_sorts_by_when_it_became_available(self, client: TestClient, catalog: Seeded) -> None:
        """Newest arrival first, by when it appeared rather than when we stored it."""
        body = client.get(
            "/api/v1/titles", params={"sort": "recently_added", "available": "any"}
        ).json()

        assert ids(body)[0] == catalog.fauda


class TestSortDirection:
    """Every sort reads both ways; what the catalog does not know sorts last."""

    def _ids(self, client: TestClient, **params: object) -> list[int]:
        return ids(client.get("/api/v1/titles", params={"available": "any", **params}).json())

    def test_ascending_is_the_reverse_of_descending(self, client: TestClient) -> None:
        assert self._ids(client, sort="year", order="asc") == list(
            reversed(self._ids(client, sort="year", order="desc"))
        )

    def test_leaving_it_unset_answers_as_it_always_did(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """Every URL written before this parameter existed keeps its meaning."""
        assert self._ids(client, sort="year") == self._ids(client, sort="year", order="desc")
        assert self._ids(client, sort="name") == self._ids(client, sort="name", order="asc")

    def test_the_worst_reviewed_can_be_asked_for(self, client: TestClient, catalog: Seeded) -> None:
        scored = self._ids(client, sort="score", order="asc")

        assert scored[0] == catalog.fauda  # the lower of the two scores
        assert scored.index(catalog.foxtrot) < scored.index(catalog.shtisel)

    def test_an_unscored_title_sorts_last_in_both_directions(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """Asking for the worst reviewed must not answer with the unreviewed."""
        assert self._ids(client, sort="score", order="desc")[-1] == catalog.shtisel
        assert self._ids(client, sort="score", order="asc")[-1] == catalog.shtisel

    def test_a_title_with_no_year_sorts_last_in_both_directions(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """The 1,836 titles nobody has dated are the worst possible answer to
        "show me the oldest", which is what a naive ascending sort gives: SQLite
        sorts NULL lowest."""
        with session_factory() as session:
            session.get(Title, catalog.shtisel).year = None
            session.commit()

        assert self._ids(client, sort="year", order="asc")[-1] == catalog.shtisel
        assert self._ids(client, sort="year", order="desc")[-1] == catalog.shtisel

    def test_names_can_be_read_backwards(self, client: TestClient) -> None:
        forwards = self._ids(client, sort="name", order="asc")

        assert self._ids(client, sort="name", order="desc") == list(reversed(forwards))

    def test_an_unknown_direction_is_rejected(self, client: TestClient) -> None:
        response = client.get("/api/v1/titles", params={"sort": "year", "order": "sideways"})

        assert response.status_code == 422


class TestPaging:
    def test_pages_the_results(self, client: TestClient, catalog: Seeded) -> None:
        first = client.get("/api/v1/titles", params={"page_size": 1, "available": "any"}).json()
        second = client.get(
            "/api/v1/titles", params={"page_size": 1, "page": 2, "available": "any"}
        ).json()

        assert len(first["items"]) == 1
        assert ids(first) != ids(second)

    def test_total_counts_every_match_not_the_page(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"page_size": 1, "available": "any"}).json()

        assert body["total"] == 3

    def test_a_page_past_the_end_is_empty_not_an_error(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"page": 99, "available": "any"}).json()

        assert body["items"] == []

    def test_page_size_is_capped(self, client: TestClient, catalog: Seeded) -> None:
        assert client.get("/api/v1/titles", params={"page_size": 5000}).status_code == 422


class TestTitleCard:
    def _fauda(self, client: TestClient) -> dict:
        body = client.get("/api/v1/titles", params={"q": "Fauda", "available": "any"}).json()
        return body["items"][0]

    def test_carries_both_names_and_the_scores(self, client: TestClient, catalog: Seeded) -> None:
        card = self._fauda(client)

        assert card["name_he"] == "פאודה"
        assert card["name_en"] == "Fauda"
        assert card["score"] == 85
        assert card["score_israeli"] == 89

    def test_poster_is_a_servable_url(self, client: TestClient, catalog: Seeded) -> None:
        assert self._fauda(client)["poster_url"] == "/images/posters/1/w500.jpg"

    def test_embeds_availability_so_a_grid_is_one_request(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        card = self._fauda(client)

        assert {entry["source_key"] for entry in card["availability"]} == {"netflix_il", "mako"}

    def test_a_card_shows_only_current_availability(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"q": "Foxtrot", "available": "any"}).json()

        assert body["items"][0]["availability"] == []


class TestTitleDetail:
    def test_returns_the_full_record(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get(f"/api/v1/titles/{catalog.fauda}").json()

        assert body["overview_he"] == "יחידה מסתערבת."
        assert body["seasons"] == 4

    def test_every_rating_is_attributed_and_linked(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """A score without its source is a rumour."""
        ratings = client.get(f"/api/v1/titles/{catalog.fauda}").json()["ratings"]

        by_provider = {rating["provider"]: rating for rating in ratings}
        assert by_provider["imdb"]["provider_name"] == "IMDb"
        assert by_provider["imdb"]["score_display"] == "8.3"
        assert by_provider["imdb"]["url"].startswith("https://www.imdb.com/")

    def test_percentage_providers_display_as_percentages(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        ratings = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()["ratings"]

        assert ratings[0]["score_display"] == "94%"

    def test_exposes_how_the_aggregate_was_computed(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get(f"/api/v1/titles/{catalog.fauda}").json()

        assert body["aggregate"]["score"] == 85
        assert "imdb" in body["aggregate"]["components"]

    def test_detail_includes_availability_that_lapsed(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """The page can say "was on Netflix until…" rather than staying silent."""
        entries = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()["availability"]

        assert len(entries) == 1
        assert entries[0]["is_current"] is False
        assert entries[0]["gone_since"] is not None

    def test_flags_a_source_no_longer_tracked(self, client: TestClient, catalog: Seeded) -> None:
        entries = client.get(f"/api/v1/titles/{catalog.shtisel}").json()["availability"]

        assert entries[0]["source_active"] is False

    def test_an_unknown_title_is_a_problem_document(self, client: TestClient) -> None:
        response = client.get("/api/v1/titles/999999")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")


class TestSourcesAndGenres:
    def test_lists_every_source_including_retired_ones(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/sources").json()

        by_key = {source["key"]: source for source in body}
        assert by_key["free_tv"]["active"] is False
        assert by_key["free_tv"]["deactivated_at"] is not None

    def test_counts_what_each_source_currently_offers(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        by_key = {source["key"]: source for source in client.get("/api/v1/sources").json()}

        assert by_key["netflix_il"]["title_count"] == 1
        assert by_key["mako"]["title_count"] == 1

    def test_lists_genres(self, client: TestClient, catalog: Seeded) -> None:
        body = client.get("/api/v1/genres").json()

        assert {genre["name_en"] for genre in body} == {"Drama", "Comedy"}
        assert {genre["name_he"] for genre in body} == {"דרמה", "קומדיה"}


class TestRentalPrices:
    """A rent/buy offer carries what it costs; every other offer carries None.

    Seeded here rather than in the shared catalog: the corpus other tests count
    and sort must not shift because one source charges money.
    """

    def _rent(
        self,
        session_factory: sessionmaker[Session],
        catalog: Seeded,
        **overrides: object,
    ) -> None:
        with session_factory() as session:
            source = Source(
                key="cinematheque_vod",
                name="Cinematheque VOD (Tel Aviv)",
                kind=SourceKind.RENT_BUY,
                website_url="https://www.cinema.co.il/vod/",
            )
            session.add(source)
            session.flush()
            values: dict[str, object] = {
                "price_minor": 1990,
                "price_currency": "ILS",
                "deep_link_url": "https://cintlv.pres.global/order/132926",
            }
            values.update(overrides)
            session.add(
                Availability(
                    title_id=catalog.foxtrot,
                    source_id=source.id,
                    offer_type=OfferType.RENT,
                    **values,  # type: ignore[arg-type]
                )
            )
            session.commit()

    def _offer(self, client: TestClient, catalog: Seeded) -> dict:
        entries = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()["availability"]
        return next(entry for entry in entries if entry["source_key"] == "cinematheque_vod")

    def test_detail_exposes_the_price_with_its_currency(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        self._rent(session_factory, catalog)

        offer = self._offer(client, catalog)

        assert offer["offer_type"] == "rent"
        assert offer["price_minor"] == 1990
        assert offer["price_currency"] == "ILS"
        assert offer["deep_link_url"] == "https://cintlv.pres.global/order/132926"

    def test_an_offer_without_a_price_reports_none_not_zero(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """Included in a subscription is not the same as costing nothing."""
        entries = client.get(f"/api/v1/titles/{catalog.fauda}").json()["availability"]

        assert all(entry["price_minor"] is None for entry in entries)
        assert all(entry["price_currency"] is None for entry in entries)


class TestCreditsAndPeople:
    """Who made a title, and one page per person per role.

    Seeded here rather than in the shared catalog: the corpus other tests count
    and sort must not shift because a film gained a director.
    """

    def _seed(self, session_factory: sessionmaker[Session], catalog: Seeded) -> tuple[int, int]:
        """Returns the (director, actor) ids, which are how they are addressed."""
        with session_factory() as session:
            director = Person(name_he="איילת מנחמי", name_en="Ayelet Menachemi")
            actor = Person(name_en="Lior Raz", tmdb_id=21)
            session.add_all([director, actor])
            session.flush()
            ids = (director.id, actor.id)
            session.add_all(
                [
                    Credit(
                        title_id=catalog.foxtrot,
                        person_id=director.id,
                        role=CreditRole.DIRECTOR,
                        source="israel_film_archive",
                    ),
                    Credit(
                        title_id=catalog.foxtrot,
                        person_id=actor.id,
                        role=CreditRole.CAST,
                        character="Doron",
                        billing_order=0,
                        source="tmdb",
                    ),
                    # The same person, a different hat, on another title.
                    Credit(
                        title_id=catalog.fauda,
                        person_id=director.id,
                        role=CreditRole.CAST,
                        character="Herself",
                        billing_order=1,
                        source="tmdb",
                    ),
                ]
            )
            session.commit()
        return ids

    def test_a_title_carries_its_credits_crew_first(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        director_id, actor_id = self._seed(session_factory, catalog)

        body = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()

        assert [(c["role"], c["person"]["id"]) for c in body["credits"]] == [
            ("director", director_id),
            ("cast", actor_id),
        ]
        assert body["credits"][1]["character"] == "Doron"

    def test_a_person_page_lists_their_whole_body_of_work(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """One page per person: someone who directs and acts is one human."""
        director_id, _ = self._seed(session_factory, catalog)

        body = client.get(f"/api/v1/people/{director_id}").json()

        assert body["name_he"] == "איילת מנחמי"
        assert [(c["role"], c["title"]["id"]) for c in body["credits"]] == [
            ("director", catalog.foxtrot),
            ("cast", catalog.fauda),
        ]

    def test_credits_lead_with_what_they_made(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """Director before cast, so a page opens on their work, not a bit part."""
        director_id, _ = self._seed(session_factory, catalog)

        roles = [c["role"] for c in client.get(f"/api/v1/people/{director_id}").json()["credits"]]

        assert roles == sorted(roles, key=["director", "cinematographer", "cast"].index)

    def test_a_cast_credit_says_who_they_played(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        _, actor_id = self._seed(session_factory, catalog)

        body = client.get(f"/api/v1/people/{actor_id}").json()

        assert [(c["role"], c["character"]) for c in body["credits"]] == [("cast", "Doron")]

    def test_an_unknown_person_is_a_404(self, client: TestClient, catalog: Seeded) -> None:
        response = client.get("/api/v1/people/999999")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")

    def test_an_id_that_is_not_a_number_is_rejected(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        assert client.get("/api/v1/people/not-an-id").status_code == 422

    def test_one_source_wins_a_role_rather_than_naming_a_person_twice(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """The archive's Hebrew director and TMDB's Latin one are one human."""
        self._seed(session_factory, catalog)
        with session_factory() as session:
            canonical = Person(name_en="Ayelet Menachemi", tmdb_id=99)
            session.add(canonical)
            session.flush()
            session.add(
                Credit(
                    title_id=catalog.foxtrot,
                    person_id=canonical.id,
                    role=CreditRole.DIRECTOR,
                    source="tmdb",
                )
            )
            session.commit()
            canonical_id = canonical.id

        body = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()
        directors = [c for c in body["credits"] if c["role"] == "director"]

        assert [c["person"]["id"] for c in directors] == [canonical_id]

    def test_a_role_tmdb_says_nothing_about_keeps_the_scraped_credit(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """Most Israeli cinema has no TMDB credits at all; the archive is it."""
        director_id, _ = self._seed(session_factory, catalog)

        body = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()
        directors = [c for c in body["credits"] if c["role"] == "director"]

        assert [c["person"]["id"] for c in directors] == [director_id]

    def test_a_title_without_credits_reports_an_empty_list(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get(f"/api/v1/titles/{catalog.shtisel}").json()

        assert body["credits"] == []


class TestTitleMetadata:
    def test_language_and_countries_are_codes_the_client_can_localise(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            title = session.get(Title, catalog.foxtrot)
            assert title is not None
            title.original_language = "he"
            title.origin_countries = "IL,FR"
            session.commit()

        body = client.get(f"/api/v1/titles/{catalog.foxtrot}").json()

        assert body["original_language"] == "he"
        assert body["origin_countries"] == ["IL", "FR"]

    def test_a_title_with_no_country_reports_none_rather_than_a_blank(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get(f"/api/v1/titles/{catalog.fauda}").json()

        assert body["origin_countries"] == []
        assert body["original_language"] is None


class TestSuggest:
    """Search-as-you-type. People are here because nothing else could reach them."""

    def _people(self, session_factory: sessionmaker[Session], catalog: Seeded) -> None:
        with session_factory() as session:
            famous = Person(name_en="Gal Gadot")
            unknown = Person(name_en="Gal Gadot")  # a real hazard: names repeat
            session.add_all([famous, unknown, Person(name_he="לירז צ'רכי")])
            session.flush()
            for role in (CreditRole.CAST, CreditRole.DIRECTOR):
                session.add(
                    Credit(
                        title_id=catalog.fauda,
                        person_id=famous.id,
                        role=role,
                        source="tmdb",
                    )
                )
            session.commit()

    def test_a_title_is_offered_from_its_first_letters(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"q": "fau"}).json()
        suggested = client.get("/api/v1/suggest", params={"q": "fau"}).json()

        assert body["total"] >= 1
        assert "Fauda" in [item["name_en"] for item in suggested["titles"]]

    def test_a_hebrew_prefix_finds_the_same_title(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """One index, two languages: the whole point of the bilingual catalog."""
        body = client.get("/api/v1/suggest", params={"q": "פאוד"}).json()

        assert "Fauda" in [item["name_en"] for item in body["titles"]]

    def test_people_can_be_found_at_all(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        self._people(session_factory, catalog)

        body = client.get("/api/v1/suggest", params={"q": "gal"}).json()

        assert body["people"][0]["name_en"] == "Gal Gadot"

    def test_the_one_with_the_work_comes_first(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """A hundred names in the catalog belong to more than one person."""
        self._people(session_factory, catalog)

        people = client.get("/api/v1/suggest", params={"q": "gadot"}).json()["people"]

        assert people[0]["credit_count"] == 2
        assert people[-1]["credit_count"] == 0

    def test_the_query_comes_back_with_the_answer(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """Keystrokes outrun round trips; a client has to be able to drop a stale reply."""
        body = client.get("/api/v1/suggest", params={"q": "fau"}).json()

        assert body["query"] == "fau"

    def test_an_overview_word_is_not_a_name(self, client: TestClient, catalog: Seeded) -> None:
        """Suggesting every film whose synopsis mentions a word is noise."""
        body = client.get("/api/v1/suggest", params={"q": "undercover"}).json()

        assert body["titles"] == []

    def test_nothing_matching_is_not_an_error(self, client: TestClient) -> None:
        response = client.get("/api/v1/suggest", params={"q": "zzzzqqq"})

        assert response.status_code == 200
        assert response.json()["titles"] == []

    def test_an_empty_query_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/v1/suggest", params={"q": ""}).status_code == 422


class TestRelevance:
    """A search asks a question; the closest match is the answer to it."""

    def test_searching_orders_by_match_rather_than_score(
        self, client: TestClient, catalog: Seeded, session_factory: sessionmaker[Session]
    ) -> None:
        """A well-rated title that merely mentions the word used to come first."""
        with session_factory() as session:
            session.add(
                Title(
                    type=TitleKind.SERIES,
                    name_en="Something Else",
                    overview_en="A fauda of noise and confusion.",
                )
            )
            session.commit()

        first = ids(client.get("/api/v1/titles", params={"q": "fauda", "available": "any"}).json())

        assert first[0] == catalog.fauda

    def test_an_explicit_sort_still_wins(self, client: TestClient, catalog: Seeded) -> None:
        """Choosing an order is choosing it; only silence means "you decide"."""
        body = client.get(
            "/api/v1/titles", params={"q": "a", "sort": "year", "available": "any"}
        ).json()
        by_year = client.get("/api/v1/titles", params={"sort": "year", "available": "any"}).json()

        assert ids(body) == [i for i in ids(by_year) if i in ids(body)]

    def test_relevance_without_anything_to_match_falls_back(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        """Nothing typed means nothing to rank against, so it means best rated."""
        asked = client.get("/api/v1/titles", params={"sort": "relevance", "available": "any"})
        scored = client.get("/api/v1/titles", params={"sort": "score", "available": "any"})

        assert asked.status_code == 200
        assert ids(asked.json()) == ids(scored.json())

    def test_browsing_still_leads_with_the_best_rated(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        body = client.get("/api/v1/titles", params={"available": "any"}).json()

        assert ids(body)[0] == catalog.foxtrot

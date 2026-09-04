"""Search query building, conditional requests, and static serving."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from seed import Seeded

from eifo_api.caching import CATALOG_CACHE_CONTROL, etag_for
from eifo_api.search import MAX_TERMS, fts_query
from eifo_api.static import CLIENT_CACHE_CONTROL, IMAGE_CACHE_CONTROL, is_api_path


class TestFtsQuery:
    def test_quotes_a_single_term_and_adds_a_prefix_wildcard(self) -> None:
        assert fts_query("fauda") == '"fauda"*'

    def test_only_the_last_term_is_a_prefix(self) -> None:
        assert fts_query("waltz with bashir") == '"waltz" "with" "bashir"*'

    def test_strips_punctuation(self) -> None:
        assert fts_query("marvel's, daredevil!") == '"marvel" "s" "daredevil"*'

    def test_keeps_hebrew_terms(self) -> None:
        assert fts_query("פאודה") == '"פאודה"*'

    @pytest.mark.parametrize("query", ["", "   ", "!!!", "--", "()"])
    def test_input_with_no_terms_yields_no_filter(self, query: str) -> None:
        assert fts_query(query) is None

    @pytest.mark.parametrize("query", ['"', "AND", "OR NOT", "*", "NEAR(x", "^abc"])
    def test_fts_syntax_is_neutralised(self, query: str) -> None:
        """Whatever comes back must be quoted terms, never raw operators."""
        result = fts_query(query)

        if result is not None:
            assert result.startswith('"')

    def test_a_very_long_query_is_capped(self) -> None:
        result = fts_query(" ".join(f"term{index}" for index in range(50)))

        assert result is not None
        assert result.count('"') == MAX_TERMS * 2


class TestCaching:
    def test_catalog_responses_carry_an_etag(self, client: TestClient, catalog: Seeded) -> None:
        response = client.get("/api/v1/titles")

        assert response.headers["ETag"].startswith('W/"')
        assert response.headers["Cache-Control"] == CATALOG_CACHE_CONTROL

    def test_an_unchanged_request_is_answered_304(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        etag = client.get("/api/v1/titles").headers["ETag"]

        response = client.get("/api/v1/titles", headers={"If-None-Match": etag})

        assert response.status_code == 304
        assert response.content == b""

    def test_a_stale_validator_returns_the_body(self, client: TestClient, catalog: Seeded) -> None:
        response = client.get("/api/v1/titles", headers={"If-None-Match": 'W/"outdated"'})

        assert response.status_code == 200
        assert response.json()["items"]

    def test_different_queries_have_different_validators(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        first = client.get("/api/v1/titles").headers["ETag"]
        second = client.get("/api/v1/titles", params={"available": "any"}).headers["ETag"]

        assert first != second

    def test_sources_and_genres_are_cacheable_too(
        self, client: TestClient, catalog: Seeded
    ) -> None:
        for path in ("/api/v1/sources", "/api/v1/genres"):
            assert "ETag" in client.get(path).headers

    def test_the_etag_is_stable_for_identical_bodies(self) -> None:
        assert etag_for(b"same") == etag_for(b"same")
        assert etag_for(b"same") != etag_for(b"different")


class TestImageServing:
    def test_serves_a_stored_image(
        self, client: TestClient, settings: object, tmp_path: Path
    ) -> None:
        target = Path(settings.images_dir) / "posters" / "1"  # type: ignore[attr-defined]
        target.mkdir(parents=True, exist_ok=True)
        (target / "w500.jpg").write_bytes(b"not-really-a-jpeg")

        response = client.get("/images/posters/1/w500.jpg")

        assert response.status_code == 200
        assert response.content == b"not-really-a-jpeg"

    def test_images_are_cached_hard(
        self, client: TestClient, settings: object, tmp_path: Path
    ) -> None:
        """Paths embed the variant, so a changed image is a changed URL."""
        target = Path(settings.images_dir) / "posters" / "2"  # type: ignore[attr-defined]
        target.mkdir(parents=True, exist_ok=True)
        (target / "w500.jpg").write_bytes(b"x")

        response = client.get("/images/posters/2/w500.jpg")

        assert response.headers["Cache-Control"] == IMAGE_CACHE_CONTROL

    def test_a_missing_image_is_a_404_not_the_client(self, client: TestClient) -> None:
        """An image path must never fall back to serving index.html."""
        response = client.get("/images/posters/999/w500.jpg")

        assert response.status_code == 404


class TestClientServing:
    def test_serves_the_client_at_the_root(self, client: TestClient) -> None:
        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_the_client_files_are_revalidated_not_guessed_at(self, client: TestClient) -> None:
        """A module with no policy of its own is cached for as long as a browser
        feels like, which is how a shipped change goes on not being visible."""
        response = client.get("/js/app.js")

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == CLIENT_CACHE_CONTROL

    def test_a_deep_link_falls_back_to_the_client(self, client: TestClient) -> None:
        """A stray link should open the app, not a bare 404."""
        response = client.get("/title/42")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_an_unknown_api_path_is_still_a_problem_document(self, client: TestClient) -> None:
        """The fallback must not mask a missing endpoint as a 200 page."""
        response = client.get("/api/v1/nope")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/api/v1/titles", True),
            ("/docs", True),
            ("/openapi.json", True),
            ("/images/x.jpg", True),
            ("/", False),
            ("/title/1", False),
        ],
    )
    def test_api_paths_are_distinguished_from_client_paths(self, path: str, expected: bool) -> None:
        assert is_api_path(path) is expected


class TestClientLocation:
    """Where the client is found must not depend on how the package is installed."""

    def test_an_explicit_setting_wins(self, tmp_path: Path) -> None:
        from eifo_api.app import _web_dir
        from eifo_core.settings import Settings

        chosen = tmp_path / "somewhere-else"
        assert _web_dir(Settings(_env_file=None, web_dir=chosen)) == chosen

    def test_falls_back_to_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the container layout: the client sits beside the working dir.

        Deriving the path by counting parents from the module only works in a
        source checkout; installed into a virtualenv it lands inside the venv
        and the client silently stops being served.
        """
        from eifo_api.app import _web_dir
        from eifo_core.settings import Settings

        web = tmp_path / "web"
        web.mkdir()
        (web / "index.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert _web_dir(Settings(_env_file=None)) == web

    def test_finds_the_source_tree_when_run_from_elsewhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eifo_api.app import _web_dir
        from eifo_core.settings import Settings

        monkeypatch.chdir(tmp_path)  # no web/ here

        found = _web_dir(Settings(_env_file=None))

        assert (found / "index.html").is_file()

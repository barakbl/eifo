"""The wire contract between the fetcher and the API.

These live in core for the same reason the module does: the fetcher builds
archives and the API takes them apart, and the two never import each other. A
test that ran on only one side would leave the other free to drift, and the way
that drift announces itself is an upload one side builds happily and the other
rejects with nothing useful to say about why.
"""

from __future__ import annotations

import json

import pytest

from eifo_core.ingest import (
    ARCHIVE_VERSION,
    DIGEST_CHARS,
    MANIFEST_NAME,
    POSTER_VARIANTS,
    ManifestError,
    PosterItem,
    PosterManifest,
    member_name,
    poster_relpath,
    stored_name,
)

DIGEST = "a3f91c2e7b40de11" + "0" * 48


class TestVariants:
    def test_the_largest_is_last(self) -> None:
        # Both sides index the largest as [-1]: the fetcher to know which
        # rendition names the set, the API to know which path goes on the
        # title. Reordering this tuple would silently swap them.
        widths = [variant.width for variant in POSTER_VARIANTS]

        assert widths == sorted(widths)

    def test_every_variant_has_a_distinct_name(self) -> None:
        names = [variant.name for variant in POSTER_VARIANTS]

        assert len(set(names)) == len(names)


class TestWhereAFileGoes:
    def test_a_member_is_named_for_its_title_and_variant(self) -> None:
        assert member_name(17884, "w500") == "17884/w500.jpg"

    def test_the_stored_name_carries_the_digest(self) -> None:
        assert stored_name(DIGEST, "w500") == f"{DIGEST[:DIGEST_CHARS]}-w500.jpg"

    def test_all_of_a_title_s_variants_share_one_digest(self) -> None:
        """So that given any variant's path, the others are a suffix change away."""
        small = poster_relpath(17884, DIGEST, "w200")
        large = poster_relpath(17884, DIGEST, "w500")

        assert small.replace("w200", "w500") == large

    def test_new_bytes_mean_a_new_path(self) -> None:
        # The whole reason for content addressing: /images is served
        # `immutable`, so a poster rewritten at a fixed path is one that every
        # browser and cache already holding it will never see again.
        before = poster_relpath(17884, DIGEST, "w500")
        after = poster_relpath(17884, "7b40de11" + "f" * 56, "w500")

        assert before != after

    def test_paths_use_forward_slashes(self) -> None:
        # Written to titles.poster_path and served back as the tail of an image
        # URL. A Windows separator here is a backslash in a URL and a 404.
        assert "\\" not in poster_relpath(17884, DIGEST, "w500")
        assert poster_relpath(17884, DIGEST, "w500").startswith("posters/17884/")


class TestManifestRoundTrip:
    def test_what_is_written_can_be_read(self) -> None:
        manifest = PosterManifest(
            items=(
                PosterItem(title_id=1, variants=("w200", "w500"), source_url="https://x/1.jpg"),
                PosterItem(title_id=2, variants=("w500",)),
            )
        )

        parsed = PosterManifest.from_json(manifest.to_json())

        assert parsed == manifest

    def test_it_survives_a_name_that_is_not_ascii(self) -> None:
        manifest = PosterManifest(
            items=(PosterItem(title_id=1, variants=("w500",), source_url="https://x/הסנדק.jpg"),)
        )

        assert PosterManifest.from_json(manifest.to_json()) == manifest


class TestAManifestThisServerWillNotActOn:
    """Every refusal names what is wrong, because only the sender can fix it."""

    def test_text_that_is_not_json(self) -> None:
        with pytest.raises(ManifestError, match=MANIFEST_NAME):
            PosterManifest.from_json("{not json")

    def test_json_that_is_not_an_object(self) -> None:
        with pytest.raises(ManifestError, match="not an object"):
            PosterManifest.from_json("[]")

    def test_a_version_this_code_does_not_know(self) -> None:
        raw = json.dumps({"version": ARCHIVE_VERSION + 1, "kind": "posters", "items": []})

        with pytest.raises(ManifestError, match="newer or older"):
            PosterManifest.from_json(raw)

    def test_a_kind_this_endpoint_does_not_take(self) -> None:
        raw = json.dumps({"version": ARCHIVE_VERSION, "kind": "backdrops", "items": []})

        with pytest.raises(ManifestError, match="not 'backdrops'"):
            PosterManifest.from_json(raw)

    @pytest.mark.parametrize("title_id", [0, -1, "17884", None, True])
    def test_a_title_id_that_is_not_a_positive_integer(self, title_id: object) -> None:
        # True is in that list on purpose: bool is an int in Python, and
        # `items[0].title_id == True` would otherwise become a lookup of id 1.
        raw = json.dumps(
            {
                "version": ARCHIVE_VERSION,
                "kind": "posters",
                "items": [{"title_id": title_id, "variants": ["w500"]}],
            }
        )

        with pytest.raises(ManifestError, match="positive integer"):
            PosterManifest.from_json(raw)

    def test_a_variant_nobody_stores(self) -> None:
        raw = json.dumps(
            {
                "version": ARCHIVE_VERSION,
                "kind": "posters",
                "items": [{"title_id": 1, "variants": ["w9999"]}],
            }
        )

        with pytest.raises(ManifestError, match="unknown variants"):
            PosterManifest.from_json(raw)

    def test_a_title_that_names_no_variants(self) -> None:
        raw = json.dumps(
            {
                "version": ARCHIVE_VERSION,
                "kind": "posters",
                "items": [{"title_id": 1, "variants": []}],
            }
        )

        with pytest.raises(ManifestError, match="no variants"):
            PosterManifest.from_json(raw)

    def test_items_that_are_not_a_list(self) -> None:
        raw = json.dumps({"version": ARCHIVE_VERSION, "kind": "posters", "items": {}})

        with pytest.raises(ManifestError, match="not a list"):
            PosterManifest.from_json(raw)

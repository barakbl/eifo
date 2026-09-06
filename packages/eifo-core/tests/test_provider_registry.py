"""Storing what a plugin says about the scores it produces.

The point of the module under test is that nothing downstream has to be taught
a provider: the API renders whatever is in ``rating_providers`` and the client
renders whatever the API sends. So the tests that matter are about the handover
- that a declaration reaches the table, that a mark reaches the images root
under a name that changes when the mark does, and that a provider having a
quiet night never costs a catalog full of scores their attribution.

The declarations arrive as bytes rather than as paths, and that is the part
this side had to change for: the file ships with the plugin and the plugin is
not necessarily on this machine any more. Where it lands is decided here,
beside the artwork that lands the same way.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from eifo_core.enums import RatingProvider
from eifo_core.models import RatingProviderInfo
from eifo_core.providers import DeclaredProvider, register_declared_providers


def declared(provider: RatingProvider, **overrides: object) -> DeclaredProvider:
    fields: dict[str, object] = {
        "provider": provider,
        "label": "Tomatometer",
        "group_key": "rt",
        "group_name": "Rotten Tomatoes",
        "website_url": "https://www.rottentomatoes.com",
    }
    fields.update(overrides)
    return DeclaredProvider(**fields)  # type: ignore[arg-type]


def test_a_declaration_reaches_the_table(session: Session, tmp_path: Path) -> None:
    register_declared_providers(session, [declared(RatingProvider.RT_CRITICS)], images_dir=tmp_path)
    session.commit()

    row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
    assert row is not None
    assert row.label == "Tomatometer"
    assert row.group_key == "rt"
    assert row.group_name == "Rotten Tomatoes"


def test_a_mark_is_published_under_a_name_that_names_its_contents(
    session: Session, tmp_path: Path
) -> None:
    # The images root is served immutable, so a redrawn logo has to arrive at a
    # new URL or every browser that saw the old one shows it for a year.
    images = tmp_path / "images"

    register_declared_providers(
        session,
        [declared(RatingProvider.RT_CRITICS, logo=b"<svg>one</svg>", logo_suffix=".svg")],
        images_dir=images,
    )
    session.commit()
    row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
    assert row is not None and row.logo_path is not None
    # Read out now: the row is the same object after the second pass, so its
    # attribute is not a record of what it used to say.
    first = row.logo_path
    assert (images / first).read_text() == "<svg>one</svg>"

    register_declared_providers(
        session,
        [declared(RatingProvider.RT_CRITICS, logo=b"<svg>two</svg>", logo_suffix=".svg")],
        images_dir=images,
    )
    session.commit()
    assert row.logo_path != first
    assert (images / row.logo_path).read_text() == "<svg>two</svg>"
    # And the one nothing points at any more is gone, rather than accumulating
    # one file per redraw forever.
    assert not (images / first).exists()


def test_the_published_name_carries_the_suffix_it_was_sent_with(
    session: Session, tmp_path: Path
) -> None:
    """The sender's file had one; the bytes on the wire do not.

    Served as a static file, so the extension is what decides the content type
    a browser is told - and an SVG served as an unknown type is a broken chip
    rather than a missing one.
    """
    images = tmp_path / "images"
    register_declared_providers(
        session,
        [declared(RatingProvider.EDB, logo=b"\x89PNG\r\n", logo_suffix=".png")],
        images_dir=images,
    )
    session.commit()

    row = session.get(RatingProviderInfo, RatingProvider.EDB)
    assert row is not None and row.logo_path is not None
    assert row.logo_path.endswith(".png")


def test_publishing_the_same_mark_twice_changes_nothing(session: Session, tmp_path: Path) -> None:
    images = tmp_path / "images"
    declaration = [declared(RatingProvider.RT_CRITICS, logo=b"<svg/>", logo_suffix=".svg")]

    assert register_declared_providers(session, declaration, images_dir=images) == ["rt_critics"]
    session.commit()
    # This runs on every enrich; a second identical pass must not touch a row.
    assert register_declared_providers(session, declaration, images_dir=images) == []


def test_a_plugin_that_ships_no_mark_still_gets_a_row(session: Session, tmp_path: Path) -> None:
    register_declared_providers(session, [declared(RatingProvider.EDB)], images_dir=tmp_path)
    session.commit()

    row = session.get(RatingProviderInfo, RatingProvider.EDB)
    assert row is not None and row.logo_path is None


def test_an_images_root_that_cannot_be_written_is_not_a_failed_enrich(
    session: Session, tmp_path: Path
) -> None:
    # An enrich about to write ten thousand ratings does not stop over a logo.
    # The chip says the provider's name, as every chip did before marks existed.
    blocked = tmp_path / "images"
    blocked.write_text("this is a file, so nothing can be created inside it")

    register_declared_providers(
        session,
        [declared(RatingProvider.RT_CRITICS, logo=b"<svg/>", logo_suffix=".svg")],
        images_dir=blocked,
    )
    session.commit()

    row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
    assert row is not None and row.logo_path is None


def test_a_changed_declaration_is_carried_forward(session: Session, tmp_path: Path) -> None:
    register_declared_providers(session, [declared(RatingProvider.RT_CRITICS)], images_dir=tmp_path)
    session.commit()

    changed = register_declared_providers(
        session,
        [declared(RatingProvider.RT_CRITICS, label="Critics", position=3)],
        images_dir=tmp_path,
    )
    session.commit()

    assert changed == ["rt_critics"]
    row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
    assert row is not None
    assert (row.label, row.position) == ("Critics", 3)


def test_nothing_is_ever_removed(session: Session, tmp_path: Path) -> None:
    # `--skip rt` is a decision about tonight. The catalog still holds
    # thousands of RT scores, and a row deleted because a plugin was quiet
    # would take the name off every one of them.
    register_declared_providers(session, [declared(RatingProvider.RT_CRITICS)], images_dir=tmp_path)
    session.commit()

    register_declared_providers(session, [declared(RatingProvider.EDB)], images_dir=tmp_path)
    session.commit()

    assert session.get(RatingProviderInfo, RatingProvider.RT_CRITICS) is not None

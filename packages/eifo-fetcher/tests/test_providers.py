"""Writing down what a plugin says about the scores it produces.

The point of this module is that nothing downstream has to be taught a
provider: the API renders whatever is in ``rating_providers`` and the client
renders whatever the API sends. So the tests that matter are about the handover
- that a declaration reaches the table, that a mark reaches the images root
under a name that changes when the mark does, and that a provider having a
quiet night never costs a catalog full of scores their attribution.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import RatingProvider
from eifo_core.models import RatingProviderInfo
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import ProviderInfo
from eifo_fetcher.enrichers.imdb import ImdbDatasetLoader
from eifo_fetcher.enrichers.rt import RottenTomatoesEnricher
from eifo_fetcher.enrichers.seret import SeretEnricher
from eifo_fetcher.enrichers.tmdb_meta import TmdbMetadataEnricher
from eifo_fetcher.http import HttpClient
from eifo_fetcher.providers import (
    declared_providers,
    refresh_declared_providers,
    register_declared_providers,
)
from eifo_fetcher.runner import enrich_all


def info(provider: RatingProvider, **overrides: object) -> ProviderInfo:
    fields: dict[str, object] = {
        "provider": provider,
        "label": "Tomatometer",
        "group_key": "rt",
        "group_name": "Rotten Tomatoes",
        "website_url": "https://www.rottentomatoes.com",
    }
    fields.update(overrides)
    return ProviderInfo(**fields)  # type: ignore[arg-type]


class _Plugin:
    def __init__(self, *infos: ProviderInfo) -> None:
        self.provider_info = infos


def test_a_declaration_reaches_the_table(session: Session, tmp_path: Path) -> None:
    register_declared_providers(session, [info(RatingProvider.RT_CRITICS)], images_dir=tmp_path)
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
    icon = tmp_path / "rt.svg"
    icon.write_text("<svg>one</svg>")
    images = tmp_path / "images"

    register_declared_providers(
        session, [info(RatingProvider.RT_CRITICS, icon=icon)], images_dir=images
    )
    session.commit()
    row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
    assert row is not None and row.logo_path is not None
    # Read out now: the row is the same object after the second pass, so its
    # attribute is not a record of what it used to say.
    first = row.logo_path
    assert (images / first).read_text() == "<svg>one</svg>"

    icon.write_text("<svg>two</svg>")
    register_declared_providers(
        session, [info(RatingProvider.RT_CRITICS, icon=icon)], images_dir=images
    )
    session.commit()
    assert row.logo_path != first
    assert (images / row.logo_path).read_text() == "<svg>two</svg>"
    # And the one nothing points at any more is gone, rather than accumulating
    # one file per redraw forever.
    assert not (images / first).exists()


def test_publishing_the_same_mark_twice_changes_nothing(session: Session, tmp_path: Path) -> None:
    icon = tmp_path / "rt.svg"
    icon.write_text("<svg/>")
    images = tmp_path / "images"
    declaration = [info(RatingProvider.RT_CRITICS, icon=icon)]

    assert register_declared_providers(session, declaration, images_dir=images) == ["rt_critics"]
    session.commit()
    # This runs on every enrich; a second identical pass must not touch a row.
    assert register_declared_providers(session, declaration, images_dir=images) == []


def test_a_plugin_that_ships_no_mark_still_gets_a_row(session: Session, tmp_path: Path) -> None:
    register_declared_providers(session, [info(RatingProvider.EDB)], images_dir=tmp_path)
    session.commit()

    row = session.get(RatingProviderInfo, RatingProvider.EDB)
    assert row is not None and row.logo_path is None


def test_a_mark_that_is_not_there_is_not_a_failed_enrich(session: Session, tmp_path: Path) -> None:
    # An enrich about to write ten thousand ratings does not stop over a
    # missing logo. The chip says the provider's name, as every chip did
    # before marks existed.
    register_declared_providers(
        session,
        [info(RatingProvider.RT_CRITICS, icon=tmp_path / "nothing-here.svg")],
        images_dir=tmp_path / "images",
    )
    session.commit()

    row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
    assert row is not None and row.logo_path is None


def test_a_changed_declaration_is_carried_forward(session: Session, tmp_path: Path) -> None:
    register_declared_providers(session, [info(RatingProvider.RT_CRITICS)], images_dir=tmp_path)
    session.commit()

    changed = register_declared_providers(
        session,
        [info(RatingProvider.RT_CRITICS, label="Critics", position=3)],
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
    register_declared_providers(session, [info(RatingProvider.RT_CRITICS)], images_dir=tmp_path)
    session.commit()

    register_declared_providers(session, [info(RatingProvider.EDB)], images_dir=tmp_path)
    session.commit()

    assert session.get(RatingProviderInfo, RatingProvider.RT_CRITICS) is not None


def test_the_first_declaration_of_a_provider_wins() -> None:
    # A third-party plugin may add a provider; it should not be able to quietly
    # rename one that ships with Eifo.
    found = declared_providers(
        [
            _Plugin(info(RatingProvider.RT_CRITICS, label="Tomatometer")),
            _Plugin(info(RatingProvider.RT_CRITICS, label="Something else")),
        ]
    )

    assert [(i.provider, i.label) for i in found] == [(RatingProvider.RT_CRITICS, "Tomatometer")]


def test_a_plugin_declaring_nothing_is_skipped_rather_than_failing() -> None:
    assert declared_providers([object(), _Plugin()]) == []


def test_every_built_in_provider_that_is_collected_describes_itself() -> None:
    """The declarations and the providers each plugin returns must agree.

    A provider an enricher can return but never declares is a score on the page
    credited by its database key. This is the test that notices, because
    nothing else would until somebody looked at a title.
    """
    plugins = [
        TmdbMetadataEnricher(),
        SeretEnricher(),
        RottenTomatoesEnricher(),
        ImdbDatasetLoader,
    ]
    declared = {i.provider for i in declared_providers(plugins)}
    collected = {p for plugin in plugins for p in getattr(plugin, "providers", ())}
    collected.add(RatingProvider.IMDB)

    assert collected <= declared


def test_the_marks_that_ship_with_eifo_are_actually_there() -> None:
    """Every built-in declaration points at a file in the package.

    A path typed wrong degrades quietly - the chip falls back to the name, and
    nobody notices the logo was meant to be there.
    """
    for declaration in declared_providers(
        [TmdbMetadataEnricher(), SeretEnricher(), RottenTomatoesEnricher(), ImdbDatasetLoader]
    ):
        assert declaration.icon is not None, f"{declaration.provider} ships no mark"
        assert declaration.icon.is_file(), f"{declaration.provider}: {declaration.icon} is missing"


def test_two_figures_from_one_service_are_one_group() -> None:
    """The whole point of the grouping, asserted where it is declared.

    Rotten Tomatoes and Seret each report two figures, and each is one service.
    Shown as four chips they read as four raters, which is wrong about what the
    page is showing and wrong about how much the catalog knows.
    """
    for plugin in (RottenTomatoesEnricher(), SeretEnricher()):
        groups = {i.group_key for i in plugin.provider_info}
        assert len(groups) == 1, f"{plugin.key} should be one chip, not {len(groups)}"
        assert len(plugin.provider_info) == 2
        # Critics before the crowd, which is how both sites print them.
        assert [i.position for i in plugin.provider_info] == [0, 1]


def test_an_enrich_writes_what_the_installed_plugins_declare(
    session_factory: sessionmaker[Session],
    settings: Settings,
    http: HttpClient,
) -> None:
    """The wiring, end to end, on an empty catalog.

    Registration hangs off the enrich rather than off a command of its own, so
    that a deployment which upgrades and then runs its usual nightly comes up
    with logos and names without anybody being told to run anything. This is
    the test that would notice the call being dropped: everything downstream
    degrades quietly to provider keys, which looks like a data problem rather
    than a missing line.
    """
    enrich_all(session_factory, settings, http=http, limit=0, skip_imdb=True)

    with session_factory() as session:
        rows = {row.provider: row for row in session.scalars(select(RatingProviderInfo)).all()}

    assert RatingProvider.RT_CRITICS in rows
    assert rows[RatingProvider.RT_CRITICS].group_key == rows[RatingProvider.RT_AUDIENCE].group_key
    # And the marks landed where the API serves them from.
    logo = rows[RatingProvider.RT_CRITICS].logo_path
    assert logo is not None and (settings.images_dir / logo).is_file()


def test_any_fetcher_command_brings_the_table_up_to_date(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """Not just the enrich.

    A title page is rendered between runs, not during one, so hanging this off
    the enrich alone left a deployment that upgraded on Tuesday crediting its
    scores by database key until Wednesday's nightly finished.
    """
    changed = refresh_declared_providers(session_factory, settings)

    assert "rt_critics" in changed
    with session_factory() as session:
        row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
        assert row is not None and row.logo_path is not None
        assert (settings.images_dir / row.logo_path).is_file()


def test_a_database_without_the_table_is_not_an_error(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """A schema stopped short of 0024 still lists its sources.

    `db upgrade` is the fix and says so on its own; refusing every command
    until somebody runs it would be a poor trade for a logo.
    """
    with session_factory() as session:
        RatingProviderInfo.__table__.drop(session.get_bind())
        session.commit()

    assert refresh_declared_providers(session_factory, settings) == []

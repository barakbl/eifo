"""Saying what a plugin declares about the scores it produces.

This side collects the declarations and hands them over; what the catalog makes
of them is tested where the catalog is (``eifo-core``'s ``test_providers``).
The tests that matter here are about the collecting and the handover - that
every built-in provider describes itself, that a mark travels as bytes because
the plugin is not necessarily on the machine with the images root, and that a
missing file is a chip without a logo rather than a failed enrich.
"""

from __future__ import annotations

from base64 import b64decode
from pathlib import Path
from typing import Any

import httpx
import pytest
from live import LiveApi
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.enums import FetchPhase, RatingProvider
from eifo_core.models import RatingProviderInfo
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import ProviderInfo
from eifo_fetcher.enrichers.imdb import ImdbDatasetLoader
from eifo_fetcher.enrichers.rt import RottenTomatoesEnricher
from eifo_fetcher.enrichers.seret import SeretEnricher
from eifo_fetcher.enrichers.tmdb_meta import TmdbMetadataEnricher
from eifo_fetcher.http import HttpClient
from eifo_fetcher.ingest import IngestClient
from eifo_fetcher.providers import (
    declared_providers,
    provider_to_wire,
    refresh_declared_providers,
)
from eifo_fetcher.runner import enrich_all, phase_client


@pytest.fixture
def session_factory(live_api: LiveApi) -> sessionmaker[Session]:
    """The catalog the API writes the declarations to."""
    return live_api.session_factory


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


class TestSendingAMark:
    """The bytes travel; the path does not.

    A mark is a file that ships with the plugin, and the plugin may be on a
    laptop while the images root is on a server. Sending a path would name a
    file the far end cannot see, and the failure would be a chip quietly
    missing its logo - which looks like a design decision rather than a bug.
    """

    def test_the_mark_travels_with_the_declaration(self, tmp_path: Path) -> None:
        icon = tmp_path / "rt.svg"
        icon.write_text("<svg>one</svg>")

        sent = provider_to_wire(info(RatingProvider.RT_CRITICS, icon=icon))

        assert sent["logo"] is not None
        assert b64decode(sent["logo"]) == b"<svg>one</svg>"
        assert sent["logo_suffix"] == ".svg", "the far end serves this as a static file"

    def test_a_plugin_that_ships_no_mark_sends_none(self) -> None:
        sent = provider_to_wire(info(RatingProvider.EDB))

        assert sent["logo"] is None

    def test_a_mark_that_is_not_there_is_not_a_failed_enrich(self, tmp_path: Path) -> None:
        """The chip says the provider's name, as every chip did before marks existed."""
        sent = provider_to_wire(info(RatingProvider.RT_CRITICS, icon=tmp_path / "nothing.svg"))

        assert sent["logo"] is None
        assert sent["provider"] == RatingProvider.RT_CRITICS.value


def test_an_enrich_writes_what_the_installed_plugins_declare(
    live_api: LiveApi,
    session_factory: sessionmaker[Session],
    settings: Settings,
    http: HttpClient,
) -> None:
    """The wiring, end to end, on an empty catalog.

    Registration hangs off opening a phase rather than off a command of its own,
    so that a deployment which upgrades and then runs its usual nightly comes up
    with logos and names without anybody being told to run anything. This is the
    test that would notice the call being dropped: everything downstream
    degrades quietly to provider keys, which looks like a data problem rather
    than a missing line.

    Through ``phase_client`` because that is where it happens now. It used to be
    inside ``enrich_all`` as well, which meant every enrich declared the same
    seven providers twice - visible the moment there was a log of every call.
    """
    with phase_client(settings, FetchPhase.ENRICH, api=live_api.api):
        enrich_all(settings, http=http, api=live_api.api, limit=0, skip_imdb=True)

    with session_factory() as session:
        rows = {row.provider: row for row in session.scalars(select(RatingProviderInfo)).all()}

    assert RatingProvider.RT_CRITICS in rows
    assert rows[RatingProvider.RT_CRITICS].group_key == rows[RatingProvider.RT_AUDIENCE].group_key
    # And the marks landed where the API serves them from.
    logo = rows[RatingProvider.RT_CRITICS].logo_path
    assert logo is not None and (settings.images_dir / logo).is_file()


def test_any_fetcher_command_brings_the_table_up_to_date(
    live_api: LiveApi, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    """Not just the enrich.

    A title page is rendered between runs, not during one, so hanging this off
    the enrich alone left a deployment that upgraded on Tuesday crediting its
    scores by database key until Wednesday's nightly finished.
    """
    changed = refresh_declared_providers(live_api.api, settings)

    assert "rt_critics" in changed
    with session_factory() as session:
        row = session.get(RatingProviderInfo, RatingProvider.RT_CRITICS)
        assert row is not None and row.logo_path is not None
        assert (settings.images_dir / row.logo_path).is_file()


def test_a_catalog_that_will_not_take_them_is_not_a_failed_command(
    settings: Settings, ingest_api: Any
) -> None:
    """A refusal here is a caption on a chip, not a reason to abandon a sync.

    It used to be a table this process wrote itself, so the only way it could
    fail was a schema stopped short of the migration that adds it. Now it is a
    request, and every way a request can fail applies - so the answer has to be
    the same one: say so, and get on with the run.
    """

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nothing listening")

    api = IngestClient(
        "https://eifo.test",
        "eifo_pat_x",
        http=httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    with api:
        assert refresh_declared_providers(api, settings) == []

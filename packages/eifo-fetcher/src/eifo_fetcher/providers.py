"""Writing down what each ratings provider says about itself.

The API credits every score - a rating without its source is a rumour - but it
has no way to ask a plugin anything: it depends on ``eifo-core`` and nothing
else. So how a provider is named, which of its figures belong together and what
its mark looks like used to be a dictionary in the API, hand-kept, a package
away from the enricher that actually produced the score.

This moves it the same way sources moved (``register_declared_sources``): the
fetcher is the only process that knows what plugins exist, so it writes what
they declare into the database, which is the one thing the two share.

Marks are files rather than rows, so they are copied into the images root and
served like any other artwork. The stored name carries a hash of the contents,
because that directory is served ``immutable`` - a provider that redraws its
logo must arrive at a new URL, or every browser that has seen the old one will
go on showing it for a year.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from eifo_core.models import RatingProviderInfo
from eifo_core.settings import Settings
from eifo_fetcher.enrichers.base import ProviderInfo

logger = logging.getLogger("eifo.fetch.providers")

#: Where marks live under the images root.
LOGO_DIR = "providers"

#: How much of the content hash goes in the name. Eight hex characters is four
#: billion; these are a handful of files that change once in a blue moon.
HASH_LENGTH = 8


def refresh_declared_providers(
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> list[str]:
    """Bring the table in line with the plugins installed right now.

    Called wherever the fetcher opens a database, not from one phase, because
    the thing that needs this table is a title page being rendered - and a page
    is rendered between runs, not during one. Hanging it off the enrich alone
    meant a deployment that upgraded on Tuesday credited its scores by database
    key until Wednesday's nightly had finished: hours of the feature looking
    broken, fixed by a job nobody would think to connect it to.

    Cheap enough to belong here. Seven rows compared, four small files that are
    already where they should be, and nothing written on a run that finds the
    database already saying what the plugins say - which is every run but the
    first after an upgrade.
    """
    # Imported here rather than at module scope: this is the one function that
    # needs the plugins, and importing them costs every enricher's imports.
    from eifo_fetcher.enrichers import discover_enrichers
    from eifo_fetcher.enrichers.imdb import ImdbDatasetLoader

    with session_factory() as session:
        # A database migrated only as far as 0023 has nowhere to put this, and
        # a fetcher that refused to list its sources because of a logo would be
        # a poor trade. `db upgrade` is the fix and says so on its own.
        if not inspect(session.get_bind()).has_table(RatingProviderInfo.__tablename__):
            logger.debug("no rating_providers table yet; run `eifo-fetch db upgrade`")
            return []

        infos = declared_providers([*discover_enrichers(settings), ImdbDatasetLoader])
        changed = register_declared_providers(session, infos, images_dir=Path(settings.images_dir))
        session.commit()
    return changed


def declared_providers(sources: Iterable[object]) -> list[ProviderInfo]:
    """Every ``ProviderInfo`` the given plugins declare, in the order found.

    Takes anything with a ``provider_info`` attribute rather than an
    ``Enricher``: the IMDb pass is a bulk join over a dataset rather than an
    enricher, and it is still the thing that produces the IMDb score and so
    still the thing that should say how that score is credited.

    A provider declared twice keeps its first declaration, which is the one
    from the plugin that ships with Eifo - a third-party plugin may add a
    provider but should not be able to silently rename an existing one.
    """
    seen: set[str] = set()
    found: list[ProviderInfo] = []
    for source in sources:
        for info in getattr(source, "provider_info", ()):
            if info.provider.value in seen:
                logger.debug("provider %s already declared; keeping the first", info.provider)
                continue
            seen.add(info.provider.value)
            found.append(info)
    return found


def register_declared_providers(
    session: Session,
    infos: Iterable[ProviderInfo],
    *,
    images_dir: Path,
) -> list[str]:
    """Store what these plugins declare, and publish their marks.

    Returns the providers whose row changed, which is normally none: this runs
    on every enrich and almost always finds the database already saying what
    the plugins say.

    Nothing is ever deleted. A provider switched off for one run - ``--skip
    rt`` - still has thousands of scores in the catalog that need crediting,
    and a row removed because a plugin was quiet tonight would take the name
    off every one of them.
    """
    stored = {row.provider.value: row for row in session.scalars(select(RatingProviderInfo)).all()}
    changed: list[str] = []

    for info in infos:
        logo_path = _publish_logo(info, images_dir)
        row = stored.get(info.provider.value)
        if row is None:
            session.add(
                RatingProviderInfo(
                    provider=info.provider,
                    label=info.label,
                    group_key=info.group_key,
                    group_name=info.group_name,
                    logo_path=logo_path,
                    website_url=info.website_url,
                    position=info.position,
                )
            )
            changed.append(info.provider.value)
            continue

        fields = {
            "label": info.label,
            "group_key": info.group_key,
            "group_name": info.group_name,
            "logo_path": logo_path,
            "website_url": info.website_url,
            "position": info.position,
        }
        # Only when something actually differs: an unconditional write would
        # touch updated_at on seven rows every night for nothing.
        if any(getattr(row, name) != value for name, value in fields.items()):
            for name, value in fields.items():
                setattr(row, name, value)
            changed.append(info.provider.value)

    if changed:
        logger.info("provider details updated: %s", ", ".join(sorted(changed)))
    return changed


def _publish_logo(info: ProviderInfo, images_dir: Path) -> str | None:
    """Copy a plugin's mark into the images root, and say where it landed.

    Returns None when the plugin ships no mark, or when the file it named is
    not there - a missing logo is a chip that says the provider's name, which
    is what every chip said before marks existed. It is not a reason to fail an
    enrich that is about to write ten thousand ratings.
    """
    if info.icon is None:
        return None
    if not info.icon.is_file():
        logger.warning("%s declares a logo at %s, which is not there", info.provider, info.icon)
        return None

    digest = hashlib.sha256(info.icon.read_bytes()).hexdigest()[:HASH_LENGTH]
    name = f"{info.group_key}-{digest}{info.icon.suffix}"
    destination = images_dir / LOGO_DIR / name

    if not destination.exists():
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(info.icon, destination)
        except OSError as exc:
            # An images root that cannot be written to is a real problem, but
            # it is not this function's to raise: the caller is on its way to
            # sync a catalog, and a logo is not worth stopping that for.
            logger.warning("could not publish the %s mark: %s", info.group_key, exc)
            return None
        _sweep_older(destination)
        logger.info("published the %s mark as %s", info.group_key, name)

    return f"{LOGO_DIR}/{name}"


def _sweep_older(current: Path) -> None:
    """Remove earlier versions of this group's mark.

    Every one of them is a URL nothing points at any more, and they would
    otherwise accumulate one per redraw forever. Best effort: a file that
    cannot be removed is a few kilobytes, not a reason to stop.
    """
    prefix = current.name.split("-")[0] + "-"
    for sibling in current.parent.glob(f"{prefix}*"):
        if sibling == current:
            continue
        try:
            sibling.unlink()
        except OSError as exc:  # pragma: no cover - a permissions problem
            logger.debug("could not remove the superseded mark %s: %s", sibling, exc)

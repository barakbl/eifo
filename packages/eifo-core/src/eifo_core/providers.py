"""What a score credits itself as, written down.

A provider that produces scores is the thing that knows what it is called,
which of its figures belong together and what its mark looks like. The API only
knows what it has been told - so the fetcher tells it, on every enrich, and the
client renders whatever the table says rather than being taught a provider.

Here rather than in the fetcher because it writes: rows for the providers, and
the mark itself into the images root. The fetcher ships the bytes, because the
file belongs to the plugin and the plugin lives over there; everything about
where it lands is decided here, beside the artwork that lands the same way.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from eifo_core.enums import RatingProvider
from eifo_core.models import RatingProviderInfo

logger = logging.getLogger("eifo.providers")

#: Where marks are published, under the images root.
LOGO_DIR = "providers"

#: Characters of the content digest in a published mark's filename.
#:
#: Content-addressed for the same reason posters are: `/images` is served
#: `immutable`, so a mark rewritten at a fixed path is one every browser
#: already holding it will never see again.
HASH_LENGTH = 8


@dataclass(frozen=True, slots=True)
class DeclaredProvider:
    """One figure a provider reports, and the mark that credits it.

    The mark arrives as bytes rather than as a path: the file ships with the
    plugin, and the plugin is not necessarily on this machine any more.
    """

    provider: RatingProvider
    label: str
    group_key: str
    group_name: str
    website_url: str | None = None
    position: int = 0
    logo: bytes | None = None
    logo_suffix: str = ""


def register_declared_providers(
    session: Session,
    infos: Iterable[DeclaredProvider],
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


def _publish_logo(info: DeclaredProvider, images_dir: Path) -> str | None:
    """Copy a plugin's mark into the images root, and say where it landed.

    Returns None when no mark was sent - a chip then says the provider's name,
    which is what every chip said before marks existed. Not a reason to fail an
    enrich that is about to write ten thousand ratings.
    """
    if not info.logo:
        return None

    digest = hashlib.sha256(info.logo).hexdigest()[:HASH_LENGTH]
    name = f"{info.group_key}-{digest}{info.logo_suffix}"
    destination = images_dir / LOGO_DIR / name

    if not destination.exists():
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(info.logo)
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

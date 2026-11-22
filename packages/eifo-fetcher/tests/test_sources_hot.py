"""HOT declares itself and says plainly that there is nothing to read."""

from __future__ import annotations

import pytest

from eifo_fetcher.sources.base import FetchContext
from eifo_fetcher.sources.hot import HotCatalogError, HotPlugin


def test_declared_off_and_partial() -> None:
    (info,) = HotPlugin().sources()

    assert info.key == "hot"
    assert info.partial is True
    assert info.default_enabled is False


def test_fetch_says_there_is_no_public_catalog(http: object, settings: object) -> None:
    ctx = FetchContext(source_key="hot", http=http, settings=settings)  # type: ignore[arg-type]

    with pytest.raises(HotCatalogError, match="no public catalog"):
        list(HotPlugin().fetch(ctx))

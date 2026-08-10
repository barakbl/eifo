"""Test isolation from the developer's own configuration.

Settings read ``config/eifo.toml`` by default, and a checkout that has followed
the quick start has a real one. Tests must not depend on whether that file
exists or what a particular developer put in it, so every test runs against a
config path that deliberately does not exist. Tests that want configuration
set ``EIFO_CONFIG_FILE`` themselves, which still wins over this.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from eifo_core.settings import get_settings


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("EIFO_CONFIG_FILE", str(tmp_path / "no-such-config.toml"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

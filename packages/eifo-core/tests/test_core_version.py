"""The version this package reports is the version it was released as.

It was a hardcoded string, and it drifted the moment the project had a second
release: release-please bumps `pyproject.toml` through a marker and there was
none in `__init__.py`, so every package still said 0.1.0 at 0.11.0 - which is
what `/api/v1/meta` told clients, what the OpenAPI document declared, and what
`eifo-fetch --version` printed.

Deriving it from distribution metadata makes that particular drift impossible.
This is the guard for the other direction: somebody writing the string back.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

import eifo_core

#: This package's own pyproject - present in a source checkout, which is where
#: the suite runs, and absent from a real install, where the module lives in
#: site-packages and there is no project file above it.
PYPROJECT = Path(eifo_core.__file__).resolve().parents[2] / "pyproject.toml"


def test_the_reported_version_is_the_installed_one() -> None:
    """The check that holds however the package was installed."""
    assert eifo_core.__version__ == metadata.version("eifo-core")


@pytest.mark.skipif(not PYPROJECT.is_file(), reason="installed, not a source checkout")
def test_the_reported_version_is_the_declared_one() -> None:
    """And that the install itself is not stale against the project file."""
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    assert eifo_core.__version__ == declared


def test_it_is_not_the_placeholder_it_used_to_be_stuck_on() -> None:
    """Named, because "0.1.0 forever" is the exact shape of the old bug."""
    assert eifo_core.__version__ != "0.0.0+unknown", "this package is not installed"

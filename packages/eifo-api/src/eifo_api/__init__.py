"""Eifo REST API.

Read-only over catalog data, read/write over user data. Also serves the static
web client and the downloaded artwork, so one process is a complete deployment.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

#: The version this package was installed as, read from its own distribution
#: metadata rather than written here.
#:
#: It used to be a string in this file, and it drifted immediately: release-please
#: bumps `pyproject.toml` through an `x-release-please-version` marker and there
#: was no marker here, so every package still said 0.1.0 at 0.11.0. That is not a
#: cosmetic difference - it is what `/api/v1/meta` reports to clients, what the
#: OpenAPI document declares, and what `eifo-fetch --version` prints.
#:
#: Derived rather than marked, so there is one place to bump and it is the one
#: release tooling already owns. The fallback is for a source tree nobody has
#: installed, where there is no metadata to read and no version to be right
#: about.
try:
    __version__ = _version("eifo-api")
except PackageNotFoundError:  # pragma: no cover - a checkout that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]

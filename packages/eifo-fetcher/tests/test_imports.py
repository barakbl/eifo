"""Every module in the project imports on the platform running this.

An `import fcntl` at the top of `eifo_fetcher.lock` made every `eifo-fetch`
command unrunnable on Windows - not the locking, the import: the program could
not start. Nothing caught it, because a test suite imports the module it is
about and never asks whether the rest of the tree still loads.

So this walks all three packages and imports every module in them. It is the
cheapest test here and the one with the widest reach: a platform-only import,
a dependency that is installed but not declared, a circular import between two
packages - each of them fails a program at startup, and each of them shows up
as one name in this list rather than as a bug report from somebody's laptop.

It lives in the fetcher's suite because the fetcher is the program that has to
start: the API is imported by a server that reports its own failures, and
`eifo-fetch` is what cron runs at three in the morning.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

PACKAGES = ("eifo_core", "eifo_api", "eifo_fetcher")


def _module_names() -> list[str]:
    """Every importable module in the three packages, submodules included."""
    names: list[str] = []
    for package_name in PACKAGES:
        package = importlib.import_module(package_name)
        names.append(package_name)
        names.extend(
            info.name for info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}.")
        )
    return names


class TestEveryModuleImports:
    @pytest.mark.parametrize("module_name", _module_names())
    def test_it_imports(self, module_name: str) -> None:
        """Named one by one, so a failure says which module and not just that one did."""
        importlib.import_module(module_name)

    def test_the_walk_found_the_packages_it_was_told_about(self) -> None:
        """A typo in a package name would otherwise make this test pass by finding nothing."""
        found = _module_names()

        assert len(found) > 40
        for package_name in PACKAGES:
            assert any(name.startswith(f"{package_name}.") for name in found)

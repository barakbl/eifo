"""Access to recorded fixtures.

Every fixture under ``tests/fixtures/`` is a trimmed copy of a real response, so
parsers are exercised against the shapes sites actually serve and the suite
never touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(*parts: str) -> str:
    """Read a recorded fixture as text."""
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


def load_json_fixture(*parts: str) -> Any:
    """Read a recorded fixture as parsed JSON."""
    return json.loads(load_fixture(*parts))

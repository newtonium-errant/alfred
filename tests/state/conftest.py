"""Shared fixtures for state round-trip tests.

Every tool that persists state follows the same atomic-write contract:
``save()`` writes to ``<path>.tmp`` then ``os.replace``'s onto the target,
and ``load()`` reconstitutes the in-memory object from JSON. These tests
exercise that contract end-to-end: build a populated state object, save,
reload from disk, and assert no data was lost in transit.

The ``state_path`` fixture just hands out a throwaway JSON path under
``tmp_path`` — every tool builds its own manager around that path.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """A throwaway JSON path for a single tool's state file."""
    return tmp_path / "state.json"

"""Shared pytest fixtures for the Alfred test suite.

These fixtures are intentionally minimal — they exist to give tests a
working vault layout and a config dict that mirrors ``config.yaml.example``
without touching the real vault or the user's checked-in config.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml


# Top-level entity directories the vault ops layer expects to find. We don't
# need every type — just enough that ``vault_create`` / ``vault_search`` /
# ``vault_list`` have somewhere to put a record without blowing up on a
# missing parent.
_VAULT_DIRS = ("person", "task", "project", "note", "inbox")


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Return a temp directory laid out as a minimal Alfred vault.

    Includes:
      - empty subdirs for a handful of common record types
      - one sample person record so search/list queries have something to hit
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in _VAULT_DIRS:
        (vault / sub).mkdir()

    sample_person = dedent(
        """\
        ---
        type: person
        name: Sample Person
        created: 2026-04-18
        tags: []
        related: []
        ---

        # Sample Person

        Fixture record used by the vault_ops smoke tests.
        """
    )
    (vault / "person" / "Sample Person.md").write_text(sample_person, encoding="utf-8")
    return vault


@pytest.fixture
def ephemeral_config(tmp_vault: Path) -> dict:
    """Load ``config.yaml.example`` and repoint ``vault.path`` at ``tmp_vault``.

    Returns the parsed dict — tests can mutate it freely; nothing is written
    back to disk.
    """
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "config.yaml.example"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    raw.setdefault("vault", {})["path"] = str(tmp_vault)
    return raw

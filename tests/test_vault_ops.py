"""Smoke tests for the vault ops layer.

Bootstrap-scope: prove ``vault_create``/``vault_read``/``vault_search`` work
end-to-end against a temp vault. Per-field validation, near-match dedup,
and template handling are out of scope here — those get their own tests as
the behaviour is touched.
"""

from __future__ import annotations

from pathlib import Path

from alfred.vault.ops import vault_create, vault_read, vault_search


def test_vault_create_then_read_round_trip(tmp_vault: Path):
    result = vault_create(
        tmp_vault,
        "task",
        "Bootstrap Smoke Task",
        set_fields={"status": "todo"},
    )
    assert result["path"] == "task/Bootstrap Smoke Task.md"

    read_back = vault_read(tmp_vault, result["path"])
    fm = read_back["frontmatter"]
    assert fm["type"] == "task"
    assert fm["name"] == "Bootstrap Smoke Task"
    assert fm["status"] == "todo"
    # ``created`` is auto-populated to today's ISO date.
    assert isinstance(fm.get("created"), str) or fm.get("created") is not None


def test_vault_search_finds_known_glob(tmp_vault: Path):
    # The conftest fixture seeds person/Sample Person.md — a glob over
    # person/*.md must surface it.
    hits = vault_search(tmp_vault, glob_pattern="person/*.md")
    paths = {h["path"] for h in hits}
    assert "person/Sample Person.md" in paths

    # And the parsed metadata should round-trip the type/name from frontmatter.
    sample = next(h for h in hits if h["path"] == "person/Sample Person.md")
    assert sample["type"] == "person"
    assert sample["name"] == "Sample Person"


def test_vault_create_note_with_living_status(tmp_vault: Path):
    # Hypatia QA 2026-04-28: status='living' on a note record (e.g. a
    # permanent task list) was rejected by validation, forcing
    # status='active' which is semantically wrong for reference
    # material that never finishes. ``living`` is now a valid status
    # for ``note`` records — this test guards against the regression.
    result = vault_create(
        tmp_vault,
        "note",
        "VAC Form Unit Economics Model",
        set_fields={"status": "living"},
    )
    assert result["path"] == "note/VAC Form Unit Economics Model.md"

    read_back = vault_read(tmp_vault, result["path"])
    fm = read_back["frontmatter"]
    assert fm["type"] == "note"
    assert fm["status"] == "living"

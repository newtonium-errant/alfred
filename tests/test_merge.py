"""Tests for ``alfred.janitor.merge.merge_entities`` — Option E Q2.

Seeds a temp vault with winner/loser entity records plus a handful of
downstream records that link to the loser via various wikilink-bearing
frontmatter fields and body prose. Runs the merge and asserts:

- every downstream record now links to the winner
- loser record is deleted
- winner has absorbed unique loser fields
- the mutation log records every edit + delete

Does NOT exercise the LLM-pick-winner half of the DUP001 flow — that's
a sweep-path concern. This module covers the deterministic retargeting
half only.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.janitor.merge import MergeError, merge_entities
from alfred.vault.ops import vault_read


def _write(vault: Path, rel: str, content: str) -> None:
    """Write a markdown record to ``vault/rel``, creating parents."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


@pytest.fixture
def merge_vault(tmp_path: Path) -> Path:
    """Vault seeded with the DUP001 merge fixture.

    Layout:
      org/Pocketpills.md          (winner: lowercase-p, minimal body)
      org/PocketPills.md          (loser:  camelcase-p, has extra fields)
      person/Alice.md             links to loser via ``org:`` FM
      project/Rx Refill.md        links to loser via ``client:`` FM
      note/Order Prep.md          links to loser in body prose + related list

    After merge_entities(winner=..., loser=...):
      - org/Pocketpills.md exists with the winner's name
      - org/PocketPills.md deleted
      - All three downstream records now reference org/Pocketpills
    """
    vault = tmp_path / "vault"
    for sub in ("org", "person", "project", "note", "inbox"):
        (vault / sub).mkdir(parents=True, exist_ok=True)

    _write(vault, "org/Pocketpills.md", """\
        ---
        type: org
        name: Pocketpills
        status: active
        created: 2026-03-01
        org_type: vendor
        website: https://pocketpills.com
        related: []
        tags: []
        ---

        # Pocketpills

        Winner record.
        """)

    _write(vault, "org/PocketPills.md", """\
        ---
        type: org
        name: PocketPills
        status: active
        created: 2026-02-01
        org_type: vendor
        phone: 1-800-555-0199
        related:
          - "[[person/Alice]]"
        tags: []
        ---

        # PocketPills

        Loser record body with historical context that should be preserved.
        """)

    _write(vault, "person/Alice.md", """\
        ---
        type: person
        name: Alice
        created: 2026-01-15
        org: "[[org/PocketPills]]"
        related: []
        tags: []
        ---

        # Alice

        Works at PocketPills.
        """)

    _write(vault, "project/Rx Refill.md", """\
        ---
        type: project
        name: Rx Refill
        status: active
        created: 2026-02-10
        client: "[[org/PocketPills]]"
        related:
          - "[[org/PocketPills]]"
        tags: []
        ---

        # Rx Refill

        Project for pill refill automation.
        """)

    _write(vault, "note/Order Prep.md", """\
        ---
        type: note
        name: Order Prep
        created: 2026-03-20
        related:
          - "[[org/PocketPills]]"
        tags: []
        ---

        # Order Prep

        Called [[org/PocketPills]] about the April refill.
        """)

    return vault


def test_merge_retargets_frontmatter_single_value_links(merge_vault: Path):
    # ``person/Alice.md::org`` points at the loser; after merge it must
    # point at the winner with the winner's exact casing.
    merge_entities(merge_vault, "org/Pocketpills", "org/PocketPills")

    alice = vault_read(merge_vault, "person/Alice.md")
    assert alice["frontmatter"]["org"] == "[[org/Pocketpills]]", (
        f"Expected org retargeted to winner casing, got {alice['frontmatter']['org']!r}"
    )


def test_merge_retargets_frontmatter_list_links(merge_vault: Path):
    # ``project/Rx Refill.md::related`` is a list with one loser link.
    # ``project/Rx Refill.md::client`` is a scalar. Both must retarget.
    merge_entities(merge_vault, "org/Pocketpills", "org/PocketPills")

    rx = vault_read(merge_vault, "project/Rx Refill.md")
    fm = rx["frontmatter"]
    assert fm["client"] == "[[org/Pocketpills]]"
    assert fm["related"] == ["[[org/Pocketpills]]"]


def test_merge_retargets_body_wikilinks(merge_vault: Path):
    # ``note/Order Prep.md`` has the loser link in BOTH frontmatter
    # ``related`` AND the body prose. Both sites must retarget.
    merge_entities(merge_vault, "org/Pocketpills", "org/PocketPills")

    note = vault_read(merge_vault, "note/Order Prep.md")
    assert note["frontmatter"]["related"] == ["[[org/Pocketpills]]"]
    assert "[[org/Pocketpills]]" in note["body"]
    # And no residual loser link anywhere in the body.
    assert "[[org/PocketPills]]" not in note["body"]


def test_merge_deletes_loser_record(merge_vault: Path):
    result = merge_entities(merge_vault, "org/Pocketpills", "org/PocketPills")
    assert result.loser_deleted is True
    assert not (merge_vault / "org/PocketPills.md").exists()
    # Winner survives.
    assert (merge_vault / "org/Pocketpills.md").exists()


def test_merge_absorbs_unique_winner_fields(merge_vault: Path):
    # Winner lacks ``phone``; loser has one. After merge the winner
    # should carry the loser's phone. Winner's own ``website`` must
    # NOT be overwritten by the loser (loser doesn't set one).
    result = merge_entities(merge_vault, "org/Pocketpills", "org/PocketPills")
    assert "phone" in result.fields_merged

    winner = vault_read(merge_vault, "org/Pocketpills.md")
    assert winner["frontmatter"]["phone"] == "1-800-555-0199"
    assert winner["frontmatter"]["website"] == "https://pocketpills.com"


def test_merge_preserves_winner_identity_fields(merge_vault: Path):
    # Type, name, and created date of the winner must survive the merge
    # even though the loser sets all three.
    merge_entities(merge_vault, "org/Pocketpills", "org/PocketPills")

    winner = vault_read(merge_vault, "org/Pocketpills.md")
    fm = winner["frontmatter"]
    assert fm["type"] == "org"
    assert fm["name"] == "Pocketpills"
    # Winner was created 2026-03-01; loser was earlier. Winner wins.
    assert str(fm["created"]) == "2026-03-01"


def test_merge_appends_loser_body_with_provenance_marker(merge_vault: Path):
    result = merge_entities(merge_vault, "org/Pocketpills", "org/PocketPills")
    assert result.body_appended is True

    winner = vault_read(merge_vault, "org/Pocketpills.md")
    assert "<!-- merged from org/PocketPills.md -->" in winner["body"]
    assert "historical context that should be preserved" in winner["body"]


def test_merge_logs_mutations_to_session_file(merge_vault: Path, tmp_path: Path):
    # The audit trail hook: every edit + the final delete must show up
    # in the mutation log.
    session_path = tmp_path / "session.jsonl"
    result = merge_entities(
        merge_vault, "org/Pocketpills", "org/PocketPills",
        session_path=str(session_path),
    )

    lines = [
        json.loads(ln) for ln in session_path.read_text().splitlines() if ln
    ]
    ops_by_path = {(e["op"], e["path"]) for e in lines}

    # Winner got an absorb edit.
    assert ("edit", "org/Pocketpills.md") in ops_by_path
    # Loser was deleted.
    assert ("delete", "org/PocketPills.md") in ops_by_path
    # Every retargeted downstream file got an edit entry.
    for rel in result.retargeted_files:
        assert ("edit", rel) in ops_by_path

    # Retargeted count matches the seeded downstream records.
    assert set(result.retargeted_files) == {
        "person/Alice.md",
        "project/Rx Refill.md",
        "note/Order Prep.md",
    }


def test_merge_refuses_same_winner_and_loser(merge_vault: Path):
    with pytest.raises(MergeError, match="same record"):
        merge_entities(merge_vault, "org/Pocketpills", "org/Pocketpills")


def test_merge_refuses_missing_loser(merge_vault: Path):
    with pytest.raises(MergeError, match="Loser record not found"):
        merge_entities(merge_vault, "org/Pocketpills", "org/NonExistent")


def test_merge_refuses_missing_winner(merge_vault: Path):
    with pytest.raises(MergeError, match="Winner record not found"):
        merge_entities(merge_vault, "org/NonExistent", "org/PocketPills")


def test_merge_accepts_bracketed_and_suffixed_forms(merge_vault: Path):
    # Accepts [[org/PocketPills]], org/PocketPills.md, and plain
    # org/PocketPills interchangeably.
    result = merge_entities(
        merge_vault,
        "[[org/Pocketpills]]",
        "org/PocketPills.md",
    )
    assert result.loser_deleted is True

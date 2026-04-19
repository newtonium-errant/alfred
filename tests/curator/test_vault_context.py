"""Tests for :class:`alfred.curator.context.VaultContext.to_prompt_text`.

Upstream ba1f7d0 (ssdavidai/alfred#14) replaced the old wikilink-heavy
prompt block with a slim name index. The curator's Stage 1 prompt is
the heaviest token consumer in the pipeline, and the slim rendering is
load-bearing for staying inside context limits with larger vaults.

Contract:

- Grouped by type, one ``### <type> (N)`` header per group.
- Entries are plain entity names, comma-separated, wrapping at ~120 chars.
- No full wikilinks (``[[type/Name]]``) and no per-entity status lines.
- Empty context still renders without error.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alfred.curator.context import (
    RecordSummary,
    VaultContext,
    build_vault_context,
)


# ---------------------------------------------------------------------------
# to_prompt_text direct shape tests
# ---------------------------------------------------------------------------


def _ctx_with(records: dict[str, list[str]]) -> VaultContext:
    """Helper: build a VaultContext with the given names per type."""
    ctx = VaultContext()
    for rec_type, names in records.items():
        ctx.records_by_type[rec_type] = [
            RecordSummary(path=f"{rec_type}/{n}", name=n, status="active")
            for n in names
        ]
    return ctx


def test_to_prompt_text_emits_type_header_with_count() -> None:
    """Each type group starts with ``### <type> (<N>)``."""
    ctx = _ctx_with({"person": ["Alice", "Bob", "Carol"]})
    out = ctx.to_prompt_text()
    assert "### person (3)" in out


def test_to_prompt_text_comma_separates_names() -> None:
    """Names land comma-separated on the line(s) after the header."""
    ctx = _ctx_with({"org": ["Acme", "Globex"]})
    out = ctx.to_prompt_text()
    assert "Acme, Globex" in out


def test_to_prompt_text_never_emits_full_wikilinks() -> None:
    """Regression guard: no ``[[type/Name]]`` in the slim rendering.

    Before the slim refactor, the context prompt wrapped every entity
    in a wikilink, which ballooned token counts. If this assertion fails
    we've silently reverted the token-reduction work.
    """
    ctx = _ctx_with({
        "person": ["Alice Anderson", "Bob Builder"],
        "project": ["Alfred"],
    })
    out = ctx.to_prompt_text()
    assert "[[" not in out
    assert "]]" not in out


def test_to_prompt_text_never_emits_status_lines() -> None:
    """Per-entity status/fields are omitted from the slim rendering."""
    ctx = _ctx_with({"task": ["Write tests", "Ship feature"]})
    # Every summary in _ctx_with has status="active" — the slim rendering
    # must not leak that through.
    out = ctx.to_prompt_text()
    assert "active" not in out
    assert "status" not in out.lower()


def test_to_prompt_text_empty_context_renders_empty_string() -> None:
    """Brand-new vault → no records → no headers. Must NOT crash."""
    ctx = VaultContext()
    assert ctx.to_prompt_text() == ""


def test_to_prompt_text_wraps_long_entity_lists() -> None:
    """When the comma-joined name list would exceed ~120 chars, it wraps.

    Using stable, long names so the line-count check survives minor
    tweaks to the wrap threshold.
    """
    long_names = [f"EntityName{i:03d}LongEnoughToWrap" for i in range(12)]
    ctx = _ctx_with({"person": long_names})
    out = ctx.to_prompt_text()
    # The names block (everything after the header) should span >1 line
    lines_after_header = [
        ln for ln in out.splitlines()
        if ln and not ln.startswith("###") and ln.strip()
    ]
    assert len(lines_after_header) > 1


def test_to_prompt_text_groups_are_sorted_by_type() -> None:
    """Groups render in sorted type order — prompt stability across runs.

    A non-deterministic order would fuzz the agent's prompt cache hit
    rate on OpenClaw, so we pin sorted-by-type-name.
    """
    ctx = _ctx_with({
        "task": ["t1"],
        "org": ["o1"],
        "person": ["p1"],
    })
    out = ctx.to_prompt_text()
    # Find indices of the type headers and verify they're in sorted order
    headers = ["### org", "### person", "### task"]
    indices = [out.find(h) for h in headers]
    assert all(i >= 0 for i in indices)
    assert indices == sorted(indices)


def test_to_prompt_text_names_sorted_within_group() -> None:
    """Names within a group render in sorted order."""
    ctx = _ctx_with({"person": ["Zane", "Alice", "Mike"]})
    out = ctx.to_prompt_text()
    # Extract the line after the header
    lines = out.splitlines()
    header_idx = next(i for i, ln in enumerate(lines) if ln.startswith("### person"))
    name_line = lines[header_idx + 1]
    # Alice → Mike → Zane
    assert name_line.index("Alice") < name_line.index("Mike")
    assert name_line.index("Mike") < name_line.index("Zane")


# ---------------------------------------------------------------------------
# build_vault_context integration
# ---------------------------------------------------------------------------


def _write_record(vault: Path, rel_path: str, record_type: str, name: str) -> None:
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        dedent(
            f"""\
            ---
            type: {record_type}
            name: {name}
            status: active
            created: 2026-04-01
            tags: []
            related: []
            ---

            # {name}
            """
        ),
        encoding="utf-8",
    )


def test_build_vault_context_groups_by_type(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_record(vault, "person/Alice.md", "person", "Alice")
    _write_record(vault, "org/Acme.md", "org", "Acme")
    _write_record(vault, "project/Alfred.md", "project", "Alfred")

    ctx = build_vault_context(vault)

    assert set(ctx.records_by_type.keys()) == {"person", "org", "project"}
    assert ctx.total_records == 3


def test_build_vault_context_skips_inbox(tmp_path: Path) -> None:
    """Inbox files must NEVER land in the context — that's the whole
    point of the curator, we'd be feeding its own unprocessed inputs
    back into its prompt as "existing records"."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_record(vault, "person/Alice.md", "person", "Alice")
    _write_record(vault, "inbox/stray.md", "input", "stray")

    ctx = build_vault_context(vault)

    assert "person" in ctx.records_by_type
    # The inbox entry must not surface
    assert "input" not in ctx.records_by_type


def test_build_vault_context_honours_ignore_dirs(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_record(vault, "person/Alice.md", "person", "Alice")
    _write_record(vault, "archive/person/OldPerson.md", "person", "OldPerson")

    ctx = build_vault_context(vault, ignore_dirs=["archive"])

    names = [r.name for r in ctx.records_by_type.get("person", [])]
    assert "Alice" in names
    assert "OldPerson" not in names


def test_build_vault_context_skips_records_without_type(tmp_path: Path) -> None:
    """A markdown file missing the ``type`` frontmatter doesn't crash —
    it's silently dropped. Silent-drop is the right move here because
    the janitor owns missing-type issue reporting; the curator shouldn't
    re-detect and re-log."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "person").mkdir()
    (vault / "person" / "typeless.md").write_text(
        "---\nname: Typeless\n---\n\nNo type in frontmatter.\n",
        encoding="utf-8",
    )
    _write_record(vault, "person/Alice.md", "person", "Alice")

    ctx = build_vault_context(vault)

    names = [r.name for r in ctx.records_by_type.get("person", [])]
    assert "Alice" in names
    assert "Typeless" not in names

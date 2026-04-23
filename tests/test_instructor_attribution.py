"""c4: instructor executor's vault_create + vault_edit body paths
stamp BEGIN_INFERRED markers + attribution_audit entries when the
agent composes prose.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from alfred.instructor.executor import _dispatch_tool


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("note", "task", "project", "person"):
        (vault / sub).mkdir()
    return vault


def _read(vault: Path, rel: str) -> tuple[dict, str]:
    post = frontmatter.load(str(vault / rel))
    return dict(post.metadata), post.content


def test_instructor_create_with_body_wraps_in_inferred_markers(vault: Path):
    mutated: list[str] = []
    out = _dispatch_tool(
        "vault_create",
        {
            "type": "note",
            "name": "DirectiveOutput",
            "set_fields": {"tags": ["instructor"]},
            "body": "Synthesised content from a directive.\n",
        },
        vault,
        dry_run=False,
        session_path="note/SourceRecord.md",
        mutated_paths=mutated,
    )
    json.loads(out)  # well-formed
    assert mutated == ["note/DirectiveOutput.md"]

    fm, content = _read(vault, "note/DirectiveOutput.md")
    assert "BEGIN_INFERRED" in content
    assert "Synthesised content" in content
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    entry = audit[0]
    assert entry["agent"] == "instructor"
    assert entry["section_title"] == "DirectiveOutput"
    assert "instructor directive" in entry["reason"]
    assert "note/SourceRecord.md" in entry["reason"]


def test_instructor_create_without_body_skips_wrapping(vault: Path):
    mutated: list[str] = []
    _dispatch_tool(
        "vault_create",
        {
            "type": "note",
            "name": "TemplateDefault",
        },
        vault,
        dry_run=False,
        session_path=None,
        mutated_paths=mutated,
    )
    fm, content = _read(vault, "note/TemplateDefault.md")
    assert "BEGIN_INFERRED" not in content
    assert "attribution_audit" not in fm


def test_instructor_edit_body_append_wraps_only_appended(vault: Path):
    full = vault / "note" / "Existing.md"
    full.write_text(
        "---\ntype: note\nname: Existing\ncreated: '2026-04-23'\n---\n\n"
        "# Existing\n\nUser-typed content.\n",
        encoding="utf-8",
    )
    mutated: list[str] = []
    _dispatch_tool(
        "vault_edit",
        {
            "path": "note/Existing.md",
            "body_append": "## New Section\n\nDirective-inferred bullet.\n",
        },
        vault,
        dry_run=False,
        session_path="note/Source.md",
        mutated_paths=mutated,
    )
    fm, content = _read(vault, "note/Existing.md")
    assert "User-typed content." in content
    assert "BEGIN_INFERRED" in content
    # User-typed paragraph sits BEFORE the marker.
    assert content.find("User-typed content.") < content.find("BEGIN_INFERRED")
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "instructor"
    assert audit[0]["section_title"] == "New Section"


def test_instructor_edit_preserves_existing_audit(vault: Path):
    full = vault / "note" / "Seeded.md"
    full.write_text(
        "---\n"
        "type: note\n"
        "name: Seeded\n"
        "created: '2026-04-23'\n"
        "attribution_audit:\n"
        "  - marker_id: inf-20260420-salem-aaaaaa\n"
        "    agent: salem\n"
        "    date: '2026-04-20T00:00:00+00:00'\n"
        "    section_title: Old Section\n"
        "    reason: prior write\n"
        "    confirmed_by_andrew: false\n"
        "    confirmed_at: null\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    mutated: list[str] = []
    _dispatch_tool(
        "vault_edit",
        {
            "path": "note/Seeded.md",
            "body_append": "## Latest\n\nbullet.\n",
        },
        vault,
        dry_run=False,
        session_path=None,
        mutated_paths=mutated,
    )
    fm, _ = _read(vault, "note/Seeded.md")
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 2
    ids = {e["marker_id"] for e in audit}
    assert "inf-20260420-salem-aaaaaa" in ids

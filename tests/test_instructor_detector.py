"""Tests for ``alfred.instructor.daemon.detect_pending``.

The detector is a pure function — no SDK calls, no filesystem writes.
Exercises every branch the commit 3 plan calls out:

 (a) no instruction fields present
 (b) empty list
 (c) populated list → one PendingInstruction per directive
 (d) hash-unchanged skip
 (e) hash-changed re-detect
 (f) malformed YAML frontmatter tolerated
 (g) scalar (non-list) directive promoted to single-entry list
 (h) ignore_dirs filters paths
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alfred.instructor.daemon import detect_pending, PendingInstruction
from alfred.instructor.state import InstructorState


def _write_record(vault: Path, rel_path: str, content: str) -> Path:
    """Write a record under ``vault`` at ``rel_path`` (creating dirs).

    Returns the absolute path to the written file for further edits.
    """
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _vault(tmp_path: Path) -> Path:
    """Return a fresh vault root under ``tmp_path``."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _state(tmp_path: Path) -> InstructorState:
    return InstructorState(tmp_path / "state.json")


def test_no_instruction_fields_yields_empty_queue(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_record(
        vault,
        "task/Normal.md",
        dedent(
            """\
            ---
            type: task
            name: Normal
            created: '2026-04-20'
            ---

            Body text without instructions.
            """
        ),
    )
    pending = detect_pending(vault, _state(tmp_path))
    assert pending == []


def test_empty_list_yields_empty_queue(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_record(
        vault,
        "task/Empty.md",
        dedent(
            """\
            ---
            type: task
            name: Empty
            created: '2026-04-20'
            alfred_instructions: []
            ---

            Nothing pending.
            """
        ),
    )
    pending = detect_pending(vault, _state(tmp_path))
    assert pending == []


def test_populated_list_yields_one_pending_per_directive(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_record(
        vault,
        "note/Act.md",
        dedent(
            """\
            ---
            type: note
            name: Act
            created: '2026-04-20'
            alfred_instructions:
              - "rename this to foo"
              - "add a backlink to project/Alfred"
            ---

            Body.
            """
        ),
    )
    pending = detect_pending(vault, _state(tmp_path))
    assert len(pending) == 2
    paths = {p.rel_path for p in pending}
    assert paths == {"note/Act.md"}
    directives = [p.directive for p in pending]
    assert "rename this to foo" in directives
    assert "add a backlink to project/Alfred" in directives
    # Hash is populated and identical for both entries (same file).
    assert pending[0].record_hash == pending[1].record_hash
    assert len(pending[0].record_hash) == 64  # sha256 hex


def test_hash_unchanged_skips_file(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_record(
        vault,
        "note/Act.md",
        dedent(
            """\
            ---
            type: note
            name: Act
            created: '2026-04-20'
            alfred_instructions:
              - "do the thing"
            ---

            Body.
            """
        ),
    )
    state = _state(tmp_path)

    # First pass picks it up.
    pending_first = detect_pending(vault, state)
    assert len(pending_first) == 1

    # Manually record the hash (in real use the executor would edit
    # the file and the hash would advance naturally; here we simulate
    # the executor having "already run" by sealing the hash into state
    # without mutating the file).
    for p in pending_first:
        state.record_hash(p.rel_path, p.record_hash)

    # Second pass sees unchanged hash → empty queue.
    pending_second = detect_pending(vault, state)
    assert pending_second == []


def test_hash_changed_re_detects(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    path = _write_record(
        vault,
        "note/Act.md",
        dedent(
            """\
            ---
            type: note
            name: Act
            created: '2026-04-20'
            alfred_instructions:
              - "directive one"
            ---

            Body.
            """
        ),
    )
    state = _state(tmp_path)

    pending = detect_pending(vault, state)
    assert len(pending) == 1
    # Seal the hash so the next poll would skip if unchanged.
    state.record_hash(pending[0].rel_path, pending[0].record_hash)

    # Operator edits the directive — rewrite file with new content.
    path.write_text(
        dedent(
            """\
            ---
            type: note
            name: Act
            created: '2026-04-20'
            alfred_instructions:
              - "directive one"
              - "directive two"
            ---

            Body.
            """
        ),
        encoding="utf-8",
    )

    pending2 = detect_pending(vault, state)
    # Hash changed → re-enqueue everything on the list.
    assert len(pending2) == 2
    directives = [p.directive for p in pending2]
    assert "directive one" in directives
    assert "directive two" in directives


def test_malformed_frontmatter_is_skipped_not_raised(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_record(
        vault,
        "note/Broken.md",
        "---\n: this is not valid yaml: [[\n---\n\nbody",
    )
    # Also write one good file so detect_pending has something to return.
    _write_record(
        vault,
        "note/Good.md",
        dedent(
            """\
            ---
            type: note
            name: Good
            created: '2026-04-20'
            alfred_instructions:
              - "run this"
            ---
            """
        ),
    )
    # Should not raise — malformed file is just skipped.
    pending = detect_pending(vault, _state(tmp_path))
    assert {p.rel_path for p in pending} == {"note/Good.md"}


def test_scalar_directive_promoted_to_single_entry_list(tmp_path: Path) -> None:
    """YAML ``alfred_instructions: "do this"`` is legal shorthand.

    python-frontmatter parses that as a string, not a list. The detector
    promotes it so downstream code always sees a list. Regression guard
    for the c1-era LIST_FIELDS coercion not firing on parse-only reads.
    """
    vault = _vault(tmp_path)
    _write_record(
        vault,
        "note/Scalar.md",
        dedent(
            """\
            ---
            type: note
            name: Scalar
            created: '2026-04-20'
            alfred_instructions: "do this one thing"
            ---

            Body.
            """
        ),
    )
    pending = detect_pending(vault, _state(tmp_path))
    assert len(pending) == 1
    assert pending[0].directive == "do this one thing"


def test_ignore_dirs_filters_paths(tmp_path: Path) -> None:
    """``ignore_dirs`` suppresses records under matched paths."""
    vault = _vault(tmp_path)
    _write_record(
        vault,
        "_templates/project.md",
        dedent(
            """\
            ---
            type: project
            name: Template
            created: '2026-04-20'
            alfred_instructions:
              - "should not fire on templates"
            ---
            """
        ),
    )
    _write_record(
        vault,
        "note/Real.md",
        dedent(
            """\
            ---
            type: note
            name: Real
            created: '2026-04-20'
            alfred_instructions:
              - "should fire"
            ---
            """
        ),
    )
    pending = detect_pending(
        vault, _state(tmp_path), ignore_dirs=["_templates"]
    )
    assert {p.rel_path for p in pending} == {"note/Real.md"}

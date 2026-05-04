"""Tests for vault_edit's new body_insert_at + body_replace kwargs (c2).

c2 ships the tool surface; the scope matrix (c1) gates per-instance ×
per-type. These tests cover:

  * body_insert_at — line-exact marker matching, before/after positions,
    marker-not-found / ambiguous-marker error paths
  * body_replace — full body rewrite, frontmatter preserved
  * Mutual exclusion — at most one body-mutation kwarg per call
  * Scope integration — Salem event with gcal_event_id refuses
    body_replace; Hypatia note allowed; janitor body_replace denied
  * Existing body_append + body_rewriter paths unchanged (regression)
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred.vault.ops import (
    VaultError,
    _apply_body_insert_at,
    vault_create,
    vault_edit,
)
from alfred.vault.scope import ScopeError


# ---------------------------------------------------------------------------
# _apply_body_insert_at — pure-function unit tests
# ---------------------------------------------------------------------------


class TestApplyBodyInsertAt:
    def test_inserts_after_marker(self):
        body = "# Title\n\n## Section A\nA content.\n\n## Section B\nB content.\n"
        result = _apply_body_insert_at(
            body,
            marker="## Section A",
            position="after",
            content="Inserted paragraph.",
        )
        # New paragraph between Section A's existing content and Section B.
        assert "## Section A" in result
        assert "Inserted paragraph." in result
        assert result.index("## Section A") < result.index(
            "Inserted paragraph."
        )
        assert result.index("Inserted paragraph.") < result.index(
            "## Section B"
        )

    def test_inserts_before_marker(self):
        body = "# Title\n\n## Section A\nA content.\n\n## Section B\nB content.\n"
        result = _apply_body_insert_at(
            body,
            marker="## Section B",
            position="before",
            content="Pre-B paragraph.",
        )
        assert result.index("Pre-B paragraph.") < result.index(
            "## Section B"
        )
        assert result.index("A content.") < result.index("Pre-B paragraph.")

    def test_marker_not_found_raises(self):
        body = "# Title\n\n## Section A\nA content.\n"
        with pytest.raises(VaultError, match="marker not found"):
            _apply_body_insert_at(
                body,
                marker="## Phantom Section",
                position="after",
                content="Content.",
            )

    def test_ambiguous_marker_raises(self):
        """Same marker on multiple lines → refuse with operator-actionable
        message."""
        body = "## Section\nFirst.\n\n## Section\nSecond.\n"
        with pytest.raises(VaultError, match="matches 2 lines"):
            _apply_body_insert_at(
                body,
                marker="## Section",
                position="after",
                content="x",
            )

    def test_invalid_position_raises(self):
        with pytest.raises(VaultError, match="must be 'before' or 'after'"):
            _apply_body_insert_at(
                body="## Marker\n",
                marker="## Marker",
                position="middle",  # invalid
                content="x",
            )

    def test_line_exact_match_substring_does_not_match(self):
        """Marker must match the WHOLE line; substring match doesn't
        fire. ``## Section`` does NOT match ``## Section A``."""
        body = "## Section A\nA content.\n\n## Section B\nB content.\n"
        with pytest.raises(VaultError, match="marker not found"):
            _apply_body_insert_at(
                body,
                marker="## Section",  # substring of "## Section A"
                position="after",
                content="x",
            )


# ---------------------------------------------------------------------------
# vault_edit body_insert_at — integration with scope
# ---------------------------------------------------------------------------


class TestVaultEditBodyInsertAt:
    def test_hypatia_note_inserts_after_marker(self, tmp_vault: Path):
        vault_create(
            tmp_vault, "note", "DJ Skill Tracker",
            body=(
                "# DJ Skill Tracker\n\n"
                "## Tier 1 — Foundations\n\n"
                "## Hardware-specific drills\n"
            ),
            scope="hypatia",
        )
        vault_edit(
            tmp_vault, "note/DJ Skill Tracker.md",
            body_insert_at={
                "marker": "## Hardware-specific drills",
                "position": "before",
                "content": (
                    "## Tier 4e — MPC One workflow\n\n"
                    "Beat-making drills for MPC One."
                ),
            },
            scope="hypatia",
        )
        post = frontmatter.load(
            str(tmp_vault / "note/DJ Skill Tracker.md")
        )
        assert "Tier 4e — MPC One workflow" in post.content
        # Inserted before the Hardware-specific drills marker.
        assert post.content.index("Tier 4e") < post.content.index(
            "## Hardware-specific drills"
        )

    def test_hypatia_note_marker_not_found_clean_error(
        self, tmp_vault: Path,
    ):
        vault_create(
            tmp_vault, "note", "Marker Test",
            body="# Title\n\n## Real Section\n",
            scope="hypatia",
        )
        with pytest.raises(VaultError, match="marker not found"):
            vault_edit(
                tmp_vault, "note/Marker Test.md",
                body_insert_at={
                    "marker": "## Phantom",
                    "position": "after",
                    "content": "x",
                },
                scope="hypatia",
            )
        # Body untouched on error.
        post = frontmatter.load(str(tmp_vault / "note/Marker Test.md"))
        assert "x" not in post.content
        assert "## Phantom" not in post.content

    def test_hypatia_body_insert_at_denied_for_outside_spec_type(
        self, tmp_vault: Path,
    ):
        """Hypatia's allowlist excludes ``person`` — even though
        person isn't in the universal-deny set, the allowlist gate
        refuses."""
        # Hypatia can't create person (canonical) — seed via no-scope
        # path so we have a target to edit.
        vault_create(
            tmp_vault, "person", "Test Person",
            body="# Test Person\n\n## Bio\n",
        )
        with pytest.raises(ScopeError, match="may not 'body_insert_at'"):
            vault_edit(
                tmp_vault, "person/Test Person.md",
                body_insert_at={
                    "marker": "## Bio",
                    "position": "after",
                    "content": "x",
                },
                scope="hypatia",
            )


# ---------------------------------------------------------------------------
# vault_edit body_replace — integration with scope + gcal carve-out
# ---------------------------------------------------------------------------


class TestVaultEditBodyReplace:
    def test_talker_note_body_replace_rewrites_body(
        self, tmp_vault: Path,
    ):
        vault_create(
            tmp_vault, "note", "Replaceable Note",
            body="# Old\n\nOld content.\n",
            scope="talker",
        )
        vault_edit(
            tmp_vault, "note/Replaceable Note.md",
            body_replace="# New\n\nNew content.\n",
            scope="talker",
        )
        post = frontmatter.load(
            str(tmp_vault / "note/Replaceable Note.md")
        )
        assert "Old content." not in post.content
        assert "New content." in post.content
        # Frontmatter preserved.
        assert post.metadata["type"] == "note"
        assert post.metadata["name"] == "Replaceable Note"

    def test_talker_event_body_replace_refuses_with_gcal_event_id(
        self, tmp_vault: Path,
    ):
        """The headline carve-out: Salem event with synced GCal mirror
        refuses body_replace at the scope layer."""
        vault_create(
            tmp_vault, "event", "Halifax Music Fest",
            set_fields={
                "start": "2026-06-27T19:00:00-03:00",
                "end": "2026-06-27T22:00:00-03:00",
                "gcal_event_id": "alfred-cal-event-abc123",
                "gcal_calendar": "alfred",
            },
            body="# Halifax Music Fest\n\nOld details.\n",
            scope="talker",
        )
        with pytest.raises(ScopeError, match="synced GCal mirror"):
            vault_edit(
                tmp_vault, "event/Halifax Music Fest.md",
                body_replace="# Halifax Music Fest\n\nNew details.\n",
                scope="talker",
            )
        # Body untouched.
        post = frontmatter.load(
            str(tmp_vault / "event/Halifax Music Fest.md")
        )
        assert "Old details." in post.content
        assert "New details." not in post.content
        # gcal_event_id preserved (no silent mutation).
        assert post.metadata["gcal_event_id"] == "alfred-cal-event-abc123"

    def test_talker_event_body_replace_allowed_without_gcal_event_id(
        self, tmp_vault: Path,
    ):
        """Local-only event (no GCal mirror) IS eligible for body_replace
        per the matrix."""
        vault_create(
            tmp_vault, "event", "Local-only event",
            set_fields={
                "start": "2026-06-27T19:00:00-03:00",
                "end": "2026-06-27T22:00:00-03:00",
            },
            body="# Local-only event\n\nDraft.\n",
            scope="talker",
        )
        vault_edit(
            tmp_vault, "event/Local-only event.md",
            body_replace="# Local-only event\n\nFinalised.\n",
            scope="talker",
        )
        post = frontmatter.load(
            str(tmp_vault / "event/Local-only event.md")
        )
        assert "Finalised." in post.content
        assert "Draft." not in post.content

    def test_janitor_body_replace_denied_for_all_types(
        self, tmp_vault: Path,
    ):
        """Janitor's allow_body_replace is empty — autofix-loop risk
        per spec."""
        vault_create(
            tmp_vault, "note", "Janitor Replace Test",
            body="# Old\n",
        )
        with pytest.raises(ScopeError, match="no allowlist"):
            vault_edit(
                tmp_vault, "note/Janitor Replace Test.md",
                body_replace="# New\n",
                scope="janitor",
            )


# ---------------------------------------------------------------------------
# Mutual exclusion
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    def test_body_replace_and_body_insert_at_together_refused(
        self, tmp_vault: Path,
    ):
        vault_create(
            tmp_vault, "note", "Mutex Test",
            body="# Title\n\n## Section\n",
            scope="hypatia",
        )
        with pytest.raises(VaultError, match="at most ONE body-mutation"):
            vault_edit(
                tmp_vault, "note/Mutex Test.md",
                body_replace="# New\n",
                body_insert_at={
                    "marker": "## Section",
                    "position": "after",
                    "content": "x",
                },
                scope="hypatia",
            )

    def test_body_append_and_body_insert_at_together_refused(
        self, tmp_vault: Path,
    ):
        vault_create(
            tmp_vault, "note", "Mutex Append Insert",
            body="## Section\n",
            scope="hypatia",
        )
        with pytest.raises(VaultError, match="at most ONE body-mutation"):
            vault_edit(
                tmp_vault, "note/Mutex Append Insert.md",
                body_append="appended",
                body_insert_at={
                    "marker": "## Section",
                    "position": "after",
                    "content": "x",
                },
                scope="hypatia",
            )

    def test_body_replace_and_body_append_together_refused(
        self, tmp_vault: Path,
    ):
        vault_create(
            tmp_vault, "note", "Mutex Replace Append",
            body="# Old\n",
            scope="hypatia",
        )
        with pytest.raises(VaultError, match="at most ONE body-mutation"):
            vault_edit(
                tmp_vault, "note/Mutex Replace Append.md",
                body_replace="# New\n",
                body_append="appended",
                scope="hypatia",
            )


# ---------------------------------------------------------------------------
# Regression: existing body_append + body_rewriter paths unchanged
# ---------------------------------------------------------------------------


class TestExistingPathsUnchanged:
    def test_body_append_only_works_unchanged(self, tmp_vault: Path):
        vault_create(
            tmp_vault, "note", "Append Regression",
            body="# Title\n\nOriginal.\n",
        )
        vault_edit(
            tmp_vault, "note/Append Regression.md",
            body_append="Appended paragraph.",
        )
        post = frontmatter.load(
            str(tmp_vault / "note/Append Regression.md")
        )
        assert "Original." in post.content
        assert "Appended paragraph." in post.content

    def test_body_rewriter_only_works_unchanged(self, tmp_vault: Path):
        vault_create(
            tmp_vault, "note", "Rewriter Regression",
            body="# Title\n\nOriginal.\n",
        )
        vault_edit(
            tmp_vault, "note/Rewriter Regression.md",
            body_rewriter=lambda b: b.replace("Original.", "Rewritten."),
        )
        post = frontmatter.load(
            str(tmp_vault / "note/Rewriter Regression.md")
        )
        assert "Original." not in post.content
        assert "Rewritten." in post.content

    def test_set_fields_only_works_unchanged(self, tmp_vault: Path):
        vault_create(
            tmp_vault, "note", "Frontmatter Only",
            body="# Title\n\nBody.\n",
        )
        vault_edit(
            tmp_vault, "note/Frontmatter Only.md",
            set_fields={"status": "active"},
        )
        post = frontmatter.load(
            str(tmp_vault / "note/Frontmatter Only.md")
        )
        assert post.metadata["status"] == "active"
        # Body unchanged.
        assert "Body." in post.content


# ---------------------------------------------------------------------------
# fields_changed reporting
# ---------------------------------------------------------------------------


class TestFieldsChangedReporting:
    def test_body_insert_at_reports_body_in_fields_changed(
        self, tmp_vault: Path,
    ):
        vault_create(
            tmp_vault, "note", "Insert Reports",
            body="## Section\n",
            scope="hypatia",
        )
        result = vault_edit(
            tmp_vault, "note/Insert Reports.md",
            body_insert_at={
                "marker": "## Section",
                "position": "after",
                "content": "Inserted.",
            },
            scope="hypatia",
        )
        assert "body" in result["fields_changed"]

    def test_body_replace_reports_body_in_fields_changed(
        self, tmp_vault: Path,
    ):
        vault_create(
            tmp_vault, "note", "Replace Reports",
            body="# Old\n",
            scope="talker",
        )
        result = vault_edit(
            tmp_vault, "note/Replace Reports.md",
            body_replace="# New\n",
            scope="talker",
        )
        assert "body" in result["fields_changed"]

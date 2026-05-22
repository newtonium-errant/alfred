"""Tests for vault_edit's no-op detection gate (Hypatia 2026-05-21 fix).

The Hypatia essay-planning conversation
``2026-05-21-depression-checklist-essay-planning-e166d40d.md`` surfaced a
silent no-op: ``vault_edit`` called with ONLY ``path`` (no mutation
kwarg) returned ``{"path": ..., "fields_changed": []}`` with no error.
Operator-visible signature: Salem narrated "the edit landed"; the file
body didn't actually change. Root cause: the model's tool_use input
was max_tokens-truncated mid-emission — ``body_append`` was supposed to
follow ``path`` in the JSON, but emission ran out of budget after
``path``.

Per ``feedback_intentionally_left_blank.md`` — silence is ambiguous;
the no-op path now fail-loud-raises a VaultError with an actionable
message that names every accepted mutation kwarg and hints at the
truncation root cause.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.vault.ops import VaultError, vault_create, vault_edit


class TestVaultEditNoOpDetection:
    def test_only_path_supplied_raises_clear_error(self, tmp_vault: Path):
        """The exact Hypatia 2026-05-21 failure shape: only ``path``
        in the kwargs, no mutation surface. Previously silent no-op;
        now fail-loud with actionable error."""
        vault_create(
            tmp_vault, "note", "NoOp Repro",
            body="# Title\n\nOriginal.\n",
        )
        with pytest.raises(VaultError) as exc_info:
            vault_edit(tmp_vault, "note/NoOp Repro.md")
        msg = str(exc_info.value)
        # Error must name what's missing (so the model knows what to
        # supply on retry).
        assert "no mutation parameter" in msg
        assert "set_fields" in msg
        assert "body_append" in msg
        assert "body_replace" in msg
        assert "body_insert_at" in msg
        # Error must hint at the truncation root cause (operator/model
        # diagnostic — Layer 2 in the talker dispatcher catches it
        # explicitly, but the bare runtime gate also names it).
        assert "max_tokens" in msg
        # File body must NOT have changed.
        post_text = (tmp_vault / "note/NoOp Repro.md").read_text(
            encoding="utf-8",
        )
        assert "Original." in post_text

    def test_set_fields_only_succeeds(self, tmp_vault: Path):
        """Single-mutation-kwarg cases continue to work."""
        vault_create(
            tmp_vault, "note", "SetFields Only",
            body="# Title\n\nBody.\n",
        )
        result = vault_edit(
            tmp_vault, "note/SetFields Only.md",
            set_fields={"status": "active"},
        )
        assert "status" in result["fields_changed"]

    def test_append_fields_only_succeeds(self, tmp_vault: Path):
        """append_fields counts as a mutation surface; no-op gate
        does NOT fire."""
        vault_create(
            tmp_vault, "note", "AppendFields Only",
            body="# Title\n\nBody.\n",
            set_fields={"tags": ["existing"]},
        )
        result = vault_edit(
            tmp_vault, "note/AppendFields Only.md",
            append_fields={"tags": "added"},
        )
        assert "tags" in result["fields_changed"]

    def test_body_append_only_succeeds(self, tmp_vault: Path):
        """The most common path — body_append on its own — must
        remain a valid no-frontmatter-mutation edit."""
        vault_create(
            tmp_vault, "note", "BodyAppend Only",
            body="# Title\n\nOriginal.\n",
        )
        result = vault_edit(
            tmp_vault, "note/BodyAppend Only.md",
            body_append="Appended paragraph.",
        )
        assert "body" in result["fields_changed"]
        post_text = (tmp_vault / "note/BodyAppend Only.md").read_text(
            encoding="utf-8",
        )
        assert "Appended paragraph." in post_text

    def test_body_replace_only_succeeds(self, tmp_vault: Path):
        """body_replace alone is a mutation surface."""
        vault_create(
            tmp_vault, "note", "BodyReplace Only",
            body="# Old.\n",
            scope="talker",
        )
        result = vault_edit(
            tmp_vault, "note/BodyReplace Only.md",
            body_replace="# New.\n",
            scope="talker",
        )
        assert "body" in result["fields_changed"]

    def test_body_insert_at_only_succeeds(self, tmp_vault: Path):
        """body_insert_at alone is a mutation surface."""
        vault_create(
            tmp_vault, "note", "BodyInsertAt Only",
            body="## Section\n",
            scope="hypatia",
        )
        result = vault_edit(
            tmp_vault, "note/BodyInsertAt Only.md",
            body_insert_at={
                "marker": "## Section",
                "position": "after",
                "content": "Inserted.",
            },
            scope="hypatia",
        )
        assert "body" in result["fields_changed"]

    def test_body_rewriter_only_succeeds(self, tmp_vault: Path):
        """body_rewriter alone is a mutation surface (calibration
        writer path)."""
        vault_create(
            tmp_vault, "note", "Rewriter Only",
            body="# Title\n\nOriginal.\n",
        )
        result = vault_edit(
            tmp_vault, "note/Rewriter Only.md",
            body_rewriter=lambda b: b.replace("Original.", "Rewritten."),
        )
        # body_rewriter producing a diff lands "body" in fields_changed;
        # the no-op gate does not fire either way.
        post_text = (tmp_vault / "note/Rewriter Only.md").read_text(
            encoding="utf-8",
        )
        assert "Rewritten." in post_text
        # The result is well-formed (no exception).
        assert "path" in result

    def test_set_fields_empty_dict_treated_as_no_mutation(
        self, tmp_vault: Path,
    ):
        """``set_fields={}`` (empty dict) is falsy → the gate must
        treat it as "no mutation" rather than allowing a no-op
        through. Same shape as ``set_fields=None`` from the caller's
        perspective."""
        vault_create(
            tmp_vault, "note", "Empty SetFields",
            body="# Title\n",
        )
        with pytest.raises(VaultError, match="no mutation parameter"):
            vault_edit(
                tmp_vault, "note/Empty SetFields.md",
                set_fields={},
            )

    def test_append_fields_empty_dict_treated_as_no_mutation(
        self, tmp_vault: Path,
    ):
        """Same as above for ``append_fields={}``."""
        vault_create(
            tmp_vault, "note", "Empty AppendFields",
            body="# Title\n",
        )
        with pytest.raises(VaultError, match="no mutation parameter"):
            vault_edit(
                tmp_vault, "note/Empty AppendFields.md",
                append_fields={},
            )

    def test_set_fields_and_body_append_both_allowed(self, tmp_vault: Path):
        """Combination of frontmatter mutation + single body-mutation
        kwarg is the standard "land everything in one edit" pattern —
        must NOT false-positive on the no-op gate."""
        vault_create(
            tmp_vault, "note", "Combined Edit",
            body="# Title\n\nOriginal.\n",
        )
        result = vault_edit(
            tmp_vault, "note/Combined Edit.md",
            set_fields={"status": "active"},
            body_append="Appended.",
        )
        assert "status" in result["fields_changed"]
        assert "body" in result["fields_changed"]

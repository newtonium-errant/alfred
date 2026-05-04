"""Tests for the ``set_fields["body"]`` filter (P1 from QA 2026-05-04).

Bug class: agents (Hypatia in the DJ-tracker conversation) called
``vault_edit(set_fields={"body": "..."})`` instead of
``body_append=...``. The literal ``body`` key landed in YAML
frontmatter; ``vault_read`` returned frontmatter containing a stale
``body`` field that didn't match the on-disk markdown body;
downstream consumers (distiller scoring, surveyor entity_links) saw
confused ground-truth.

Fix: ``_filter_reserved_keys`` strips ``body`` from ``set_fields``
at the vault-ops gate (vault_create + vault_edit) and emits a
structured warning. Defense in depth via JSON-schema ``not.required``
in the tool definitions, but the runtime filter is load-bearing.

Coverage:
- vault_create strips body from set_fields + emits warning
- vault_edit strips body from set_fields + emits warning
- normal set_fields (no body key) passes through unchanged
- the on-disk file's actual body is unchanged after the filter fires
  (caller's body= arg still controls the document body)
- JSON-schema tool definitions carry the not.required.body constraint
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import structlog

from alfred.vault.ops import vault_create, vault_edit, vault_read


# ---------------------------------------------------------------------------
# vault_create — body-key filter
# ---------------------------------------------------------------------------


def test_vault_create_strips_body_from_set_fields(tmp_vault: Path):
    """vault_create called with set_fields={"body": "..."} must NOT
    write a ``body`` key to YAML frontmatter. The on-disk file's
    body is the body= arg; if neither is provided, template default."""
    with structlog.testing.capture_logs() as captured:
        result = vault_create(
            tmp_vault,
            "task",
            "Body Filter Test",
            set_fields={
                "status": "todo",
                "body": "this should NOT land in frontmatter",
            },
            body="actual on-disk body content",
        )

    # File created.
    assert result["path"] == "task/Body Filter Test.md"
    # Read the raw file via frontmatter to confirm no body key in YAML.
    file_path = tmp_vault / result["path"]
    post = frontmatter.load(str(file_path))
    assert "body" not in post.metadata, (
        f"body key leaked into YAML frontmatter: {dict(post.metadata)}"
    )
    # The legitimate set_fields entry survived.
    assert post.metadata["status"] == "todo"
    # On-disk body is the body= arg, not the leaked frontmatter value.
    assert "actual on-disk body content" in post.content
    assert "this should NOT land in frontmatter" not in post.content
    # Structured warning emitted.
    filter_logs = [
        c for c in captured
        if c.get("event") == "vault.ops.body_in_set_fields_filtered"
    ]
    assert len(filter_logs) == 1
    entry = filter_logs[0]
    assert entry["log_level"] == "warning"
    assert entry["op"] == "vault_create"
    assert entry["leaked_keys"] == ["body"]
    assert entry["rel_path"] == "task/Body Filter Test.md"


def test_vault_create_normal_set_fields_pass_through_unchanged(
    tmp_vault: Path,
):
    """Regression: set_fields with no ``body`` key must not trigger
    the filter warning. Normal flow is unaffected."""
    with structlog.testing.capture_logs() as captured:
        vault_create(
            tmp_vault,
            "task",
            "Normal Set Fields Test",
            set_fields={"status": "todo", "due": "2026-05-10"},
            body="Normal body content.",
        )
    filter_logs = [
        c for c in captured
        if c.get("event") == "vault.ops.body_in_set_fields_filtered"
    ]
    assert filter_logs == [], (
        "Filter warning fired on a clean set_fields — false positive."
    )


# ---------------------------------------------------------------------------
# vault_edit — body-key filter
# ---------------------------------------------------------------------------


def test_vault_edit_strips_body_from_set_fields(tmp_vault: Path):
    """vault_edit called with set_fields={"body": "..."} must NOT
    write a ``body`` key. The on-disk body is unchanged unless
    body_append / body_rewriter is also passed."""
    # Seed a record first.
    vault_create(
        tmp_vault,
        "task",
        "Edit Filter Test",
        set_fields={"status": "todo"},
        body="Original body content.",
    )

    with structlog.testing.capture_logs() as captured:
        vault_edit(
            tmp_vault,
            "task/Edit Filter Test.md",
            set_fields={
                "status": "in_progress",
                "body": "should never land in frontmatter",
            },
        )

    file_path = tmp_vault / "task/Edit Filter Test.md"
    post = frontmatter.load(str(file_path))
    assert "body" not in post.metadata, (
        f"body key leaked into YAML frontmatter on edit: "
        f"{dict(post.metadata)}"
    )
    # Legitimate edit applied.
    assert post.metadata["status"] == "in_progress"
    # On-disk body is unchanged (no body_append / body_rewriter passed).
    assert "Original body content." in post.content
    assert "should never land in frontmatter" not in post.content
    # Structured warning emitted.
    filter_logs = [
        c for c in captured
        if c.get("event") == "vault.ops.body_in_set_fields_filtered"
    ]
    assert len(filter_logs) == 1
    assert filter_logs[0]["op"] == "vault_edit"
    assert filter_logs[0]["leaked_keys"] == ["body"]
    assert filter_logs[0]["rel_path"] == "task/Edit Filter Test.md"


def test_vault_edit_normal_set_fields_pass_through_unchanged(
    tmp_vault: Path,
):
    vault_create(
        tmp_vault,
        "task",
        "Edit Normal Test",
        set_fields={"status": "todo"},
    )
    with structlog.testing.capture_logs() as captured:
        vault_edit(
            tmp_vault,
            "task/Edit Normal Test.md",
            set_fields={"status": "in_progress", "due": "2026-05-12"},
        )
    filter_logs = [
        c for c in captured
        if c.get("event") == "vault.ops.body_in_set_fields_filtered"
    ]
    assert filter_logs == []


# ---------------------------------------------------------------------------
# Edit + body_append works alongside the filter (the right pattern)
# ---------------------------------------------------------------------------


def test_vault_edit_body_append_works_when_body_in_set_fields_stripped(
    tmp_vault: Path,
):
    """The agent's RIGHT call shape — body_append for body content,
    set_fields for frontmatter only — works correctly even when the
    filter strips an erroneous body key from set_fields. Confirms the
    filter doesn't break the legitimate body-append path that's
    happening in the same call."""
    vault_create(
        tmp_vault,
        "task",
        "Mixed Edit Test",
        set_fields={"status": "todo"},
        body="Original body.",
    )
    vault_edit(
        tmp_vault,
        "task/Mixed Edit Test.md",
        set_fields={
            "status": "in_progress",
            # Erroneous body key — gets stripped.
            "body": "should be stripped",
        },
        # Legitimate body_append — should land in the body.
        body_append="Additional content.",
    )
    file_path = tmp_vault / "task/Mixed Edit Test.md"
    post = frontmatter.load(str(file_path))
    assert "body" not in post.metadata
    assert post.metadata["status"] == "in_progress"
    # body_append landed.
    assert "Additional content." in post.content
    # Original body preserved.
    assert "Original body." in post.content
    # Erroneous body-from-set_fields did NOT land.
    assert "should be stripped" not in post.content


# ---------------------------------------------------------------------------
# Schema-level constraint pin (defense in depth)
# ---------------------------------------------------------------------------


class TestSchemaConstraints:
    """The JSON-schema in the LLM-facing tool definitions carries a
    ``not.required.body`` constraint to hint to the model that
    ``body`` doesn't belong in ``set_fields``. The runtime filter is
    load-bearing; this layer is a hint, not a hard gate (the
    Anthropic SDK doesn't strictly validate input_schema client-
    side). Pin the constraint so it can't drift away silently."""

    def test_talker_vault_edit_set_fields_rejects_body_in_required(
        self,
    ):
        from alfred.telegram.conversation import TALKER_VAULT_TOOLS
        edit = next(t for t in TALKER_VAULT_TOOLS if t["name"] == "vault_edit")
        set_fields_schema = edit["input_schema"]["properties"]["set_fields"]
        assert "not" in set_fields_schema, (
            "vault_edit's set_fields schema must carry a "
            "``not.required.[body]`` constraint hinting to the LLM "
            "that body doesn't go in set_fields."
        )
        assert set_fields_schema["not"] == {"required": ["body"]}

    def test_talker_vault_create_set_fields_rejects_body_in_required(
        self,
    ):
        from alfred.telegram.conversation import TALKER_VAULT_TOOLS
        create = next(
            t for t in TALKER_VAULT_TOOLS if t["name"] == "vault_create"
        )
        set_fields_schema = create["input_schema"]["properties"]["set_fields"]
        assert set_fields_schema.get("not") == {"required": ["body"]}

    def test_kalle_vault_create_set_fields_rejects_body_in_required(
        self,
    ):
        """KAL-LE's vault_create tool (for ~/aftermath-lab/) carries
        the same constraint."""
        from alfred.telegram.conversation import _KALLE_VAULT_CREATE_TOOL
        set_fields_schema = (
            _KALLE_VAULT_CREATE_TOOL["input_schema"]
            ["properties"]["set_fields"]
        )
        assert set_fields_schema.get("not") == {"required": ["body"]}

    def test_instructor_vault_edit_set_fields_rejects_body_in_required(
        self,
    ):
        from alfred.instructor.executor import VAULT_TOOLS as INSTRUCTOR_VAULT_TOOLS
        edit = next(
            t for t in INSTRUCTOR_VAULT_TOOLS if t["name"] == "vault_edit"
        )
        set_fields_schema = edit["input_schema"]["properties"]["set_fields"]
        assert set_fields_schema.get("not") == {"required": ["body"]}

    def test_instructor_vault_create_set_fields_rejects_body_in_required(
        self,
    ):
        from alfred.instructor.executor import VAULT_TOOLS as INSTRUCTOR_VAULT_TOOLS
        create = next(
            t for t in INSTRUCTOR_VAULT_TOOLS if t["name"] == "vault_create"
        )
        set_fields_schema = create["input_schema"]["properties"]["set_fields"]
        assert set_fields_schema.get("not") == {"required": ["body"]}

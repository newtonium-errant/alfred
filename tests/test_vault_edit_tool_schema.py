"""Schema-pin tests for the vault_edit tool surface (c3 of body-mutation arc).

c3 exposes body_insert_at + body_replace through the LLM-facing JSON
schemas in conversation.py + instructor/executor.py.

Mutual-exclusion enforcement (HISTORICAL note — readers following
breadcrumbs from commit ``0d7e7a6``):

  c3 originally shipped a top-level ``oneOf`` constraint enumerating
  the four valid body-kwarg shapes (zero / one of three) as defense
  in depth above the runtime gate in ``vault.ops.vault_edit``. The
  ``oneOf`` was removed 2026-05-06 because Anthropic's Messages API
  request validator rejects ``oneOf`` / ``allOf`` / ``anyOf`` at the
  top level of any tool's ``input_schema`` with HTTP 400 before the
  model runs:

    tools.N.custom.input_schema: input_schema does not support
    oneOf, allOf, or anyOf at the top level

  Surfaced when Salem (+ KAL-LE + Hypatia) restarted into the cherry-
  picked code; every conversation carrying the tool list 400'd at
  the request validator. Production fix is removal — the runtime
  gate in ``vault_edit`` (raises ``VaultError("at most ONE
  body-mutation kwarg per call")``) is THE load-bearing protection.
  Mutual-exclusion regression coverage lives in
  ``tests/test_vault_edit_body_mutation.py`` (3 tests at lines
  ~410/430/450 covering every pairwise combination).

  The schema's PROPERTY-LEVEL descriptions on body_append /
  body_insert_at / body_replace each say "Mutually exclusive
  with ..." in plain English so the LLM still gets the constraint
  as guidance — just not as a structurally enforceable schema.

Coverage retained here (post-2026-05-06):
  * talker vault_edit schema declares body_insert_at + body_replace
  * instructor vault_edit schema declares body_insert_at + body_replace
  * body_insert_at carries the {marker, position, content} required
    fields; position enum is ['before', 'after']
  * Each valid call shape (zero body kwargs, exactly one of three)
    validates cleanly — these stay as positive-coverage tests
  * set_fields still carries the not.required.body constraint from
    P1 (c2 in body-filter arc) — different mechanism
    (``not`` at the property level, NOT top-level), still allowed
    by Anthropic
  * Anthropic-API guard: assert no top-level oneOf / allOf / anyOf
    in the schemas (regression test for the 2026-05-06 fix)
"""

from __future__ import annotations

import pytest

# NOTE on jsonschema dep handling:
#
# ``jsonschema`` is an OPTIONAL test dep. Pre-2026-05-06 this module
# called ``jsonschema = pytest.importorskip("jsonschema")`` at module
# scope, which collection-skipped the ENTIRE file when the dep was
# missing — including ``TestNoTopLevelSchemaCombinators``, the
# regression pin specifically built to catch the production-breaking
# bug fixed in ``708eddd``.
#
# Bug shape: a future environment regression (CI without the dep,
# fresh venv, dep removal) would silently disable the regression pin.
# A reintroduction of top-level ``oneOf`` could land green there.
#
# Fix: ``importorskip`` MOVED to function scope inside each test
# method that actually calls ``jsonschema.validate``. The shape and
# regression-pin tests now run unconditionally; only the positive
# validation tests skip when the dep is missing. The runtime gate in
# ``vault.ops.vault_edit`` is the load-bearing mutual-exclusion
# protection — ``jsonschema``-based positive coverage is just
# additional smoke testing.


def _talker_vault_edit_schema():
    from alfred.telegram.conversation import TALKER_VAULT_TOOLS
    edit = next(t for t in TALKER_VAULT_TOOLS if t["name"] == "vault_edit")
    return edit["input_schema"]


def _instructor_vault_edit_schema():
    from alfred.instructor.executor import VAULT_TOOLS
    edit = next(t for t in VAULT_TOOLS if t["name"] == "vault_edit")
    return edit["input_schema"]


# ---------------------------------------------------------------------------
# Talker — schema declares the new properties
# ---------------------------------------------------------------------------


class TestTalkerSchemaShape:
    def test_body_insert_at_property_exists(self):
        schema = _talker_vault_edit_schema()
        props = schema["properties"]
        assert "body_insert_at" in props
        bia = props["body_insert_at"]
        assert bia["type"] == "object"
        assert set(bia["properties"].keys()) == {"marker", "position", "content"}
        assert bia["properties"]["position"]["enum"] == ["before", "after"]
        assert set(bia["required"]) == {"marker", "position", "content"}

    def test_body_replace_property_exists(self):
        schema = _talker_vault_edit_schema()
        assert "body_replace" in schema["properties"]
        assert schema["properties"]["body_replace"]["type"] == "string"

    def test_body_append_still_present(self):
        """Regression: existing body_append kwarg unchanged."""
        schema = _talker_vault_edit_schema()
        assert "body_append" in schema["properties"]
        assert schema["properties"]["body_append"]["type"] == "string"

    def test_set_fields_still_rejects_body_key(self):
        """Regression from P1 (c2 of body-filter arc): set_fields
        carries the not.required.[body] constraint. This is at the
        PROPERTY level (not top level), so Anthropic accepts it."""
        schema = _talker_vault_edit_schema()
        assert schema["properties"]["set_fields"]["not"] == {
            "required": ["body"],
        }


class TestInstructorSchemaShape:
    def test_body_insert_at_property_exists(self):
        schema = _instructor_vault_edit_schema()
        props = schema["properties"]
        assert "body_insert_at" in props
        assert set(props["body_insert_at"]["required"]) == {
            "marker", "position", "content",
        }

    def test_body_replace_property_exists(self):
        schema = _instructor_vault_edit_schema()
        assert "body_replace" in schema["properties"]


# ---------------------------------------------------------------------------
# Anthropic-API guard — no top-level oneOf / allOf / anyOf
# ---------------------------------------------------------------------------
#
# 2026-05-06 production breakage regression: the Anthropic Messages
# API rejects oneOf / allOf / anyOf at the top level of any tool's
# input_schema with HTTP 400 BEFORE the model runs. Pin both schemas
# so a future "schema as defense in depth" instinct can't quietly
# reintroduce the same break.


class TestNoTopLevelSchemaCombinators:
    """No top-level oneOf / allOf / anyOf in any tool's input_schema.
    Anthropic's request validator 400s on this; runtime gate in
    vault_edit is THE load-bearing mutual-exclusion protection."""

    @pytest.mark.parametrize(
        "schema_factory",
        [
            _talker_vault_edit_schema,
            _instructor_vault_edit_schema,
        ],
        ids=["talker", "instructor"],
    )
    def test_no_top_level_combinator(self, schema_factory):
        schema = schema_factory()
        for forbidden_key in ("oneOf", "allOf", "anyOf"):
            assert forbidden_key not in schema, (
                f"top-level {forbidden_key!r} in vault_edit input_schema "
                f"will trigger Anthropic HTTP 400; the runtime gate in "
                f"vault.ops.vault_edit enforces mutual exclusion. See "
                f"tests/test_vault_edit_body_mutation.py for runtime-gate "
                f"coverage."
            )


# ---------------------------------------------------------------------------
# Positive validation — every legitimate call shape passes the schema
# ---------------------------------------------------------------------------
#
# Pre-2026-05-06 these tests AND a parallel set of "multi-body-kwarg
# rejected" tests both ran. The rejection tests pinned a structural
# constraint (top-level oneOf) that we can't ship to Anthropic, so
# they were removed. Production-side mutual-exclusion regression
# coverage moved to tests/test_vault_edit_body_mutation.py against
# the runtime gate in vault.ops.vault_edit. The positive tests stay
# here because they confirm the LEGITIMATE call shapes the LLM can
# emit are still well-formed against the (simpler) schema.


class TestTalkerSchemaPositiveValidation:
    def test_zero_body_kwargs_passes(self):
        """Frontmatter-only edit (no body kwargs) must validate."""
        jsonschema = pytest.importorskip("jsonschema")
        schema = _talker_vault_edit_schema()
        instance = {"path": "note/X.md", "set_fields": {"status": "active"}}
        jsonschema.validate(instance=instance, schema=schema)  # no raise

    def test_only_body_append_passes(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = _talker_vault_edit_schema()
        instance = {"path": "note/X.md", "body_append": "added"}
        jsonschema.validate(instance=instance, schema=schema)

    def test_only_body_insert_at_passes(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_insert_at": {
                "marker": "## Section",
                "position": "after",
                "content": "added",
            },
        }
        jsonschema.validate(instance=instance, schema=schema)

    def test_only_body_replace_passes(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = _talker_vault_edit_schema()
        instance = {"path": "note/X.md", "body_replace": "# New\n"}
        jsonschema.validate(instance=instance, schema=schema)

    def test_body_insert_at_missing_required_field_rejected(self):
        """Property-level required-field validation still works (this
        is inside the body_insert_at property's own schema, not at
        the top level — Anthropic-safe)."""
        jsonschema = pytest.importorskip("jsonschema")
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_insert_at": {
                "marker": "## S",
                "position": "after",
                # missing "content"
            },
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)

    def test_body_insert_at_invalid_position_rejected(self):
        """Property-level enum validation still works (Anthropic-safe)."""
        jsonschema = pytest.importorskip("jsonschema")
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_insert_at": {
                "marker": "## S",
                "position": "middle",  # not in enum
                "content": "x",
            },
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)

    def test_set_fields_with_body_key_rejected(self):
        """Cross-check the P1 not.required.[body] constraint. This is
        a property-level ``not`` (inside set_fields' schema), not a
        top-level combinator — Anthropic-safe."""
        jsonschema = pytest.importorskip("jsonschema")
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "set_fields": {"body": "should not be here"},
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)


class TestInstructorSchemaPositiveValidation:
    def test_only_body_replace_passes(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = _instructor_vault_edit_schema()
        instance = {"path": "note/X.md", "body_replace": "# new\n"}
        jsonschema.validate(instance=instance, schema=schema)

    def test_only_body_append_passes(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = _instructor_vault_edit_schema()
        instance = {"path": "note/X.md", "body_append": "added"}
        jsonschema.validate(instance=instance, schema=schema)

    def test_only_body_insert_at_passes(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = _instructor_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_insert_at": {
                "marker": "## S",
                "position": "before",
                "content": "x",
            },
        }
        jsonschema.validate(instance=instance, schema=schema)

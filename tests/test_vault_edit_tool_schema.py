"""Schema-pin tests for the vault_edit tool surface (c3 of body-mutation arc).

c3 exposes body_insert_at + body_replace through the LLM-facing JSON
schemas in conversation.py + instructor/executor.py. The runtime
gate at vault_edit (c2) is load-bearing; these schema constraints
are defense in depth (Anthropic SDK doesn't strictly validate
input_schema client-side — the schema is a hint to the LLM).

Coverage:
  * talker vault_edit schema declares body_insert_at + body_replace
  * instructor vault_edit schema declares body_insert_at + body_replace
  * body_insert_at carries the {marker, position, content} required
    fields; position enum is ['before', 'after']
  * Mutual-exclusion oneOf constraint structurally rejects calls
    with multiple body kwargs (validates against a real JSON-schema
    validator)
  * set_fields still carries the not.required.body constraint
    from P1 (c2 in body-filter arc)
"""

from __future__ import annotations

import pytest

# Optional dep: jsonschema. Skipped if not installed; the runtime
# gate in vault_edit (c2) carries the load-bearing protection.
jsonschema = pytest.importorskip("jsonschema")


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
        carries the not.required.[body] constraint."""
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
# Mutual exclusion via oneOf — validates against real jsonschema validator
# ---------------------------------------------------------------------------


class TestTalkerSchemaMutualExclusion:
    """The talker vault_edit schema's oneOf constraint must structurally
    reject calls that include multiple body-mutation kwargs.

    Note: the Anthropic SDK doesn't enforce input_schema client-side
    (it's a hint to the LLM). These tests use python-jsonschema to
    validate the SCHEMA SHAPE — confirming a strict validator would
    reject the malformed calls. The runtime gate in vault_edit is
    load-bearing for production.
    """

    def test_zero_body_kwargs_passes(self):
        """Frontmatter-only edit (no body kwargs) must validate."""
        schema = _talker_vault_edit_schema()
        instance = {"path": "note/X.md", "set_fields": {"status": "active"}}
        jsonschema.validate(instance=instance, schema=schema)  # no raise

    def test_only_body_append_passes(self):
        schema = _talker_vault_edit_schema()
        instance = {"path": "note/X.md", "body_append": "added"}
        jsonschema.validate(instance=instance, schema=schema)

    def test_only_body_insert_at_passes(self):
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
        schema = _talker_vault_edit_schema()
        instance = {"path": "note/X.md", "body_replace": "# New\n"}
        jsonschema.validate(instance=instance, schema=schema)

    def test_body_append_plus_body_replace_rejected(self):
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_append": "x",
            "body_replace": "y",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)

    def test_body_insert_at_plus_body_replace_rejected(self):
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_insert_at": {
                "marker": "## S",
                "position": "after",
                "content": "x",
            },
            "body_replace": "y",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)

    def test_body_append_plus_body_insert_at_rejected(self):
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_append": "x",
            "body_insert_at": {
                "marker": "## S",
                "position": "after",
                "content": "y",
            },
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)

    def test_all_three_body_kwargs_rejected(self):
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_append": "x",
            "body_insert_at": {
                "marker": "## S",
                "position": "after",
                "content": "y",
            },
            "body_replace": "z",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)

    def test_body_insert_at_missing_required_field_rejected(self):
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
        """Cross-check the P1 not.required.[body] constraint via
        the same validator."""
        schema = _talker_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "set_fields": {"body": "should not be here"},
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)


class TestInstructorSchemaMutualExclusion:
    """Same oneOf contract on the instructor's vault_edit schema."""

    def test_only_body_replace_passes(self):
        schema = _instructor_vault_edit_schema()
        instance = {"path": "note/X.md", "body_replace": "# new\n"}
        jsonschema.validate(instance=instance, schema=schema)

    def test_body_append_plus_body_replace_rejected(self):
        schema = _instructor_vault_edit_schema()
        instance = {
            "path": "note/X.md",
            "body_append": "x",
            "body_replace": "y",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=instance, schema=schema)

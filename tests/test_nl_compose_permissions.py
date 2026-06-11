"""``apply_compose_permissions`` — the NL-lane compose-tier extractor.

The composition-grant mirror of ``apply_field_permissions``: reads
``nl_query.compose_fields`` for one peer × type and extracts ONLY those
values from raw frontmatter. Fail-closed at every lookup miss; never
sees bodies (callers only pass frontmatter dicts by construction —
pinned end-to-end in test_nl_query_handler.py).
"""

from __future__ import annotations

from alfred.transport.canonical import apply_compose_permissions
from alfred.transport.config import NLQueryRules, PeerFieldRules, PeerQueryRules


_FM = {
    "name": "Call with Ben",
    "date": "2026-05-26",
    "description": "Discussed the RRTS proposal and next steps.",
    "phone": "555-0000",
    "preferences": {"coding": "tabs", "contact": "email"},
}


def _perms_with_compose(compose_fields: list[str]) -> dict:
    return {
        "hypatia": {
            "event": PeerFieldRules(
                fields=["name", "date"],
                query=PeerQueryRules(),
                nl_query=NLQueryRules(compose_fields=compose_fields),
            ),
        },
    }


# ---------------------------------------------------------------------------
# Default-deny paths
# ---------------------------------------------------------------------------


def test_none_perms_yields_empty() -> None:
    extras, used = apply_compose_permissions("hypatia", "event", _FM, None)
    assert extras == {} and used == []


def test_missing_peer_yields_empty() -> None:
    extras, used = apply_compose_permissions(
        "kal-le", "event", _FM, _perms_with_compose(["description"]),
    )
    assert extras == {} and used == []


def test_missing_type_yields_empty() -> None:
    extras, used = apply_compose_permissions(
        "hypatia", "person", _FM, _perms_with_compose(["description"]),
    )
    assert extras == {} and used == []


def test_absent_nl_query_block_yields_empty() -> None:
    perms = {
        "hypatia": {
            "event": PeerFieldRules(fields=["name"], query=PeerQueryRules()),
        },
    }
    extras, used = apply_compose_permissions("hypatia", "event", _FM, perms)
    assert extras == {} and used == []


def test_empty_compose_fields_yields_empty() -> None:
    """Ratified Decision C default: compose tier ships empty."""
    extras, used = apply_compose_permissions(
        "hypatia", "event", _FM, _perms_with_compose([]),
    )
    assert extras == {} and used == []


# ---------------------------------------------------------------------------
# Extraction semantics
# ---------------------------------------------------------------------------


def test_extracts_only_compose_granted_fields() -> None:
    """● Compose tier extracts the grant and NOTHING else (no phone, no name)."""
    extras, used = apply_compose_permissions(
        "hypatia", "event", _FM, _perms_with_compose(["description"]),
    )
    assert extras == {"description": "Discussed the RRTS proposal and next steps."}
    assert used == ["description"]
    assert "phone" not in extras
    assert "name" not in extras


def test_dotted_compose_field_nests_like_field_gate() -> None:
    extras, used = apply_compose_permissions(
        "hypatia", "event", _FM, _perms_with_compose(["preferences.coding"]),
    )
    assert extras == {"preferences": {"coding": "tabs"}}
    assert used == ["preferences.coding"]
    # The sibling dotted leaf was NOT extracted.
    assert "contact" not in extras["preferences"]


def test_field_absent_from_frontmatter_is_skipped() -> None:
    extras, used = apply_compose_permissions(
        "hypatia", "event", _FM, _perms_with_compose(["description", "location"]),
    )
    assert extras == {"description": "Discussed the RRTS proposal and next steps."}
    assert used == ["description"]


def test_raw_dict_perms_shape_supported() -> None:
    """Handler tests use raw-dict perms; both shapes must behave alike."""
    perms = {
        "hypatia": {
            "event": {
                "fields": ["name"],
                "query": {"filter_dims": {}},
                "nl_query": {"compose_fields": ["description"]},
            },
        },
    }
    extras, used = apply_compose_permissions("hypatia", "event", _FM, perms)
    assert extras == {"description": "Discussed the RRTS proposal and next steps."}
    assert used == ["description"]

"""Smoke tests for ``alfred.vault.schema`` module-level constants.

Bootstrap-scope: verify the constants new callers depend on are exported
with the expected shape. Broader schema coverage lands with the code that
consumes the constants.
"""

from __future__ import annotations


def test_instruction_fields_is_importable():
    # INSTRUCTION_FIELDS is the single source of truth for the field
    # names the instructor daemon polls. Importing it here guards
    # against silent renames.
    from alfred.vault.schema import INSTRUCTION_FIELDS

    assert "alfred_instructions" in INSTRUCTION_FIELDS
    assert "alfred_instructions_last" in INSTRUCTION_FIELDS


def test_instruction_fields_contains_both_field_names():
    from alfred.vault.schema import INSTRUCTION_FIELDS

    # Exactly these two, in this order — pending queue first, executed
    # archive second. Downstream callers may iterate positionally.
    assert tuple(INSTRUCTION_FIELDS) == (
        "alfred_instructions",
        "alfred_instructions_last",
    )


def test_list_fields_includes_both_instruction_fields():
    # Both instruction fields must be in LIST_FIELDS so the existing
    # frontmatter-list coercion treats them as lists rather than
    # scalars when parsing records.
    from alfred.vault.schema import INSTRUCTION_FIELDS, LIST_FIELDS

    for field in INSTRUCTION_FIELDS:
        assert field in LIST_FIELDS, (
            f"{field!r} must be in LIST_FIELDS so instruction queues "
            f"are parsed as lists, not coerced to scalar strings."
        )


def test_note_status_includes_living():
    # ``status: living`` is a long-running-document marker for ``note``
    # records (e.g. a permanent task list, an evolving reference page).
    # Hypatia's QA flagged the gap on 2026-04-28 — the original status
    # set rejected ``living``, forcing a fall-back to ``active`` which
    # is semantically wrong for permanent reference material.
    from alfred.vault.schema import STATUS_BY_TYPE

    assert "living" in STATUS_BY_TYPE["note"], (
        "note records must accept status='living' for long-running "
        "reference docs (Hypatia QA 2026-04-28)."
    )
    # Other statuses must still be accepted — the addition is additive.
    for legacy in ("draft", "active", "review", "final"):
        assert legacy in STATUS_BY_TYPE["note"]


def test_tier_fields_v1_base_tier_escalate_to_removed():
    # Routine-systems consolidation Step 1 (2026-06-25): the dead
    # Tier-V1 surface (``base_tier`` / ``escalate_to``) was removed from
    # ``TIER_FIELDS`` so the schema stops describing a retired model.
    # V1 was retired 2026-05-29 (Ship 3) and neither field has a live
    # consumer. Reintroducing either into the tuple would re-assert the
    # dead model — pin the absence.
    from alfred.vault.schema import TIER_FIELDS

    assert "base_tier" not in TIER_FIELDS, (
        "base_tier is a dead Tier-V1 field (retired 2026-05-29); it must "
        "not be in TIER_FIELDS. See routine-systems consolidation Step 1."
    )
    assert "escalate_to" not in TIER_FIELDS, (
        "escalate_to is a dead Tier-V1 field (retired 2026-05-29); it must "
        "not be in TIER_FIELDS. See routine-systems consolidation Step 1."
    )


def test_tier_fields_keeps_live_escalate_at_days():
    # The other half of the Step-1 cut: ``escalate_at_days`` is the LIVE
    # V2 due-window knob (consumed by
    # ``alfred.tier.compute.compute_auto_t1_candidates`` — see the
    # liveness pin in tests/tier/test_compute.py). It MUST survive the
    # V1 removal. This is the both-ways pin: V1 gone, V2 kept.
    from alfred.vault.schema import TIER_FIELDS

    assert "escalate_at_days" in TIER_FIELDS, (
        "escalate_at_days is the live V2 due-window field; it must stay "
        "in TIER_FIELDS. Removing it would break task auto-T1 surfacing."
    )

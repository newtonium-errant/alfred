"""FeedItem model pins — id identity, KIND_DEFAULTS, schema-tolerance both ways.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from alfred.feed.model import (
    ATTENTION_FYI,
    ATTENTION_NEEDS_YOU,
    KIND_DEFAULTS,
    KINDS,
    MODE_DECIDE,
    MODE_FYI,
    STATE_OPEN,
    FeedItem,
    make_id,
)


def test_make_id_is_kind_colon_key() -> None:
    assert make_id("proposal", "corr-123") == "proposal:corr-123"


def test_create_applies_decision_defaults() -> None:
    item = FeedItem.create(kind="proposal", stable_key="corr-1", instance="salem", title="X")
    assert item.id == "proposal:corr-1"
    assert item.mode == MODE_DECIDE
    assert item.attention == ATTENTION_NEEDS_YOU
    assert item.state == STATE_OPEN
    assert item.created_at  # stamped


def test_create_applies_awareness_defaults() -> None:
    item = FeedItem.create(kind="radar", stable_key="rec/x.md", instance="salem", title="X")
    assert item.mode == MODE_FYI
    assert item.attention == ATTENTION_FYI


def test_create_explicit_override_wins() -> None:
    item = FeedItem.create(
        kind="proposal", stable_key="c", instance="salem", title="X",
        mode=MODE_FYI, attention=ATTENTION_FYI,
    )
    assert item.mode == MODE_FYI
    assert item.attention == ATTENTION_FYI


def test_kind_defaults_covers_every_kind() -> None:
    # No kind may be missing from the defaults table (a missing kind would
    # silently degrade to fyi/fyi — catch it at the contract).
    assert set(KIND_DEFAULTS) == set(KINDS)
    for mode, attention in KIND_DEFAULTS.values():
        assert mode in {MODE_DECIDE, MODE_FYI}
        assert attention in {ATTENTION_NEEDS_YOU, ATTENTION_FYI}


def test_roundtrip_to_from_dict() -> None:
    item = FeedItem.create(
        kind="email_tier", stable_key="note/A.md", instance="salem", title="tier",
        evidence={"sender": "a@b.com"}, actions=[{"verb": "confirm", "label": "Keep"}],
        source_ref={"store": "corpus"},
    )
    restored = FeedItem.from_dict(item.to_dict())
    assert restored == item


def test_from_dict_drops_unknown_fields_forward_compat() -> None:
    # A newer writer added a field this version doesn't know — must not crash.
    data = FeedItem.create(kind="pending", stable_key="u1", instance="salem", title="p").to_dict()
    data["future_field"] = {"nested": 1}
    restored = FeedItem.from_dict(data)
    assert restored.id == "pending:u1"
    assert not hasattr(restored, "future_field")


def test_from_dict_tolerates_missing_optional_fields() -> None:
    # An older writer omitted fields this version added — defaults fill in.
    restored = FeedItem.from_dict({"id": "health:curator", "kind": "health"})
    assert restored.instance == ""
    assert restored.state == STATE_OPEN
    assert restored.evidence == {}
    assert restored.actions == []

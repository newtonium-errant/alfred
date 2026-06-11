"""Compose-tier extraction in ``_execute_filtered_search`` (NL lane).

The single-disclosure-path property extended: when the NL broker asks
for the compose tier (``include_compose_tier=True``), the SAME engine
that runs gates 1-3 also extracts the policy's ``compose_fields`` from
raw frontmatter — there is no second vault read anywhere.

DISCLOSURE PINS ● here:
  * default ``include_compose_tier=False`` → result dict has NO
    compose keys at all (byte-identical to pre-LLM-lane shape);
  * compose extras carry ONLY the policy's compose_fields — never an
    ungranted field, never body content;
  * bodies are structurally unreachable (the engine parses
    ``post.metadata`` only) — pinned with a sentinel-in-body fixture.

These tests run UNCONDITIONALLY — pure stdlib + frontmatter, no
optional deps, no importorskip (per feedback_regression_pin_unconditional).
"""

from __future__ import annotations

import json

from alfred.transport.config import (
    CanonicalConfig,
    FilterDimRule,
    NLQueryRules,
    PeerFieldRules,
    PeerQueryRules,
    TransportConfig,
)
from alfred.transport.peer_handlers import _execute_filtered_search


BODY_SENTINEL = "BODY-SENTINEL-NEVER-DISCLOSED-d41d8cd9"
SECRET_SENTINEL = "SECRET-FIELD-VALUE-NEVER-DISCLOSED-8f14e45f"


def _config(tmp_path, compose_fields: list[str]) -> TransportConfig:
    return TransportConfig(
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=str(tmp_path / "audit.jsonl"),
            peer_permissions={
                "hypatia": {
                    "event": PeerFieldRules(
                        fields=["name", "date", "participants"],
                        query=PeerQueryRules(
                            filter_dims={
                                "participants": FilterDimRule(op=["contains"]),
                                "date": FilterDimRule(op=["gte", "lte"]),
                            },
                            sort=["date"],
                            max_limit=10,
                            default_limit=5,
                        ),
                        nl_query=NLQueryRules(compose_fields=compose_fields),
                    ),
                },
            },
        ),
    )


def _vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "event").mkdir(parents=True)
    (vault / "event" / "Call with Ben.md").write_text(
        "---\n"
        "name: Call with Ben\n"
        "date: '2026-05-26'\n"
        "description: Discussed the RRTS proposal in detail.\n"
        f"secret_notes: {SECRET_SENTINEL}\n"
        "participants:\n"
        "  - '[[person/Andrew Newton]]'\n"
        "  - '[[person/Ben]]'\n"
        "---\n"
        f"{BODY_SENTINEL}\n",
        encoding="utf-8",
    )
    return vault


def _search(tmp_path, *, compose_fields: list[str], **kwargs):
    return _execute_filtered_search(
        config=_config(tmp_path, compose_fields),
        vault_path=_vault(tmp_path),
        peer="hypatia",
        record_type="event",
        raw_filter=[{"dim": "participants", "op": "contains", "value": "Ben"}],
        raw_sort=None,
        raw_limit=None,
        requested_fields=[],
        correlation_id="cid-compose-tier",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# ● Back-compat pin — default call shape unchanged
# ---------------------------------------------------------------------------


def test_default_call_has_no_compose_keys(tmp_path) -> None:
    """● PIN: without include_compose_tier the result dict carries NO
    compose keys, so /peer/search and kind=query can never even see
    compose values internally. (The internal ``denied`` bookkeeping key
    rides every success result for the nl audit row — wire responses
    stay byte-identical, pinned at the HTTP layer in
    test_nl_query_handler.py::test_peer_search_response_carries_no_
    compose_keys.)"""
    result = _search(tmp_path, compose_fields=["description"])
    assert result["ok"] is True
    assert "compose_extras" not in result
    assert "compose_fields_used" not in result
    # And the gated records still withhold the compose-tier field.
    assert all("description" not in r for r in result["records"])


# ---------------------------------------------------------------------------
# Compose-tier extraction
# ---------------------------------------------------------------------------


def test_compose_tier_extracts_policy_fields_aligned_with_records(tmp_path) -> None:
    result = _search(
        tmp_path, compose_fields=["description"], include_compose_tier=True,
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["compose_extras"] == [
        {"description": "Discussed the RRTS proposal in detail."},
    ]
    assert result["compose_fields_used"] == ["description"]
    # The GATED record still does NOT carry description — compose tier
    # rides a separate channel, never widening `records`.
    assert "description" not in result["records"][0]


def test_compose_tier_empty_policy_yields_empty_extras(tmp_path) -> None:
    """Decision C default: empty compose_fields → empty extras, not an error."""
    result = _search(tmp_path, compose_fields=[], include_compose_tier=True)
    assert result["ok"] is True
    assert result["compose_extras"] == [{}]
    assert result["compose_fields_used"] == []


# ---------------------------------------------------------------------------
# ● Disclosure pins — ungranted fields + bodies never in extras
# ---------------------------------------------------------------------------


def test_compose_tier_never_carries_ungranted_fields(tmp_path) -> None:
    """● PIN: an ungranted, non-compose field's VALUE never appears
    anywhere in the engine result; its NAME appears ONLY in the internal
    ``denied`` bookkeeping (audit-bound by design — P1 precedent: the
    audit row has always listed withheld field names) and never in the
    outbound-bound surfaces (``records`` / ``compose_extras``, the only
    keys that feed prompts or reply payloads).

    The reply-side enforcement of this split — that ``denied`` can never
    become reply-bound — is pinned at the wire layer in
    test_nl_query_handler.py::test_reply_payload_keyset_is_exactly_the_
    designed_shape.
    """
    result = _search(
        tmp_path, compose_fields=["description"], include_compose_tier=True,
    )
    # Value: absolute — nowhere in the result dict, any key.
    serialized = json.dumps(result)
    assert SECRET_SENTINEL not in serialized
    # Name: absent from every outbound-bound surface...
    outbound = json.dumps({
        "records": result["records"],
        "compose_extras": result["compose_extras"],
    })
    assert "secret_notes" not in outbound
    # ...and present in the internal audit bookkeeping (by design).
    assert "secret_notes" in result["denied"]


def test_compose_tier_never_carries_body(tmp_path) -> None:
    """● PIN: bodies are structurally unreachable — the engine parses
    ``post.metadata`` only, and the compose tier rides the same loader."""
    result = _search(
        tmp_path,
        # Even a hostile/misconfigured policy naming body-ish fields
        # cannot reach the body: it is never in the frontmatter dict.
        compose_fields=["description", "body", "content"],
        include_compose_tier=True,
    )
    serialized = json.dumps(result)
    assert BODY_SENTINEL not in serialized
    assert result["compose_fields_used"] == ["description"]

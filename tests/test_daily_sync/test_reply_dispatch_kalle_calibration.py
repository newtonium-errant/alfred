"""Regression tests for the 2026-05-10 KAL-LE '1 confirm' incident.

Andrew sent "1 confirm" to KAL-LE's Daily Sync (msg_id 74) targeting a
batch with one Hypatia-proposed canonical proposal (item_number=1) and
five distiller-marker attribution items (item_numbers 2-6) — zero email
items. The bot logged ``unparsed=1`` and replied with:

    "Calibration: didn't understand item 1 — could you restate?
     (Tip: 'Same' / 'Ditto' / 'Same as #N' are supported for list items.)"

Two bugs surfaced:

* **Bug 1** — the dispatcher buried the actual execution-failure
  reason under the canned "didn't understand" message. The proposal-
  confirm correctly routed to ``_resolve_proposal_correction``; the
  resolver called ``vault_create`` which raised ``ScopeError`` (KAL-LE
  isn't the canonical owner of person records). The error string was
  perfectly informative but got bucketed as if it were a parse failure.

* **Bug 2** — the calibration hint was hardcoded "Same / Ditto / Same
  as #N" (email-section verbs) even though the batch had zero email
  items. The hint should be item-type-aware: when the batch has
  attribution and/or proposal items, the hint should advertise
  ``N confirm`` / ``N reject`` (which is what the batch message body
  itself advertises — see ``attribution_section.py:357`` and
  ``canonical_proposals_section.py:186``).

These tests pin:

* "1 confirm" against the KAL-LE batch shape (1 proposal + 5
  attribution items, vault_create patched to succeed) round-trips to
  ``proposal_written=1``, ``unparsed=0``, ``execution_failures=0``.
* When ``vault_create`` raises (the production failure shape on
  May 10), the resulting message surfaces the actual error string
  verbatim — NOT "didn't understand".
* The calibration hint shape varies by batch composition:
  attribution-only / proposal-only / pending-only batches get verb-
  specific hints; email-only preserves Salem's historical hint.
* Salem's contract is unchanged when the batch has email items.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alfred.daily_sync.config import AttributionConfig, DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.reply_dispatch import (
    _compose_calibration_hint,
    _is_verb_mismatch_error,
    handle_daily_sync_reply,
)
from alfred.transport.canonical_proposals import (
    Proposal,
    STATE_PENDING,
    append_proposal,
)


# --- Fixtures --------------------------------------------------------------


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.attribution = AttributionConfig(
        enabled=True,
        batch_size=5,
        scan_paths=[],
        corpus_path=str(tmp_path / "attribution_corpus.jsonl"),
    )
    return cfg


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    return vault


def _attribution_item(num: int, marker_id: str) -> dict[str, Any]:
    return {
        "item_number": num,
        "record_path": f"x/item{num}.md",
        "marker_id": marker_id,
        "agent": "distiller",
        "date": "2026-05-13T17:22:14+00:00",
        "section_title": f"Section {num}",
        "reason": "distiller pipeline",
        "content_preview": "preview text",
    }


def _proposal_item(num: int, correlation_id: str, name: str = "Test Person") -> dict[str, Any]:
    return {
        "item_number": num,
        "correlation_id": correlation_id,
        "proposer": "hypatia",
        "record_type": "person",
        "name": name,
        "proposed_fields": {"role": "Operations Manager", "org": "RRTS"},
        "source": "test",
    }


def _seed_kalle_batch(
    cfg: DailySyncConfig,
    *,
    parent_msg_id: int = 74,
    correlation_id: str = "hypatia-propose-person-c2a000",
    proposal_name: str = "Ben McMillan",
) -> None:
    """Seed the persisted state with KAL-LE's 2026-05-10 batch shape.

    Mirrors the verified state file contents at the time of the
    incident: 0 email items, 5 attribution items (item_numbers 2-6),
    1 proposal item (item_number=1).
    """
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-05-10",
            "items": [],
            "message_ids": [parent_msg_id],
            "attribution_items": [
                _attribution_item(2, "inf-20260513-distiller-69a6d9"),
                _attribution_item(3, "inf-20260513-distiller-f4c26a"),
                _attribution_item(4, "inf-20260513-distiller-e87315"),
                _attribution_item(5, "inf-20260513-distiller-1a163a"),
                _attribution_item(6, "inf-20260512-distiller-728c89"),
            ],
            "proposal_items": [
                _proposal_item(1, correlation_id, name=proposal_name),
            ],
        },
    })


def _patch_proposals_queue_path(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import alfred.daily_sync.reply_dispatch as rd
    monkeypatch.setattr(
        rd, "_canonical_proposals_queue_path", lambda *a, **kw: str(path),
    )


def _patch_vault_create_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``vault_create`` with a recording stub that succeeds."""
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"path": "person/Ben McMillan.md", "warnings": []}

    import alfred.vault.ops as ops_mod
    monkeypatch.setattr(ops_mod, "vault_create", _spy)
    return captured


def _patch_vault_create_scope_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``vault_create`` with a stub that raises the production
    ScopeError message — mirrors the 2026-05-10 KAL-LE failure path.
    """
    from alfred.vault.scope import ScopeError

    def _raise(**kwargs: Any) -> dict[str, Any]:
        raise ScopeError(
            f"Scope {kwargs.get('scope', '?')!r} may not create local "
            f"{kwargs.get('record_type', '?')!r} records — those are "
            f"Salem's canonical authority. Use the 'propose_person' "
            f"tool to propose creation on Salem instead."
        )

    import alfred.vault.ops as ops_mod
    monkeypatch.setattr(ops_mod, "vault_create", _raise)


# --- Bug 1 round-trip: '1 confirm' on KAL-LE batch shape -----------------


def test_kalle_one_confirm_writes_proposal_when_create_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The 'happy path' pin — Andrew's '1 confirm' fully round-trips.

    When ``vault_create`` succeeds (canonical owner CAN create the
    proposed record), '1 confirm' must produce ``proposal_written=1``
    and ``unparsed=0``. Catches a regression where the proposal-confirm
    routing breaks on a batch with no email items + a single proposal.
    """
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    queue_path = tmp_path / "proposals.jsonl"
    _patch_proposals_queue_path(monkeypatch, queue_path)
    captured = _patch_vault_create_spy(monkeypatch)

    correlation_id = "hypatia-propose-person-c2a000"
    append_proposal(str(queue_path), Proposal(
        correlation_id=correlation_id,
        ts="2026-05-10T12:00:00+00:00",
        state=STATE_PENDING,
        proposer="hypatia",
        record_type="person",
        name="Ben McMillan",
        proposed_fields={"role": "Operations Manager", "org": "RRTS"},
        source="test",
    ))
    _seed_kalle_batch(cfg, correlation_id=correlation_id)

    result = handle_daily_sync_reply(
        cfg,
        parent_message_id=74,
        reply_text="1 confirm",
        vault_path=vault,
        instance_scope="kalle",
        instance_name="kalle",
    )
    assert result is not None
    assert result["confirmed_count"] == 1
    assert result["proposal_count"] == 1
    assert result["email_count"] == 0
    assert result["attribution_count"] == 0
    assert result["unparsed"] == []
    assert result["all_ok"] is False
    # vault_create was called with the right scope + record details.
    assert captured.get("scope") == "kalle"
    assert captured.get("record_type") == "person"
    assert captured.get("name") == "Ben McMillan"


def test_kalle_one_confirm_surfaces_scope_deny_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The 2026-05-10 production failure path — execution error must
    surface verbatim instead of "didn't understand".

    Pre-fix: ``vault_create`` ScopeError got bucketed into
    ``unparsed_item_numbers``; user saw "Calibration: didn't understand
    item 1 — could you restate?" with an email-section hint.

    Post-fix: the scope-deny error is in ``execution_errors`` and
    surfaces in the user-facing message verbatim. The "didn't
    understand" template is NOT used because the parser DID understand.
    """
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    queue_path = tmp_path / "proposals.jsonl"
    _patch_proposals_queue_path(monkeypatch, queue_path)
    _patch_vault_create_scope_deny(monkeypatch)

    correlation_id = "hypatia-propose-person-c2a000"
    append_proposal(str(queue_path), Proposal(
        correlation_id=correlation_id,
        ts="2026-05-10T12:00:00+00:00",
        state=STATE_PENDING,
        proposer="hypatia",
        record_type="person",
        name="Ben McMillan",
        proposed_fields={},
        source="test",
    ))
    _seed_kalle_batch(cfg, correlation_id=correlation_id)

    result = handle_daily_sync_reply(
        cfg,
        parent_message_id=74,
        reply_text="1 confirm",
        vault_path=vault,
        instance_scope="kalle",
        instance_name="kalle",
    )
    assert result is not None
    # confirmed_count is zero (no record was actually written).
    assert result["confirmed_count"] == 0
    # The error string is preserved — Andrew sees the ScopeError reason.
    assert len(result["unparsed"]) == 1
    assert "Scope 'kalle' may not create local 'person' records" in result["unparsed"][0]
    # User-facing message surfaces the error verbatim, NOT the canned
    # "didn't understand" template.
    msg = result["message"]
    assert "Scope 'kalle' may not create local 'person' records" in msg
    assert "didn't understand" not in msg.lower()
    # The "Tip: ..." hint shouldn't show up either — there's no parse
    # failure to hint about.
    assert "tip:" not in msg.lower()


# --- Bug 2 hint composition ------------------------------------------------


def test_hint_attribution_only_advertises_confirm_reject():
    """Batch with only attribution items → hint is `N confirm` / `N reject`."""
    hint = _compose_calibration_hint(
        has_email=False,
        has_attribution=True,
        has_proposal=False,
        has_pending=False,
    )
    assert "N confirm" in hint
    assert "N reject" in hint
    # Email-section verbs MUST be absent (the regression).
    assert "Same" not in hint
    assert "Ditto" not in hint


def test_hint_proposal_only_advertises_confirm_reject():
    """Batch with only proposal items → same `N confirm` / `N reject` shape."""
    hint = _compose_calibration_hint(
        has_email=False,
        has_attribution=False,
        has_proposal=True,
        has_pending=False,
    )
    assert "N confirm" in hint
    assert "N reject" in hint
    assert "Same" not in hint


def test_hint_pending_only_advertises_noted_show_me():
    """Batch with only pending items → `N noted` / `N show me`."""
    hint = _compose_calibration_hint(
        has_email=False,
        has_attribution=False,
        has_proposal=False,
        has_pending=True,
    )
    assert "N noted" in hint
    assert "N show me" in hint
    assert "Same" not in hint


def test_hint_email_only_preserves_salem_default():
    """Email-only batch → preserves the historical "Same / Ditto" hint.

    Salem's contract MUST be unchanged when the batch is email-only;
    that's the dominant shape and the hint Andrew already trained
    against. Regressing this would surprise Salem users without
    benefit.
    """
    hint = _compose_calibration_hint(
        has_email=True,
        has_attribution=False,
        has_proposal=False,
        has_pending=False,
    )
    assert "Same" in hint
    assert "Ditto" in hint
    assert "Same as #N" in hint
    # The "N confirm" verbs are NOT advertised when email is the only
    # type — those verbs don't apply to email items.
    assert "N confirm" not in hint


def test_hint_mixed_batch_lists_applicable_verbs():
    """Mixed batch (e.g., email + attribution) → list both verb sets."""
    hint = _compose_calibration_hint(
        has_email=True,
        has_attribution=True,
        has_proposal=False,
        has_pending=False,
    )
    assert "N confirm" in hint
    assert "Same" in hint


def test_hint_empty_batch_returns_empty_string():
    """No items flagged → empty hint (no stray "Tip:" prefix)."""
    hint = _compose_calibration_hint(
        has_email=False,
        has_attribution=False,
        has_proposal=False,
        has_pending=False,
    )
    assert hint == ""


# --- End-to-end hint surfacing via the dispatcher --------------------------


def test_dispatch_attribution_only_batch_uses_attribution_hint(
    tmp_path: Path,
):
    """Attribution-only batch + verb-mismatch input → user message hints
    `N confirm` / `N reject`, NOT the email-section default.

    The 2026-05-10 incident's user-facing symptom in concentrated form.
    """
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    save_state(cfg.state.path, {
        "last_batch": {
            "items": [],
            "message_ids": [100],
            "attribution_items": [_attribution_item(1, "inf-foo")],
        },
    })

    # "1 high" is a tier verb (only valid on email items). Hits the
    # attribution-only-accepts gate → verb-mismatch → user-facing hint.
    result = handle_daily_sync_reply(
        cfg,
        parent_message_id=100,
        reply_text="1 high",
        vault_path=vault,
        instance_scope="kalle",
        instance_name="kalle",
    )
    assert result is not None
    msg = result["message"]
    # New (post-fix) hint: attribution verbs.
    assert "N confirm" in msg
    assert "N reject" in msg
    # OLD (Salem) hint MUST be absent — this is the regression we fixed.
    assert "Same" not in msg
    assert "Ditto" not in msg


def test_dispatch_email_only_batch_preserves_salem_hint(
    tmp_path: Path,
):
    """Salem regression: email-only batch + verb-mismatch input → user
    message hints `Same / Ditto / Same as #N` (existing behaviour).
    """
    cfg = _config(tmp_path)
    save_state(cfg.state.path, {
        "last_batch": {
            "items": [{
                "item_number": 1,
                "record_path": "note/A.md",
                "classifier_priority": "medium",
                "classifier_action_hint": None,
                "classifier_reason": "test",
                "sender": "alice@example.com",
                "subject": "x",
                "snippet": "y",
            }],
            "message_ids": [100],
        },
    })

    # "1 reject" is an attribution verb (invalid on email items).
    result = handle_daily_sync_reply(
        cfg,
        parent_message_id=100,
        reply_text="1 reject",
    )
    assert result is not None
    msg = result["message"]
    # Salem's contract — historical hint preserved.
    assert "Same" in msg
    assert "Ditto" in msg
    # Attribution verbs MUST NOT appear (would confuse Salem users).
    assert "N confirm" not in msg


# --- Verb-mismatch discriminator -------------------------------------------


def test_verb_mismatch_discriminator_catches_only_accept_pattern():
    assert _is_verb_mismatch_error(
        "item 1: attribution items only accept `confirm`/`keep`/`yes`"
    ) is True


def test_verb_mismatch_discriminator_catches_only_meaningful_pattern():
    assert _is_verb_mismatch_error(
        "item 1: `reject` is only meaningful for attribution items"
    ) is True


def test_verb_mismatch_discriminator_catches_not_meaningful_pattern():
    assert _is_verb_mismatch_error(
        "item 1: `reject` not meaningful — use `noted`"
    ) is True


def test_verb_mismatch_discriminator_rejects_scope_deny():
    """Scope-deny errors are EXECUTION failures, not verb-mismatch.

    The 2026-05-10 incident hinged on this discrimination — pre-fix,
    the scope-deny string got bucketed as if it were a parse failure.
    """
    assert _is_verb_mismatch_error(
        "item 1: couldn't create person/Ben McMillan: Scope 'kalle' "
        "may not create local 'person' records"
    ) is False


def test_verb_mismatch_discriminator_rejects_vault_path_missing():
    """vault_path-not-provided is a config-failure → execution bucket."""
    assert _is_verb_mismatch_error(
        "item 1: vault_path not provided"
    ) is False


def test_verb_mismatch_discriminator_rejects_item_not_in_batch():
    """Item-out-of-range messages don't carry a verb-mismatch marker —
    they're routed via the explicit ``not in last batch`` branch which
    still uses ``unparsed_item_numbers`` directly (see dispatch loop).
    """
    assert _is_verb_mismatch_error(
        "item 7 not in last batch"
    ) is False

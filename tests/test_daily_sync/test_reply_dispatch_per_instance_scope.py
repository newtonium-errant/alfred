"""Per-instance scope routing for canonical-proposal confirms.

Item 4 of the deferred Hypatia hardcoding sweep
(``project_hardcoding_followups.md``): the proposal-confirm path in
``_resolve_proposal_correction`` was hardcoded to ``scope="talker"``.
On a Hypatia bot with Daily Sync enabled, a confirm on a
canonical-record proposal would refuse the create at the
``talker_types_only`` gate before the Hypatia-specific
``hypatia_types_only`` rule ever ran — same shape as the b8c843d
talker dispatcher fix on the bot path.

Threading ``instance_scope`` through :func:`handle_daily_sync_reply`
→ :func:`_resolve_proposal_correction` → :func:`vault_create` makes
the proposal-confirm honour the running instance's
``config.instance.tool_set``.

The test patches ``vault_create`` with a spy so we can assert the
``scope=`` kwarg without setting up a full Hypatia vault scaffold.
That keeps the test independent of ``SCOPE_RULES`` evolution — we're
proving the threading, not re-proving scope.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.reply_dispatch import handle_daily_sync_reply
from alfred.transport.canonical_proposals import (
    Proposal,
    STATE_PENDING,
    append_proposal,
)


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    return vault


def _seed_proposal_state(
    cfg: DailySyncConfig,
    *,
    item_number: int,
    correlation_id: str,
    record_type: str,
    name: str,
) -> None:
    """Stash a proposal item into the persisted Daily Sync batch state."""
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-04-26",
            "message_ids": [100],
            "proposal_items": [{
                "item_number": item_number,
                "correlation_id": correlation_id,
                "proposer": "kal-le",
                "record_type": record_type,
                "name": name,
                "proposed_fields": {},
                "source": "kal-le observed in session",
            }],
        },
    })


def _seed_proposals_queue(
    queue_path: Path,
    *,
    correlation_id: str,
    record_type: str,
    name: str,
) -> None:
    """Append one pending proposal to the canonical-proposals JSONL queue."""
    append_proposal(
        str(queue_path),
        Proposal(
            correlation_id=correlation_id,
            ts="2026-04-26T12:00:00+00:00",
            state=STATE_PENDING,
            proposer="kal-le",
            record_type=record_type,
            name=name,
            proposed_fields={},
            source="test",
        ),
    )


def _patch_proposals_queue_path(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Override the transport-config lookup so the dispatcher uses our queue."""
    import alfred.daily_sync.reply_dispatch as rd
    monkeypatch.setattr(
        rd, "_canonical_proposals_queue_path", lambda: str(path),
    )


def _patch_vault_create_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``vault_create`` with a recording stub.

    Returns a captured-call dict the test can assert against. The stub
    returns the canonical-success shape so the dispatcher's downstream
    state-flip path runs without a real disk write.
    """
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"path": "person/Test.md", "warnings": []}

    # The dispatcher imports vault_create inside the function body, so
    # we patch the canonical module location.
    import alfred.vault.ops as ops_mod
    monkeypatch.setattr(ops_mod, "vault_create", _spy)
    return captured


# --- tests ---------------------------------------------------------------


def test_proposal_confirm_dispatches_hypatia_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A Hypatia-instance confirm dispatches ``vault_create(scope="hypatia")``.

    Mirrors the b8c843d talker dispatcher fix on the Daily Sync path:
    instead of always passing ``"talker"``, the per-instance scope is
    threaded in from ``config.instance.tool_set``.
    """
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    queue_path = tmp_path / "proposals.jsonl"
    _patch_proposals_queue_path(monkeypatch, queue_path)
    captured = _patch_vault_create_spy(monkeypatch)

    correlation_id = "kal-le-propose-person-1"
    _seed_proposals_queue(
        queue_path,
        correlation_id=correlation_id,
        record_type="person",
        name="Test Person",
    )
    _seed_proposal_state(
        cfg, item_number=1, correlation_id=correlation_id,
        record_type="person", name="Test Person",
    )

    result = handle_daily_sync_reply(
        cfg,
        parent_message_id=100,
        reply_text="1 confirm",
        vault_path=vault,
        instance_scope="hypatia",
    )
    assert result is not None
    assert result["confirmed_count"] == 1

    # The threading contract: vault_create must have been called with
    # scope="hypatia", not the legacy "talker".
    assert captured.get("scope") == "hypatia"
    assert captured.get("record_type") == "person"
    assert captured.get("name") == "Test Person"


def test_proposal_confirm_dispatches_kalle_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A KAL-LE-instance confirm dispatches ``vault_create(scope="kalle")``."""
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    queue_path = tmp_path / "proposals.jsonl"
    _patch_proposals_queue_path(monkeypatch, queue_path)
    captured = _patch_vault_create_spy(monkeypatch)

    correlation_id = "salem-propose-person-1"
    _seed_proposals_queue(
        queue_path,
        correlation_id=correlation_id,
        record_type="person",
        name="Test Person 2",
    )
    _seed_proposal_state(
        cfg, item_number=1, correlation_id=correlation_id,
        record_type="person", name="Test Person 2",
    )

    result = handle_daily_sync_reply(
        cfg,
        parent_message_id=100,
        reply_text="1 confirm",
        vault_path=vault,
        instance_scope="kalle",
    )
    assert result is not None
    assert result["confirmed_count"] == 1
    assert captured.get("scope") == "kalle"


def test_proposal_confirm_default_scope_preserves_legacy_talker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """No-keyword call keeps the historical ``"talker"`` default — Salem's
    contract is unchanged when the caller doesn't thread an instance
    scope through.
    """
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    queue_path = tmp_path / "proposals.jsonl"
    _patch_proposals_queue_path(monkeypatch, queue_path)
    captured = _patch_vault_create_spy(monkeypatch)

    correlation_id = "kal-le-propose-person-default"
    _seed_proposals_queue(
        queue_path,
        correlation_id=correlation_id,
        record_type="person",
        name="Default Person",
    )
    _seed_proposal_state(
        cfg, item_number=1, correlation_id=correlation_id,
        record_type="person", name="Default Person",
    )

    result = handle_daily_sync_reply(
        cfg,
        parent_message_id=100,
        reply_text="1 confirm",
        vault_path=vault,
    )
    assert result is not None
    assert result["confirmed_count"] == 1
    assert captured.get("scope") == "talker"

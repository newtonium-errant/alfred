"""Ticket pipeline c7 — KAL-LE→VERA outcome write-back.

Closes the loop the opposite direction from the c2/c4 link-back: after
KAL-LE's nightly effectiveness loop observes a tracked GitHub issue
reach a TERMINAL disposition, it pushes the outcome to the origin
instance (VERA) so the ORIGINATING ticket flips out of the open
worklist.

Coverage:
    * Status-mapping pin (disposition → (status, ticket_disposition)).
    * TicketOutcomeConfig loader (both roles: pusher / receiver).
    * The push from ``check_ticket_outcomes``:
        - fires once on the open→terminal transition; sets
          ``outcome_pushed_at`` (idempotency: a second pass does NOT
          re-push, and never re-queries GitHub for a latched entry).
        - a push FAILURE leaves ``outcome_pushed_at`` empty and the
          next pass retries WITHOUT re-querying GitHub.
        - the pusher disabled → no push, capture-only as before.
    * Log-emission pins (feedback_log_emission_test_pattern.md):
      ``kalle.digest.ticket_outcome_pushed`` on the push, and the ILB
      no-op ``kalle.digest.no_ticket_outcomes_to_propagate``.
    * The VERA-side resolver core
      (``resolve_ticket_outcome`` / ``find_ticket_by_uid``): applies the
      flip under the narrow scope, idempotent, not-found contract, and
      drops the ticket from VERA's open digest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.brief.kalle_digest import (
    TICKET_OUTCOME_WRITEBACK_MAP,
    check_ticket_outcomes,
)
from alfred.transport.exceptions import TransportError, TransportRejected
from alfred.transport.ticket_intake import (
    TicketIntakeEntry,
    TicketIntakeState,
    TicketOutcomeConfig,
    find_ticket_by_uid,
    load_ticket_outcome_config,
    resolve_ticket_outcome,
)
from alfred.vault.ops import vault_create, vault_read
from alfred.vault.scope import ScopeError


NOW = datetime(2026, 6, 15, 8, 30, tzinfo=timezone.utc)


def _ago(**kw: float) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _log_events(captured: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [c for c in captured if c.get("event") == event]


_TICKET_FIELDS = {
    "ticket_type": "bug",
    "reporter": "ben",
    "area": "transport-admin-portal",
}


# ---------------------------------------------------------------------------
# Status-mapping pin
# ---------------------------------------------------------------------------


def test_outcome_writeback_map_pin() -> None:
    """CONTRACT PIN: disposition → (status, ticket_disposition).

    merged_* → resolved; closed_unmerged → closed. Both statuses are
    outside VERA's OPEN_TICKET_STATUSES so either drops the ticket from
    the open worklist. Changing the mapping is a deliberate contract
    change — update this pin in the same commit.
    """
    assert TICKET_OUTCOME_WRITEBACK_MAP == {
        "merged_clean": ("resolved", "merged"),
        "merged_after_rework": ("resolved", "merged_after_rework"),
        "closed_unmerged": ("closed", "closed_no_merge"),
    }
    # Every terminal disposition has a mapping (no terminal flip can fall
    # through unmapped).
    from alfred.brief.kalle_digest import TICKET_TERMINAL_DISPOSITIONS

    assert set(TICKET_OUTCOME_WRITEBACK_MAP) == set(TICKET_TERMINAL_DISPOSITIONS)


# ---------------------------------------------------------------------------
# TicketOutcomeConfig loader
# ---------------------------------------------------------------------------


def test_outcome_config_absent_section_all_disabled() -> None:
    cfg = load_ticket_outcome_config({})
    assert cfg.enabled is False
    assert cfg.receiver_enabled is False
    assert cfg.self_name == ""
    assert cfg.target_peer == "vera"


def test_outcome_config_pusher_role() -> None:
    cfg = load_ticket_outcome_config({
        "ticket_outcome": {
            "enabled": True,
            "self_name": "kal-le",
            "target_peer": "vera",
        },
    })
    assert cfg.enabled is True
    assert cfg.receiver_enabled is False
    assert cfg.self_name == "kal-le"
    assert cfg.target_peer == "vera"


def test_outcome_config_receiver_role() -> None:
    cfg = load_ticket_outcome_config({
        "ticket_outcome": {"receiver_enabled": True},
    })
    assert cfg.receiver_enabled is True
    assert cfg.enabled is False


def test_outcome_config_malformed_section_is_default() -> None:
    cfg = load_ticket_outcome_config({"ticket_outcome": "not-a-dict"})
    assert cfg == TicketOutcomeConfig()


# ---------------------------------------------------------------------------
# State schema-tolerance — the new outcome_pushed_at field
# ---------------------------------------------------------------------------


def test_outcome_pushed_at_round_trips(tmp_path: Path) -> None:
    """The c7 idempotency flag persists + reloads (schema-tolerance
    contract, per CLAUDE.md state load() rule)."""
    state = TicketIntakeState(path=tmp_path / "s.json")
    state.entries["uid-1"] = TicketIntakeEntry(
        issue_number=7, disposition="merged_clean",
        outcome_pushed_at="2026-06-15T08:30:00+00:00",
    )
    state.save()
    reloaded = TicketIntakeState.load(state.path)
    assert reloaded.entries["uid-1"].outcome_pushed_at == "2026-06-15T08:30:00+00:00"


def test_outcome_pushed_at_defaults_empty_on_old_state(tmp_path: Path) -> None:
    """A state file written before the field existed (no outcome_pushed_at
    key) loads with the empty default — backward-safe."""
    import json

    p = tmp_path / "old.json"
    p.write_text(
        json.dumps({"entries": {"uid-1": {
            "issue_number": 7, "disposition": "merged_clean",
        }}}),
        encoding="utf-8",
    )
    reloaded = TicketIntakeState.load(p)
    assert reloaded.entries["uid-1"].outcome_pushed_at == ""


# ---------------------------------------------------------------------------
# Push from check_ticket_outcomes — the trigger + idempotency
# ---------------------------------------------------------------------------


class _RecordingSender:
    """Captures peer_send_ticket_outcome calls; configurable ack/raise."""

    def __init__(
        self,
        *,
        ack: dict[str, Any] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.ack = ack if ack is not None else {"applied": True, "relpath": "ticket/A.md"}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, peer_name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"peer_name": peer_name, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.ack


class _NoCheckClient:
    """GitHub client that MUST NOT be called for a terminal entry.

    A terminal entry's push must never re-query GitHub (the disposition
    is already final). Any method call fails the test loudly.
    """

    class _Cfg:
        repo = "acme/site"

    config = _Cfg()

    async def issue_timeline(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("terminal entry must not re-query GitHub")

    async def pr_get(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("terminal entry must not re-query GitHub")

    async def pr_reviews(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("terminal entry must not re-query GitHub")


def _terminal_state(tmp_path: Path, *, pushed: str = "") -> TicketIntakeState:
    state = TicketIntakeState(path=tmp_path / "ticket_intake_state.json")
    state.entries["vera-20260613-6ca5b92f"] = TicketIntakeEntry(
        recorded_at=_ago(days=2),
        kalle_relpath="ticket/A.md",
        issue_number=7,
        issue_created_at=_ago(days=2),
        ticket_type="bug",
        pr_number=8,
        pr_state="merged",
        disposition="merged_clean",
        outcome_checked_at=_ago(hours=1),
        outcome_pushed_at=pushed,
    )
    return state


def _pusher_config() -> TicketOutcomeConfig:
    return TicketOutcomeConfig(
        enabled=True, self_name="kal-le", target_peer="vera",
    )


async def test_push_fires_on_terminal_and_sets_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal-but-unpushed entry pushes once; outcome_pushed_at set;
    GitHub is never re-queried."""
    sender = _RecordingSender()
    monkeypatch.setattr(
        "alfred.transport.client.peer_send_ticket_outcome", sender,
    )
    state = _terminal_state(tmp_path)
    audit = tmp_path / "audit.jsonl"
    with structlog.testing.capture_logs() as captured:
        await check_ticket_outcomes(
            state, _NoCheckClient(), now=NOW, audit_log_path=str(audit),
            outcome_config=_pusher_config(),
            transport_config=object(),  # opaque — sender is monkeypatched
        )
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["peer_name"] == "vera"
    assert call["ticket_uid"] == "vera-20260613-6ca5b92f"
    assert call["status"] == "resolved"
    assert call["disposition"] == "merged"
    assert call["pr_number"] == 8
    assert call["self_name"] == "kal-le"
    # Flag set + persisted.
    entry = state.entries["vera-20260613-6ca5b92f"]
    assert entry.outcome_pushed_at != ""
    reloaded = TicketIntakeState.load(state.path)
    assert reloaded.entries["vera-20260613-6ca5b92f"].outcome_pushed_at != ""
    # Log-emission pin.
    pushed = _log_events(captured, "kalle.digest.ticket_outcome_pushed")
    assert len(pushed) == 1
    assert pushed[0]["status"] == "resolved"
    assert pushed[0]["disposition"] == "merged"
    assert pushed[0]["target_peer"] == "vera"
    assert pushed[0]["applied"] is True


async def test_push_idempotent_already_pushed_no_repush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-pushed terminal entry is fully latched: no re-push, no
    GitHub re-query, and the ILB no-op line fires."""
    sender = _RecordingSender()
    monkeypatch.setattr(
        "alfred.transport.client.peer_send_ticket_outcome", sender,
    )
    state = _terminal_state(tmp_path, pushed=_ago(days=1))
    audit = tmp_path / "audit.jsonl"
    with structlog.testing.capture_logs() as captured:
        await check_ticket_outcomes(
            state, _NoCheckClient(), now=NOW, audit_log_path=str(audit),
            outcome_config=_pusher_config(), transport_config=object(),
        )
    assert sender.calls == []  # never re-pushed
    noop = _log_events(captured, "kalle.digest.no_ticket_outcomes_to_propagate")
    assert len(noop) == 1
    assert noop[0]["checked"] == 1


async def test_push_failure_leaves_flag_empty_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push that raises (transport down / unknown peer) leaves
    outcome_pushed_at empty so the next pass retries — and never
    crashes the digest."""
    sender = _RecordingSender(
        raises=TransportRejected("nope", status_code=502, body="upstream"),
    )
    monkeypatch.setattr(
        "alfred.transport.client.peer_send_ticket_outcome", sender,
    )
    state = _terminal_state(tmp_path)
    audit = tmp_path / "audit.jsonl"
    with structlog.testing.capture_logs() as captured:
        # Must not raise.
        await check_ticket_outcomes(
            state, _NoCheckClient(), now=NOW, audit_log_path=str(audit),
            outcome_config=_pusher_config(), transport_config=object(),
        )
    assert len(sender.calls) == 1
    entry = state.entries["vera-20260613-6ca5b92f"]
    assert entry.outcome_pushed_at == ""  # retry next pass
    failed = _log_events(captured, "kalle.digest.ticket_outcome_push_failed")
    assert len(failed) == 1
    assert failed[0]["http_status"] == 502

    # Second pass with a healthy sender now succeeds (the retry).
    sender2 = _RecordingSender()
    monkeypatch.setattr(
        "alfred.transport.client.peer_send_ticket_outcome", sender2,
    )
    await check_ticket_outcomes(
        state, _NoCheckClient(), now=NOW, audit_log_path=str(audit),
        outcome_config=_pusher_config(), transport_config=object(),
    )
    assert len(sender2.calls) == 1
    assert state.entries["vera-20260613-6ca5b92f"].outcome_pushed_at != ""


async def test_push_unknown_peer_transport_error_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_peer raises TransportError for an unconfigured peer —
    contained, flag stays empty, digest survives."""
    sender = _RecordingSender(raises=TransportError("peer 'vera' not configured"))
    monkeypatch.setattr(
        "alfred.transport.client.peer_send_ticket_outcome", sender,
    )
    state = _terminal_state(tmp_path)
    await check_ticket_outcomes(
        state, _NoCheckClient(), now=NOW, audit_log_path=str(tmp_path / "a.jsonl"),
        outcome_config=_pusher_config(), transport_config=object(),
    )
    assert state.entries["vera-20260613-6ca5b92f"].outcome_pushed_at == ""


async def test_push_disabled_no_push_no_noop_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pusher disabled → no push attempt, and the no-op ILB line does
    NOT fire (the no-op line is gated on push_enabled)."""
    sender = _RecordingSender()
    monkeypatch.setattr(
        "alfred.transport.client.peer_send_ticket_outcome", sender,
    )
    state = _terminal_state(tmp_path)
    with structlog.testing.capture_logs() as captured:
        await check_ticket_outcomes(
            state, _NoCheckClient(), now=NOW,
            audit_log_path=str(tmp_path / "a.jsonl"),
            outcome_config=TicketOutcomeConfig(enabled=False),
            transport_config=object(),
        )
    assert sender.calls == []
    assert _log_events(captured, "kalle.digest.no_ticket_outcomes_to_propagate") == []


async def test_push_no_self_name_fails_loud_no_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enabled but self_name empty → fail-loud log, NO push (never
    default the sender identity, per feedback_hardcoding_and_alfred_naming)."""
    sender = _RecordingSender()
    monkeypatch.setattr(
        "alfred.transport.client.peer_send_ticket_outcome", sender,
    )
    state = _terminal_state(tmp_path)
    with structlog.testing.capture_logs() as captured:
        await check_ticket_outcomes(
            state, _NoCheckClient(), now=NOW,
            audit_log_path=str(tmp_path / "a.jsonl"),
            outcome_config=TicketOutcomeConfig(enabled=True, self_name=""),
            transport_config=object(),
        )
    assert sender.calls == []
    assert _log_events(captured, "kalle.digest.ticket_outcome_push_no_self_name")


# ---------------------------------------------------------------------------
# VERA-side resolver core
# ---------------------------------------------------------------------------


def _make_open_ticket(tmp_path: Path, uid: str) -> str:
    result = vault_create(
        tmp_path, "ticket", "Strip debug console logging",
        set_fields={**dict(_TICKET_FIELDS), "status": "open", "ticket_uid": uid},
        scope="kalle",
    )
    return result["path"]


def test_find_ticket_by_uid(tmp_path: Path) -> None:
    rel = _make_open_ticket(tmp_path, "vera-20260613-6ca5b92f")
    found = find_ticket_by_uid(tmp_path, "vera-20260613-6ca5b92f")
    assert found == rel
    assert find_ticket_by_uid(tmp_path, "vera-nope") is None


def test_find_ticket_by_uid_no_ticket_dir(tmp_path: Path) -> None:
    assert find_ticket_by_uid(tmp_path, "vera-x") is None


def test_resolver_applies_flip(tmp_path: Path) -> None:
    """The resolver flips status + writes the disposition fields under
    the narrow scope; the ticket leaves the open set."""
    uid = "vera-20260613-6ca5b92f"
    _make_open_ticket(tmp_path, uid)
    out = resolve_ticket_outcome(
        tmp_path,
        ticket_uid=uid,
        status="resolved",
        disposition="merged",
        pr_number=8,
        resolved_at="2026-06-15T12:00:00+00:00",
    )
    assert out["found"] is True
    assert out["applied"] is True
    fm = vault_read(tmp_path, out["relpath"])["frontmatter"]
    assert fm["status"] == "resolved"
    assert fm["ticket_disposition"] == "merged"
    assert fm["resolved_at"] == "2026-06-15T12:00:00+00:00"
    assert fm["github_pr"] == 8


def test_resolver_idempotent_already_resolved(tmp_path: Path) -> None:
    uid = "vera-20260613-6ca5b92f"
    _make_open_ticket(tmp_path, uid)
    resolve_ticket_outcome(
        tmp_path, ticket_uid=uid, status="resolved", disposition="merged",
    )
    # Re-apply — harmless, still applied=True.
    out = resolve_ticket_outcome(
        tmp_path, ticket_uid=uid, status="resolved", disposition="merged",
    )
    assert out["applied"] is True
    fm = vault_read(tmp_path, out["relpath"])["frontmatter"]
    assert fm["status"] == "resolved"


def test_resolver_not_found_contract(tmp_path: Path) -> None:
    """No ticket with the uid → {found: False} (handler maps to 404).
    No write happens."""
    _make_open_ticket(tmp_path, "vera-other")
    out = resolve_ticket_outcome(
        tmp_path, ticket_uid="vera-missing", status="resolved",
        disposition="merged",
    )
    assert out == {"found": False}


def test_resolver_closed_disposition(tmp_path: Path) -> None:
    uid = "vera-closed-1"
    _make_open_ticket(tmp_path, uid)
    out = resolve_ticket_outcome(
        tmp_path, ticket_uid=uid, status="closed",
        disposition="closed_no_merge",
    )
    fm = vault_read(tmp_path, out["relpath"])["frontmatter"]
    assert fm["status"] == "closed"
    assert fm["ticket_disposition"] == "closed_no_merge"


def test_resolver_drops_ticket_from_vera_open_digest(tmp_path: Path) -> None:
    """End-to-end with the VERA digest: a resolved ticket no longer
    appears in the open-ticket scan."""
    from alfred.brief.vera_ticket_digest import _scan_open_tickets

    uid = "vera-20260613-6ca5b92f"
    _make_open_ticket(tmp_path, uid)
    assert len(_scan_open_tickets(tmp_path)) == 1  # open before
    resolve_ticket_outcome(
        tmp_path, ticket_uid=uid, status="resolved", disposition="merged",
    )
    assert _scan_open_tickets(tmp_path) == []  # gone after


def test_resolver_optional_fields_omitted(tmp_path: Path) -> None:
    """No pr_number / resolved_at → only status + disposition written
    (the optional fields are absent, not null)."""
    uid = "vera-min"
    _make_open_ticket(tmp_path, uid)
    out = resolve_ticket_outcome(
        tmp_path, ticket_uid=uid, status="resolved", disposition="merged",
    )
    fm = vault_read(tmp_path, out["relpath"])["frontmatter"]
    assert fm["status"] == "resolved"
    assert "github_pr" not in fm
    assert "resolved_at" not in fm

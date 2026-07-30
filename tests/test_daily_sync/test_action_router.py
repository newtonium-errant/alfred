"""Feed action router — the deck/feed decision path onto the SAME resolvers.

Heavy-gate pins for Feed Phase B slice B1:

  * Per-kind act → store-mutation against the REAL resolver paths for all five
    routed families (email_tier / attribution / proposal / routine_match /
    pending) — no resolver mocks. The store mutation asserted is the resolver's
    OWN durable write (corpus row / vault marker flip / proposal-queue flip /
    pending-queue resolve), plus the feed item flips to ``acted``.
  * already_acted ok-noop (folded-state check fires first; the resolver never
    re-runs on a non-open item).
  * stale_item — id absent from the store, AND open item aged out of last_batch.
  * invalid_action — unmapped (kind, action), and ``reject`` on an email item.
  * ack — FYI item → acked directly (no resolver); ack on a decide item → invalid.
  * Capability ceiling — a crafted (kind, action) can never reach a resolver or a
    vault op: the store stays untouched.
  * Item source — the router works off last_batch, NEVER the feed item's
    display evidence (evidence deliberately empty → act still succeeds).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.daily_sync import action_router as arouter
from alfred.daily_sync import reply_dispatch as rd
from alfred.daily_sync.action_router import (
    STATUS_ACKED,
    STATUS_ACTED,
    STATUS_ALREADY_ACTED,
    STATUS_INVALID_ACTION,
    STATUS_STALE_ITEM,
    act,
)
from alfred.daily_sync.config import AttributionConfig, DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.corpus import iter_corrections
from alfred.daily_sync.feed_producer import _FAMILIES, build_feed_items
from alfred.feed import FeedStore
from alfred.feed.model import STATE_ACKED, STATE_ACTED, STATE_OPEN, FeedItem, make_id
from alfred.vault.attribution import (
    AuditEntry,
    append_audit_entry,
    parse_audit_entries,
)

import frontmatter


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _ds_config(tmp_path: Path) -> DailySyncConfig:
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


def _store(tmp_path: Path) -> FeedStore:
    return FeedStore(str(tmp_path / "feed.jsonl"))


def _publish(store: FeedStore, kind: str, item: dict[str, Any], instance: str = "salem") -> str:
    """Upsert the feed item for ``item`` EXACTLY as the producer would, and
    return its id. Guarantees the router re-derives the same id from last_batch."""
    fis = build_feed_items(kind, [item], instance)
    assert len(fis) == 1, "item must be stably keyable"
    store.upsert(fis[0])
    return fis[0].id


def _feed_id(kind: str, item: dict[str, Any]) -> str:
    return make_id(kind, _FAMILIES[kind][0](item))


def _seed_batch(cfg: DailySyncConfig, **families: Any) -> None:
    payload: dict[str, Any] = {"date": "2026-07-30", "message_ids": [100]}
    payload.update(families)
    save_state(cfg.state.path, {"last_batch": payload})


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    (vault / "person").mkdir(parents=True)
    return vault


# --- per-family item builders (shapes mirror the existing dispatcher tests) --


def _email_item(num: int = 1, *, priority: str = "medium") -> dict[str, Any]:
    return {
        "item_number": num,
        "record_path": f"note/Email{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": f"sender{num}@example.com",
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def _attribution_item(num: int, marker_id: str, *, record_path: str) -> dict[str, Any]:
    return {
        "item_number": num,
        "record_path": record_path,
        "marker_id": marker_id,
        "agent": "salem",
        "date": "2026-07-30T18:44:00+00:00",
        "section_title": "Test Section",
        "reason": "talker conversation turn (session=abc123)",
        "content_preview": "Wrapped content preview text.",
    }


def _proposal_item(num: int, *, correlation_id: str, record_type: str, name: str) -> dict[str, Any]:
    return {
        "item_number": num,
        "correlation_id": correlation_id,
        "proposer": "kal-le",
        "record_type": record_type,
        "name": name,
        "proposed_fields": {},
        "source": "kal-le observed in session",
    }


def _routine_match_item(num: int, *, query: str, matched_to: str, record: str = "Daily") -> dict[str, Any]:
    return {
        "item_number": num,
        "query": query,
        "matched_to": matched_to,
        "record": record,
        "confidence": 0.4,
        "completion_date": "2026-07-29",
        "captured_at": "2026-07-29T09:00:00+00:00",
    }


def _pending_item(num: int, *, item_id: str, created_by: str = "salem") -> dict[str, Any]:
    return {
        "item_number": num,
        "id": item_id,
        "category": "outbound_failure",
        "created_by_instance": created_by,
        "session_id": "abc",
        "context": "a queued note",
        "resolution_options": [
            {"id": "noted", "label": "Noted, no action"},
            {"id": "show_me", "label": "Show me the text"},
        ],
    }


def _seed_attr_record(vault: Path, rel_path: str, *, marker_id: str) -> None:
    fm: dict[str, Any] = {"type": "note", "name": rel_path.removesuffix(".md").rsplit("/", 1)[-1]}
    append_audit_entry(fm, AuditEntry(
        marker_id=marker_id,
        agent="salem",
        date="2026-07-30T18:44:00+00:00",
        section_title="Test Section",
        reason="talker conversation turn",
        confirmed_by_andrew=False,
        confirmed_at=None,
    ))
    body = (
        f'<!-- BEGIN_INFERRED marker_id="{marker_id}" -->\n'
        f"wrapped content body\n"
        f'<!-- END_INFERRED marker_id="{marker_id}" -->'
    )
    (vault / rel_path).write_text(
        frontmatter.dumps(frontmatter.Post(body, **fm)) + "\n", encoding="utf-8",
    )


def _read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _call(store: FeedStore, cfg: DailySyncConfig, feed_id: str, action_id: str, **kw: Any):
    """act() with the router's non-store args defaulted for a Salem instance."""
    return act(
        feed_id,
        action_id,
        feed_store=store,
        config=cfg,
        vault_path=kw.get("vault_path"),
        instance_name=kw.get("instance_name", "salem"),
        instance_scope=kw.get("instance_scope", "talker"),
        raw_config=kw.get("raw_config"),
    )


# ---------------------------------------------------------------------------
# email_tier — real corpus write
# ---------------------------------------------------------------------------


def test_email_confirm_writes_corpus_and_acts(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    result = _call(store, cfg, fid, "confirm")

    assert result.ok is True
    assert result.status == STATUS_ACTED
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    assert rows[0].andrew_priority == "medium"  # confirm → classifier priority
    assert rows[0].record_path == "note/Email1.md"
    assert store.load()[fid].state == STATE_ACTED


def test_email_high_sets_explicit_tier(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="low")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    result = _call(store, cfg, fid, "high")

    assert result.ok and result.status == STATUS_ACTED
    rows = list(iter_corrections(cfg.corpus.path))
    assert rows[0].andrew_priority == "high"
    assert "high" in result.detail


def test_email_down_modifier_lowers_tier(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    result = _call(store, cfg, fid, "down")

    assert result.ok and result.status == STATUS_ACTED
    rows = list(iter_corrections(cfg.corpus.path))
    assert rows[0].andrew_priority == "low"  # medium → down → low


# ---------------------------------------------------------------------------
# attribution — real vault marker flip + corpus row
# ---------------------------------------------------------------------------


def test_attribution_confirm_flips_marker_and_acts(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_attr_record(vault, "note/A.md", marker_id="inf-x-1")
    item = _attribution_item(6, "inf-x-1", record_path="note/A.md")
    fid = _publish(store, "attribution", item)
    _seed_batch(cfg, attribution_items=[item])

    result = _call(store, cfg, fid, "confirm", vault_path=vault)

    assert result.ok and result.status == STATUS_ACTED
    post = frontmatter.load(str(vault / "note/A.md"))
    entries = parse_audit_entries(post.metadata)
    assert entries[0].confirmed_by_andrew is True
    rows = _read_jsonl(cfg.attribution.corpus_path)
    assert len(rows) == 1 and rows[0]["andrew_action"] == "confirm"
    assert store.load()[fid].state == STATE_ACTED


def test_attribution_reject_writes_corpus_and_acts(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_attr_record(vault, "note/B.md", marker_id="inf-x-2")
    item = _attribution_item(6, "inf-x-2", record_path="note/B.md")
    fid = _publish(store, "attribution", item)
    _seed_batch(cfg, attribution_items=[item])

    result = _call(store, cfg, fid, "reject", vault_path=vault)

    assert result.ok and result.status == STATUS_ACTED
    rows = _read_jsonl(cfg.attribution.corpus_path)
    assert len(rows) == 1 and rows[0]["andrew_action"] == "reject"
    assert store.load()[fid].state == STATE_ACTED


def test_attribution_without_vault_path_is_error(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _attribution_item(6, "inf-x-3", record_path="note/C.md")
    fid = _publish(store, "attribution", item)
    _seed_batch(cfg, attribution_items=[item])

    result = _call(store, cfg, fid, "confirm", vault_path=None)

    assert result.ok is False
    assert result.status == "error"
    assert "vault not configured" in result.detail
    assert store.load()[fid].state == STATE_OPEN  # not acted on failure


# ---------------------------------------------------------------------------
# proposal — real queue flip (reject) + confirm routing (vault_create spy)
# ---------------------------------------------------------------------------


def _seed_proposals_queue(queue_path: Path, *, correlation_id: str, record_type: str, name: str) -> None:
    from alfred.transport.canonical_proposals import (
        STATE_PENDING,
        Proposal,
        append_proposal,
    )
    append_proposal(str(queue_path), Proposal(
        correlation_id=correlation_id,
        ts="2026-07-30T12:00:00+00:00",
        state=STATE_PENDING,
        proposer="kal-le",
        record_type=record_type,
        name=name,
        proposed_fields={},
        source="test",
    ))


def _queue_state(queue_path: Path, correlation_id: str) -> str | None:
    rows = _read_jsonl(queue_path)
    match = [r for r in rows if r.get("correlation_id") == correlation_id]
    return match[-1].get("state") if match else None


def test_proposal_reject_flips_queue_and_acts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    queue_path = tmp_path / "proposals.jsonl"
    cid = "propose-person-1"
    _seed_proposals_queue(queue_path, correlation_id=cid, record_type="person", name="Test Person")
    monkeypatch.setattr(rd, "_canonical_proposals_queue_path", lambda *a, **kw: str(queue_path))

    item = _proposal_item(1, correlation_id=cid, record_type="person", name="Test Person")
    fid = _publish(store, "proposal", item)
    _seed_batch(cfg, proposal_items=[item])

    result = _call(store, cfg, fid, "reject", vault_path=tmp_path / "vault")

    assert result.ok and result.status == STATUS_ACTED
    assert _queue_state(queue_path, cid) == "rejected"
    assert store.load()[fid].state == STATE_ACTED


def test_proposal_confirm_creates_record_and_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    vault = _make_vault(tmp_path)
    queue_path = tmp_path / "proposals.jsonl"
    cid = "propose-person-2"
    _seed_proposals_queue(queue_path, correlation_id=cid, record_type="person", name="Test Person")
    monkeypatch.setattr(rd, "_canonical_proposals_queue_path", lambda *a, **kw: str(queue_path))

    # Spy on the downstream vault op (not the resolver) so the confirm path
    # runs without a real disk write; assert the scope was threaded through.
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"path": "person/Test Person.md", "warnings": []}

    import alfred.vault.ops as ops_mod
    monkeypatch.setattr(ops_mod, "vault_create", _spy)

    item = _proposal_item(1, correlation_id=cid, record_type="person", name="Test Person")
    fid = _publish(store, "proposal", item)
    _seed_batch(cfg, proposal_items=[item])

    result = _call(store, cfg, fid, "confirm", vault_path=vault, instance_scope="hypatia")

    assert result.ok and result.status == STATUS_ACTED
    assert captured.get("scope") == "hypatia"  # instance scope threaded to vault_create
    assert _queue_state(queue_path, cid) == "accepted"
    assert store.load()[fid].state == STATE_ACTED


def test_proposal_queue_unconfigured_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    monkeypatch.setattr(rd, "_canonical_proposals_queue_path", lambda *a, **kw: None)
    item = _proposal_item(1, correlation_id="cid-x", record_type="person", name="Nobody")
    fid = _publish(store, "proposal", item)
    _seed_batch(cfg, proposal_items=[item])

    result = _call(store, cfg, fid, "confirm", vault_path=tmp_path / "vault")

    assert result.ok is False and result.status == "error"
    assert "not configured" in result.detail
    assert store.load()[fid].state == STATE_OPEN


# ---------------------------------------------------------------------------
# routine_match — real glossary corpus write
# ---------------------------------------------------------------------------


def test_routine_match_confirm_writes_corpus_and_acts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alfred.routine import match_calibration as mc

    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    corpus = tmp_path / "routine_match_corpus.jsonl"
    monkeypatch.setattr(rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus))

    item = _routine_match_item(1, query="walk doggo", matched_to="Walk dog")
    fid = _publish(store, "routine_match", item)
    _seed_batch(cfg, routine_match_items=[item])

    result = _call(store, cfg, fid, "confirm")

    assert result.ok and result.status == STATUS_ACTED
    rows = _read_jsonl(corpus)
    assert len(rows) == 1
    assert rows[0]["type"] == mc.CORPUS_CONFIRM
    assert rows[0]["query_key"] == mc.query_key("walk doggo")
    assert store.load()[fid].state == STATE_ACTED


def test_routine_match_reject_writes_reject_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alfred.routine import match_calibration as mc

    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    corpus = tmp_path / "routine_match_corpus.jsonl"
    monkeypatch.setattr(rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus))

    item = _routine_match_item(1, query="walk doggo", matched_to="Walk dog")
    fid = _publish(store, "routine_match", item)
    _seed_batch(cfg, routine_match_items=[item])

    result = _call(store, cfg, fid, "reject")

    assert result.ok and result.status == STATUS_ACTED
    rows = _read_jsonl(corpus)
    assert rows[0]["type"] == mc.CORPUS_REJECT


# ---------------------------------------------------------------------------
# pending — real local queue resolution
# ---------------------------------------------------------------------------


def _seed_pending_queue(queue_path: Path, *, item_id: str, created_by: str = "salem") -> None:
    from alfred.pending_items.queue import PendingItem, ResolutionOption, append_item

    append_item(str(queue_path), PendingItem(
        id=item_id,
        category="outbound_failure",
        created_at="2026-07-30T09:00:00+00:00",
        created_by_instance=created_by,
        session_id="abc",
        context="a queued note",
        resolution_options=[
            ResolutionOption(id="noted", label="Noted, no action", action_plan=None),
        ],
    ))


def test_pending_noted_resolves_queue_and_acts(tmp_path: Path) -> None:
    from alfred.pending_items.queue import STATUS_RESOLVED, find_by_id

    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    queue_path = tmp_path / "pending_items.jsonl"
    _seed_pending_queue(queue_path, item_id="item-1")
    raw_config = {
        "pending_items": {"enabled": True, "queue_path": str(queue_path)},
        "vault": {"path": str(tmp_path / "vault")},
    }

    item = _pending_item(1, item_id="item-1")
    fid = _publish(store, "pending", item)
    _seed_batch(cfg, pending_items=[item])

    result = _call(store, cfg, fid, "noted", instance_name="salem", raw_config=raw_config)

    assert result.ok and result.status == STATUS_ACTED
    resolved = find_by_id(queue_path, "item-1")
    assert resolved is not None
    assert resolved.status == STATUS_RESOLVED
    assert resolved.resolution == "noted"
    assert store.load()[fid].state == STATE_ACTED


def test_pending_empty_instance_name_is_clean_error(tmp_path: Path) -> None:
    """Pre-guard preserves the fail-loud-on-empty-identity contract as a clean
    error (never silently route as Salem) — no crash out of the handler."""
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _pending_item(1, item_id="item-9")
    fid = _publish(store, "pending", item)
    _seed_batch(cfg, pending_items=[item])

    result = _call(store, cfg, fid, "noted", instance_name="", raw_config={})

    assert result.ok is False and result.status == "error"
    assert "instance identity not configured" in result.detail
    assert store.load()[fid].state == STATE_OPEN


# ---------------------------------------------------------------------------
# already_acted / stale_item
# ---------------------------------------------------------------------------


def test_already_acted_is_ok_noop_and_skips_resolver(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])
    store.set_state(fid, STATE_ACTED)  # already decided elsewhere

    result = _call(store, cfg, fid, "high")

    assert result.ok is True
    assert result.status == STATUS_ALREADY_ACTED
    # Resolver did NOT run — no corpus row written.
    assert list(iter_corrections(cfg.corpus.path)) == []


def test_stale_item_not_in_store(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    result = _call(store, cfg, "email_tier:note/Ghost.md", "confirm")
    assert result.ok is False
    assert result.status == STATUS_STALE_ITEM


def test_stale_item_aged_out_of_last_batch(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[])  # batch moved on — item absent

    result = _call(store, cfg, fid, "confirm")

    assert result.ok is False
    assert result.status == STATUS_STALE_ITEM
    assert list(iter_corrections(cfg.corpus.path)) == []  # no mutation


# ---------------------------------------------------------------------------
# invalid_action / capability ceiling
# ---------------------------------------------------------------------------


def test_reject_on_email_is_invalid_action(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    result = _call(store, cfg, fid, "reject")  # reject is not a mapped email action

    assert result.ok is False and result.status == STATUS_INVALID_ACTION
    assert list(iter_corrections(cfg.corpus.path)) == []
    assert store.load()[fid].state == STATE_OPEN


def test_unknown_action_is_invalid(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    result = _call(store, cfg, fid, "obliterate")

    assert result.ok is False and result.status == STATUS_INVALID_ACTION


def test_missing_id_or_action_is_invalid(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    assert _call(store, cfg, "", "confirm").status == STATUS_INVALID_ACTION
    assert _call(store, cfg, "email_tier:x", "").status == STATUS_INVALID_ACTION


def test_capability_ceiling_crafted_action_never_reaches_resolver(tmp_path: Path) -> None:
    """A crafted (kind, action) on a real open decide item is refused BEFORE any
    resolver / vault op runs — the corpus stays untouched."""
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    # A mapped-looking-but-wrong action (attribution's verb on an email item).
    result = _call(store, cfg, fid, "confirm_marker")

    assert result.status == STATUS_INVALID_ACTION
    assert list(iter_corrections(cfg.corpus.path)) == []
    assert store.load()[fid].state == STATE_OPEN


def test_capability_ceiling_ack_cannot_drive_a_decide_resolver(tmp_path: Path) -> None:
    """'ack' on a decide item is invalid — it can never be a backdoor to acking
    (and thus skipping) an item that demands a real decision."""
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    result = _call(store, cfg, fid, "ack")

    assert result.ok is False and result.status == STATUS_INVALID_ACTION
    assert store.load()[fid].state == STATE_OPEN


# ---------------------------------------------------------------------------
# ack — FYI items
# ---------------------------------------------------------------------------


def test_fyi_kind_cannot_reach_a_decide_resolver(tmp_path: Path) -> None:
    """Ceiling: a FYI kind (radar) supports ONLY 'ack' — a decide verb like
    'confirm' is invalid and can never route to a resolver."""
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    radar = {"record_path": "radar/thing.md", "record_type": "note", "event_id": "r1"}
    fid = _publish(store, "radar", radar)

    result = _call(store, cfg, fid, "confirm")

    assert result.ok is False and result.status == STATUS_INVALID_ACTION
    assert store.load()[fid].state == STATE_OPEN


def test_ack_fyi_item_sets_acked(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    # radar is a FYI kind (mode=fyi) — publish one via the producer.
    radar = {"record_path": "radar/thing.md", "record_type": "note", "event_id": "r1"}
    fid = _publish(store, "radar", radar)
    assert store.load()[fid].mode == "fyi"

    result = _call(store, cfg, fid, "ack")

    assert result.ok is True and result.status == STATUS_ACKED
    assert store.load()[fid].state == STATE_ACKED


# ---------------------------------------------------------------------------
# item source — last_batch, NOT feed evidence
# ---------------------------------------------------------------------------


def test_router_uses_last_batch_not_feed_evidence(tmp_path: Path) -> None:
    """Evidence is display-only. With EMPTY evidence on the stored feed item but
    a correct last_batch entry, the act still resolves against the authoritative
    batch item."""
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    # Stored feed item carries the right id but deliberately-empty evidence.
    fid = _feed_id("email_tier", item)
    store.upsert(FeedItem.create(
        kind="email_tier", stable_key=_FAMILIES["email_tier"][0](item),
        instance="salem", title="opaque", evidence={},
    ))
    assert store.load()[fid].evidence == {}
    _seed_batch(cfg, items=[item])

    result = _call(store, cfg, fid, "confirm")

    assert result.ok and result.status == STATUS_ACTED
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1 and rows[0].record_path == "note/Email1.md"


# ---------------------------------------------------------------------------
# observability pins (feedback_log_emission_test_pattern)
# ---------------------------------------------------------------------------


def test_acted_log_emitted_with_fields(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    with structlog.testing.capture_logs() as cap:
        _call(store, cfg, fid, "confirm")

    matches = [c for c in cap if c.get("event") == "feed.act.acted"]
    assert len(matches) == 1
    assert matches[0]["id"] == fid
    assert matches[0]["kind"] == "email_tier"
    assert matches[0]["action"] == "confirm"


def test_invalid_action_log_emitted(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    item = _email_item(priority="medium")
    fid = _publish(store, "email_tier", item)
    _seed_batch(cfg, items=[item])

    with structlog.testing.capture_logs() as cap:
        _call(store, cfg, fid, "reject")

    matches = [c for c in cap if c.get("event") == "feed.act.invalid_action"]
    assert len(matches) == 1
    assert matches[0]["kind"] == "email_tier"
    assert matches[0]["action"] == "reject"

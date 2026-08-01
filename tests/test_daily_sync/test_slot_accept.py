"""Board ACCEPT path — slot_suggestion day-planning router (Phase C slice 2).

Heavy-gate pins for the board's ``accept`` on auto-surfaced slot candidates.
The slice commits a candidate INTO the daily ``tier_curation`` block from the
WEB path via the deterministic ``tier.tier_confirm`` writer — so the gates here
run against the REAL writer (no writer mocks), per design §5:

  * Per-tier accept → REAL writer:
      - T1 task → ``T1T2Entry(task=[[task/<name>]], confirmed=True)`` preserving
        the auto ``source``; the feed item flips ``acted``; the act carries a
        committed-render payload.
      - T1/T2 routine → ``T1T2Entry(routine_item={record,text})`` (T1 confirmed;
        T2 ``source="operator"``, no confirmed).
      - T3 (routine self-care / cadence / self-care task) → free-text
        ``T3Entry(item=..., source="operator")``.
  * Idempotent re-accept (one entry; second is an ok-noop).
  * Folded-state already_acted (accepting an already-acted candidate).
  * Committed-never-accepts — the provenance guard, MUTATION-VERIFIED
    (output-bound: the daily file is byte-unchanged, not merely a call-count).
  * Output-bound: after the writer runs the producer's OWN re-read sees the item
    committed (``candidate=False``) — the writer's OUTPUT binds to what the
    producer reads.
  * Reconcile (Fork B): accept → acted → next emit re-emits committed → reconcile
    revives the SAME episode (task lane, name==stem) as open+committed.
  * Identity pin — the board's accept calls the SAME ``confirm_slot_candidate``
    object (task #21 will route the talker through it too).
  * Concurrency — two accept taps on one item → one entry, second ok-noop.
  * Fail-loud — invalid_tier / thin_evidence (no write).

Contract-first, no dep-gated skips (``feedback_regression_pin_unconditional``).
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.brief.feed_producer import slot_suggestion_feed_items
from alfred.daily_sync import action_router as arouter
from alfred.daily_sync.action_router import (
    STATUS_ACTED,
    STATUS_ALREADY_ACTED,
    STATUS_INVALID_ACTION,
    act,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed import FeedStore, try_feed_reconcile
from alfred.feed.model import STATE_ACTED, STATE_OPEN, FeedItem
from alfred.tier.daily_curation import (
    DailyCuration,
    T1T2Entry,
    load_daily_curation,
    save_tier_curation,
)
from alfred.tier.tier_confirm import (
    CONFIRM_KIND_IDEMPOTENT_NOOP,
    CONFIRM_KIND_INVALID_TIER,
    CONFIRM_KIND_SUCCESS,
    CONFIRM_KIND_THIN_EVIDENCE,
    confirm_slot_candidate,
)

# Reference instant — 2026-07-22 13:00 UTC. ds_config timezone is pinned to UTC
# so the router's ``_today_for`` and the producer's ``now.date()`` agree.
NOW = datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 22)
TODAY_ISO = "2026-07-22"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_router_today(monkeypatch):
    """Clamp the router's completion date to the fixture day (see the C1
    sibling)."""
    monkeypatch.setattr(arouter, "_today_for", lambda config: TODAY)


def _ds_config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True)
    cfg.state.path = str(tmp_path / "state.json")
    cfg.schedule.timezone = "UTC"
    return cfg


def _store(tmp_path: Path) -> FeedStore:
    return FeedStore(str(tmp_path / "feed.jsonl"))


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    (vault / "task").mkdir(parents=True, exist_ok=True)
    return vault


def _task_due_today(vault: Path, *, name: str = "Interview") -> None:
    (vault / "task" / f"{name}.md").write_text(
        f"---\ntype: task\nstatus: todo\nname: {name}\ndue: {TODAY_ISO}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _task_due_today_named(vault: Path, *, filename: str, name: str) -> None:
    """A task whose frontmatter ``name`` differs from its file stem — the ~5%
    sanitized/truncated-filename shape (real Salem examples: ``$100`` → ``00``)."""
    (vault / "task" / f"{filename}.md").write_text(
        f"---\ntype: task\nstatus: todo\nname: {name}\ndue: {TODAY_ISO}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _daily_path(vault: Path) -> Path:
    return vault / "daily" / f"{TODAY_ISO}.md"


def _candidate_item(
    *,
    tier: int,
    origin: str,
    source: str,
    stable_key: str,
    name: str = "",
    path: str = "",
    routine_record: str | None = None,
    item_text: str | None = None,
    confirmed: Any = None,
    candidate: bool = True,
) -> FeedItem:
    """Craft a slot_suggestion feed item with the producer-stamped evidence a
    real candidate carries — the trusted contract the router acts on."""
    return FeedItem.create(
        kind="slot_suggestion",
        stable_key=stable_key,
        instance="salem",
        title=f"T{tier}: {name or item_text}",
        evidence={
            "tier": tier,
            "origin": origin,
            "name": name,
            "path": path,
            "routine_record": routine_record,
            "item_text": item_text,
            "source": source,
            "confirmed": confirmed,
            "candidate": candidate,
        },
        source_ref={"producer": "brief"},
    )


def _slot_items(vault: Path) -> list[FeedItem]:
    return slot_suggestion_feed_items(vault, NOW, None, instance="salem") or []


def _act(store: FeedStore, cfg: DailySyncConfig, vault: Path, fid: str, action: str) -> Any:
    return act(
        fid, action,
        feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker", raw_config=None,
    )


# ---------------------------------------------------------------------------
# Per-tier accept → REAL writer
# ---------------------------------------------------------------------------


def test_accept_t1_task_writes_confirmed_entry_and_acts(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="auto-due", confirmed=False, stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    res = _act(store, cfg, vault, item.id, "accept")

    assert res.ok and res.status == STATUS_ACTED
    assert res.render == {"tier": 1, "name": "Interview", "committed": True}
    cur = load_daily_curation(vault, TODAY)
    assert len(cur.t1) == 1  # REAL write
    assert cur.t1[0].task == "[[task/Interview]]"
    assert cur.t1[0].confirmed is True  # confirmed flips the candidate flag off
    assert cur.t1[0].source == "auto-due"  # auto provenance preserved
    assert store.load()[item.id].state == STATE_ACTED  # optimistic


def test_accept_t1_routine_writes_confirmed_routine_item(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="routine_item", name="Pay Rent", path="routine/Bills.md",
        routine_record="Bills", item_text="Pay Rent", source="auto-due-routine",
        confirmed=False, stable_key="routine:Bills::Pay Rent",
    )
    store.upsert(item)

    res = _act(store, cfg, vault, item.id, "accept")

    assert res.ok and res.status == STATUS_ACTED
    cur = load_daily_curation(vault, TODAY)
    assert len(cur.t1) == 1
    assert cur.t1[0].routine_item == {"record": "Bills", "text": "Pay Rent"}
    assert cur.t1[0].confirmed is True
    assert cur.t1[0].source == "auto-due-routine"


def test_accept_t2_routine_writes_operator_source_no_confirmed(tmp_path: Path) -> None:
    """T2 has no ``confirmed`` field — the accept must write a NON-auto source
    ("operator") so the producer's ``candidate`` derivation flips off."""
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=2, origin="routine_item", name="Pay Rent", path="routine/Bills.md",
        routine_record="Bills", item_text="Pay Rent",
        source="auto-surface-routine", confirmed=None,
        stable_key="routine:Bills::Pay Rent",
    )
    store.upsert(item)

    res = _act(store, cfg, vault, item.id, "accept")

    assert res.ok and res.status == STATUS_ACTED
    cur = load_daily_curation(vault, TODAY)
    assert len(cur.t2) == 1 and not cur.t1
    assert cur.t2[0].routine_item == {"record": "Bills", "text": "Pay Rent"}
    assert cur.t2[0].source == "operator"  # committed, NOT the auto source
    assert cur.t2[0].confirmed is None


def test_accept_t3_routine_writes_free_text_entry(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=3, origin="routine_item", name="Meditate", path="routine/Self Care.md",
        routine_record="Self Care", item_text="Meditate", source="self-care",
        confirmed=None, stable_key="routine:Self Care::Meditate",
    )
    store.upsert(item)

    res = _act(store, cfg, vault, item.id, "accept")

    assert res.ok and res.status == STATUS_ACTED
    cur = load_daily_curation(vault, TODAY)
    assert len(cur.t3) == 1
    assert cur.t3[0].item == "Meditate"  # free-text T3
    assert cur.t3[0].source == "operator"


def test_accept_t3_selfcare_task_writes_free_text_from_name(tmp_path: Path) -> None:
    """A self-care TASK candidate (no item_text) commits as free-text T3 from
    its ``name`` — T3 is free-text-only by data-model."""
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=3, origin="task", name="Book a massage", path="task/Book a massage.md",
        source="self-care", confirmed=None, stable_key="task:task/Book a massage.md",
    )
    store.upsert(item)

    res = _act(store, cfg, vault, item.id, "accept")

    assert res.ok and res.status == STATUS_ACTED
    cur = load_daily_curation(vault, TODAY)
    assert len(cur.t3) == 1
    assert cur.t3[0].item == "Book a massage"


# ---------------------------------------------------------------------------
# Idempotency + folded-state
# ---------------------------------------------------------------------------


def test_accept_folded_state_already_acted(tmp_path: Path) -> None:
    """A second accept on an already-acted candidate is the folded-state ok-noop
    (no re-drive of the writer)."""
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="auto-due", confirmed=False, stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    first = _act(store, cfg, vault, item.id, "accept")
    second = _act(store, cfg, vault, item.id, "accept")

    assert first.ok and first.status == STATUS_ACTED
    assert second.ok and second.status == STATUS_ALREADY_ACTED
    assert len(load_daily_curation(vault, TODAY).t1) == 1  # exactly one entry


def test_writer_idempotent_reaccept_one_entry(tmp_path: Path) -> None:
    """Writer-direct: a second confirm of the same identity → idempotent_noop,
    no duplicate."""
    vault = _vault(tmp_path)
    kw = dict(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        routine_record=None, item_text=None, source="auto-due", date=TODAY,
    )
    first = confirm_slot_candidate(vault, **kw)
    second = confirm_slot_candidate(vault, **kw)

    assert first.kind == CONFIRM_KIND_SUCCESS and first.changed
    assert second.kind == CONFIRM_KIND_IDEMPOTENT_NOOP and not second.changed
    assert len(load_daily_curation(vault, TODAY).t1) == 1


def test_accept_appends_to_existing_curation(tmp_path: Path) -> None:
    """Accept read-preserve-appends: an existing operator T1 survives; the new
    entry is added (the aggregator's other keys are preserved by the inner
    write)."""
    vault = _vault(tmp_path)
    save_tier_curation(
        vault, TODAY,
        DailyCuration(t1=[T1T2Entry(task="[[task/Existing]]", source="operator")]),
    )
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="auto-due", confirmed=False, stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    _act(store, cfg, vault, item.id, "accept")

    cur = load_daily_curation(vault, TODAY)
    names = sorted(e.task for e in cur.t1)
    assert names == ["[[task/Existing]]", "[[task/Interview]]"]


# ---------------------------------------------------------------------------
# Provenance guard — committed items never accept (MUTATION-VERIFIED)
# ---------------------------------------------------------------------------


def test_committed_item_never_accepts_mutation_verified(tmp_path: Path) -> None:
    """A committed item (real producer output: a confirmed-curated T1 →
    candidate=False) refuses accept, and the daily file is BYTE-UNCHANGED — the
    provenance guard proven output-bound, not by a call-count."""
    vault = _vault(tmp_path)
    _task_due_today(vault, name="Interview")
    save_tier_curation(
        vault, TODAY,
        DailyCuration(t1=[T1T2Entry(task="[[task/Interview]]", source="auto-due", confirmed=True)]),
    )
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    items = _slot_items(vault)
    committed = [it for it in items if it.evidence.get("tier") == 1]
    assert len(committed) == 1
    item = committed[0]
    assert item.evidence["candidate"] is False  # committed → not a candidate
    store.upsert(item)

    before = _daily_path(vault).read_text(encoding="utf-8")
    res = _act(store, cfg, vault, item.id, "accept")
    after = _daily_path(vault).read_text(encoding="utf-8")

    assert not res.ok and res.status == STATUS_INVALID_ACTION
    assert after == before  # MUTATION-VERIFIED: no write happened
    assert store.load()[item.id].state == STATE_OPEN  # not acted


def test_crafted_committed_candidate_false_refuses(tmp_path: Path) -> None:
    """Defense-in-depth: even a stale FE that fires accept on a candidate=False
    open item is refused (no write)."""
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="operator", confirmed=True, candidate=False,
        stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    res = _act(store, cfg, vault, item.id, "accept")

    assert not res.ok and res.status == STATUS_INVALID_ACTION
    assert not _daily_path(vault).exists()  # no write


# ---------------------------------------------------------------------------
# Output-bound: the writer's OUTPUT is what the producer reads back
# ---------------------------------------------------------------------------


def test_accept_output_bound_producer_reads_committed(tmp_path: Path) -> None:
    """After the accept writer runs, the producer's OWN re-read sees the SAME
    item as committed (candidate=False, confirmed=True) — binds the writer's
    OUTPUT to the producer's read, not a call-count."""
    vault = _vault(tmp_path)
    _task_due_today(vault, name="Interview")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    items = _slot_items(vault)
    assert len(items) == 1 and items[0].evidence["candidate"] is True  # before
    item = items[0]
    store.upsert(item)

    _act(store, cfg, vault, item.id, "accept")

    reemit = [it for it in _slot_items(vault) if it.id == item.id]
    assert len(reemit) == 1
    assert reemit[0].evidence["candidate"] is False  # committed now
    assert reemit[0].evidence["confirmed"] is True


# ---------------------------------------------------------------------------
# Reconcile (Fork B) — accept → acted → re-emit committed → revive same episode
# ---------------------------------------------------------------------------


def test_accept_reconcile_revives_same_episode_committed(tmp_path: Path) -> None:
    """Task lane (name==stem): accept flips the candidate to ``acted``; the next
    producer emit re-emits the SAME stable key as committed; reconcile revives it
    open (a new episode) with candidate=False. Verified against feed/store.py."""
    vault = _vault(tmp_path)
    _task_due_today(vault, name="Interview")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _slot_items(vault)[0]
    store.upsert(item)
    assert item.evidence["candidate"] is True

    _act(store, cfg, vault, item.id, "accept")
    assert store.load()[item.id].state == STATE_ACTED

    # Next fire re-emits the committed item → reconcile revives it open.
    try_feed_reconcile(store, "slot_suggestion", _slot_items(vault))

    revived = store.load()[item.id]  # SAME id (name==stem round-trips)
    assert revived.state == STATE_OPEN
    assert revived.evidence["candidate"] is False  # committed
    assert revived.evidence["confirmed"] is True
    slots = [i for i in store.load().values() if i.kind == "slot_suggestion"]
    assert len(slots) == 1  # no duplicate episode


def test_accept_name_differs_from_stem_no_double_surface(tmp_path: Path) -> None:
    """The ~5% name≠stem path (§3 flag, pinned per heavy-gate NIT). The feed
    stable key is path-based (``task/<stem>.md``) but ``compute_today_view``
    dedups AND the curated wikilink round-trips on the ``name`` field — so after
    accept the committed re-emit lands under a DIFFERENT id (``task/<name>.md``).

    Case-B pin: (a) exactly ONE open committed slot after the next emit +
    reconcile — compute's name-keyed dedup suppresses the auto candidate, so
    NO double-surface; (b) the old ``task:<stem-path>`` id is folded ``acted``
    (the accept set it) and never revived (it was acted, so it never enters
    ``previously_open`` — the new name-derived id opens a fresh episode)."""
    vault = _vault(tmp_path)
    _task_due_today_named(vault, filename="pay-bill", name="Pay Bill")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)

    # Auto candidate — its feed id is path/stem-based.
    item = _slot_items(vault)[0]
    old_id = item.id
    assert old_id == "slot_suggestion:task:task/pay-bill.md"
    assert item.evidence["candidate"] is True
    store.upsert(item)

    _act(store, cfg, vault, old_id, "accept")
    assert store.load()[old_id].state == STATE_ACTED

    # Next fire re-emits the committed entry under a NAME-derived id → reconcile.
    try_feed_reconcile(store, "slot_suggestion", _slot_items(vault))

    folded = store.load()
    open_slots = [
        i for i in folded.values()
        if i.kind == "slot_suggestion" and i.state == STATE_OPEN
    ]
    # (a) exactly ONE open committed slot — no double-surface.
    assert len(open_slots) == 1
    committed = open_slots[0]
    assert committed.id == "slot_suggestion:task:task/Pay Bill.md"  # name-derived
    assert committed.id != old_id
    assert committed.evidence["candidate"] is False
    assert committed.evidence["confirmed"] is True
    # (b) the old auto id is folded acted, never revived.
    assert folded[old_id].state == STATE_ACTED


# ---------------------------------------------------------------------------
# Identity pin — the board's accept calls the SHARED writer
# ---------------------------------------------------------------------------


def test_accept_writer_identity_pin_board(tmp_path: Path, monkeypatch) -> None:
    """The board's accept dispatcher calls ``tier_confirm.confirm_slot_candidate``
    — the SAME object task #21 will route the talker through. Monkeypatch the
    module attr → the router's lazy import binds to the spy at call time. Fork
    the write inline → the spy misses → this pin reddens."""
    import alfred.tier.tier_confirm as tc

    real = tc.confirm_slot_candidate
    seen: list[str] = []

    def _spy(vault_path, **kw):
        seen.append(kw["name"])
        return real(vault_path, **kw)

    monkeypatch.setattr(tc, "confirm_slot_candidate", _spy)

    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="auto-due", confirmed=False, stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    _act(store, cfg, vault, item.id, "accept")
    assert seen == ["Interview"], "board accept must route through the shared writer"


# ---------------------------------------------------------------------------
# Fail-loud — invalid_tier / thin_evidence (no write)
# ---------------------------------------------------------------------------


def test_writer_invalid_tier_no_write(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    res = confirm_slot_candidate(
        vault, tier=5, origin="task", name="X", path="task/X.md",
        routine_record=None, item_text=None, source="auto-due", date=TODAY,
    )
    assert res.kind == CONFIRM_KIND_INVALID_TIER and not res.ok
    assert not _daily_path(vault).exists()


def test_writer_thin_evidence_t1_routine_missing_identity(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    res = confirm_slot_candidate(
        vault, tier=1, origin="routine_item", name="", path="",
        routine_record=None, item_text=None, source="auto-due-routine", date=TODAY,
    )
    assert res.kind == CONFIRM_KIND_THIN_EVIDENCE and not res.ok
    assert not _daily_path(vault).exists()


def test_writer_thin_evidence_t3_no_text(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    res = confirm_slot_candidate(
        vault, tier=3, origin="routine_item", name="", path="",
        routine_record="Rec", item_text=None, source="self-care", date=TODAY,
    )
    assert res.kind == CONFIRM_KIND_THIN_EVIDENCE and not res.ok


def test_router_invalid_tier_from_writer_is_error(tmp_path: Path) -> None:
    """A candidate with a bad tier reaches the writer (candidate=True passes the
    guard) → the writer fails loud → the router surfaces it as an error, feed
    state UNTOUCHED."""
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=9, origin="task", name="Weird", path="task/Weird.md",
        source="auto-due", confirmed=False, stable_key="task:task/Weird.md",
    )
    store.upsert(item)

    res = _act(store, cfg, vault, item.id, "accept")

    assert not res.ok and res.status == "error"
    assert store.load()[item.id].state == STATE_OPEN  # not acted


# ---------------------------------------------------------------------------
# Log-emission pins (feedback_log_emission_test_pattern)
# ---------------------------------------------------------------------------


def test_accept_emits_named_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="auto-due", confirmed=False, stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    with structlog.testing.capture_logs() as cap:
        _act(store, cfg, vault, item.id, "accept")

    accepted = [c for c in cap if c.get("event") == "feed.act.slot.accepted"]
    assert len(accepted) == 1
    assert accepted[0]["tier"] == 1
    assert accepted[0]["item"] == "Interview"


def test_not_a_candidate_emits_named_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="operator", confirmed=True, candidate=False,
        stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    with structlog.testing.capture_logs() as cap:
        _act(store, cfg, vault, item.id, "accept")

    guard = [c for c in cap if c.get("event") == "feed.act.slot.not_a_candidate"]
    assert len(guard) == 1
    assert guard[0]["candidate"] is False


def test_writer_success_emits_named_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with structlog.testing.capture_logs() as cap:
        confirm_slot_candidate(
            vault, tier=2, origin="routine_item", name="Pay Rent",
            path="routine/Bills.md", routine_record="Bills", item_text="Pay Rent",
            source="auto-surface-routine", date=TODAY,
        )
    success = [c for c in cap if c.get("event") == "tier.confirm.success"]
    assert len(success) == 1
    assert success[0]["tier"] == 2
    assert success[0]["name"] == "Pay Rent"


def test_writer_invalid_tier_emits_named_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with structlog.testing.capture_logs() as cap:
        confirm_slot_candidate(
            vault, tier=0, origin="task", name="X", path="task/X.md",
            routine_record=None, item_text=None, source="auto-due", date=TODAY,
        )
    events = [c for c in cap if c.get("event") == "tier.confirm.invalid_tier"]
    assert len(events) == 1


def test_writer_thin_evidence_emits_named_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with structlog.testing.capture_logs() as cap:
        confirm_slot_candidate(
            vault, tier=1, origin="routine_item", name="", path="",
            routine_record=None, item_text=None, source="auto-due-routine", date=TODAY,
        )
    events = [c for c in cap if c.get("event") == "tier.confirm.thin_evidence"]
    assert len(events) == 1
    assert events[0]["reason"] == "routine_no_record_or_text"


# ---------------------------------------------------------------------------
# Concurrency — two accept taps, one entry, second ok-noop
# ---------------------------------------------------------------------------


def test_two_accept_taps_serialize_to_one_entry(tmp_path: Path) -> None:
    """Two concurrent accept taps on the SAME item: the per-item mutex serializes
    them, so the second sees the first's committed ``acted`` state (ok-noop) and
    the curation carries exactly ONE entry."""
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _candidate_item(
        tier=1, origin="task", name="Interview", path="task/Interview.md",
        source="auto-due", confirmed=False, stable_key="task:task/Interview.md",
    )
    store.upsert(item)

    barrier = threading.Barrier(2)
    results: list[Any] = []
    lock = threading.Lock()

    def _tap() -> None:
        barrier.wait()
        r = _act(store, cfg, vault, item.id, "accept")
        with lock:
            results.append(r)

    t1 = threading.Thread(target=_tap)
    t2 = threading.Thread(target=_tap)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    assert len(load_daily_curation(vault, TODAY).t1) == 1  # exactly one entry
    assert all(r.ok for r in results)
    assert sorted(r.status for r in results) == [STATUS_ACTED, STATUS_ALREADY_ACTED]

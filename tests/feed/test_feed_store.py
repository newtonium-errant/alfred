"""FeedStore pins — fold semantics, unknown-ev skip, schema-tolerance, reconcile
decided-detection, compaction, and the #37-style append-vs-compaction lock race.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import structlog

from alfred.common.file_lock import file_rmw_lock
from alfred.feed.model import (
    ACTION_RETIRED,
    STATE_ACKED,
    STATE_ACTED,
    STATE_EXPIRED,
    STATE_OPEN,
    FeedItem,
)
from alfred.feed.store import FeedStore


def _item(kind: str, key: str, *, title: str = "t", state: str = STATE_OPEN) -> FeedItem:
    return FeedItem.create(kind=kind, stable_key=key, instance="salem", title=title, state=state)


def _store(tmp_path: Path, **kw) -> FeedStore:
    return FeedStore(tmp_path / "feed.jsonl", **kw)


# --- fold semantics ----------------------------------------------------------


def test_upsert_then_load(tmp_path: Path) -> None:
    s = _store(tmp_path)
    it = _item("proposal", "c1")
    s.upsert(it)
    folded = s.load()
    assert set(folded) == {"proposal:c1"}
    assert folded["proposal:c1"].title == "t"


def test_last_write_wins(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert(_item("proposal", "c1", title="first"))
    s.upsert(_item("proposal", "c1", title="second"))
    assert s.load()["proposal:c1"].title == "second"


def test_set_state_folds_onto_existing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert(_item("pending", "u1"))
    s.set_state("pending:u1", STATE_ACTED)
    folded = s.load()
    assert folded["pending:u1"].state == STATE_ACTED
    assert folded["pending:u1"].acted_at  # stamped on acted


def test_state_for_unknown_id_is_ignored(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_state("pending:never-upserted", STATE_ACTED)
    assert s.load() == {}


# --- acted_action (Phase C slice 2 — the acted verb) -------------------------


def test_set_state_stamps_acted_action(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert(_item("slot_suggestion", "task:task/X.md"))
    s.set_state("slot_suggestion:task:task/X.md", STATE_ACTED, action="accept")
    folded = s.load()["slot_suggestion:task:task/X.md"]
    assert folded.state == STATE_ACTED
    assert folded.acted_action == "accept"


def test_acted_action_newest_event_wins(tmp_path: Path) -> None:
    """accept then a later done → the fold ends "done" (newest acted wins)."""
    s = _store(tmp_path)
    s.upsert(_item("slot_suggestion", "task:task/X.md"))
    s.set_state("slot_suggestion:task:task/X.md", STATE_ACTED, action="accept")
    s.set_state("slot_suggestion:task:task/X.md", STATE_ACTED, action="done")
    assert s.load()["slot_suggestion:task:task/X.md"].acted_action == "done"


def test_acted_action_legacy_verbless_event_is_none(tmp_path: Path) -> None:
    """A legacy acted event (no ``action`` — pre-amendment / reconcile-decided)
    folds to ``acted_action=None`` (C1 compat)."""
    s = _store(tmp_path)
    s.upsert(_item("slot_suggestion", "task:task/X.md"))
    s.set_state("slot_suggestion:task:task/X.md", STATE_ACTED)  # no action
    folded = s.load()["slot_suggestion:task:task/X.md"]
    assert folded.state == STATE_ACTED
    assert folded.acted_action is None


def test_acted_action_cleared_on_return_to_open(tmp_path: Path) -> None:
    """A non-acted transition (undo → open) clears the verb — an open item never
    carries a stale acted_action."""
    s = _store(tmp_path)
    s.upsert(_item("slot_suggestion", "task:task/X.md"))
    s.set_state("slot_suggestion:task:task/X.md", STATE_ACTED, action="accept")
    s.set_state("slot_suggestion:task:task/X.md", STATE_OPEN)
    folded = s.load()["slot_suggestion:task:task/X.md"]
    assert folded.state == STATE_OPEN
    assert folded.acted_action is None


def test_acted_action_in_list_payload(tmp_path: Path) -> None:
    """acted_action rides the list payload (to_dict) so GET /feed/items exposes
    it to the FE."""
    s = _store(tmp_path)
    s.upsert(_item("slot_suggestion", "task:task/X.md"))
    s.set_state("slot_suggestion:task:task/X.md", STATE_ACTED, action="accept")
    payload = s.load()["slot_suggestion:task:task/X.md"].to_dict()
    assert payload["acted_action"] == "accept"


def test_empty_store_loads_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).load() == {}


# --- forward-compat: unknown ev + torn line ---------------------------------


def test_unknown_ev_is_skipped(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert(_item("radar", "r1"))
    # A newer writer appended an ev this version doesn't know — must be skipped.
    with open(s.path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ev": "future_op", "id": "radar:r1", "blob": 1}) + "\n")
    folded = s.load()
    assert set(folded) == {"radar:r1"}
    assert folded["radar:r1"].state == STATE_OPEN  # future_op didn't touch it


def test_torn_or_unparseable_line_is_skipped(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert(_item("radar", "r1"))
    with open(s.path, "a", encoding="utf-8") as fh:
        fh.write('{"ev": "upsert", "item": {broken json\n')  # torn tail
    assert set(s.load()) == {"radar:r1"}  # didn't crash, skipped the torn line


def test_schema_tolerant_event_with_extra_item_field(tmp_path: Path) -> None:
    s = _store(tmp_path)
    with open(s.path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ev": "upsert", "ts": "2026-07-30T00:00:00+00:00",
            "item": {"id": "pending:u1", "kind": "pending", "future_field": 99},
        }) + "\n")
    folded = s.load()
    assert folded["pending:u1"].kind == "pending"


def test_upsert_event_missing_id_is_skipped(tmp_path: Path) -> None:
    s = _store(tmp_path)
    with open(s.path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ev": "upsert", "item": {"kind": "pending"}}) + "\n")  # no id
    assert s.load() == {}


# --- reconcile: decided-detection -------------------------------------------


def test_reconcile_upserts_and_marks_absent_acted(tmp_path: Path) -> None:
    s = _store(tmp_path)
    # Fire 1: two proposals open.
    counts1 = s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])
    assert counts1 == {"open": 2, "acted": 0, "suppressed": 0,
                       "deferred_held": 0, "defer_returned": 0}
    assert {i for i, it in s.load().items() if it.state == STATE_OPEN} == {"proposal:c1", "proposal:c2"}

    # Fire 2: c1 still open, c2 gone (decided elsewhere) → c2 becomes acted.
    counts2 = s.reconcile("proposal", [_item("proposal", "c1")])
    assert counts2 == {"open": 1, "acted": 1, "suppressed": 0,
                       "deferred_held": 0, "defer_returned": 0}
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_OPEN
    assert folded["proposal:c2"].state == STATE_ACTED


def test_reconcile_only_touches_its_own_kind(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1")])
    s.reconcile("pending", [_item("pending", "u1")])
    # Reconciling proposal with an empty set acts c1 but must NOT touch pending.
    s.reconcile("proposal", [])
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_ACTED
    assert folded["pending:u1"].state == STATE_OPEN


def test_reconcile_preserves_created_at_while_open(tmp_path: Path) -> None:
    """created_at is the CONTINUOUSLY-OPEN episode's first-seen — a re-upsert
    across fires with a DISTINCT item object (fresh timestamp) must not drift it.
    (The old idempotency test reused the same object, so it was blind to this.)"""
    s = _store(tmp_path)
    first = FeedItem.create(kind="proposal", stable_key="c1", instance="salem", title="v1",
                            created_at="2026-07-30T00:00:00+00:00")
    s.reconcile("proposal", [first])
    # Fire 2: a genuinely new object for the SAME key, with a later created_at.
    second = FeedItem.create(kind="proposal", stable_key="c1", instance="salem", title="v2",
                             created_at="2026-07-31T09:00:00+00:00")
    s.reconcile("proposal", [second])
    folded = s.load()
    assert folded["proposal:c1"].title == "v2"  # evidence/title DID refresh
    assert folded["proposal:c1"].created_at == "2026-07-30T00:00:00+00:00"  # first-seen preserved


def test_acted_then_reappears_is_new_episode_with_fresh_created_at(tmp_path: Path) -> None:
    """A key that went acted and reappears in the open set is a NEW episode:
    revived to open, with the fresh created_at (not the old episode's)."""
    s = _store(tmp_path)
    s.reconcile("proposal", [FeedItem.create(kind="proposal", stable_key="c1", instance="salem",
                                             title="v1", created_at="2026-07-30T00:00:00+00:00")])
    s.reconcile("proposal", [])  # c1 → acted
    assert s.load()["proposal:c1"].state == STATE_ACTED
    # Reappears — the authority re-opened it → new episode, fresh created_at.
    s.reconcile("proposal", [FeedItem.create(kind="proposal", stable_key="c1", instance="salem",
                                             title="v2", created_at="2026-08-01T00:00:00+00:00")])
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_OPEN  # revived
    assert folded["proposal:c1"].created_at == "2026-08-01T00:00:00+00:00"  # new episode's first-seen
    assert folded["proposal:c1"].acted_at is None  # prior episode's acted_at cleared


def test_upsert_also_preserves_created_at_while_open(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert(FeedItem.create(kind="radar", stable_key="r1", instance="salem", title="a",
                             created_at="2026-07-30T00:00:00+00:00"))
    s.upsert(FeedItem.create(kind="radar", stable_key="r1", instance="salem", title="b",
                             created_at="2026-08-05T00:00:00+00:00"))
    assert s.load()["radar:r1"].created_at == "2026-07-30T00:00:00+00:00"


def test_reconcile_idempotent_rerun(tmp_path: Path) -> None:
    s = _store(tmp_path)
    open_set = [_item("routine_match", "q1|r1"), _item("routine_match", "q2|r2")]
    s.reconcile("routine_match", open_set)
    first = {i: it.state for i, it in s.load().items()}
    counts = s.reconcile("routine_match", open_set)  # same set again
    assert counts == {"open": 2, "acted": 0, "suppressed": 0,
                      "deferred_held": 0, "defer_returned": 0}
    assert {i: it.state for i, it in s.load().items()} == first  # folded state unchanged


# --- compaction --------------------------------------------------------------


def test_compaction_preserves_state_and_shrinks(tmp_path: Path) -> None:
    # Tiny threshold so a few writes trip compaction.
    s = _store(tmp_path, compact_threshold_bytes=200)
    for n in range(20):
        s.upsert(_item("proposal", "c1", title=f"rev{n}"))  # same id, 20 revisions
    folded = s.load()
    assert folded["proposal:c1"].title == "rev19"  # last-write-wins survived compaction
    # After compaction the log holds ~1 upsert per live id, not all 20 revisions.
    line_count = sum(1 for ln in s.path.read_text(encoding="utf-8").splitlines() if ln.strip())
    assert line_count < 20


def test_explicit_compact_is_lossless(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])
    s.reconcile("proposal", [_item("proposal", "c1")])  # c2 → acted
    before = {i: it.state for i, it in s.load().items()}
    s.compact()
    after = {i: it.state for i, it in s.load().items()}
    assert after == before


# --- GOLD STANDARD: append-vs-compaction lost-update lock race (#37) ---------


def test_no_lost_update_append_racing_compaction(tmp_path: Path) -> None:
    """An upsert racing a compaction's atomic rewrite must not be lost — the
    flock serializes them. Deterministic both ways via a stale-read + sleep,
    mirroring ``tests/routine/test_routine_record_lock.py``:

    Main holds ``file_rmw_lock(store.path)`` and folds a STALE view ({A}, no B).
    It starts a worker running a REAL ``upsert(B)``, sleeps, then rewrites the
    stale view (compaction, atomic ``os.replace``) and releases.

      * WITH the lock: the worker BLOCKS on the flock the whole sleep, so it
        can't append B until main's compaction lands; it then appends B to the
        compacted file → {A, B}. Both survive. ✓
      * WITHOUT the lock (neuter ``alfred.feed.store.file_rmw_lock``): the worker
        appends B to the OLD inode during the sleep; main's ``os.replace``
        overwrites it → {A} only. This pin FAILS.

    Mutation-verified: a no-op lock loses B.
    """
    s = _store(tmp_path)
    s.upsert(_item("proposal", "A"))  # seed A

    def _worker() -> None:
        s.upsert(_item("proposal", "B"))

    with file_rmw_lock(s.path):
        stale = s._fold_from_disk()  # sees {A} only
        t = threading.Thread(target=_worker)
        t.start()
        time.sleep(0.3)  # unlocked worker appends B here; locked worker blocks
        s._rewrite_locked(stale)  # compaction of the stale {A} view
    t.join(timeout=5)
    assert not t.is_alive(), "worker upsert did not complete"

    folded = s.load()
    assert "proposal:A" in folded, "A (main's compaction) must survive"
    assert "proposal:B" in folded, "B (concurrent upsert) must survive — no lost update"


# --- per-kind revival policy: the event ack groundhog -------------------------
#
# Operator screenshot 2026-08-03: the awareness feed re-demanded ACK on the same
# appointment cards every morning. ``event`` ids are stable across days by
# design (``event:<event date>|<name>``), so the daily producer re-emitted the
# same open set and reconcile's upsert revived every acked card. Operator
# ruling: events surface when FIRST ADDED and when their CONTENT CHANGES; an
# ack sticks otherwise.


def _event(key: str, *, time_display: str = "09:00", name: str = "Dentist") -> FeedItem:
    return FeedItem.create(
        kind="event", stable_key=key, instance="salem",
        title=f"2026-08-10: {name}",
        evidence={
            "date_iso": "2026-08-10", "name": name,
            "rec_type": "event", "time_display": time_display,
        },
    )


def test_acked_event_is_not_revived_by_the_next_snapshot(tmp_path: Path) -> None:
    """THE GROUNDHOG PIN. Mutation: drop the ``_revival_suppressed`` check in
    reconcile → this fails."""
    s = _store(tmp_path)
    key = "2026-08-10|Dentist"
    s.reconcile("event", [_event(key)])
    s.set_state(f"event:{key}", STATE_ACKED)

    counts = s.reconcile("event", [_event(key)])  # tomorrow's identical snapshot

    assert s.load()[f"event:{key}"].state == STATE_ACKED  # ack STUCK
    assert counts["suppressed"] == 1
    assert counts["open"] == 1  # the producer still emitted it


def test_acked_event_revives_when_its_time_moves(tmp_path: Path) -> None:
    """Content change IS news — a moved appointment must come back."""
    s = _store(tmp_path)
    key = "2026-08-10|Dentist"
    s.reconcile("event", [_event(key, time_display="09:00")])
    s.set_state(f"event:{key}", STATE_ACKED)

    counts = s.reconcile("event", [_event(key, time_display="14:30")])

    assert s.load()[f"event:{key}"].state == STATE_OPEN  # revived
    assert counts["suppressed"] == 0


def test_acted_event_is_also_kept_sticky(tmp_path: Path) -> None:
    """``acted`` and ``acked`` are both decisions — neither should be undone by
    an unchanged re-emission."""
    s = _store(tmp_path)
    key = "2026-08-10|Dentist"
    s.reconcile("event", [_event(key)])
    s.set_state(f"event:{key}", STATE_ACTED)
    s.reconcile("event", [_event(key)])
    assert s.load()[f"event:{key}"].state == STATE_ACTED


def test_open_event_still_refreshes_normally(tmp_path: Path) -> None:
    """The policy only guards DECIDED items — an open item still gets its
    evidence/title refreshed every fire."""
    s = _store(tmp_path)
    key = "2026-08-10|Dentist"
    s.reconcile("event", [_event(key, time_display="09:00")])
    counts = s.reconcile("event", [_event(key, time_display="14:30")])
    assert counts["suppressed"] == 0
    assert s.load()[f"event:{key}"].evidence["time_display"] == "14:30"


def test_health_keeps_episode_revival(tmp_path: Path) -> None:
    """REGRESSION GUARD: health is episode-shaped — a warn that returns after
    being acted IS new news and must still revive. Mutation: add "health" to
    SNAPSHOT_FINGERPRINT_FIELDS → this fails."""
    s = _store(tmp_path)
    item = FeedItem.create(
        kind="health", stable_key="surveyor", instance="salem",
        title="Health: surveyor WARN",
        evidence={"tool": "surveyor", "status": "warn", "detail": "ollama 404"},
    )
    s.reconcile("health", [item])
    s.set_state("health:surveyor", STATE_ACTED)

    counts = s.reconcile("health", [item])  # same warn recurs

    assert s.load()["health:surveyor"].state == STATE_OPEN  # revived, as before
    assert counts["suppressed"] == 0


def test_absent_event_still_goes_acted(tmp_path: Path) -> None:
    """absent→acted reconcile is UNCHANGED by the policy: an appointment that
    drops out of the window still closes."""
    s = _store(tmp_path)
    s.reconcile("event", [_event("2026-08-10|Dentist"), _event("2026-08-11|Vet")])
    counts = s.reconcile("event", [_event("2026-08-10|Dentist")])
    assert counts["acted"] == 1
    assert s.load()["event:2026-08-11|Vet"].state == STATE_ACTED


def test_prechange_event_without_evidence_still_revives(tmp_path: Path) -> None:
    """READ-BELT: an item stored before this policy (or with evidence we can't
    fingerprint) keeps TODAY'S behaviour — revive — rather than being silently
    suppressed forever on a guess.

    Mutation: treat an unfingerprintable stored item as "matching" → this
    fails."""
    s = _store(tmp_path)
    key = "2026-08-10|Dentist"
    # Stored with no evidence at all — the pre-policy shape.
    s.reconcile("event", [FeedItem.create(
        kind="event", stable_key=key, instance="salem", title="old", evidence={},
    )])
    s.set_state(f"event:{key}", STATE_ACKED)

    counts = s.reconcile("event", [_event(key)])

    assert s.load()[f"event:{key}"].state == STATE_OPEN
    assert counts["suppressed"] == 0


def test_open_snapshot_item_refreshes_a_non_fingerprint_field(tmp_path: Path) -> None:
    """The decided-state guard is load-bearing, half 1 of 2.

    An OPEN item must always be upserted, even when its fingerprint is
    unchanged — otherwise a title/evidence edit outside the fingerprint fields
    would never reach the store. Reviewer-specified: unchanged fingerprint +
    differing NON-fingerprint field (title) → suppressed==0 AND title refreshed.

    Mutation: delete the ``stored.state not in (ACTED, ACKED)`` guard → this
    fails (the open item gets suppressed and keeps its stale title)."""
    s = _store(tmp_path)
    key = "2026-08-10|Dentist"
    s.reconcile("event", [_event(key)])

    renamed = _event(key)
    renamed.title = "2026-08-10: Dentist (moved building)"  # NOT a fingerprint field
    counts = s.reconcile("event", [renamed])

    assert counts["suppressed"] == 0
    assert s.load()[f"event:{key}"].title == "2026-08-10: Dentist (moved building)"


def test_expired_snapshot_item_still_revives(tmp_path: Path) -> None:
    """The decided-state guard is load-bearing, half 2 of 2.

    ``expired`` is not a DECISION — the operator never acted on it, it simply
    aged out. So an expired item whose fingerprint is unchanged must revive
    exactly as it did before this policy; only acted/acked are sticky.
    Reviewer-specified: expired + unchanged fingerprint → suppressed==0 AND
    state=="open".

    Mutation: widen the guard to ``stored.state != STATE_OPEN`` → this fails
    (the expired item stays expired forever)."""
    s = _store(tmp_path)
    key = "2026-08-10|Dentist"
    s.reconcile("event", [_event(key)])
    s.set_state(f"event:{key}", STATE_EXPIRED)

    counts = s.reconcile("event", [_event(key)])

    assert counts["suppressed"] == 0
    assert s.load()[f"event:{key}"].state == "open"


# --- peer_digest snapshot policy (smalls#2 R12) ------------------------------


def _digest(peer: str = "kalle", *, body: str = "3 tickets closed.",
            truncated: bool = False, date: str = "2026-08-04") -> FeedItem:
    """A peer_digest as ``brief/feed_producer.py:276-284`` emits it."""
    return FeedItem.create(
        kind="peer_digest", stable_key=f"{peer}|{date}", instance="salem",
        title=f"Peer digest: {peer}",
        evidence={"peer": peer, "date": date, "body": body, "truncated": truncated},
    )


def test_acked_peer_digest_survives_a_same_day_refire(tmp_path: Path) -> None:
    """The bug: peer_digest's stable key is ``<peer>|<today_iso>``, so it is
    stable WITHIN a day — a brief retry or a manual re-fire re-emitted the same
    id and the reconcile upsert revived a digest the operator had already
    acked. Same groundhog-ack mechanism as the event fix, lower exposure
    (the brief fires once daily) but identical operator experience."""
    s = _store(tmp_path)
    s.reconcile("peer_digest", [_digest()])
    s.set_state("peer_digest:kalle|2026-08-04", STATE_ACKED)

    counts = s.reconcile("peer_digest", [_digest()])  # identical re-fire

    assert s.load()["peer_digest:kalle|2026-08-04"].state == STATE_ACKED
    assert counts["suppressed"] == 1


def test_acked_peer_digest_revives_when_the_body_changes(tmp_path: Path) -> None:
    """The half that makes the fingerprint fields load-bearing: a re-fire
    carrying MORE from the peer is new news and must resurface. Keying on
    ``peer``/``date`` (both already in the stable key) would suppress this
    too — which is why the fields are body+truncated."""
    s = _store(tmp_path)
    s.reconcile("peer_digest", [_digest(body="3 tickets closed.")])
    s.set_state("peer_digest:kalle|2026-08-04", STATE_ACKED)

    counts = s.reconcile("peer_digest", [_digest(body="5 tickets closed, 1 escalated.")])

    assert s.load()["peer_digest:kalle|2026-08-04"].state == STATE_OPEN
    assert counts["suppressed"] == 0


def test_acked_peer_digest_revives_when_truncation_changes(tmp_path: Path) -> None:
    """``truncated`` earns its place in the tuple: the same leading 4000 chars
    clipped vs complete is a DIFFERENT digest to the reader. Mutation: drop
    "truncated" from the tuple → this fails while the body test still passes."""
    s = _store(tmp_path)
    s.reconcile("peer_digest", [_digest(body="x" * 100, truncated=True)])
    s.set_state("peer_digest:kalle|2026-08-04", STATE_ACKED)

    counts = s.reconcile("peer_digest", [_digest(body="x" * 100, truncated=False)])

    assert s.load()["peer_digest:kalle|2026-08-04"].state == STATE_OPEN
    assert counts["suppressed"] == 0


def test_retirement_names_the_ids_it_retired(tmp_path: Path) -> None:
    """ILB — WHICH cards were retired, not just how many (P1 follow-on).

    The belt already logs an ``acted`` COUNT, which cannot answer the question
    that mattered on 2026-08-15: five email cards were dealt against a stale
    ``last_batch``, every verdict the operator gave was refused, and the next
    reconcile would have retired them here as "decided elsewhere" — recording
    as decided the decisions the system had just declined to accept. The act
    path is fixed; this is what makes the retirement itself identifiable if a
    sibling case ever appears.

    Paired with a control: a fire that retires nothing must stay silent, or the
    line is noise on every reconcile rather than a signal about retirement.
    """
    import structlog

    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])

    with structlog.testing.capture_logs() as cap:
        s.reconcile("proposal", [_item("proposal", "c1")])
    hits = [c for c in cap if c.get("event") == "feed.store.retired_absent"]
    assert len(hits) == 1
    assert hits[0]["kind"] == "proposal"
    assert hits[0]["count"] == 1
    assert hits[0]["ids"] == ["proposal:c2"]

    # Control: nothing absent this fire → no line.
    with structlog.testing.capture_logs() as quiet:
        s.reconcile("proposal", [_item("proposal", "c1")])
    assert [c for c in quiet if c.get("event") == "feed.store.retired_absent"] == []


# ===========================================================================
# PY-B items 2a + 3 — the retirement verb, and the would-retire-all tripwire
# ===========================================================================
#
# A reconcile retirement (item left the producer's open set) and an operator
# decision both appended a byte-identical VERBLESS ``state=acted`` event, so
# the log could not tell "he judged it" from "it stopped being emitted". The
# verb splits them. STATE IS UNCHANGED — ``acted`` stays ``acted``; this is
# the forward-compatible half of the pending retire-vs-decided ratification.


def test_reconcile_retirement_stamps_the_retired_verb(tmp_path: Path) -> None:
    """Mutation that reds this: drop ``"action": ACTION_RETIRED`` from the
    absent-set events in ``reconcile``."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])
    s.reconcile("proposal", [_item("proposal", "c1")])

    folded = s.load()
    retired = folded["proposal:c2"]
    assert retired.state == STATE_ACTED  # STATE UNCHANGED — the whole point
    assert retired.acted_action == ACTION_RETIRED
    assert retired.acted_at
    # The survivor is untouched and carries NO verb — it was never acted.
    assert folded["proposal:c1"].state == STATE_OPEN
    assert folded["proposal:c1"].acted_action is None


def test_an_operator_verb_is_not_overwritten_by_the_retirement_verb(
    tmp_path: Path,
) -> None:
    """POSITIVE CONTROL + the ordering that matters. An item the operator
    ACTED on carries his verb; it is already absent from the next open set,
    but it is also no longer OPEN, so reconcile's absent-set never includes it
    and cannot overwrite ``accept`` with ``retired``."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1")])
    s.set_state("proposal:c1", STATE_ACTED, action="accept")
    s.reconcile("proposal", [])

    it = s.load()["proposal:c1"]
    assert it.acted_action == "accept"
    assert it.acted_action != ACTION_RETIRED


def test_the_retired_verb_is_inert_for_every_current_reader(tmp_path: Path) -> None:
    """WHY THIS IS SAFE TO SHIP AHEAD OF THE STATE RULING. Every reader of
    ``acted_action`` (Python and web) compares it for EQUALITY against a
    specific verb — ``accept`` / ``done`` / ``snooze`` — and nothing anywhere
    tests it for ABSENCE. So a fourth value changes no branch: what used to
    read as "not accept" still reads as "not accept".

    ``rings.ts`` is the case worth naming, since it renders from this field:
    ``item.acted_action === 'accept' ? 'planned' : 'done'`` sent a verbless
    retirement to ``done`` before, and sends ``retired`` to ``done`` now."""
    s = _store(tmp_path)
    s.reconcile("slot_suggestion", [_item("slot_suggestion", "task:task/X.md")])
    s.reconcile("slot_suggestion", [])

    verb = s.load()["slot_suggestion:task:task/X.md"].acted_action
    assert verb == ACTION_RETIRED
    assert verb not in {"accept", "done", "snooze"}
    assert verb is not None  # it IS stamped — not a no-op dressed as one


def test_tripwire_fires_when_an_empty_open_set_would_retire_everything(
    tmp_path: Path,
) -> None:
    """The mass-retirement event stops being silent. Semantics UNCHANGED —
    the retirement still happens; only the silence is fixed.

    Mutation that reds this: delete the ``if not open_items and
    previously_present`` warn from ``reconcile``."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])

    with structlog.testing.capture_logs() as captured:
        s.reconcile("proposal", [])

    matches = [
        c for c in captured
        if c.get("event") == "feed.store.reconcile_would_retire_all_open"
    ]
    assert len(matches) == 1
    assert matches[0]["log_level"] == "warning"
    assert matches[0]["kind"] == "proposal"
    assert matches[0]["count"] == 2
    assert matches[0]["ids"] == ["proposal:c1", "proposal:c2"]
    # ...and the reconcile proceeded exactly as before.
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_ACTED
    assert folded["proposal:c2"].state == STATE_ACTED


def test_tripwire_silent_on_a_partial_retirement(tmp_path: Path) -> None:
    """POSITIVE CONTROL. A normal reconcile that retires SOME items must not
    warn — a tripwire that fires on ordinary traffic is one the operator
    learns to ignore, which is the same as not having it."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])

    with structlog.testing.capture_logs() as captured:
        s.reconcile("proposal", [_item("proposal", "c1")])

    assert [
        c for c in captured
        if c.get("event") == "feed.store.reconcile_would_retire_all_open"
    ] == []
    assert s.load()["proposal:c2"].acted_action == ACTION_RETIRED  # it DID retire


def test_tripwire_silent_when_there_was_nothing_open(tmp_path: Path) -> None:
    """The other quiet case: an empty producer against an empty store retires
    nothing, so there is nothing to announce."""
    s = _store(tmp_path)
    with structlog.testing.capture_logs() as captured:
        s.reconcile("proposal", [])
    assert [
        c for c in captured
        if c.get("event") == "feed.store.reconcile_would_retire_all_open"
    ] == []

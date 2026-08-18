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
    STATE_RETIRED,
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
    assert counts1 == {"open": 2, "acted": 0, "retired": 0, "refused": 0,
                       "suppressed": 0,
                       "deferred_held": 0, "defer_returned": 0}
    assert {i for i, it in s.load().items() if it.state == STATE_OPEN} == {"proposal:c1", "proposal:c2"}

    # Fire 2: c1 still open, c2 gone (decided elsewhere) → c2 becomes acted.
    counts2 = s.reconcile("proposal", [_item("proposal", "c1")])
    assert counts2 == {"open": 1, "acted": 1, "retired": 1, "refused": 0,
                       "suppressed": 0,
                       "deferred_held": 0, "defer_returned": 0}
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_OPEN
    assert folded["proposal:c2"].state == STATE_RETIRED


def test_reconcile_only_touches_its_own_kind(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1")])
    s.reconcile("pending", [_item("pending", "u1")])
    # Reconciling proposal with an empty set acts c1 but must NOT touch pending.
    s.reconcile("proposal", [], empty_is_authoritative=True)
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_RETIRED
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
    s.reconcile("proposal", [], empty_is_authoritative=True)  # c1 → acted
    assert s.load()["proposal:c1"].state == STATE_RETIRED
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
    assert counts == {"open": 2, "acted": 0, "retired": 0, "refused": 0,
                      "suppressed": 0,
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
    assert s.load()["event:2026-08-11|Vet"].state == STATE_RETIRED


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
    # PREMISE REPLACED by the operator's A+C ruling. This line read
    # ``== STATE_ACTED  # STATE UNCHANGED — the whole point`` when the verb
    # shipped alone: the verb annotated the record while the state kept
    # calling a retirement a decision. The state IS the point now.
    assert retired.state == STATE_RETIRED
    assert retired.state != STATE_ACTED  # nobody decided this
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
    s.reconcile("proposal", [], empty_is_authoritative=True)

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
    # ``empty_is_authoritative=True`` because this test is about the VERB, and
    # without the declaration the breaker refuses the wipe and there is no
    # retirement to inspect — the test would pass its ``not in {...}`` checks
    # against a card that simply never retired.
    s.reconcile("slot_suggestion", [], empty_is_authoritative=True)

    verb = s.load()["slot_suggestion:task:task/X.md"].acted_action
    assert verb == ACTION_RETIRED
    assert verb not in {"accept", "done", "snooze"}
    assert verb is not None  # it IS stamped — not a no-op dressed as one


def test_a_non_authoritative_wholesale_wipe_is_REFUSED(tmp_path: Path) -> None:
    """PREMISE UPGRADED by the operator's A+C ruling. This test used to end
    "...and the reconcile proceeded exactly as before" — the tripwire observed
    the mass retirement and then allowed it, because the refusal half was
    unratified. It is ratified; the observation is now a refusal.

    A caller that has not declared ``empty_is_authoritative`` cannot tell a
    failed read from a quiet day, so its wholesale wipe is not obeyed.

    Mutation that reds this: delete the breaker from ``reconcile``."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])

    with structlog.testing.capture_logs() as captured:
        counts = s.reconcile("proposal", [])

    matches = [
        c for c in captured
        if c.get("event") == "feed.store.retirement_refused"
    ]
    assert len(matches) == 1
    assert matches[0]["log_level"] == "warning"
    assert matches[0]["kind"] == "proposal"
    assert matches[0]["count"] == 2
    assert matches[0]["ids"] == ["proposal:c1", "proposal:c2"]
    assert matches[0]["reason"] == "empty_open_set_not_authoritative"
    # THE CARDS SURVIVE — that is the refusal, not just its announcement.
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_OPEN
    assert folded["proposal:c2"].state == STATE_OPEN
    # And the counts SAY it was a refusal rather than a quiet fire: two
    # identical-looking zeros ("nothing retired") with opposite meanings.
    assert counts["retired"] == 0
    assert counts["refused"] == 2


def test_an_AUTHORITATIVE_wholesale_wipe_RETIRES(tmp_path: Path) -> None:
    """THE IMMORTAL-CARD HAZARD, pinned. A caller that separates failure from
    emptiness is believed, and a genuinely cleared kind retires normally — a
    free day clearing every slot suggestion is the lifecycle working.

    Without this the breaker would be a card-immortality machine: refuse, next
    fire refuses again, forever, and a kind that legitimately empties never
    clears. That is a worse failure than the one the breaker prevents.

    Mutation that reds this: make the breaker refuse authoritative empties too
    (drop the ``not empty_is_authoritative`` clause)."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])

    with structlog.testing.capture_logs() as captured:
        counts = s.reconcile("proposal", [], empty_is_authoritative=True)

    folded = s.load()
    assert folded["proposal:c1"].state == STATE_RETIRED
    assert folded["proposal:c2"].state == STATE_RETIRED
    assert counts["retired"] == 2
    assert counts["refused"] == 0
    # No refusal was announced — the caller was believed, silently.
    assert [
        c for c in captured
        if c.get("event") == "feed.store.retirement_refused"
    ] == []


def test_no_refusal_on_a_partial_retirement(tmp_path: Path) -> None:
    """POSITIVE CONTROL. A normal reconcile that retires SOME items must not
    warn — a tripwire that fires on ordinary traffic is one the operator
    learns to ignore, which is the same as not having it."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])

    with structlog.testing.capture_logs() as captured:
        s.reconcile("proposal", [_item("proposal", "c1")])

    assert [
        c for c in captured
        if c.get("event") == "feed.store.retirement_refused"
    ] == []
    assert s.load()["proposal:c2"].acted_action == ACTION_RETIRED  # it DID retire


def test_no_refusal_when_there_was_nothing_open(tmp_path: Path) -> None:
    """The other quiet case: an empty producer against an empty store retires
    nothing, so there is nothing to announce."""
    s = _store(tmp_path)
    with structlog.testing.capture_logs() as captured:
        s.reconcile("proposal", [])
    assert [
        c for c in captured
        if c.get("event") == "feed.store.retirement_refused"
    ] == []


def test_the_refusal_repeats_every_sweep_while_the_fault_persists(
    tmp_path: Path,
) -> None:
    """PREMISE INVERTED, deliberately, and the inversion is the ratification.

    As a TRIPWIRE this pinned the opposite: fires once per episode, never every
    sweep. That was correct THEN and is wrong NOW, and the reason is the whole
    difference between observing and refusing. The tripwire let the retirement
    proceed, so its own condition cleared — everything went terminal and the
    next empty fire found nothing present. The breaker REFUSES, so the cards
    stay open and the condition is still true on the next fire.

    Which means the warning repeats for as long as the producer stays broken —
    and that is what it should do. A refusal is an ONGOING fault, not an
    episode: a breaker that tripped silently from its second fire onward would
    be a fault going quiet while it was still actively suppressing
    retirements. The noise stops when the producer is fixed or adopts the
    contract, which is exactly the right lever.

    A producer that stays broken reconciles empty on every sweep. If the
    tripwire fired each time, it would be a warning every few minutes forever —
    which is the failure mode that trains an operator to ignore it, i.e. the
    same as not having it.

    It is self-limiting by construction: ``previously_present`` filters to
    OPEN/DEFERRED, so once the first empty reconcile has retired everything to
    ACTED, the second finds nothing present and says nothing. The mutation that
    reds this is widening that filter to include acted items."""
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1"), _item("proposal", "c2")])

    with structlog.testing.capture_logs() as first:
        s.reconcile("proposal", [])
    with structlog.testing.capture_logs() as second:
        s.reconcile("proposal", [])
    with structlog.testing.capture_logs() as third:
        s.reconcile("proposal", [])

    ev = "feed.store.retirement_refused"
    assert len([c for c in first if c.get("event") == ev]) == 1
    assert len([c for c in second if c.get("event") == ev]) == 1
    assert len([c for c in third if c.get("event") == ev]) == 1
    # ...and the cards are still open every time, which is WHY it repeats.
    folded = s.load()
    assert folded["proposal:c1"].state == STATE_OPEN
    assert folded["proposal:c2"].state == STATE_OPEN


# --- item 5: what a retired card does NEXT -----------------------------------


def test_a_retired_card_revives_when_its_producer_offers_it_again(
    tmp_path: Path,
) -> None:
    """The RESURFACE half of item 2's operator-facing sentence, pinned where it
    is actually decided.

    ``_act_locked`` tells the operator a withdrawn card "will come back on its
    own if it turns up again". That is a promise made by the router about
    behaviour owned by the store, so it is pinned here rather than trusted:
    reconcile upserts at ``state=open`` and ``_apply_event`` replaces the
    folded item wholesale.

    Mutation that reds this: make ``_revival_suppressed`` return True for
    ``STATE_RETIRED``.
    """
    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1")], empty_is_authoritative=True)
    s.reconcile("proposal", [], empty_is_authoritative=True)
    assert s.load()["proposal:c1"].state == STATE_RETIRED

    s.reconcile("proposal", [_item("proposal", "c1")], empty_is_authoritative=True)

    assert s.load()["proposal:c1"].state == STATE_OPEN


def test_a_retired_snapshot_card_revives_where_an_ACKED_one_stays_suppressed(
    tmp_path: Path,
) -> None:
    """The BEHAVIOUR CHANGE item 1 made here, asserted so it is deliberate
    rather than incidental — and its positive control in the same test.

    ``_revival_suppressed`` keeps a DECISION sticky on snapshot kinds: an acked
    appointment must not come back every morning. Before retirement was its own
    state it was stored as ``acted``, so a withdrawn-then-re-offered snapshot
    card was suppressed by that same rule — silently terminal forever, on the
    strength of a decision nobody made.

    Now ``retired`` falls through and revives, while ``acked`` still suppresses.
    Both halves are asserted here because either alone is passable by a broken
    build: pin only the revival and a build that suppresses NOTHING is green
    (re-opening the groundhog bug); pin only the suppression and item 1's change
    is invisible.
    """
    ev = {"date_iso": "2026-08-20", "name": "Dentist", "rec_type": "appt",
          "time_display": "09:00"}

    # ACKED — the decision is kept sticky. Unchanged behaviour, the control.
    s1 = _store(tmp_path / "acked")
    e1 = FeedItem.create(kind="event", stable_key="2026-08-20|Dentist",
                         instance="salem", title="Dentist", evidence=ev)
    s1.reconcile("event", [e1], empty_is_authoritative=True)
    s1.set_state(e1.id, STATE_ACKED)
    s1.reconcile("event", [e1], empty_is_authoritative=True)
    assert s1.load()[e1.id].state == STATE_ACKED, "an acked snapshot must stay acked"

    # RETIRED — no decision to keep sticky, so it comes back.
    s2 = _store(tmp_path / "retired")
    e2 = FeedItem.create(kind="event", stable_key="2026-08-20|Dentist",
                         instance="salem", title="Dentist", evidence=ev)
    s2.reconcile("event", [e2], empty_is_authoritative=True)
    s2.reconcile("event", [], empty_is_authoritative=True)
    assert s2.load()[e2.id].state == STATE_RETIRED
    s2.reconcile("event", [e2], empty_is_authoritative=True)
    assert s2.load()[e2.id].state == STATE_OPEN, (
        "a retirement is not a decision — there is nothing to keep sticky"
    )


def test_belt_logs_the_retired_alias_beside_acted(tmp_path: Path) -> None:
    """Item 3 shipped ``retired`` in the counts; this is the pin that it has a
    READER.

    A returned field nobody emits is indistinguishable from one that was never
    added, and the belt's ``feed.reconcile`` line was the counts' only consumer.
    Both names are asserted, and asserted EQUAL, because that equality is the
    migration contract the alias was shipped under.

    Mutation that reds this: drop ``retired=`` from the belt's log call.
    """
    from alfred.feed.belt import try_feed_reconcile

    s = _store(tmp_path)
    s.reconcile("proposal", [_item("proposal", "c1")], empty_is_authoritative=True)

    with structlog.testing.capture_logs() as captured:
        counts = try_feed_reconcile(s, "proposal", [], empty_is_authoritative=True)

    assert counts is not None and counts["retired"] == 1
    [line] = [c for c in captured if c.get("event") == "feed.reconcile"]
    assert line["retired"] == 1
    assert line["acted"] == line["retired"]


def test_sweep_excludes_a_retired_card_but_still_collects_an_open_one(
    tmp_path: Path,
) -> None:
    """``collect_live_cards`` filters to OPEN/DEFERRED. Item 5 asked whether a
    retired card can regress that; this is the answer, run rather than reasoned.

    It cannot, and the reason is worth pinning: the filter is an ALLOWLIST, so a
    state it has never been taught about is excluded by construction. Including
    one would be the groundhog bug — the sweep hands its result to a reconcile
    that upserts at ``state=open``, so a terminal card collected here would be
    resurrected on the next tick, and the one after.

    The open card is the POSITIVE CONTROL: without it, "retired is excluded"
    passes identically against a build whose sweep returns nothing at all.
    """
    from alfred.feed.sweep import collect_live_cards

    s = _store(tmp_path)
    s.upsert(_item("proposal", "gone"))
    s.upsert(_item("proposal", "live"))
    s.set_state("proposal:gone", STATE_RETIRED, action=ACTION_RETIRED)

    prepared = collect_live_cards(s, "proposal")

    assert [i.id for i in prepared.open_items] == ["proposal:live"]
    assert prepared.empty is False


def test_a_legacy_retirement_on_disk_still_loads(tmp_path: Path) -> None:
    """SCHEMA TOLERANCE across the item-1 boundary — the no-migration half of
    the ruling.

    Retirements written BEFORE the state existed are on disk as
    ``state=acted`` carrying ``acted_action=retired``, and the ruling was
    explicitly not to rewrite them. So the old shape must keep folding, and it
    must keep reading as ``acted`` rather than being silently reinterpreted —
    the record is the record.

    The unknown-field half is asserted in the same test because a state file
    from a NEWER build is the other direction of the same contract
    (``from_dict`` filters to known fields).
    """
    path = tmp_path / "feed.jsonl"
    legacy = FeedItem.create(kind="proposal", stable_key="old", instance="salem",
                             title="t").to_dict()
    legacy["a_field_from_a_future_build"] = "ignored"
    path.write_text(
        json.dumps({"ev": "upsert", "ts": "2026-08-01T00:00:00Z", "item": legacy}) + "\n"
        + json.dumps({"ev": "state", "ts": "2026-08-01T00:00:01Z",
                      "id": "proposal:old", "state": STATE_ACTED,
                      "action": ACTION_RETIRED}) + "\n",
        encoding="utf-8",
    )

    folded = FeedStore(path).load()

    assert folded["proposal:old"].state == STATE_ACTED
    assert folded["proposal:old"].acted_action == ACTION_RETIRED

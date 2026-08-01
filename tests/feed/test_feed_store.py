"""FeedStore pins — fold semantics, unknown-ev skip, schema-tolerance, reconcile
decided-detection, compaction, and the #37-style append-vs-compaction lock race.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from alfred.common.file_lock import file_rmw_lock
from alfred.feed.model import STATE_ACTED, STATE_OPEN, FeedItem
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
    assert counts1 == {"open": 2, "acted": 0}
    assert {i for i, it in s.load().items() if it.state == STATE_OPEN} == {"proposal:c1", "proposal:c2"}

    # Fire 2: c1 still open, c2 gone (decided elsewhere) → c2 becomes acted.
    counts2 = s.reconcile("proposal", [_item("proposal", "c1")])
    assert counts2 == {"open": 1, "acted": 1}
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
    assert counts == {"open": 2, "acted": 0}
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

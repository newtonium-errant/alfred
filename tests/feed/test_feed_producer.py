"""Daily-sync → feed translation pins (Feed Phase A producer #1).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

from alfred.daily_sync.feed_producer import build_feed_items, emit_sync_feed
from alfred.feed import STATE_ACTED, STATE_OPEN, FeedStore


class _Item:
    def __init__(self, **d):
        self._d = d

    def to_dict(self):
        return dict(self._d)


def test_email_stable_key_prefers_cluster_head() -> None:
    it = _Item(record_path="note/A.md", cluster_record_paths=["note/HEAD.md", "note/A.md"], sender="a@b.com", subject="Hi")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.id == "email_tier:note/HEAD.md"
    assert feed_item.instance == "salem"
    assert feed_item.evidence == it.to_dict()  # verbatim
    assert feed_item.mode == "decide"  # KIND_DEFAULTS applied


def test_email_stable_key_falls_back_to_record_path() -> None:
    it = _Item(record_path="note/A.md", cluster_record_paths=[], sender="a@b.com", subject="Hi")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.id == "email_tier:note/A.md"


def test_stable_keys_per_family() -> None:
    cases = {
        "attribution": (_Item(record_path="p/x.md", marker_id="inf-1"), "attribution:p/x.md|inf-1"),
        "proposal": (_Item(correlation_id="corr-9"), "proposal:corr-9"),
        "pending": (_Item(id="uuid-7"), "pending:uuid-7"),
        "routine_match": (_Item(query="walk dog", record="Daily", matched_to="Walk"), "routine_match:walk dog|Daily"),
        "radar": (_Item(record_path="digests/x.md", record_type="synthesis"), "radar:digests/x.md"),
        "friction": (_Item(event_id="ev-3"), "friction:ev-3"),
    }
    for kind, (item, expected_id) in cases.items():
        [feed_item] = build_feed_items(kind, [item], "salem")
        assert feed_item.id == expected_id, kind


def test_unkeyable_item_is_skipped() -> None:
    # A proposal item missing correlation_id can't be stably keyed → skipped
    # (never minted with an unstable id).
    assert build_feed_items("proposal", [_Item(record_type="person")], "salem") == []


def test_emit_sync_feed_decided_detection(tmp_path: Path) -> None:
    store = FeedStore(tmp_path / "feed.jsonl")
    # Fire 1: two proposals open.
    emit_sync_feed(store, "salem", proposal_items=[_Item(correlation_id="c1"), _Item(correlation_id="c2")])
    folded = store.load()
    assert folded["proposal:c1"].state == STATE_OPEN
    assert folded["proposal:c2"].state == STATE_OPEN

    # Fire 2: c1 still open, c2 gone (decided elsewhere) → c2 becomes acted.
    emit_sync_feed(store, "salem", proposal_items=[_Item(correlation_id="c1")])
    folded = store.load()
    assert folded["proposal:c1"].state == STATE_OPEN
    assert folded["proposal:c2"].state == STATE_ACTED


def test_emit_reconciles_every_family_even_empty(tmp_path: Path) -> None:
    store = FeedStore(tmp_path / "feed.jsonl")
    # Open a pending item, then a fire with NO pending items → it goes acted
    # (empty family this fire = the queue cleared).
    emit_sync_feed(store, "salem", pending_items=[_Item(id="u1")])
    assert store.load()["pending:u1"].state == STATE_OPEN
    emit_sync_feed(store, "salem")  # nothing this fire
    assert store.load()["pending:u1"].state == STATE_ACTED

"""Daily-sync → feed translation pins (Feed Phase A producer #1).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

from alfred.daily_sync.email_section import SENDER_PLACEHOLDER
from alfred.daily_sync.feed_producer import _SENDER_ABSENT, build_feed_items, emit_sync_feed
import structlog

from alfred.feed import STATE_ACTED, STATE_OPEN, STATE_RETIRED, FeedStore


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


# --- title: sender segment present when named, dropped when absent (#28) ------


def test_email_title_names_a_real_sender() -> None:
    it = _Item(record_path="note/A.md", sender="jamie@example.com", subject="Friday meeting")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.title == "Email tier: jamie@example.com — Friday meeting"


def test_email_title_drops_unknown_sender_segment() -> None:
    # email_section injects the literal "(unknown)" placeholder for a senderless
    # email — the title must not read "Email tier: (unknown) — ..." (#28).
    it = _Item(record_path="note/A.md", sender="(unknown)", subject="Your hold is ready")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.title == "Email tier: Your hold is ready"


def test_email_title_drops_empty_sender_segment() -> None:
    it = _Item(record_path="note/A.md", sender="", subject="Your hold is ready")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.title == "Email tier: Your hold is ready"


def test_email_title_drops_bare_unknown_sender_segment() -> None:
    # The other sentinel in email_section's no-sender set (case-insensitive).
    it = _Item(record_path="note/A.md", sender="Unknown", subject="Your hold is ready")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.title == "Email tier: Your hold is ready"


def test_email_title_absent_sender_and_subject() -> None:
    it = _Item(record_path="note/A.md", sender="(unknown)", subject="")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.title == "Email tier: (no subject)"


def test_email_title_named_sender_no_subject() -> None:
    it = _Item(record_path="note/A.md", sender="jamie@example.com", subject="")
    [feed_item] = build_feed_items("email_tier", [it], "salem")
    assert feed_item.title == "Email tier: jamie@example.com — (no subject)"


def test_title_sentinel_covers_email_section_placeholder() -> None:
    # Cross-module drift pin (#28): feed_producer keeps its own local
    # absent-sender sentinel set (no prod import, to avoid coupling), but it MUST
    # cover the placeholder email_section actually injects. If email_section ever
    # changes SENDER_PLACEHOLDER, this reddens here instead of the card title
    # silently regressing to "Email tier: <new-placeholder> — subject".
    assert SENDER_PLACEHOLDER.lower() in _SENDER_ABSENT


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
    assert folded["proposal:c2"].state == STATE_RETIRED


def test_emit_reconciles_every_family_even_empty(tmp_path: Path) -> None:
    """An EMPTY family still reconciles — ``[]`` means the caller read its
    source and there was nothing, so a cleared queue retires normally.

    The empty list is now passed EXPLICITLY. It used to rely on the argument's
    ``None`` default, which under the failure-vs-emptiness contract means the
    opposite thing (could-not-read → skip). Same two characters on the page,
    two opposite instructions — which is exactly the distinction the contract
    exists to draw, and this test is the one that would notice it being lost.
    """
    store = FeedStore(tmp_path / "feed.jsonl")
    emit_sync_feed(store, "salem", pending_items=[_Item(id="u1")])
    assert store.load()["pending:u1"].state == STATE_OPEN
    emit_sync_feed(store, "salem", pending_items=[])  # read it; nothing there
    assert store.load()["pending:u1"].state == STATE_RETIRED


def test_a_family_reported_UNREADABLE_is_skipped_not_emptied(
    tmp_path: Path,
) -> None:
    """THE CAUSE-LAYER CONTRACT, and the reason the lane touched this producer.

    ``None`` means the section could not be read. Its cards must be left ALONE
    — reconciling it to nothing would let one failed read terminalize the
    operator's open cards for that kind, which is the property the operator
    ratified against.

    Mutation that reds this: drop the ``if raw is None: continue`` guard so a
    None family falls through to ``build_feed_items`` and reconciles empty."""
    store = FeedStore(tmp_path / "feed.jsonl")
    emit_sync_feed(store, "salem", pending_items=[_Item(id="u1")])
    assert store.load()["pending:u1"].state == STATE_OPEN

    with structlog.testing.capture_logs() as captured:
        emit_sync_feed(store, "salem", pending_items=None)

    # Untouched — not retired, not refused, simply not reconciled.
    assert store.load()["pending:u1"].state == STATE_OPEN
    # Filtered to THIS kind: every family defaults to None, so a call naming
    # only ``pending`` legitimately reports the other six as unreadable too.
    # That default is the safe reading — a caller that says nothing about a
    # family has vouched for nothing — and production passes all seven.
    matches = [
        c for c in captured
        if c.get("event") == "feed.producer.family_unreadable"
        and c.get("kind") == "pending"
    ]
    assert len(matches) == 1
    assert matches[0]["log_level"] == "warning"

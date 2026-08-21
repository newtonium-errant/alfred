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


# --- PROVIDER_FOR_FAMILY: the two ends that are actually true (Task 0) --------
#
# The map's own comment used to claim this pin held it "against BOTH ends:
# every family has a provider, and every registered provider maps to a family."
# The second half is FALSE and could not be written: seven of the fourteen
# registered providers (capture_close, contracts_awaiting, demotion_proposals,
# tier_recurrence, stt_vocab, ticket_notify, triage_queue) feed no feed family
# at all, and correctly so. A pin written to the comment as it stood would have
# been RED against correct code. The comment has been corrected to match these.


def _register_every_section() -> list[str]:
    """Register all production section providers and return their names.

    Drives the REAL ``register()`` of each module rather than a hand-kept list,
    which is the whole point: a hand-kept list would drift in exactly the way
    this pin exists to catch. ``daemon.py`` calls these fifteen
    unconditionally (each provider self-gates by returning ``None`` when its
    instance hasn't opted in), so this is the production registry.
    """
    from alfred.daily_sync import (
        assembler,
        attribution_section,
        calibration_section,
        canonical_proposals_section,
        capture_close_section,
        contracts_awaiting_section,
        demotion_section,
        email_section,
        friction_section,
        pending_items_section,
        radar_section,
        recurrence_section,
        routine_match_section,
        stt_vocab_section,
        ticket_notify_section,
        triage_section,
    )

    assembler.clear_providers()
    for mod in (
        email_section, attribution_section, canonical_proposals_section,
        demotion_section, capture_close_section, contracts_awaiting_section,
        pending_items_section, radar_section, friction_section, triage_section,
        routine_match_section, stt_vocab_section, recurrence_section,
        ticket_notify_section, calibration_section,
    ):
        mod.register()
    return assembler.registered_providers()


def test_provider_family_map_covers_every_family() -> None:
    """END ONE — every feed family has a provider, and the map names no family
    that isn't one.

    This is the KeyError end. ``daemon.fire_once`` does a bare
    ``PROVIDER_FOR_FAMILY[kind]`` for each family, so a family added to
    ``_FAMILIES`` without a map entry raises mid-fire; a map entry for a family
    that no longer exists is dead weight that reads as coverage. Asserting SET
    EQUALITY rather than a subset closes both directions at once.

    Mutation that reds this: delete any entry from either dict.
    """
    from alfred.daily_sync.feed_producer import _FAMILIES, PROVIDER_FOR_FAMILY

    assert set(PROVIDER_FOR_FAMILY) == set(_FAMILIES)
    # Positive control: the sets are non-empty, so the equality above is a real
    # correspondence and not two empties agreeing about nothing.
    assert len(_FAMILIES) == 7


def test_every_mapped_provider_is_actually_registered() -> None:
    """END TWO — every provider NAME the map points at is one a section module
    really registers.

    This is the end with teeth, and it is the silent one. The map's values are
    matched against ``assembler.failed_sections()`` to decide which families
    are passed as ``None`` (could-not-read). A provider renamed in its own
    module and not here does not raise anything: the lookup simply stops
    matching, the failure signal goes permanently dark, and every existing test
    stays green while a broken section is once again indistinguishable from a
    quiet one.

    Mutation that reds this: rename a value, e.g. ``"attribution":
    "attribution_audit"`` → ``"attribution_audit_v2"``.
    """
    from alfred.daily_sync import assembler
    from alfred.daily_sync.feed_producer import PROVIDER_FOR_FAMILY

    try:
        registered = set(_register_every_section())
        assert set(PROVIDER_FOR_FAMILY.values()) <= registered
        # POSITIVE CONTROL for the subset above: a subset assertion passes
        # vacuously if the left side is empty AND says nothing about whether
        # ``registered`` was really populated. Both are pinned here, so the
        # subset is load-bearing.
        assert len(PROVIDER_FOR_FAMILY) == 7
        assert len(registered) == 15
    finally:
        # The registry is module-global; leaving it populated would leak into
        # every later test in the session.
        assembler.clear_providers()


def test_eight_registered_providers_deliberately_feed_no_family() -> None:
    """The NEGATIVE half, asserted rather than assumed — and the reason the
    map's original comment was wrong.

    Most registered providers are not feed families: they render a section for
    the operator to read and emit no cards. Naming them here means a future
    provider that SHOULD have had a family shows up as a diff on this list
    instead of silently joining the unmapped majority.
    """
    from alfred.daily_sync import assembler
    from alfred.daily_sync.feed_producer import PROVIDER_FOR_FAMILY

    try:
        unmapped = set(_register_every_section()) - set(PROVIDER_FOR_FAMILY.values())
        assert unmapped == {
            "capture_close", "contracts_awaiting", "demotion_proposals",
            "tier_recurrence", "stt_vocab", "ticket_notify", "triage_queue",
            # R4 (2026-08-21) — DELIBERATELY UNMAPPED, AND UNLIKE ITS SEVEN
            # NEIGHBOURS THIS ONE IS EXPECTED TO MOVE. The operator ruled the
            # calibration loop should have BOTH a feed card and a CLI + Daily
            # Sync surface. The CLI + section half shipped; the feed card is
            # BLOCKED on an operator decision, not on effort:
            #
            #   A quiet (fyi/fyi) card carrying plain confirm/reject verbs is
            #   produced, served and verb-carrying — and its verbs render
            #   NOWHERE. ``FeedRow``'s FYI affordance is an Ack (plus the
            #   attribution-only contest door); the board dispatches exactly two
            #   verbs (``ack`` / ``contest``, see ``useFeedBoard``); and generic
            #   verbs are dispatched only from the DECK, which admits an item
            #   via ``isDeckCandidate = mode === 'decide' || hasSuggestedChoice``
            #   — and ``hasSuggestedChoice`` needs a co-equal choice GROUP of
            #   >= 2 members, which a yes/no confirm is not.
            #
            # So the three options are: ring the phone (MODE_DECIDE), invent a
            # choice group the operator did not ask for, or widen the FYI row's
            # contract for every kind. All three are his call, so the card is
            # not built rather than built wrong — and this entry is the diff
            # that will surface when it is.
            "calibration_review",
        }
    finally:
        assembler.clear_providers()


# --- item 4: retirement is not a dark stratum --------------------------------


def test_retirement_summary_reports_the_kind_that_retired(tmp_path: Path) -> None:
    """The count line carries the BREAKDOWN, not just a total — "3 cards went
    away" without saying from where is a number the operator can't act on.

    Mutation that reds this: drop ``by_kind`` from the log call, or stop
    accumulating ``retired_by_kind``.
    """
    store = FeedStore(tmp_path / "feed.jsonl")
    emit_sync_feed(store, "salem", pending_items=[_Item(id="u1"), _Item(id="u2")])

    with structlog.testing.capture_logs() as captured:
        emit_sync_feed(store, "salem", pending_items=[_Item(id="u1")])

    [summary] = [
        c for c in captured
        if c.get("event") == "feed.producer.retirement_summary"
    ]
    assert summary["retired"] == 1
    assert summary["by_kind"] == {"pending": 1}
    assert "1 card(s) left their producer's open set" in summary["detail"]


def test_retirement_summary_fires_on_a_quiet_fire_too(tmp_path: Path) -> None:
    """ILB, and the case that makes this surface worth having.

    The all-zero fire is the COMMON one. If the line only appeared when
    something retired, then "nothing retired" and "the summary never ran"
    would be the same silence — which is the exact ambiguity item 4 was
    ratified to close, reintroduced at the surface built to close it.

    Mutation that reds this: guard the log call behind ``if total_retired:``.
    """
    store = FeedStore(tmp_path / "feed.jsonl")

    with structlog.testing.capture_logs() as captured:
        emit_sync_feed(store, "salem", pending_items=[])

    [summary] = [
        c for c in captured
        if c.get("event") == "feed.producer.retirement_summary"
    ]
    assert summary["retired"] == 0
    assert summary["by_kind"] == {}
    assert summary["detail"] == "no cards retired this fire"


def test_summary_distinguishes_a_quiet_zero_from_an_unreadable_one(
    tmp_path: Path,
) -> None:
    """THE POSITIVE CONTROL for the zero above, and the reason all four
    outcomes are on the line.

    Both fires retire nothing. One is healthy; in the other a family could not
    be read at all and its cards were deliberately left alone. A summary that
    reported only ``retired=0`` would render those identically — two
    identical-looking zeros with opposite meanings, one level up from the
    counts split that item 3 shipped to prevent exactly that.
    """
    store = FeedStore(tmp_path / "feed.jsonl")
    emit_sync_feed(store, "salem", pending_items=[_Item(id="u1")])

    with structlog.testing.capture_logs() as captured:
        emit_sync_feed(store, "salem", pending_items=None)  # could-not-read

    [summary] = [
        c for c in captured
        if c.get("event") == "feed.producer.retirement_summary"
    ]
    assert summary["retired"] == 0
    assert "pending" in summary["families_unreadable"]
    # The card it declined to retire is still open — the zero is explained by
    # the skip, and the skip is visible on the same line.
    assert store.load()["pending:u1"].state == STATE_OPEN

"""The MOC card's act path — driven END TO END from a real queue row.

Every test here goes through ``act()`` (the wire both the deck's swipe and its
hold-selector fire) and through ``moc_suggestion_feed_items`` (the producer the
brief calls), never the writer directly. That is the pin class that catches an
unwired dispatcher — the failure mode where a gate parameter is tested by
direct invocation, threaded nowhere, and every pin stays green while the
feature is dead in the field.

The row that starts each test is a REAL queue row written through the real
queue writer, not a hand-built FeedItem: the flow under test is
queue -> producer -> card -> act -> vault + queue, and hand-building the card
would skip the half of it that has never run in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from alfred.brief.feed_producer import moc_suggestion_feed_items
from alfred.daily_sync.action_router import MOC_APPLY_ACTION, act
from alfred.feed import FeedStore
from alfred.feed.model import STATE_ACTED, STATE_DEFERRED, STATE_OPEN
from alfred.surveyor.moc_suggester import MocSuggestion
from alfred.surveyor.moc_suggestion_queue import load_queue, upsert_proposals
from alfred.tier.sort_proposal import corrections_path_for


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "MOC").mkdir(parents=True, exist_ok=True)
    (vault / "zettel").mkdir(parents=True, exist_ok=True)
    (vault / "source").mkdir(parents=True, exist_ok=True)
    (vault / "note").mkdir(parents=True, exist_ok=True)
    return vault


def _moc(vault: Path, stem: str) -> str:
    (vault / "MOC" / f"{stem}.md").write_text(
        f"---\ntype: moc\nname: {stem}\ncreated: 2026-08-20\n---\n\n# Contents\n\n",
        encoding="utf-8",
    )
    return f"MOC/{stem}.md"


def _member(vault: Path, name: str, record_type: str = "zettel") -> str:
    (vault / record_type / f"{name}.md").write_text(
        f"---\ntype: {record_type}\nname: {name}\ncreated: 2026-08-20\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return f"{record_type}/{name}.md"


def _row(
    queue: Path,
    *,
    suggestion_id: str,
    target: str,
    members: list[str],
    signal: str = "member_overlap",
    created: str = "2026-06-01T00:00:00+00:00",
) -> MocSuggestion:
    row = MocSuggestion(
        id=suggestion_id,
        cluster_id_at_proposal=1,
        cluster_tags=["stoicism"],
        cluster_member_paths=sorted(members),
        target_moc_rel_path=target,
        proposed_new_moc_name=None,
        mapping_signal=signal,
        mapping_score=0.8,
        candidate_members_to_add=list(members),
        reasoning="3 of 5 cluster members already cite this MOC.",
        created=created,
    )
    upsert_proposals(
        queue, [row], max_pending_per_target=50, max_proposals_per_sweep=50,
    )
    return row


def _ds_config(tmp_path: Path, queue: Path):
    """A config carrying the surveyor queue path, exactly as the act router's
    ``_moc_queue_path`` reads it — the read and write sides must resolve to
    one file, so the test supplies the real shape rather than a stub."""
    from alfred.daily_sync.config import DailySyncConfig

    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")

    class _MocCfg:
        queue_path = str(queue)

    class _Surveyor:
        moc_suggestion = _MocCfg()
        state = None

    cfg.surveyor = _Surveyor()
    return cfg


def _deal(vault: Path, queue: Path, store: FeedStore, cap: int = 3):
    """Run the PRODUCER and put its cards in the store — the brief's own path."""
    tracked = {
        i: it.state for i, it in store.load().items()
        if it.kind == "moc_suggestion"
    }
    items = moc_suggestion_feed_items(
        vault, instance="hypatia", tracked=tracked, cap=cap, queue_path=queue,
    )
    assert items is not None
    for it in items:
        store.upsert(it)
    return items


def _act(store, cfg, feed_id, action_id, vault, target=None):
    return act(
        feed_id, action_id,
        feed_store=store, config=cfg, vault_path=vault,
        instance_name="hypatia", instance_scope="hypatia",
        correction_target=target,
    )


def _mocs_of(vault: Path, rel: str) -> list:
    return frontmatter.load(str(vault / rel)).metadata.get("mocs") or []


# ---------------------------------------------------------------------------
# THE OPERATOR'S FLOW, end to end
# ---------------------------------------------------------------------------


def test_affirm_applies_the_proposed_moc_and_retires_the_row(tmp_path: Path) -> None:
    """THE WIRE PIN. A real pending row deals a quiet card; the affirm writes
    the membership onto every member ON DISK; the queue row retires to
    ``applied``; the card is decided."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")  # the alternate — the hold family needs two
    members = [_member(vault, "Memento Mori"), _member(vault, "Amor Fati")]
    _row(queue, suggestion_id="ms-1", target=target, members=members)

    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    cards = _deal(vault, queue, store)

    assert len(cards) == 1
    card = cards[0]
    # QUIET — a proposal must never ring the phone.
    assert (card.mode, card.attention) == ("fyi", "fyi")
    # N ON THE FACE, before the swipe.
    assert card.title == "Add 2 notes to Roman Philosophy MOC?"

    result = _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=target)

    assert result.ok is True, result.detail
    # THE RECORD — the one durable thing. Read off disk, not off a return value.
    for rel in members:
        assert _mocs_of(vault, rel) == ["[[MOC/Roman Philosophy MOC]]"]
    # The queue row RETIRES.
    rows = {r.id: r for r in load_queue(queue)}
    assert rows["ms-1"].status == "applied"
    assert sorted(rows["ms-1"].applied_members) == sorted(members)
    # The card is decided.
    stored = store.load()[card.id]
    assert stored.state == STATE_ACTED
    assert stored.acted_action == MOC_APPLY_ACTION


def test_the_apply_ALSO_mirrors_into_the_MOC_contents(tmp_path: Path) -> None:
    """THE REGRESSION PIN for this lane's sharpest finding.

    One operator gesture writes TWO records: the member's ``mocs``
    frontmatter AND the MOC's ``# Contents`` body, the latter by the Sub-arc A
    hook — which calls ``vault_edit`` UNDER THE CALLER'S SCOPE. The first
    build of ``moc_apply`` had ``allow_body_writes: False`` on a docstring's
    claim that the mirror ran "under its own authority". It does not. The
    membership landed, the mirror failed with a logged ScopeError, and the
    result was a half-applied membership that reads as done on the member and
    is invisible on the MOC — precisely what the type gate exists to prevent,
    arriving through the other door.

    A POSITIVE observation is what pins it: the bullet is IN the MOC body. An
    absence-of-error assertion would have passed against the broken build,
    because the hook is failure-isolated and swallowed its own refusal."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    members = [_member(vault, "Memento Mori")]
    _row(queue, suggestion_id="ms-1", target=target, members=members)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    card = _deal(vault, queue, store)[0]

    result = _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=target)
    assert result.ok is True, result.detail

    body = (vault / target).read_text(encoding="utf-8")
    assert "- [[zettel/Memento Mori]]" in body
    # And the OTHER MOC was not touched — the mirror follows the chosen
    # target, not every MOC on disk.
    assert "Memento Mori" not in (vault / "MOC" / "Stoicism MOC.md").read_text(
        encoding="utf-8",
    )


def test_the_mirror_arm_does_not_open_a_general_body_surface(
    tmp_path: Path,
) -> None:
    """Arm 1 admits body writes ONLY on a ``moc`` record and ONLY with no
    frontmatter riding along — so permitting the mirror did not hand this
    scope a general body-write capability."""
    from alfred.vault.ops import vault_edit
    from alfred.vault.scope import ScopeError

    vault = _vault(tmp_path)
    _moc(vault, "Roman Philosophy MOC")
    member = _member(vault, "Memento Mori")

    # A body write on a MEMBER record is refused, and for the mirror reason.
    with pytest.raises(ScopeError) as exc:
        vault_edit(vault, member, body_append="TAMPER", scope="moc_apply")
    assert "may only write bodies on 'moc' records" in str(exc.value)
    assert "TAMPER" not in (vault / member).read_text(encoding="utf-8")

    # Frontmatter may not ride along with a mirror write.
    with pytest.raises(ScopeError) as exc2:
        vault_edit(
            vault, "MOC/Roman Philosophy MOC.md",
            body_append="x", set_fields={"mocs": ["[[MOC/Other]]"]},
            scope="moc_apply",
        )
    assert "may not change frontmatter in the same edit" in str(exc2.value)


def test_hold_choosing_a_different_moc_applies_the_CHOSEN_one(
    tmp_path: Path,
) -> None:
    """The selector path: choosing a NON-proposed target is one interaction on
    the same wire, writes the CHOSEN MOC (not the proposed one), and records
    proposed-vs-chosen as a correction."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    proposed = _moc(vault, "Roman Philosophy MOC")
    chosen = _moc(vault, "Stoicism MOC")
    members = [_member(vault, "Memento Mori")]
    _row(queue, suggestion_id="ms-1", target=proposed, members=members)

    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    card = _deal(vault, queue, store)[0]

    result = _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=chosen)

    assert result.ok is True, result.detail
    # The CHOSEN MOC landed and the PROPOSED one did not.
    assert _mocs_of(vault, members[0]) == ["[[MOC/Stoicism MOC]]"]

    # THE CORRECTION, in the platform's ruling-row shape (same writer as the
    # sort rotation's). This is the "how does this get better with use?"
    # answer: the suggester's proposal is scored against the operator's pick.
    rows = [
        json.loads(line)
        for line in corrections_path_for(store.path).read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["proposed"] == proposed
    assert rows[0]["chosen"] == chosen
    assert rows[0]["confirmed"] is False
    assert rows[0]["shape"] == "moc:member_overlap"


def test_a_quick_affirm_records_a_CONFIRMATION(tmp_path: Path) -> None:
    """The positive control for the correction pin above: the same wire, the
    PROPOSED target, records confirmed=True. Without this, the pin above
    passes against a build that records every ruling as a correction."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    members = [_member(vault, "Memento Mori")]
    _row(queue, suggestion_id="ms-1", target=target, members=members)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    card = _deal(vault, queue, store)[0]

    _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=target)

    rows = [
        json.loads(line)
        for line in corrections_path_for(store.path).read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["confirmed"] is True


# ---------------------------------------------------------------------------
# The target gate — fail-closed, both refusals named
# ---------------------------------------------------------------------------


def test_apply_without_a_target_is_refused_not_defaulted(tmp_path: Path) -> None:
    """Defaulting to the card's proposal would tell the operator their choice
    was taken while writing something they may not have picked."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    members = [_member(vault, "Memento Mori")]
    _row(queue, suggestion_id="ms-1", target=target, members=members)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    card = _deal(vault, queue, store)[0]

    result = _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=None)

    assert result.ok is False
    assert result.status == "invalid_action"
    assert "needs the MOC you meant" in result.detail
    # NOTHING was touched — not the record, not the queue, not the card.
    assert _mocs_of(vault, members[0]) == []
    assert load_queue(queue)[0].status == "pending"
    assert store.load()[card.id].state == STATE_OPEN


def test_a_target_the_card_never_offered_is_refused(tmp_path: Path) -> None:
    """The open-set analogue of the ceiling: the ceiling bounds the VERBS, and
    the card's served choices bound the TARGETS. A hand-crafted POST naming a
    MOC outside them is refused even though that MOC exists."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    smuggled = _moc(vault, "Unrelated MOC")
    members = [_member(vault, "Memento Mori")]
    _row(queue, suggestion_id="ms-1", target=target, members=members)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    # Narrow the card's own choices so the smuggled target is genuinely not
    # among them (the producer offers up to MOC_MAX_CHOICES).
    card = _deal(vault, queue, store)[0]
    item = store.load()[card.id]
    item.evidence["moc_choices"] = [
        {"target": target, "label": "Roman", "suggested": True},
        {"target": "MOC/Stoicism MOC.md", "label": "Stoicism", "suggested": False},
    ]
    store.upsert(item)

    result = _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=smuggled)

    assert result.ok is False
    assert result.status == "invalid_action"
    assert "isn't one of this card's choices" in result.detail
    assert _mocs_of(vault, members[0]) == []

    # POSITIVE CONTROL: an OFFERED target on the same card lands. Without it
    # this pin passes against a build that refuses every target.
    ok = _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=target)
    assert ok.ok is True
    assert _mocs_of(vault, members[0]) == ["[[MOC/Roman Philosophy MOC]]"]


# ---------------------------------------------------------------------------
# Reject defers, and the row survives the next sweep
# ---------------------------------------------------------------------------


def test_reject_defers_and_the_row_survives_the_next_sweep(tmp_path: Path) -> None:
    """The reject swipe is the quick defer — "not now", never a decline. The
    row must NOT be marked rejected, and the next producer run must carry the
    deferred card rather than retiring it (the reconcile-retires-deferred
    trap)."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    members = [_member(vault, "Memento Mori")]
    _row(queue, suggestion_id="ms-1", target=target, members=members)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    card = _deal(vault, queue, store)[0]

    result = _act(store, cfg, card.id, "defer", vault)
    assert result.ok is True
    assert store.load()[card.id].state == STATE_DEFERRED

    # The queue row is untouched — a defer is not a decision.
    assert load_queue(queue)[0].status == "pending"
    # Nothing was written.
    assert _mocs_of(vault, members[0]) == []

    # THE NEXT SWEEP still carries it. A deferred card absent from the emit
    # would be retired by reconcile, destroying the operator's own window.
    again = _deal(vault, queue, store)
    assert [c.id for c in again] == [card.id]


# ---------------------------------------------------------------------------
# Idempotence + the partial-apply state
# ---------------------------------------------------------------------------


def test_re_applying_the_same_membership_writes_nothing_twice(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    members = [_member(vault, "Memento Mori")]
    _row(queue, suggestion_id="ms-1", target=target, members=members)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    card = _deal(vault, queue, store)[0]

    _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=target)
    after_first = (vault / members[0]).read_text(encoding="utf-8")

    # A second card for the same row (a fresh deal after a hypothetical
    # re-propose) applies again — and changes nothing.
    from alfred.surveyor.moc_apply import apply_membership

    second = apply_membership(
        vault, member_rel_paths=members, target_moc_rel_path=target,
    )
    assert second.ok
    assert second.already == members
    assert (vault / members[0]).read_text(encoding="utf-8") == after_first


def test_a_partial_apply_keeps_the_row_and_the_card(tmp_path: Path) -> None:
    """2026-08-20 ruling: the row does NOT retire. It records the members that
    landed (so a retry is idempotent) and stays actionable with the failure
    visible; the card stays open for that retry."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    good = _member(vault, "Memento Mori")
    missing = "zettel/Vanished.md"
    _row(queue, suggestion_id="ms-1", target=target, members=[good, missing])
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path, queue)
    card = _deal(vault, queue, store)[0]

    result = _act(store, cfg, card.id, MOC_APPLY_ACTION, vault, target=target)

    assert result.ok is False
    assert "stays for a retry" in result.detail
    # The good member LANDED — one failure does not abandon the rest.
    assert _mocs_of(vault, good) == ["[[MOC/Roman Philosophy MOC]]"]
    # The row stays actionable, carrying what landed.
    row = load_queue(queue)[0]
    assert row.status == "pending"
    assert row.applied_members == [good]
    assert row.is_partial is True
    assert row.last_apply_error
    # The card stays open for the retry.
    assert store.load()[card.id].state == STATE_OPEN

    # THE RETRY SKIPS WHAT LANDED. The next deal drops the applied member
    # from the card's remaining set.
    again = _deal(vault, queue, store)
    assert again[0].evidence["members"] == [missing]
    assert again[0].evidence["already_applied_count"] == 1


# ---------------------------------------------------------------------------
# The backlog — bounded, declared order
# ---------------------------------------------------------------------------


def test_the_backlog_deals_oldest_first_and_is_capped(tmp_path: Path) -> None:
    """Multiple stuck-pending rows deal in a bounded, declared order: cap 3
    (the rotation's ratified 2-3/day), oldest first — backlog semantics, where
    the longest-stuck row has waited longest to be answered."""
    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    for n, day in enumerate(["05", "01", "03", "04", "02"], start=1):
        _row(
            queue, suggestion_id=f"ms-{n}", target=target,
            members=[_member(vault, f"Note {n}")],
            created=f"2026-06-{day}T00:00:00+00:00",
        )
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cards = _deal(vault, queue, store, cap=3)

    assert len(cards) == 3
    # Oldest first: ms-2 (06-01), ms-5 (06-02), ms-3 (06-03).
    assert [c.evidence["suggestion_id"] for c in cards] == ["ms-2", "ms-5", "ms-3"]


def test_propose_new_rows_are_counted_not_dealt(tmp_path: Path) -> None:
    """v1 is member_overlap only — but a reader that silently reads a fraction
    of the queue is a reader that lies about it. The propose_new population is
    NAMED in an always-emitted readout, and the rows stay pending (not
    wiped)."""
    import structlog

    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")
    _moc(vault, "Stoicism MOC")
    _row(
        queue, suggestion_id="ms-1", target=target,
        members=[_member(vault, "Memento Mori")],
    )
    new_row = MocSuggestion(
        id="ms-2", cluster_id_at_proposal=2, cluster_tags=["new"],
        cluster_member_paths=["zettel/X.md"], target_moc_rel_path=None,
        proposed_new_moc_name="Ethics MOC", mapping_signal="propose_new",
        mapping_score=0.0, candidate_members_to_add=["zettel/X.md"],
        reasoning="No existing MOC matched.", created="2026-06-02T00:00:00+00:00",
    )
    upsert_proposals(
        queue, [new_row], max_pending_per_target=50, max_proposals_per_sweep=50,
    )
    store = FeedStore(str(tmp_path / "feed.jsonl"))

    with structlog.testing.capture_logs() as captured:
        cards = _deal(vault, queue, store)

    # Only the actionable row is dealt.
    assert [c.evidence["suggestion_id"] for c in cards] == ["ms-1"]
    # The gap is NAMED, with every field present.
    readouts = [
        c for c in captured
        if c.get("event") == "brief.moc_suggestion.queue_readout"
    ]
    assert len(readouts) == 1
    assert readouts[0]["pending"] == 2
    assert readouts[0]["actionable"] == 1
    assert readouts[0]["deferred_propose_new"] == 1
    assert "propose_new" in readouts[0]["reason"]
    # And the row is NOT wiped — the backlog survives the reader.
    assert {r.id for r in load_queue(queue)} == {"ms-1", "ms-2"}


def test_a_row_with_no_alternate_is_not_dealt_and_says_why(
    tmp_path: Path,
) -> None:
    """A quiet card with no co-equal family is not a deck candidate at all, so
    emitting one would serve an apply verb onto a surface with no gesture. The
    producer withholds it and NAMES the reason."""
    import structlog

    vault = _vault(tmp_path)
    queue = tmp_path / "moc_suggestions.jsonl"
    target = _moc(vault, "Roman Philosophy MOC")  # the ONLY MOC
    _row(
        queue, suggestion_id="ms-1", target=target,
        members=[_member(vault, "Memento Mori")],
    )
    store = FeedStore(str(tmp_path / "feed.jsonl"))

    with structlog.testing.capture_logs() as captured:
        cards = _deal(vault, queue, store)

    assert cards == []
    skips = [
        c for c in captured
        if c.get("event") == "brief.moc_suggestion.skipped_no_choice_family"
    ]
    assert len(skips) == 1
    assert skips[0]["skipped"] == 1

    # POSITIVE CONTROL: add a SECOND MOC and the same row deals.
    _moc(vault, "Stoicism MOC")
    assert len(_deal(vault, queue, store)) == 1

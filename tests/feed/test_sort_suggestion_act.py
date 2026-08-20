"""The rotation card's act path — accept-the-proposal, choose-instead, not-now.

Driven through ``act()`` (the wire both the deck's swipe and its hold-selector
fire), never the writer directly — the pin class that catches an unwired
dispatcher. The writer itself is ``tier.sort_writer.assign_slot``, the SAME
function the board's sort verbs call; its own semantics are pinned in
``tests/tier/test_sort_affordance.py`` and deliberately not re-pinned here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import pytest
import structlog

from alfred.daily_sync.action_router import (
    SORT_ACTION_BY_SLOT,
    SORT_SUGGESTION_KIND,
    act,
    actions_for,
    actions_for_item,
)
from alfred.feed import FeedItem, FeedStore
from alfred.feed.model import (
    STATE_ACKED,
    STATE_ACTED,
    STATE_DEFERRED,
    STATE_OPEN,
)
from alfred.tier.sort_proposal import corrections_path_for, load_tally

NOW = datetime(2026, 8, 19, 13, 0, 0, tzinfo=timezone.utc)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True, exist_ok=True)
    return vault


def _task(vault: Path, name: str) -> None:
    (vault / "task" / f"{name}.md").write_text(
        f"---\ntype: task\nstatus: todo\nname: {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _card(
    store: FeedStore,
    *,
    name: str = "Fix shed door",
    proposed: str | None = "duty",
    state: str | None = None,
) -> FeedItem:
    evidence = {
        "origin": "task",
        "name": name,
        "path": f"task/{name}.md",
        "tier": 2,
        "slot": "unslotted",
        "slot_rule": "no_signal",
    }
    if proposed is not None:
        evidence["proposed_slot"] = proposed
        evidence["proposed_rule"] = "default_duty"
        evidence["proposal_shape"] = "task|due:n|t2"
    item = FeedItem.create(
        kind=SORT_SUGGESTION_KIND,
        stable_key=f"task:task/{name}.md",
        instance="salem",
        title=f"Sort: {name}",
        evidence=evidence,
    )
    store.upsert(item)
    if state is not None:
        store.set_state(item.id, state)
    return item


def _ds_config(tmp_path: Path):
    from alfred.daily_sync.config import DailySyncConfig

    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _act(store: FeedStore, cfg, feed_id: str, action_id: str, vault_path: Path | None):
    return act(
        feed_id, action_id,
        feed_store=store, config=cfg, vault_path=vault_path,
        instance_name="salem", instance_scope="talker",
    )


# --- the accept-proposal path (quick affirm) ----------------------------------


def test_quick_affirm_writes_the_proposed_slot_and_decides_the_card(tmp_path: Path) -> None:
    """THE WIRE PIN: the affirm gesture's verb (the proposed slot's sort verb)
    lands the ruling on the RECORD, answers with the server-confirmed render,
    decides the card (acted + verb-stamped), and records a CONFIRMED ruling in
    the corrections store — the whole gesture-grammar contract in one drive."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store, proposed="duty")

    result = _act(store, cfg, card.id, "sort_duty", vault)

    assert result.ok is True
    assert result.status == "sorted"
    assert result.render == {"slot": "duty", "sorted": True}
    # The record — the one durable thing.
    assert frontmatter.load(
        str(vault / "task" / "Fix shed door.md")
    ).metadata["slot"] == "duty"
    # The card is DECIDED (unlike the board sibling, whose card stays open).
    stored = store.load()[card.id]
    assert stored.state == STATE_ACTED
    assert stored.acted_action == "sort_duty"
    # The correction signal: a confirmation, keyed by the deal-time shape.
    tally = load_tally(corrections_path_for(store.path))
    assert tally == {"task|due:n|t2": {"duty": 1}}


def test_hold_choose_different_records_a_correction(tmp_path: Path) -> None:
    """The selector path: choosing a NON-proposed slot is one interaction (the
    same act wire), writes the chosen slot, and records proposed-vs-chosen as a
    correction — part (1) of the standard, captured at the act."""
    import json as _json

    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store, proposed="duty")

    result = _act(store, cfg, card.id, "sort_fuel", vault)

    assert result.ok is True
    assert frontmatter.load(
        str(vault / "task" / "Fix shed door.md")
    ).metadata["slot"] == "fuel"
    rows = [
        _json.loads(line)
        for line in corrections_path_for(store.path).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["proposed"] == "duty"
    assert rows[0]["chosen"] == "fuel"
    assert rows[0]["confirmed"] is False
    assert rows[0]["shape"] == "task|due:n|t2"


def test_a_capture_failure_never_costs_the_sort(tmp_path: Path, monkeypatch) -> None:
    """The belt, driven: the learning store raising leaves the ruling APPLIED
    and the card decided, with the degradation NAMED in the log (ILB — a
    rotation that silently stopped learning is undiagnosable)."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store, proposed="duty")

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("alfred.tier.sort_proposal.record_ruling", _boom)
    with structlog.testing.capture_logs() as cap:
        result = _act(store, cfg, card.id, "sort_duty", vault)

    assert result.ok is True
    assert store.load()[card.id].state == STATE_ACTED
    assert frontmatter.load(
        str(vault / "task" / "Fix shed door.md")
    ).metadata["slot"] == "duty"
    fails = [c for c in cap if c.get("event") == "feed.act.sort.capture_failed"]
    assert len(fails) == 1 and fails[0]["error_type"] == "OSError"


def test_a_proposal_less_card_still_sorts_with_the_skip_named(tmp_path: Path) -> None:
    """The degraded/legacy payload: no proposal means no scoreable ruling, so
    the capture is SKIPPED and says so — but the operator's sort still lands
    and still decides the card."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store, proposed=None)

    with structlog.testing.capture_logs() as cap:
        result = _act(store, cfg, card.id, "sort_rhythm", vault)

    assert result.ok is True
    assert store.load()[card.id].state == STATE_ACTED
    assert not corrections_path_for(store.path).exists()
    skips = [c for c in cap if c.get("event") == "feed.act.sort.capture_skipped"]
    assert len(skips) == 1


def test_a_failed_write_decides_nothing_and_records_nothing(tmp_path: Path) -> None:
    """The refusal end: a missing backing record answers ``unsupported_item``
    (the card moved on, not a bad request), the card stays OPEN, and no ruling
    is recorded — a correction signal from a write that never landed would
    teach the proposer from fiction."""
    vault = _vault(tmp_path)  # note: no task file written
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store, proposed="duty")

    result = _act(store, cfg, card.id, "sort_duty", vault)

    assert result.ok is False
    assert result.status == "unsupported_item"
    assert store.load()[card.id].state == STATE_OPEN
    assert not corrections_path_for(store.path).exists()


# --- the other verbs on the card ----------------------------------------------


def test_reject_is_the_quick_defer_and_lands_deferred(tmp_path: Path) -> None:
    """'Not now' (the ruling's reject swipe): the generic defer dispatcher
    answers — the card goes DEFERRED, not acted, and no vault write happens.
    The intercept ordering is load-bearing: the defer branch sits ABOVE the
    sort intercept, so this can never be swallowed into a sort."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store, proposed="duty")

    result = _act(store, cfg, card.id, "defer", vault)

    assert result.ok is True
    assert store.load()[card.id].state == STATE_DEFERRED
    assert "slot" not in frontmatter.load(
        str(vault / "task" / "Fix shed door.md")
    ).metadata
    assert not corrections_path_for(store.path).exists()  # reject = no signal


def test_ack_on_the_fyi_card_acks(tmp_path: Path) -> None:
    """The universal FYI ack stays reachable ((FYI, FYI) kind) — and honest:
    an acked-but-still-unsorted episode item revives at the next fire, which
    the emit-half tests cover; the defer family is the intended 'not now'."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store)

    result = _act(store, cfg, card.id, "ack", vault)

    assert result.ok is True
    assert store.load()[card.id].state == STATE_ACKED


def test_an_unmapped_verb_is_invalid_action(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store)

    for verb in ("accept", "done", "snooze_1d", "banana"):
        result = _act(store, cfg, card.id, verb, vault)
        assert result.ok is False, verb
        assert result.status == "invalid_action", verb
    assert store.load()[card.id].state == STATE_OPEN


def test_a_deferred_card_is_not_sortable_with_positive_control(tmp_path: Path) -> None:
    """The folded-state gate holds for this kind (no exemption): a parked card
    is not actionable until it returns. Positive control: the same card, back
    to open, sorts."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _card(store, proposed="duty", state=STATE_DEFERRED)

    parked = _act(store, cfg, card.id, "sort_duty", vault)
    assert parked.ok is True  # idempotent noop, not an error...
    assert parked.status == "already_acted"  # ...and nothing was written:
    assert "slot" not in frontmatter.load(
        str(vault / "task" / "Fix shed door.md")
    ).metadata

    store.set_state(card.id, STATE_OPEN)
    assert _act(store, cfg, card.id, "sort_duty", vault).status == "sorted"


# --- structure ----------------------------------------------------------------


def test_the_rotation_sort_can_never_reach_a_completion_or_accept_writer() -> None:
    """STRUCTURAL, mirroring the board sibling's pin — one level deep (direct
    references in the dispatcher's own code object), not a transitive proof.
    ``set_state`` is NOT in the forbidden set here, unlike the sibling's: this
    dispatcher's contract is that a sort DECIDES the rotation card, and the
    positive assertion below pins that the call is really there — deleting it
    would strand every sorted card open and re-deal it forever."""
    from alfred.daily_sync.action_router import _dispatch_sort_ruling

    names = set(_dispatch_sort_ruling.__code__.co_names)
    for consts in _dispatch_sort_ruling.__code__.co_consts:
        if isinstance(consts, tuple):
            names |= {str(c) for c in consts}
    forbidden = {
        "mark_task_done", "confirm_slot_candidate", "_dispatch_slot_completion",
        "_dispatch_slot_confirm", "_dispatch_slot_snooze",
    }
    assert not (names & forbidden), f"sort path can reach: {names & forbidden}"
    assert "set_state" in names  # the deciding write — this kind's contract
    assert "assign_slot" in names  # the ONE writer, shared with the board


# --- the served verbs (the wire the deck derives everything from) -------------


def test_the_kind_level_table_has_no_gesture_on_any_sort_verb() -> None:
    """CO-EQUALITY AT THE KIND LEVEL: no sort verb is the static "yes". The
    kind-level affirm gesture count is zero; the quick defer carries the
    reject gesture (the 'not now' swipe); the three sort verbs carry the
    choice group."""
    served = actions_for(SORT_SUGGESTION_KIND)
    by_verb = {v["verb"]: v for v in served}
    assert set(by_verb) == {
        "sort_duty", "sort_rhythm", "sort_fuel",
        "defer", "defer_1d", "defer_3d", "defer_7d",
    }
    assert [v["verb"] for v in served if v.get("gesture") == "affirm"] == []
    assert [v["verb"] for v in served if v.get("gesture") == "reject"] == ["defer"]
    assert by_verb["defer"]["label"] == "Not now"
    for verb in ("sort_duty", "sort_rhythm", "sort_fuel"):
        assert by_verb[verb]["group"] == "slot", verb
        assert by_verb[verb]["weight"] == "light", verb


@pytest.mark.parametrize("slot", ["duty", "rhythm", "fuel"])
def test_the_proposal_becomes_the_items_affirm_gesture(slot: str) -> None:
    """THE INVARIANT (not a count): exactly one gesture-bearing affirm verb per
    served item, and it is the verb matching the card's OWN proposed_slot — so
    the swipe means "accept the proposal" and no slot is ever the static yes."""
    item = {
        "kind": SORT_SUGGESTION_KIND,
        "evidence": {"proposed_slot": slot, "proposal_shape": "task|due:n|t2"},
    }
    served = actions_for_item(item)
    affirmed = [v["verb"] for v in served if v.get("gesture") == "affirm"]
    assert affirmed == [SORT_ACTION_BY_SLOT_INVERSE[slot]]
    # The other two group members stay gesture-free — co-equal, one hold away.
    others = [
        v for v in served
        if v.get("group") == "slot" and v["verb"] != affirmed[0]
    ]
    assert len(others) == 2
    assert all("gesture" not in v for v in others)


SORT_ACTION_BY_SLOT_INVERSE = {v: k for k, v in SORT_ACTION_BY_SLOT.items()}


def test_a_card_with_no_scoreable_proposal_serves_no_affirm_gesture() -> None:
    """The degraded belt: an unrecognised or missing proposal stamps nothing —
    the deck will not deal a swipe whose meaning it cannot state, and the verbs
    stay served (menu-reachable, honest)."""
    for evidence in ({}, {"proposed_slot": "banana"}, {"proposed_slot": ""}):
        served = actions_for_item({"kind": SORT_SUGGESTION_KIND, "evidence": evidence})
        assert [v for v in served if v.get("gesture") == "affirm"] == [], evidence
        assert {v["verb"] for v in served if v.get("group") == "slot"} == {
            "sort_duty", "sort_rhythm", "sort_fuel",
        }


def test_the_per_item_stamp_does_not_leak_into_the_kind_table() -> None:
    """``actions_for`` builds fresh dicts per call — pin it: serving an item
    with a proposal must not mutate the kind-level answer the NEXT caller gets
    (a leak here would make the first proposal of the day every card's yes)."""
    actions_for_item({
        "kind": SORT_SUGGESTION_KIND,
        "evidence": {"proposed_slot": "fuel"},
    })
    kind_level = actions_for(SORT_SUGGESTION_KIND)
    assert [v for v in kind_level if v.get("gesture") == "affirm"] == []


def test_the_web_defer_quick_constant_matches_the_router() -> None:
    """CROSS-LANGUAGE DRIFT PIN, server half — the SORT_ACTION_BY_SLOT pin's
    shape (tests/tier/test_sort_affordance.py). The client keeps a hand-typed
    copy of the quick defer's id (`DEFER_QUICK_ACTION`) because it needs the
    spelling BEFORE any response exists — to say the honest 'not now' toast
    instead of 'Rejected.'. Parsed from the TS source, never restated here."""
    import re

    from alfred.daily_sync.action_router import DEFER_NEXT_RENDER

    ts = (
        Path(__file__).resolve().parents[2]
        / "web" / "lib" / "algernon" / "feedConstants.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"export const DEFER_QUICK_ACTION = '([^']+)';", ts)
    assert match, "DEFER_QUICK_ACTION not found in feedConstants.ts"
    assert match.group(1) == DEFER_NEXT_RENDER


# --- the operator surface (part 3 of the standard) ----------------------------


def test_the_learning_readout_dispatches_through_the_real_cli(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """CLI DISPATCH PIN (the registered-but-unwired trap, per the T5 ledger):
    ``alfred tier-sort-learning`` through ``cli.main()`` itself — parser,
    handlers dict, path derivation and render in one drive. The store carries
    an active override so the assertion is on CONTENT the readout could only
    produce by really folding the store, not on the command merely existing."""
    import sys

    from alfred.cli import main as cli_main
    from alfred.tier.sort_proposal import record_ruling

    (tmp_path / "data").mkdir()
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        f"vault:\n  path: {tmp_path / 'vault'}\n"
        f"feed:\n  store_path: {tmp_path / 'data' / 'feed.jsonl'}\n",
        encoding="utf-8",
    )
    for _ in range(3):
        record_ruling(
            tmp_path / "data" / "sort_corrections.jsonl",
            shape="task|due:n|t2", proposed="duty", chosen="fuel",
        )

    monkeypatch.setattr(
        sys, "argv", ["alfred", "--config", str(cfg), "tier-sort-learning"],
    )
    cli_main()

    out = capsys.readouterr().out
    assert "rulings: 3" in out
    assert "correction rate: 100%" in out
    assert "proposes fuel (was duty via default_duty, 3 rulings)" in out


def test_the_readout_says_empty_rather_than_nothing(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """ILB half of the dispatch pin: no store → an explicit sentence."""
    import sys

    from alfred.cli import main as cli_main

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        f"vault:\n  path: {tmp_path / 'vault'}\n"
        f"feed:\n  store_path: {tmp_path / 'data' / 'feed.jsonl'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys, "argv", ["alfred", "--config", str(cfg), "tier-sort-learning"],
    )
    cli_main()

    out = capsys.readouterr().out
    assert "no rulings recorded yet" in out

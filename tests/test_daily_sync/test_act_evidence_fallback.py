"""An aged-out batch must not throw away the operator's verdict (P1, 2026-08-15).

He swiped five email cards; every act 409'd ``aged_out_of_last_batch`` because
the fire that dealt them never persisted its authority (see
``test_web_only_batch_authority.py`` for that half). Two spam and three confirm
verdicts went nowhere — and worse than a visible refusal, the next reconcile
would have retired those cards as ``acted``, recording as decided the very
decisions the system had just declined to accept.

So ``_load_batch_item`` missing now falls back to the card's OWN stamped
evidence instead of refusing. That is sound because the evidence IS the batch
item: ``build_feed_items`` stamps ``evidence=d`` verbatim from the same dict the
resolver consumes, and reconcile re-upserts every still-open card each fire.

THE TWO VERB CLASSES ARE PINNED SEPARATELY because they need different things,
and a fallback that served only one would look complete:
  * ``confirm`` is RELATIVE — it means "the classifier's tier was right", so it
    resolves to ``andrew_priority = classifier_priority`` and is contentless
    without the classifier's guess;
  * ``spam`` is ABSOLUTE — an explicit ``new_tier`` needing no classifier
    context at all.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.daily_sync import action_router as arouter
from alfred.daily_sync.action_router import (
    STATUS_ACTED,
    STATUS_ALREADY_ACTED,
    STATUS_STALE_ITEM,
    act,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.corpus import iter_corrections
from alfred.daily_sync.feed_producer import build_feed_items
from alfred.feed import FeedStore


def _cfg(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _email(priority: str = "medium", path: str = "email/Jun Note.md") -> dict[str, Any]:
    return {
        "record_path": path,
        "subject": "An old thing",
        "sender": "someone@example.com",
        "classifier_priority": priority,
        "classifier_reason": "looked routine",
        "item_number": 1,
    }


def _publish(store: FeedStore, item: dict[str, Any]) -> str:
    fis = build_feed_items("email_tier", [item], "salem")
    assert len(fis) == 1
    store.upsert(fis[0])
    return fis[0].id


def _aged_out(cfg: DailySyncConfig) -> None:
    """The production condition: a batch exists but does NOT hold this card."""
    save_state(cfg.state.path, {
        "last_batch": {"date": "2026-08-14", "items": [], "message_ids": []},
    })


def _seed_batch(cfg: DailySyncConfig, items: list[dict[str, Any]]) -> None:
    save_state(cfg.state.path, {
        "last_batch": {"date": "2026-08-15", "items": items, "message_ids": [9001]},
    })


def _call(store: FeedStore, cfg: DailySyncConfig, fid: str, action: str):
    return act(
        fid, action, feed_store=store, config=cfg, vault_path=None,
        instance_name="salem", instance_scope="talker",
    )


# ---------------------------------------------------------------------------
# both verb classes complete through the fallback
# ---------------------------------------------------------------------------


def test_relative_verb_resolves_against_the_cards_own_classifier_tier(
    tmp_path: Path,
) -> None:
    """``confirm`` needs the classifier's guess; the card carries it."""
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email(priority="medium"))
    _aged_out(cfg)

    result = _call(store, cfg, fid, "confirm")

    assert result.ok is True and result.status == STATUS_ACTED
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    assert rows[0].record_path == "email/Jun Note.md"
    assert rows[0].andrew_priority == "medium"  # == the evidence's classifier tier
    assert store.load()[fid].state == "acted"


def test_absolute_verb_needs_no_classifier_context(tmp_path: Path) -> None:
    """``spam`` states its own tier — it never needed the batch at all, which
    is why two of the five lost verdicts were lost for no reason whatsoever."""
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email(priority="medium"))
    _aged_out(cfg)

    result = _call(store, cfg, fid, "spam")

    assert result.ok is True and result.status == STATUS_ACTED
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    assert rows[0].andrew_priority == "spam"


# ---------------------------------------------------------------------------
# it is a FALLBACK, not a replacement
# ---------------------------------------------------------------------------


def test_resident_batch_wins_over_stamped_evidence(tmp_path: Path) -> None:
    """When the batch HAS the item, the batch is still the authority.

    The two sources are given deliberately DIFFERENT classifier tiers so the
    result names which one was consulted. Without this, a build that resolved
    everything from evidence — abandoning the authority rather than falling
    back to the card — would pass every other test in this file.
    """
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    # The CARD says medium (what it was stamped with when dealt)...
    fid = _publish(store, _email(priority="medium"))
    # ...while the RESIDENT batch says low (the classifier moved since).
    _seed_batch(cfg, [_email(priority="low")])

    result = _call(store, cfg, fid, "confirm")

    assert result.ok is True
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    assert rows[0].andrew_priority == "low", "the resident batch must win"


def test_unusable_evidence_still_refuses(tmp_path: Path) -> None:
    """The fallback needs something to fall back TO.

    An evidence-less card (a hand-written store line, an older schema) has no
    authority anywhere, so refusing is right — and the refusal carries its own
    reason so it is distinguishable in the log from the aged-out case the
    fallback now handles.
    """
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email())
    stored = store.load()[fid]
    stored.evidence = {}
    store.upsert(stored)
    _aged_out(cfg)

    with structlog.testing.capture_logs() as cap:
        result = _call(store, cfg, fid, "confirm")

    assert result.ok is False and result.status == STATUS_STALE_ITEM
    assert list(iter_corrections(cfg.corpus.path)) == []
    stale = [c for c in cap if c.get("event") == "feed.act.stale_item"]
    assert len(stale) == 1
    assert stale[0]["reason"] == "aged_out_of_last_batch_and_no_evidence"


# ---------------------------------------------------------------------------
# BOTH safety claims the fallback rides on (neither reads last_batch)
# ---------------------------------------------------------------------------


def test_folded_state_still_prevents_double_application(tmp_path: Path) -> None:
    """SAFETY CLAIM 1 — the ``open``-only gate, exercised ON the fallback path.

    ``append_correction`` is an unconditional append, so "applied once" is a
    property of the state machine, not of the writer. A second swipe on an
    already-applied card must be an idempotent no-op even though the batch is
    still absent and the fallback would happily resolve it again.
    """
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email())
    _aged_out(cfg)

    first = _call(store, cfg, fid, "confirm")
    second = _call(store, cfg, fid, "confirm")

    assert first.status == STATUS_ACTED
    assert second.status == STATUS_ALREADY_ACTED
    assert len(list(iter_corrections(cfg.corpus.path))) == 1


def test_per_item_lock_still_serializes_on_the_fallback_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAFETY CLAIM 2 — the per-item mutex, exercised ON the fallback path.

    Same ``_dispatch_barrier`` seam the resident-batch concurrency pin uses: the
    lock holder parks inside the critical section while the second thread is
    PROVEN not to have entered (it never reaches the seam). Pinned here because
    the fallback must not become a second entrance to the resolver that skips
    the serialization the resident path gets.
    """
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email())
    _aged_out(cfg)

    arrivals = threading.Semaphore(0)
    gate = threading.Event()

    def _seam(feed_item_id: str) -> None:
        arrivals.release()
        assert gate.wait(timeout=5)

    monkeypatch.setattr(arouter, "_dispatch_barrier", _seam)

    results: dict[str, Any] = {}

    def _worker(name: str) -> None:
        results[name] = _call(store, cfg, fid, "confirm")

    t1 = threading.Thread(target=_worker, args=("t1",), name="t1")
    t2 = threading.Thread(target=_worker, args=("t2",), name="t2")
    t1.start()
    t2.start()

    assert arrivals.acquire(timeout=5) is True
    second_reached = arrivals.acquire(timeout=0.5)
    gate.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert second_reached is False, "per-item lock failed on the fallback path"
    assert len(list(iter_corrections(cfg.corpus.path))) == 1
    assert sorted(r.status for r in results.values()) == [STATUS_ACTED, STATUS_ALREADY_ACTED]


# ---------------------------------------------------------------------------
# observability (feedback_log_emission_test_pattern)
# ---------------------------------------------------------------------------


def test_fallback_announces_itself(tmp_path: Path) -> None:
    """The act SUCCEEDS on a path the operator cannot see.

    Without this line, "resolved from the card" is indistinguishable in the log
    from the ordinary resident-batch act, and the next outage would leave no
    trace that the fallback is what carried the day's verdicts. Paired with the
    control below so it can't pass by logging on every act.
    """
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email())
    _aged_out(cfg)

    with structlog.testing.capture_logs() as cap:
        _call(store, cfg, fid, "confirm")

    hits = [c for c in cap if c.get("event") == "feed.act.resolved_from_evidence"]
    assert len(hits) == 1
    assert hits[0]["id"] == fid
    assert hits[0]["kind"] == "email_tier"
    assert hits[0]["action"] == "confirm"
    assert hits[0]["reason"] == "aged_out_of_last_batch"


def test_resident_batch_act_does_not_announce_a_fallback(tmp_path: Path) -> None:
    """The control: the ordinary path stays quiet, so the pin above is a
    signal about the fallback rather than a per-act log line."""
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    item = _email()
    fid = _publish(store, item)
    _seed_batch(cfg, [item])

    with structlog.testing.capture_logs() as cap:
        result = _call(store, cfg, fid, "confirm")

    assert result.ok is True, "premise: the resident-batch act must succeed"
    assert [c for c in cap if c.get("event") == "feed.act.resolved_from_evidence"] == []


# ---------------------------------------------------------------------------
# PY-B item 2b — the resolver success path stamps the operator's VERB
# ---------------------------------------------------------------------------
#
# Before this, a resolver success appended a VERBLESS ``acted`` event — byte
# identical to the one ``reconcile`` writes when an item merely falls out of a
# producer's open set. "He confirmed it" and "it was auto-retired" were the
# same line, so the day's history could not distinguish an operator decision
# from a producer's silence. FORWARD ONLY: events already on disk stay
# verbless and stay ambiguous, which is precisely why this ships now.


def test_the_operators_verb_is_recorded_on_the_acted_event(
    tmp_path: Path,
) -> None:
    """Mutation that reds this: drop ``action=action_id`` from the
    ``feed_store.set_state`` call on the resolver success path."""
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email(priority="medium"))
    _aged_out(cfg)

    assert _call(store, cfg, fid, "spam").status == STATUS_ACTED

    item = store.load()[fid]
    assert item.state == "acted"          # STATE UNCHANGED
    assert item.acted_action == "spam"    # ...the VERB is what is new


def test_a_different_verb_records_differently(tmp_path: Path) -> None:
    """POSITIVE CONTROL, and the point of the field: two operator decisions on
    the same kind must be distinguishable from each other, not merely from
    absence. A stamp that recorded a constant would pass the test above."""
    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    fid = _publish(store, _email(priority="medium"))
    _aged_out(cfg)

    assert _call(store, cfg, fid, "confirm").status == STATUS_ACTED
    assert store.load()[fid].acted_action == "confirm"


def test_an_operator_decision_is_distinguishable_from_a_retirement(
    tmp_path: Path,
) -> None:
    """THE WHOLE LANE, in one assertion. These two outcomes were byte-identical
    verbless ``acted`` events; now the log says which is which."""
    from alfred.feed.model import ACTION_RETIRED

    cfg = _cfg(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    decided = _publish(store, _email(priority="medium", path="email/Decided.md"))
    _aged_out(cfg)
    assert _call(store, cfg, decided, "spam").status == STATUS_ACTED

    retired = _publish(store, _email(priority="medium", path="email/Retired.md"))
    # Authoritative: this test's subject is the RETIREMENT, so it must happen.
    store.reconcile(
        store.load()[retired].kind, [], empty_is_authoritative=True,
    )

    folded = store.load()
    # A+C: they no longer share a state either. The verb distinguished them;
    # the STATE is what consumers read, and it now says which is which.
    assert folded[decided].state == "acted"
    assert folded[retired].state == "retired"
    assert folded[decided].acted_action == "spam"
    assert folded[retired].acted_action == ACTION_RETIRED
    assert folded[decided].acted_action != folded[retired].acted_action

"""T2-1 — the returned-reminder feed card (``reminder_returned``).

The lane's claim is that a snoozed or waiting task whose reminder comes due now
REACHES the operator: a card in the deck and a push on the phone, independent of
the Telegram leg that used to be the only notification and that returns ``[]``
(read as success) on any instance without a bot.

Pinned here:

  * REGISTRATION — the kind, its ``KIND_DEFAULTS`` entry, and the capability
    ceiling that keeps the card dealable rather than verbless.
  * EMISSION, driven through ``_tick`` (the production entry point, never the
    emitter alone): content from the shared renderers, and — the load-bearing
    one — emission that survives a send that raises.
  * IDENTITY — the key is (record, remind_at), so a retry folds to a no-op and a
    genuine re-snooze deals a new card.
  * RETIREMENT — the sweep retires a card whose task left returned-state, says
    WHY, holds a card it cannot verify, and never revives a decided one.
  * THE DEFER PROMISE — held inside the window, returned after it.

The web half of the ring (that a needs-you card actually reaches ``sendPush``)
is pinned in ``web/tests/reminderReturnedRing.test.ts``, which reads this kind's
``KIND_DEFAULTS`` entry out of the Python source: the switch is here, the
behaviour it controls is there.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.daily_sync.action_router import (
    ACTION_META,
    FEED_ACTIONS,
    STATUS_ACTED,
    STATUS_INVALID_ACTION,
    act,
    actions_for,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed import FeedEmitHandle, FeedItem, FeedStore
from alfred.feed.model import (
    ATTENTION_NEEDS_YOU,
    KIND_DEFAULTS,
    KIND_REMINDER_RETURNED,
    KINDS,
    MODE_DECIDE,
    STATE_ACKED,
    STATE_ACTED,
    STATE_DEFERRED,
    STATE_OPEN,
)
from alfred.tier.compute import compute_returned_task_candidates
from alfred.transport.config import (
    AuthConfig,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.returns_feed import (
    RETIRE_RE_ARMED,
    RETIRE_RECORD_GONE,
    RETIRE_TASK_CLOSED,
    return_card_stable_key,
    sweep_return_cards,
)
from alfred.transport.scheduler import _tick, run
from alfred.transport.state import TransportState

# ``_tick`` stamps ``datetime.now`` itself — it takes no clock — so a fixture
# pinned to a fixed date lands outside the scheduler's 60s past-grace and is
# REFUSED as a writer error rather than fired. Every due-reminder fixture here is
# therefore relative to the real clock, and computed at WRITE time rather than at
# import time: a module-level constant would age past the grace during a long
# suite run, which is a flake built into the fixture.
_AUTO_DUE = "auto"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _within_grace() -> str:
    """A ``remind_at`` that is due and inside the past-grace window."""
    return (_now() - timedelta(seconds=5)).isoformat()


#: For synthetic cards whose record is never read (the containment cases) — the
#: value only has to be a stable half of a key.
FIXED_REMIND_AT = "2026-08-14T09:00:00+00:00"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_task(
    vault: Path,
    name: str,
    *,
    status: str = "todo",
    remind_at: str | None = _AUTO_DUE,
    reminded_at: str | None = None,
    waiting_on: str | None = None,
    return_slot: str | None = None,
    escalate_on: str | None = None,
    escalate_to: str | None = None,
    reminder_text: str | None = None,
    due: str | None = None,
    type_: str = "task",
) -> Path:
    if remind_at == _AUTO_DUE:
        remind_at = _within_grace()
    lines = [f"type: {type_}", f"name: {name}", f"status: {status}", "created: 2026-08-01"]
    for key, value in (
        ("remind_at", remind_at),
        ("reminded_at", reminded_at),
        ("waiting_on", waiting_on),
        ("return_slot", return_slot),
        ("escalate_on", escalate_on),
        ("escalate_to", escalate_to),
        ("reminder_text", reminder_text),
        ("due", due),
    ):
        if value is not None:
            lines.append(f'{key}: "{value}"')
    path = vault / "task" / f"{name}.md"
    path.write_text(
        "---\n" + "\n".join(lines) + f"\n---\n\n# {name}\n\nBody.\n", encoding="utf-8",
    )
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "task").mkdir()
    return tmp_path


@pytest.fixture
def handle(tmp_path: Path) -> FeedEmitHandle:
    return FeedEmitHandle(
        store_path=str(tmp_path / "feed.jsonl"), instance="Salem", enabled=True,
    )


@pytest.fixture
def store(handle: FeedEmitHandle) -> FeedStore:
    return FeedStore(handle.store_path)


def _env(vault: Path) -> tuple[TransportConfig, TransportState, list[str], Any]:
    sent: list[str] = []

    async def _send(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        sent.append(text)
        return []

    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(poll_interval_seconds=30, stale_reminder_max_minutes=180),
        auth=AuthConfig(),
        state=StateConfig(),
    )
    return config, TransportState.create(vault / "state.json"), sent, _send


async def _run_tick(
    vault: Path, handle: FeedEmitHandle | None, *, send: Any = None,
) -> list[str]:
    """One production tick. ``now`` is not injectable into ``_tick`` (it stamps
    ``datetime.now``), so tests that need a controlled clock call the sweep
    directly; this drives the real fire path."""
    config, state, sent, default_send = _env(vault)
    await _tick(config, state, send or default_send, vault, 42, feed_handle=handle)
    return sent


def _cards(store: FeedStore) -> dict[str, Any]:
    return {
        i.id: i for i in store.load().values() if i.kind == KIND_REMINDER_RETURNED
    }


def _events(captured: list[dict], name: str) -> list[dict]:
    return [c for c in captured if c.get("event") == name]


# ---------------------------------------------------------------------------
# registration — the switch that rings
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_kind_is_registered_with_the_defaults_that_ring(self) -> None:
        """BOTH registrations. Membership in ``KINDS`` is the vocabulary; the
        ``KIND_DEFAULTS`` entry is the wiring — the push poller fetches by
        attention, so this tuple is the whole reason the phone rings."""
        assert KIND_REMINDER_RETURNED in KINDS
        assert KIND_DEFAULTS[KIND_REMINDER_RETURNED] == (
            MODE_DECIDE, ATTENTION_NEEDS_YOU,
        )

    def test_the_card_is_dealable_not_verbless(self) -> None:
        """A decide kind the ceiling has no entry for arrives ``arrivedVerbless``
        — reported as a fault, never dealt. So the ring would land the operator
        on a card he could not clear. One gesture-bearing verb is the minimum."""
        advertised = actions_for(KIND_REMINDER_RETURNED)
        assert advertised, "kind must be in FEED_ACTIONS or the deck cannot deal it"
        gesture_verbs = [a for a in advertised if a.get("gesture")]
        assert gesture_verbs, advertised
        ack = next(a for a in advertised if a["verb"] == "ack")
        assert ack["gesture"] == "affirm"
        assert ack["weight"] == "light"  # writes nothing outside the feed store

    def test_ceiling_is_ack_plus_the_generic_defers(self) -> None:
        assert set(FEED_ACTIONS[KIND_REMINDER_RETURNED]) == {
            "ack", "defer", "defer_1d", "defer_3d", "defer_7d",
        }

    def test_presentation_entry_exists_for_the_ack(self) -> None:
        assert ACTION_META[KIND_REMINDER_RETURNED]["ack"]["gesture"] == "affirm"

    def test_the_recurrence_kind_was_not_touched(self) -> None:
        """Reserved, producerless, and explicitly out of this lane's scope."""
        assert "recurrence" in KINDS
        assert "recurrence" not in FEED_ACTIONS


class TestProductionThreading:
    """``feed_handle`` is a default-``None`` gate parameter, which is the
    standing trap: the tests thread it, production never does, every pin stays
    green, and the feature is accepted-then-ignored in the field. Threading is a
    property of the code that was ALREADY THERE, so the pin is on the CALL SITE.

    Source inspection, brittle by design — the same idiom (and the same reason)
    as ``tests/test_talker_transport_wiring.py``: a refactor that moves the call
    must re-establish the threading or this reds. Read from the checkout next to
    this test, never the installed copy, so a worktree validates against itself.
    """

    def _daemon_source(self) -> str:
        path = (
            Path(__file__).resolve().parent.parent
            / "src" / "alfred" / "telegram" / "daemon.py"
        )
        return path.read_text(encoding="utf-8")

    def test_the_daemon_threads_the_ring_into_the_scheduler(self) -> None:
        source = self._daemon_source()
        calls = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_scheduler"
        ]
        # Positive control: the search found the real call, so the assertions
        # below are about something rather than vacuously true of an empty list.
        assert len(calls) == 1, "expected exactly one run_scheduler call site"

        kwargs = {kw.arg: kw.value for kw in calls[0].keywords}
        assert "feed_handle" in kwargs, (
            "the scheduler's feed ring is not threaded at its production call "
            "site — the write side would be live and the read side dead"
        )
        # And it is the handle the daemon actually built, not a None literal
        # that would satisfy the keyword while ringing nothing.
        passed = kwargs["feed_handle"]
        assert isinstance(passed, ast.Name), ast.dump(passed)
        assert passed.id == "feed_emit_handle"
        assert "feed_emit_handle = FeedEmitHandle(" in source


# ---------------------------------------------------------------------------
# the act path — ack decides, defer defers
# ---------------------------------------------------------------------------


def _act(store: FeedStore, tmp_path: Path, feed_id: str, action: str):
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "ds_state.json")
    return act(
        feed_id, action, feed_store=store, config=cfg, vault_path=None,
        instance_name="salem", instance_scope="talker",
    )


class TestActing:
    async def test_ack_decides_the_card(self, vault: Path, handle, store, tmp_path) -> None:
        _write_task(vault, "Chase the plumber", waiting_on="Plumber")
        await _run_tick(vault, handle)
        feed_id = next(iter(_cards(store)))

        result = _act(store, tmp_path, feed_id, "ack")

        assert result.ok and result.status == STATUS_ACTED
        stored = store.load()[feed_id]
        # ACTED, not ACKED: this is a decide card, and the operator judged it.
        assert stored.state == STATE_ACTED
        assert stored.acted_action == "ack"

    async def test_defer_defers_and_does_not_masquerade_as_an_ack(
        self, vault: Path, handle, store, tmp_path,
    ) -> None:
        """The verb-gated intercept, pinned.

        A kind-gated intercept (``if kind == RETURN_KIND:``) would swallow every
        verb the ceiling admits — including the auto-folded defers — and answer
        them with ``acted``, turning "later" into "decided" at the one moment the
        operator said the opposite. The distinguishing evidence is the STATE, not
        the ok flag: both shapes return ok=True.
        """
        _write_task(vault, "Book the MOT")
        await _run_tick(vault, handle)
        feed_id = next(iter(_cards(store)))

        result = _act(store, tmp_path, feed_id, "defer_3d")

        assert result.ok
        stored = store.load()[feed_id]
        assert stored.state == STATE_DEFERRED, "a defer must not land as acted"
        assert stored.deferred_until, "a dated defer must carry its window"

    async def test_ceiling_is_closed(self, vault: Path, handle, store, tmp_path) -> None:
        _write_task(vault, "Ring the vet")
        await _run_tick(vault, handle)
        feed_id = next(iter(_cards(store)))

        for bad in ("confirm", "reject", "done", "accept", "snooze_1d"):
            result = _act(store, tmp_path, feed_id, bad)
            assert result.status == STATUS_INVALID_ACTION, bad
            assert store.load()[feed_id].state == STATE_OPEN, bad


# ---------------------------------------------------------------------------
# emission — through _tick, the production entry point
# ---------------------------------------------------------------------------


class TestEmission:
    async def test_a_due_reminder_deals_one_card(self, vault, handle, store) -> None:
        _write_task(vault, "Fix Carfax Mileage", waiting_on="Carfax")

        sent = await _run_tick(vault, handle)

        cards = _cards(store)
        assert len(cards) == 1
        card = next(iter(cards.values()))
        assert card.mode == MODE_DECIDE
        assert card.attention == ATTENTION_NEEDS_YOU
        assert card.state == STATE_OPEN
        assert card.instance == "Salem"
        # The chase vocabulary comes from tier.slots.chase_phrase through
        # render_return_line — the same words the Telegram line uses.
        assert card.title == "Chase Carfax: Fix Carfax Mileage"
        assert sent == ["Chase Carfax: Fix Carfax Mileage"]
        assert card.evidence["record_path"] == "task/Fix Carfax Mileage.md"
        assert card.evidence["return_kind"] == "waiting_chase"
        assert card.evidence["waiting_on"] == "Carfax"
        # D7 interval extent: the reminder's instant, a moment with no end.
        assert card.starts_at == card.evidence["remind_at"]
        assert card.ends_at is None

    async def test_reminder_text_still_wins_the_wording(self, vault, handle, store) -> None:
        """Proves the title is the SHARED renderer rather than a second spelling:
        an operator-written ``reminder_text`` outranks the chase frame there, so
        it must outrank it here too."""
        _write_task(
            vault, "Septic tank", waiting_on="Dave", reminder_text="Pump the tank",
        )
        await _run_tick(vault, handle)
        assert next(iter(_cards(store).values())).title == "Pump the tank"

    async def test_the_slot_is_the_escalation_aware_one(self, vault, handle, store) -> None:
        """The card carries the slot the day-plan reader would resolve, via the
        shared ``resolve_effective_slot`` — escalation included."""
        _write_task(
            vault, "Septic inspection", return_slot="routine",
            escalate_on="2026-01-01", escalate_to="duty",
        )
        await _run_tick(vault, handle)
        card = next(iter(_cards(store).values()))
        assert card.evidence["slot"] == "duty"  # boundary passed → escalated
        assert card.evidence["slot_rule"] == "escalated"

    async def test_the_ring_survives_a_send_that_raises(self, vault, handle, store) -> None:
        """THE POINT OF THE LANE. The emit sits above ``send_fn`` and shares no
        failure exit with it: move it below (or gate it on send success) and this
        goes red, because the send's ``continue`` would skip it."""
        _write_task(vault, "Call the school")

        async def _boom(user_id: int, text: str, dedupe_key: str | None = None):
            raise RuntimeError("telegram is down")

        await _run_tick(vault, handle, send=_boom)

        assert len(_cards(store)) == 1
        # And the record is untouched by the failed send — still armed, so the
        # retry can happen. (Pre-existing behaviour, pinned here because the
        # emit now runs before it.)
        text = (vault / "task" / "Call the school.md").read_text(encoding="utf-8")
        assert "remind_at" in text
        assert "reminded_at" not in text

    async def test_a_bot_less_instance_still_rings(self, vault, handle, store) -> None:
        """The silent-consumption sting: ``_send_via_telegram`` returns ``[]`` on
        a web-only instance, which the scheduler reads as SUCCESS and stamps. The
        feed leg must not care what the send returned."""
        _write_task(vault, "Renew the insurance")

        async def _no_bot(user_id: int, text: str, dedupe_key: str | None = None):
            return []

        await _run_tick(vault, handle, send=_no_bot)
        assert len(_cards(store)) == 1

    async def test_feed_off_emits_nothing_and_the_send_still_goes(
        self, vault, store,
    ) -> None:
        _write_task(vault, "Water the plants")
        sent = await _run_tick(vault, None)
        assert sent == ["Reminder: Water the plants"]  # positive control
        assert _cards(store) == {}

    async def test_emit_failure_is_loud_and_does_not_cost_the_send(
        self, vault, tmp_path,
    ) -> None:
        """A feed fault must be greppable, not silent — and must not take the
        Telegram message with it. The store path is a DIRECTORY, so the upsert
        raises inside the emitter."""
        broken = tmp_path / "not-a-file"
        broken.mkdir()
        handle = FeedEmitHandle(store_path=str(broken), instance="Salem", enabled=True)
        _write_task(vault, "Post the letter")

        with structlog.testing.capture_logs() as captured:
            sent = await _run_tick(vault, handle)

        failures = _events(captured, "transport.scheduler.return_card_emit_failed")
        assert len(failures) == 1
        assert failures[0]["path"] == "task/Post the letter.md"
        assert failures[0]["error_type"]
        assert sent == ["Reminder: Post the letter"]

    async def test_emission_is_logged_with_its_identity(self, vault, handle) -> None:
        _write_task(vault, "Chase the solicitor", waiting_on="Solicitor")
        with structlog.testing.capture_logs() as captured:
            await _run_tick(vault, handle)
        emitted = _events(captured, "transport.scheduler.return_card_emitted")
        assert len(emitted) == 1
        assert emitted[0]["path"] == "task/Chase the solicitor.md"
        assert emitted[0]["return_kind"] == "waiting_chase"
        assert emitted[0]["feed_id"].startswith(f"{KIND_REMINDER_RETURNED}:")

    async def test_startup_says_whether_the_ring_is_armed(self, vault, handle) -> None:
        """A doorbell that was never armed looks exactly like "nothing came due".
        The startup line is the only thing that tells them apart, so it speaks on
        BOTH outcomes."""
        import asyncio

        config, state, _sent, send = _env(vault)
        for feed_handle, expected in ((handle, "armed"), (None, "off")):
            stop = asyncio.Event()
            stop.set()
            with structlog.testing.capture_logs() as captured:
                await run(
                    config, state, send, vault, 42,
                    shutdown_event=stop, feed_handle=feed_handle,
                )
            starting = _events(captured, "transport.scheduler.starting")
            assert len(starting) == 1
            assert starting[0]["feed_ring"] == expected


# ---------------------------------------------------------------------------
# identity — (record, remind_at), never wall-clock
# ---------------------------------------------------------------------------


class TestIdentity:
    async def test_the_key_carries_the_record_and_the_remind_at(
        self, vault, handle, store,
    ) -> None:
        _write_task(vault, "Order the parts")
        await _run_tick(vault, handle)
        card = next(iter(_cards(store).values()))
        assert card.id == "{}:{}".format(
            KIND_REMINDER_RETURNED,
            return_card_stable_key("task/Order the parts.md", card.evidence["remind_at"]),
        )

    async def test_a_retry_tick_folds_to_one_card(self, vault, handle, store) -> None:
        """Idempotency, exercised through the shape that produces it: a send that
        raises leaves the reminder unstamped, so the NEXT tick re-fires the same
        entry. A wall-clock key would deal a second card every retry."""
        _write_task(vault, "Confirm the booking")

        async def _boom(user_id: int, text: str, dedupe_key: str | None = None):
            raise RuntimeError("down")

        await _run_tick(vault, handle, send=_boom)
        first = set(_cards(store))
        await _run_tick(vault, handle, send=_boom)

        assert set(_cards(store)) == first
        assert len(first) == 1

    async def test_a_genuine_re_snooze_deals_a_new_card(
        self, vault, handle, store,
    ) -> None:
        """The positive control for the key: a NEW remind_at is a NEW return and
        must arrive as its own item, not silently fold onto the old one."""
        _write_task(vault, "Chase the quote")
        await _run_tick(vault, handle)
        first = next(iter(_cards(store)))

        # The operator pushes it again — a new remind_at on the same record.
        _write_task(
            vault, "Chase the quote",
            remind_at=(_now() - timedelta(seconds=2)).isoformat(),
            reminded_at=(_now() - timedelta(seconds=45)).isoformat(),
        )
        await _run_tick(vault, handle)

        cards = _cards(store)
        assert len(cards) == 2, cards
        assert first in cards


# ---------------------------------------------------------------------------
# retirement — the sweep
# ---------------------------------------------------------------------------


class TestRetirement:
    async def _one_card(self, vault: Path, handle: FeedEmitHandle, store: FeedStore, **kw) -> str:
        _write_task(vault, "Fix the gate", **kw)
        await _run_tick(vault, handle)
        cards = _cards(store)
        assert len(cards) == 1
        return next(iter(cards))

    async def test_a_finished_task_retires_its_card_and_says_why(
        self, vault, handle, store,
    ) -> None:
        feed_id = await self._one_card(vault, handle, store)
        _write_task(vault, "Fix the gate", status="done", remind_at=None,
                    reminded_at=_now().isoformat())

        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())

        assert store.load()[feed_id].state == STATE_ACTED
        retired = _events(captured, "transport.scheduler.return_cards_retired")
        assert len(retired) == 1
        # WHY, not just that: "he finished two tasks" and "two records went
        # missing" are the same count and completely different news.
        assert retired[0]["retired"] == [{"id": feed_id, "reason": RETIRE_TASK_CLOSED}]

    async def test_a_cancelled_task_retires_its_card(self, vault, handle, store) -> None:
        feed_id = await self._one_card(vault, handle, store)
        _write_task(vault, "Fix the gate", status="cancelled", remind_at=None,
                    reminded_at=_now().isoformat())
        sweep_return_cards(handle, vault, _now())
        assert store.load()[feed_id].state == STATE_ACTED

    async def test_a_re_snoozed_task_retires_its_old_card(
        self, vault, handle, store,
    ) -> None:
        feed_id = await self._one_card(vault, handle, store)
        _write_task(
            vault, "Fix the gate",
            remind_at=(_now() + timedelta(days=1)).isoformat(),
            reminded_at=_now().isoformat(),
        )

        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())

        assert store.load()[feed_id].state == STATE_ACTED
        assert _events(captured, "transport.scheduler.return_cards_retired")[0][
            "retired"
        ] == [{"id": feed_id, "reason": RETIRE_RE_ARMED}]

    async def test_an_unstamped_fire_is_not_a_re_arm(self, vault, handle, store) -> None:
        """The distinction the whole re-arm rule turns on. A send that failed
        leaves the SAME ``remind_at`` on the record; reading that as a re-arm
        would retire, one tick later, every card whose Telegram send failed —
        cancelling the ring on exactly the fault it exists to be independent of.
        """
        _write_task(vault, "Fix the gate")

        async def _boom(user_id: int, text: str, dedupe_key: str | None = None):
            raise RuntimeError("down")

        await _run_tick(vault, handle, send=_boom)
        feed_id = next(iter(_cards(store)))
        assert "remind_at" in (vault / "task" / "Fix the gate.md").read_text()

        sweep_return_cards(handle, vault, _now())

        assert store.load()[feed_id].state == STATE_OPEN

    async def test_a_deleted_record_retires_its_card(self, vault, handle, store) -> None:
        feed_id = await self._one_card(vault, handle, store)
        (vault / "task" / "Fix the gate.md").unlink()

        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())

        assert store.load()[feed_id].state == STATE_ACTED
        assert _events(captured, "transport.scheduler.return_cards_retired")[0][
            "retired"
        ] == [{"id": feed_id, "reason": RETIRE_RECORD_GONE}]

    async def test_an_unreadable_record_holds_the_card(self, vault, handle, store) -> None:
        """Doubt is not an ending. A card that vanishes because a file briefly
        failed to parse takes the operator's notification with it and leaves
        nothing to ask about; a lingering card is visible and dismissable."""
        feed_id = await self._one_card(vault, handle, store)
        (vault / "task" / "Fix the gate.md").write_text(
            "---\n: : not: yaml: [\n---\n", encoding="utf-8",
        )

        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())

        assert store.load()[feed_id].state == STATE_OPEN
        assert _events(captured, "transport.scheduler.return_card_record_unreadable")

    async def test_a_still_returned_task_keeps_its_card(
        self, vault, handle, store,
    ) -> None:
        """The positive control for every retirement above: the sweep CAN say
        keep, so a retirement is a verdict rather than the only outcome."""
        feed_id = await self._one_card(vault, handle, store)
        for _ in range(3):
            sweep_return_cards(handle, vault, _now())
        assert store.load()[feed_id].state == STATE_OPEN

    async def test_a_decided_card_is_never_revived(
        self, vault, handle, store, tmp_path,
    ) -> None:
        """The groundhog pin. ``reconcile`` upserts everything it is handed at
        ``state=open``, so passing it an acked card would revive it on every
        tick, forever — the bug this codebase has already fixed twice. Only
        open/deferred cards go into the sweep's open set.
        """
        feed_id = await self._one_card(vault, handle, store)
        _act(store, tmp_path, feed_id, "ack")
        assert store.load()[feed_id].state == STATE_ACTED

        # The task is STILL in returned-state — the tempting input for a revive.
        for _ in range(3):
            sweep_return_cards(handle, vault, _now())

        assert store.load()[feed_id].state == STATE_ACTED

    async def test_the_slot_is_refreshed_as_a_boundary_passes(
        self, vault, handle, store,
    ) -> None:
        """A card open across an escalation boundary must move WITH the day-plan
        row, not sit on the slot it was born with — the septic case, which fires
        as rhythm and escalates months later while sitting in returned-state."""
        _write_task(
            vault, "Septic pump-out", return_slot="routine",
            escalate_on="2026-09-01", escalate_to="duty",
        )
        await _run_tick(vault, handle)
        feed_id = next(iter(_cards(store)))
        assert store.load()[feed_id].evidence["slot"] == "rhythm"  # before

        sweep_return_cards(handle, vault, datetime(2026, 9, 2, tzinfo=timezone.utc))

        after = store.load()[feed_id]
        assert after.evidence["slot"] == "duty"
        assert after.state == STATE_OPEN
        # And the reader that renders the day plan agrees, from the same helper.
        reader = compute_returned_task_candidates(
            vault, datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        assert [c.explicit_slot for c in reader] == ["duty"]

    async def test_the_card_outlives_the_day_plan_row_for_an_active_task(
        self, vault, handle, store,
    ) -> None:
        """A DELIBERATE asymmetry, pinned so a future unification is a decision
        rather than an accident.

        The scheduler fires for ``todo`` AND ``active`` (``_ELIGIBLE_STATUSES``);
        ``compute_returned_task_candidates`` surfaces ``todo`` only. Retiring on
        the reader's predicate would make an ``active`` task's card vanish within
        one 30s tick — the phone rings and the card is gone before he looks. So
        the card retires on the eligibility that EMITTED it.
        """
        _write_task(vault, "Fix the gate", status="active")
        await _run_tick(vault, handle)
        feed_id = next(iter(_cards(store)))

        sweep_return_cards(handle, vault, _now())

        assert store.load()[feed_id].state == STATE_OPEN
        assert compute_returned_task_candidates(vault, _now()) == []  # the divergence

    def test_the_sweep_is_a_no_op_with_no_cards(self, vault, handle, store) -> None:
        assert sweep_return_cards(handle, vault, _now()) is None
        assert sweep_return_cards(None, vault, _now()) is None


# ---------------------------------------------------------------------------
# the defer promise
# ---------------------------------------------------------------------------


class TestDeferReturns:
    async def test_a_defer_is_held_then_returned(
        self, vault, handle, store, tmp_path,
    ) -> None:
        """A defer is a PROMISE: the card must come back. For an upsert-only kind
        nothing would keep it — ``_revival_suppressed`` (which owns the window)
        is reached from ``reconcile`` alone, and the read path filters state by
        exact string. The sweep IS this kind's reconciler, which is why the
        generic defer verbs are safe to carry here.
        """
        _write_task(vault, "Fix the gate")
        await _run_tick(vault, handle)
        feed_id = next(iter(_cards(store)))
        _act(store, tmp_path, feed_id, "defer_3d")
        assert store.load()[feed_id].state == STATE_DEFERRED

        # Inside the window — held. (The window is judged against the store's
        # OWN wall clock inside ``reconcile``, not against the ``now`` this
        # sweep is given — that argument dates the slot resolution. So the two
        # halves are expressed as windows relative to real time rather than by
        # moving a test clock the store does not read.)
        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())
        assert store.load()[feed_id].state == STATE_DEFERRED
        assert _events(captured, "feed.store.defer_held")

        # Past it — returned. A window already behind the clock is exactly what
        # the store holds after the three days have actually passed.
        store.defer(feed_id, until=(_now() - timedelta(minutes=1)).isoformat())
        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())
        assert store.load()[feed_id].state == STATE_OPEN
        assert _events(captured, "feed.store.defer_returned")

    async def test_a_deferred_card_whose_task_closes_is_retired(
        self, vault, handle, store, tmp_path,
    ) -> None:
        """A deferred card counts as PRESENT for absent-detection, so a task
        finished while the card was parked retires it. Without that it would be
        stranded deferred forever with no producer left to return it."""
        _write_task(vault, "Fix the gate")
        await _run_tick(vault, handle)
        feed_id = next(iter(_cards(store)))
        _act(store, tmp_path, feed_id, "defer_7d")

        _write_task(vault, "Fix the gate", status="done", remind_at=None,
                    reminded_at=_now().isoformat())
        sweep_return_cards(handle, vault, _now() + timedelta(days=1))

        assert store.load()[feed_id].state == STATE_ACTED


# ---------------------------------------------------------------------------
# containment — the evidence path is input, not a promise
# ---------------------------------------------------------------------------


class TestContainment:
    def test_a_card_pointing_outside_the_vault_is_held_not_followed(
        self, vault, handle, store, tmp_path,
    ) -> None:
        """The stored evidence path is data on disk. An escape must be refused
        (and the refusal logged by ``resolve_in_vault``), and the card held —
        never read from, never silently retired on the strength of a path we
        declined to follow."""
        from alfred.feed import FeedItem

        outside = tmp_path / "outside.md"
        outside.write_text("---\ntype: task\nstatus: done\n---\n", encoding="utf-8")
        card = FeedItem.create(
            kind=KIND_REMINDER_RETURNED,
            stable_key=return_card_stable_key("../outside.md", FIXED_REMIND_AT),
            instance="Salem",
            title="Escaping card",
            evidence={"record_path": "../outside.md", "remind_at": FIXED_REMIND_AT},
        )
        store.upsert(card)

        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())

        assert store.load()[card.id].state == STATE_OPEN
        denials = _events(captured, "vault.containment.escape_denied")
        assert len(denials) == 1
        assert denials[0]["reason"] == "outside_vault_root"

    async def test_malformed_evidence_holds_the_card_and_never_wedges_the_tick(
        self, vault, handle, store,
    ) -> None:
        """``evidence`` is typed as a dict and is not guaranteed to be one.

        The store folds whatever a writer wrote — ``from_dict`` filters unknown
        FIELDS, never their types — so one malformed line hands the sweep a
        string. A bare ``.get`` on it raises AttributeError, and the raise would
        travel out of the sweep and out of ``_tick``, taking that tick's
        PENDING-QUEUE DRAIN with it on every tick until the line is removed.
        That is the property asserted here: the card is held, and the drain (an
        unrelated scheduler leg) still runs.
        """
        store.path.parent.mkdir(parents=True, exist_ok=True)
        with open(store.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ev": "upsert", "ts": _now().isoformat(),
                "item": {
                    "id": f"{KIND_REMINDER_RETURNED}:task/Bad.md|{FIXED_REMIND_AT}",
                    "kind": KIND_REMINDER_RETURNED,
                    "title": "Malformed card",
                    "state": STATE_OPEN,
                    "evidence": "not-a-dict",
                },
            }) + "\n")
        bad_id = f"{KIND_REMINDER_RETURNED}:task/Bad.md|{FIXED_REMIND_AT}"

        # A queued send parked for the past — the leg that must survive.
        config, state, sent, send = _env(vault)
        state.pending_queue.append({
            "id": "pending-1", "user_id": 42, "text": "parked message",
            "dedupe_key": "pending-1",
            "scheduled_at": (_now() - timedelta(minutes=1)).isoformat(),
        })

        with structlog.testing.capture_logs() as captured:
            await _tick(config, state, send, vault, 42, feed_handle=handle)

        assert store.load()[bad_id].state == STATE_OPEN, "held, not retired"
        assert sent == ["parked message"], "the pending drain ran"
        assert not _events(captured, "transport.scheduler.tick_error")
        # WHICH mechanism handled it, not merely that something did. The
        # per-card belt would produce an identical card state and an identical
        # drain, so without this the pin is equally green against a build with
        # no type guard at all — the belt catching the AttributeError every
        # tick, forever, on a card that can never be verified.
        assert not _events(captured, "transport.scheduler.return_card_verdict_failed"), (
            "the evidence type guard should have handled this, not the belt"
        )

    async def test_a_non_string_id_does_not_wedge_the_sweep(
        self, vault, handle, store,
    ) -> None:
        """The SIBLING of the malformed-evidence hazard, found by sweeping for
        it rather than by hitting it.

        The fold does not coerce types, so a card can carry a non-string id.
        Comparing one against a string id raises TypeError inside ``sorted`` —
        BEFORE the per-card belt, which is why this sweep's sort key is rendered
        rather than raw. Asserted through ``_tick`` because what is actually at
        stake is the leg below it: the pending-queue drain.

        THE FLIP THIS TEST'S DOCSTRING PROMISED. It used to record a
        degradation: the same mixed-type comparison reappeared downstream in
        ``FeedStore.reconcile``'s own ``sorted(absent)``, the belt caught it as
        ``feed.reconcile_failed``, and retirement for the kind was BLOCKED until
        the malformed line left the store. That was shared store code with every
        producer behind it, so the lane that found it deliberately did not touch
        it and wrote down the condition for changing the expectation instead.
        The shared sort is now total (``sorted(absent, key=str)``), so the
        assertions below are the ones the old docstring said they would become:
        the reconcile SUCCEEDS and the cards retire. The red test was the fix
        lane's instruction, which is what a documented degradation is for.
        """
        good = FeedItem.create(
            kind=KIND_REMINDER_RETURNED,
            stable_key=return_card_stable_key("task/Gone.md", FIXED_REMIND_AT),
            instance="Salem", title="Real card",
            evidence={"record_path": "task/Gone.md", "remind_at": FIXED_REMIND_AT},
        )
        store.upsert(good)
        with open(store.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ev": "upsert", "ts": _now().isoformat(),
                "item": {
                    "id": 12345,  # not a string — sorts against `good.id`
                    "kind": KIND_REMINDER_RETURNED,
                    "title": "Numeric id",
                    "state": STATE_OPEN,
                    "evidence": {"record_path": "task/Gone.md"},
                },
            }) + "\n")

        config, state, sent, send = _env(vault)
        state.pending_queue.append({
            "id": "pending-1", "user_id": 42, "text": "parked message",
            "dedupe_key": "pending-1",
            "scheduled_at": (_now() - timedelta(minutes=1)).isoformat(),
        })

        with structlog.testing.capture_logs() as captured:
            await _tick(config, state, send, vault, 42, feed_handle=handle)

        assert sent == ["parked message"], "the pending drain ran"
        assert not _events(captured, "transport.scheduler.tick_error")
        # THIS sweep's sort survived — with a raw-id key the TypeError would be
        # caught by the outer belt instead, aborting every per-card verdict.
        assert not _events(captured, "transport.scheduler.return_card_sweep_failed")
        # Positive control that the sweep really ran and reached a verdict on
        # BOTH cards (neither record exists), rather than returning early.
        retired = _events(captured, "transport.scheduler.return_cards_retired")
        assert len(retired) == 1
        assert {r["reason"] for r in retired[0]["retired"]} == {RETIRE_RECORD_GONE}
        assert len(retired[0]["retired"]) == 2
        # …and the reconcile now COMPLETES over the mixed-type id set, so the
        # retirement actually lands. No belt refusal, and the card whose record
        # does not exist goes acted.
        assert not _events(captured, "feed.reconcile_failed")
        assert store.load()[good.id].state == STATE_ACTED

    def test_the_outer_belt_keeps_a_sweep_fault_off_the_rest_of_the_tick(
        self, vault, handle, store, monkeypatch,
    ) -> None:
        """The layer of last resort: whatever the sweep body raises, the tick
        continues. Distinct from the per-card belt — this covers the code
        OUTSIDE the loop, where a raise would otherwise reach ``_tick``."""
        from alfred.transport import returns_feed

        def _boom(*a: Any, **kw: Any) -> None:
            raise RuntimeError("sweep body exploded")

        monkeypatch.setattr(returns_feed, "_sweep_locked", _boom)

        with structlog.testing.capture_logs() as captured:
            result = sweep_return_cards(handle, vault, _now())

        assert result is None
        failures = _events(captured, "transport.scheduler.return_card_sweep_failed")
        assert len(failures) == 1
        assert failures[0]["error_type"] == "RuntimeError"

    def test_an_unexpected_verdict_fault_holds_the_card(
        self, vault, handle, store, monkeypatch,
    ) -> None:
        """The belt itself, exercised rather than assumed: whatever the verdict
        path raises, that ONE card is held and the sweep still completes."""
        from alfred.transport import returns_feed

        _write_task(vault, "Fix the gate")
        card = FeedItem.create(
            kind=KIND_REMINDER_RETURNED,
            stable_key=return_card_stable_key("task/Fix the gate.md", FIXED_REMIND_AT),
            instance="Salem", title="Held card",
            evidence={
                "record_path": "task/Fix the gate.md", "remind_at": FIXED_REMIND_AT,
            },
        )
        store.upsert(card)

        def _boom(*a: Any, **kw: Any) -> None:
            raise RuntimeError("verdict exploded")

        monkeypatch.setattr(returns_feed, "_refresh_or_retire", _boom)

        with structlog.testing.capture_logs() as captured:
            counts = sweep_return_cards(handle, vault, _now())

        assert store.load()[card.id].state == STATE_OPEN
        assert counts is not None, "the sweep completed despite the fault"
        faults = _events(captured, "transport.scheduler.return_card_verdict_failed")
        assert len(faults) == 1
        assert faults[0]["error_type"] == "RuntimeError"

    def test_a_card_with_no_record_path_is_held(self, vault, handle, store) -> None:
        from alfred.feed import FeedItem

        card = FeedItem.create(
            kind=KIND_REMINDER_RETURNED, stable_key="unkeyed", instance="Salem",
            title="No path", evidence={},
        )
        store.upsert(card)

        with structlog.testing.capture_logs() as captured:
            sweep_return_cards(handle, vault, _now())

        assert store.load()[card.id].state == STATE_OPEN
        assert _events(captured, "transport.scheduler.return_card_unkeyed")

    def test_other_kinds_are_untouched_by_the_sweep(self, vault, handle, store) -> None:
        """The sweep reconciles ONE kind. An open card of another kind must not
        be marked acted by it — reconcile's absent-detection is per-kind, and
        this is the pin that says so."""
        from alfred.feed import FeedItem

        other = FeedItem.create(
            kind="email_urgent", stable_key="note/Urgent.md", instance="Salem",
            title="Urgent email", evidence={"record_path": "note/Urgent.md"},
        )
        store.upsert(other)
        mine = FeedItem.create(
            kind=KIND_REMINDER_RETURNED,
            stable_key=return_card_stable_key("task/Gone.md", FIXED_REMIND_AT),
            instance="Salem", title="Gone",
            evidence={"record_path": "task/Gone.md", "remind_at": FIXED_REMIND_AT},
        )
        store.upsert(mine)

        sweep_return_cards(handle, vault, _now())

        folded = store.load()
        assert folded[other.id].state == STATE_OPEN  # untouched
        assert folded[mine.id].state == STATE_ACTED  # its record never existed
        assert folded[other.id].state != STATE_ACKED

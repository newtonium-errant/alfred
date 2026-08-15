"""The authority write must not depend on Telegram (P1, 2026-08-15).

THE INCIDENT. Salem went web-only. On the first sync after that, ``send_batch``
logged ``telegram_send_skipped detail='web-only mode (no bot_token)'`` and
returned no ids — a deliberate SKIP that reads at the call site exactly like a
failure. ``fire_once`` persisted ``state["last_batch"]`` only ``if (items ...)
and message_ids``, so the authority write was skipped; ``emit_sync_feed`` is
NOT so gated, so the deck was dealt cards anyway. Every act then re-derived an
id absent from a stale ``last_batch`` and 409'd ``aged_out_of_last_batch``. The
operator swiped five email cards; all five verdicts were refused.

WHAT THESE PINS DEFEND is the relationship, not either half alone: **any fire
that deals cards must also persist the authority those cards' verbs resolve
against.** That is why the load-bearing test drives a REAL ``fire_once`` and
then asks ``_load_batch_item`` — the function that actually returned ``None``
in production — rather than asserting on the state dict and calling it proved.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from alfred.daily_sync.action_router import _load_batch_item
from alfred.daily_sync.config import DailySyncConfig, RoutineMatchConfig
from alfred.daily_sync.confidence import load_state
from alfred.daily_sync.daemon import fire_once
from alfred.feed import FeedStore
from alfred.routine import match_calibration as mc

#: What the transport returns on a web-only instance. NOT an error and not an
#: exception — ``send_batch`` skips the send and reports success with no ids,
#: which is precisely why the old gate mistook a healthy web-only fire for a
#: failed push. A fixture that raised instead would test the wrong thing.
_WEB_ONLY_RESPONSE: dict = {}


def _config(tmp_path: Path) -> DailySyncConfig:
    pending = tmp_path / "pending.jsonl"
    mc.append_pending(pending, mc.PendingMatch(
        query="walk doggo", matched_to="Walk dog", record="Daily",
        confidence=0.4, completion_date="2026-06-28",
    ))
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.routine_match = RoutineMatchConfig(enabled=True, pending_path=str(pending))
    return cfg


def _empty_config(tmp_path: Path) -> DailySyncConfig:
    """A fire with NOTHING to say — no pending matches, no families."""
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _patch_transport(monkeypatch: pytest.MonkeyPatch, response: dict) -> None:
    async def _fake_send_batch(user_id, chunks, *, dedupe_key=None, client_name=None):
        return response

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "send_outbound_batch", _fake_send_batch)


async def _fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
    response: dict, cfg: DailySyncConfig | None = None,
):
    cfg = cfg or _config(tmp_path)
    _patch_transport(monkeypatch, response)
    store_path = tmp_path / "feed.jsonl"
    raw_config = {"feed": {"enabled": True, "store_path": str(store_path)}}
    await fire_once(
        cfg, tmp_path, user_id=42, today=date(2026, 6, 28), raw_config=raw_config,
    )
    batch = load_state(cfg.state.path).get("last_batch") or {}
    cards = FeedStore(str(store_path)).load() if store_path.exists() else {}
    return cfg, batch, cards


# ---------------------------------------------------------------------------
# the regression: a web-only fire persists its authority
# ---------------------------------------------------------------------------


async def test_web_only_fire_persists_last_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Telegram ids must not mean no authority."""
    _cfg, batch, _cards = await _fire(
        tmp_path, monkeypatch, response=_WEB_ONLY_RESPONSE,
    )
    assert batch, "a fire with items must persist last_batch even with no message_ids"
    assert batch.get("routine_match_items"), "the family that produced cards must be in the batch"


async def test_web_only_cards_are_actionable_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE pin. Every card a web-only fire deals must resolve against the
    authority that same fire persisted.

    Asks ``_load_batch_item`` — the function that returned ``None`` in
    production — for each emitted card's real id, so this fails against the
    shipped bug rather than merely describing the fix. A state-dict assertion
    could not: ``last_batch`` can be non-empty and still not contain the card.
    """
    cfg, _batch, cards = await _fire(
        tmp_path, monkeypatch, response=_WEB_ONLY_RESPONSE,
    )
    assert cards, "the fire must have dealt at least one card (else this is vacuous)"

    unresolvable = [
        card.id for card in cards.values()
        if _load_batch_item(card.kind, card.id, cfg) is None
    ]
    assert not unresolvable, (
        f"cards dealt whose verbs cannot succeed: {unresolvable}"
    )


async def test_message_ids_still_stored_for_the_telegram_reply_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field survives the gate's removal — the reply path keys off it.

    Empty on a web-only fire, populated when the bot is live. Both are pinned
    together because the point is that the field still TRACKS the push rather
    than being abandoned: ``reply_dispatch._last_batch_message_ids`` degrades
    to "no thread to route to" on the empty one, which is correct, not broken.
    """
    _cfg, web_batch, _ = await _fire(
        tmp_path / "web", monkeypatch, response=_WEB_ONLY_RESPONSE,
    )
    assert web_batch.get("message_ids") == []

    _cfg2, tg_batch, _ = await _fire(
        tmp_path / "tg", monkeypatch, response={"telegram_message_ids": [9001]},
    )
    assert tg_batch.get("message_ids") == [9001]


async def test_empty_fire_still_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preserved half of the old gate, and the control for the pins above.

    Dropping ``and message_ids`` must not turn "persist when there is anything
    to persist" into "persist unconditionally": a fire with no items in any
    family has nothing to match an act or a reply against, and writing an empty
    batch would clobber a good one. Without this, every assertion above would
    pass just as well against a build that persists on every fire.
    """
    _cfg, batch, _cards = await _fire(
        tmp_path, monkeypatch, response={"telegram_message_ids": [9001]},
        cfg=_empty_config(tmp_path),
    )
    assert not batch, "a fire with no items must not persist a batch"

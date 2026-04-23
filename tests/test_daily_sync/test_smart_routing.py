"""Tests for the Option B smart-routing reply parser.

The smart-routing path lets Andrew reply to a recent Daily Sync push
WITHOUT using Telegram's reply-to-message context — the FIRST message
after a push that looks like a calibration response gets routed
through the dispatcher. Subsequent messages fall through to normal
conversation. Andrew's UX expectation:

    "If my first message after receiving the calibration data looks
    like a calibration response, including partial responses, treat
    it as such. If I need to add more detail later I will use the
    reply to message function."

Covers:
    * Heuristic detection (numbered list, ack token, multi-numbered).
    * False-positive guard (parser produces nothing → flag reverts).
    * Replied-flag persistence across calls (second message falls
      through; explicit reply-to-message still works as override).
    * No batch persisted → smart-routing falls through.
"""

from __future__ import annotations

from pathlib import Path

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import load_state, save_state
from alfred.daily_sync.corpus import iter_corrections
from alfred.daily_sync.reply_dispatch import (
    handle_daily_sync_reply,
    is_latest_batch_replied,
    looks_like_calibration_reply,
    mark_batch_replied,
    maybe_smart_route_reply,
)


# --- Fixtures -------------------------------------------------------------


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _item(num: int, *, priority: str = "medium", sender: str = "alice@example.com") -> dict:
    return {
        "item_number": num,
        "record_path": f"note/Item{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": sender,
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def _seed_batch(cfg: DailySyncConfig, *, items: list[dict], message_ids: list[int]) -> None:
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-04-23",
            "items": items,
            "message_ids": message_ids,
        },
    })


# --- Heuristic detection ---------------------------------------------------


def test_heuristic_matches_numbered_list_at_start():
    assert looks_like_calibration_reply("1. down")
    assert looks_like_calibration_reply("1) down")
    assert looks_like_calibration_reply("1 down")
    assert looks_like_calibration_reply("  2. high — urgent")


def test_heuristic_matches_check_alone():
    assert looks_like_calibration_reply("✅")
    assert looks_like_calibration_reply("ok")
    assert looks_like_calibration_reply("all good")
    assert looks_like_calibration_reply("looks good.")


def test_heuristic_matches_multi_numbered_references():
    assert looks_like_calibration_reply("1 down, 2 spam")
    assert looks_like_calibration_reply("3 high; 6 confirm")


def test_heuristic_rejects_prose():
    assert not looks_like_calibration_reply("create a note about Jamie")
    assert not looks_like_calibration_reply("how's the weather looking")
    assert not looks_like_calibration_reply("")
    # Single coincidental "1 hour" isn't enough — needs two number+verb
    # tokens or a leading-bullet shape.
    assert not looks_like_calibration_reply("Will reply in 1 hour about the project.")


# --- Smart routing --------------------------------------------------------


def test_smart_route_numbered_reply_without_reply_context(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="medium"),
        _item(2, priority="high"),
    ], message_ids=[100, 101])

    result = maybe_smart_route_reply(cfg, "1 down, 2 down")
    assert result is not None
    assert result["confirmed_count"] == 2
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 2
    assert is_latest_batch_replied(cfg) is True


def test_smart_route_check_emoji_alone(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    result = maybe_smart_route_reply(cfg, "✅")
    assert result is not None
    assert result["all_ok"] is True
    assert result["confirmed_count"] == 1
    assert is_latest_batch_replied(cfg) is True


def test_smart_route_falls_through_for_prose(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    result = maybe_smart_route_reply(cfg, "create a note about my morning run")
    assert result is None
    # Flag must NOT be set — this wasn't a calibration reply.
    assert is_latest_batch_replied(cfg) is False


def test_smart_route_falls_through_after_replied(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    # First smart-routed reply lands.
    result1 = maybe_smart_route_reply(cfg, "✅")
    assert result1 is not None
    assert is_latest_batch_replied(cfg) is True

    # Second message of the same SHAPE falls through — Andrew's UX
    # expectation is that follow-up clarifications use Telegram's
    # reply-to-message instead.
    result2 = maybe_smart_route_reply(cfg, "1 high")
    assert result2 is None


def test_smart_route_after_replied_reply_to_message_still_works(tmp_path: Path):
    """An explicit reply-to-message after the smart-route still
    routes through the dispatcher — the override semantics live at
    the bot layer (``_maybe_handle_daily_sync_reply``) and don't
    depend on the smart-routing flag.

    This test exercises the dispatcher directly to confirm the flag
    doesn't gate the explicit-reply path.
    """
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    # Smart-route the first message.
    maybe_smart_route_reply(cfg, "✅")
    assert is_latest_batch_replied(cfg) is True

    # Andrew long-presses the Daily Sync message and replies with
    # additional context — the dispatcher accepts it (the smart-route
    # flag doesn't block the explicit-reply path).
    result = handle_daily_sync_reply(cfg, 100, "1 down")
    assert result is not None
    assert result["confirmed_count"] == 1


def test_smart_route_false_positive_reverts_flag(tmp_path: Path):
    """When the heuristic matches but the parser extracts zero
    actionable corrections, the smart-route is treated as a false
    positive: flag reverts, return None, caller falls through to
    normal conversation."""
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    # "5. " is a numbered-list shape, but item 5 isn't in the batch
    # (batch only has item 1), so the parser produces zero corrections
    # and the dispatcher emits zero confirmed.
    # We construct a shape that hits the heuristic but produces
    # nothing actionable — "1." with no verb falls into _parse_fragment
    # 's "no recognised token" branch and lands in unparsed.
    result = maybe_smart_route_reply(cfg, "1. coffee")
    # Either smart-route returned None (false-positive guard fired) or
    # it returned a result with zero confirmed AND zero all_ok.
    if result is not None:
        assert result["confirmed_count"] == 0
        assert result["all_ok"] is False
    # The flag must be reverted in either case so the next legitimate
    # calibration reply still smart-routes.
    assert is_latest_batch_replied(cfg) is False


def test_smart_route_no_batch_falls_through(tmp_path: Path):
    """No Daily Sync ever pushed → no batch persisted → smart-route
    must return None without flipping any flag."""
    cfg = _config(tmp_path)
    # No _seed_batch call.

    result = maybe_smart_route_reply(cfg, "✅")
    assert result is None
    assert is_latest_batch_replied(cfg) is False


def test_smart_route_empty_text_falls_through(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    assert maybe_smart_route_reply(cfg, "") is None
    assert maybe_smart_route_reply(cfg, "   ") is None
    assert is_latest_batch_replied(cfg) is False


# --- Replied-flag bookkeeping --------------------------------------------


def test_explicit_reply_marks_batch_replied(tmp_path: Path):
    """The reply-to-message dispatcher also flips the replied flag
    when something material lands — keeps the smart-route window
    closed after Andrew has already engaged with the batch."""
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    assert is_latest_batch_replied(cfg) is False
    handle_daily_sync_reply(cfg, 100, "✅")
    assert is_latest_batch_replied(cfg) is True


def test_explicit_reply_with_zero_corrections_does_not_mark_replied(tmp_path: Path):
    """A reply-to-message that produces NEITHER all_ok NOR a
    correction (pure noise) shouldn't lock the smart-route window."""
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    handle_daily_sync_reply(cfg, 100, "garbage with no item refs")
    assert is_latest_batch_replied(cfg) is False


def test_mark_batch_replied_idempotent(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    mark_batch_replied(cfg)
    mark_batch_replied(cfg)
    assert is_latest_batch_replied(cfg) is True

    state = load_state(cfg.state.path)
    assert state["last_batch"]["replied"] is True


def test_mark_batch_replied_noop_when_no_batch(tmp_path: Path):
    cfg = _config(tmp_path)
    # No batch persisted.
    mark_batch_replied(cfg)
    assert is_latest_batch_replied(cfg) is False

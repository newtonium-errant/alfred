"""G1 (2026-07-30) — smart-route mistyped-calibration nudge.

Closes the ILB gap where a FREE-STANDING mistyped calibration reply
("3 confrim") fell through to SILENCE. The nudge fires ONLY for the
high-confidence case (real item number + a near-miss of a verb advertised
for the batch's item types); ordinary prose with an incidental leading
digit still falls through to normal conversation untouched.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.reply_dispatch import (
    _detect_mistyped_calibration,
    _is_calibration_verb_typo,
    _osa_distance,
    is_latest_batch_replied,
    maybe_smart_route_reply,
)


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _email_item(num: int, *, priority: str = "medium") -> dict:
    return {
        "item_number": num,
        "record_path": f"note/Item{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": "alice@example.com",
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def _seed_email_batch(cfg: DailySyncConfig, *, count: int, message_ids: list[int]) -> None:
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-07-30",
            "items": [_email_item(n) for n in range(1, count + 1)],
            "message_ids": message_ids,
        },
    })


def _seed_attribution_batch(cfg: DailySyncConfig, *, count: int, message_ids: list[int]) -> None:
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-07-30",
            "items": [],
            "attribution_items": [
                {"item_number": n, "record_path": f"person/P{n}.md", "name": f"P{n}"}
                for n in range(1, count + 1)
            ],
            "message_ids": message_ids,
        },
    })


# --- The G1 nudge (required test a) --------------------------------------


def test_mistyped_email_verb_nudges(tmp_path: Path) -> None:
    """Free-standing "3 dwon" (typo of email verb "down") → hint, not silence."""
    cfg = _config(tmp_path)
    _seed_email_batch(cfg, count=3, message_ids=[100])

    result = maybe_smart_route_reply(cfg, "3 dwon")

    assert result is not None                      # NOT silence
    assert result["confirmed_count"] == 0          # nothing applied
    assert result["all_ok"] is False
    assert "Tip" in result["message"]              # item-type-aware hint present
    # Calibration window stays OPEN so the corrected reply still routes.
    assert is_latest_batch_replied(cfg) is False


def test_mistyped_confirm_verb_nudges_attribution(tmp_path: Path) -> None:
    """The reported incident: "3 confrim" on a confirm/reject batch → hint."""
    cfg = _config(tmp_path)
    _seed_attribution_batch(cfg, count=3, message_ids=[100])

    result = maybe_smart_route_reply(cfg, "3 confrim")

    assert result is not None
    assert result["confirmed_count"] == 0
    # The hint advertises the verbs that apply to THIS batch (confirm/reject).
    assert "confirm" in result["message"].lower()
    assert is_latest_batch_replied(cfg) is False


def test_two_word_verb_typo_nudges(tmp_path: Path) -> None:
    """"1 shwo me" (typo of pending verb "show me") on a pending batch → hint."""
    cfg = _config(tmp_path)
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-07-30",
            "items": [],
            "pending_items": [{"item_number": 1, "id": "u1", "category": "x"}],
            "message_ids": [100],
        },
    })
    result = maybe_smart_route_reply(cfg, "1 shwo me")
    assert result is not None
    assert "Tip" in result["message"]
    assert is_latest_batch_replied(cfg) is False


def test_nudge_keeps_window_open_for_corrected_reply(tmp_path: Path) -> None:
    """After the nudge, the operator's corrected reply still smart-routes."""
    cfg = _config(tmp_path)
    _seed_email_batch(cfg, count=3, message_ids=[100])

    nudge = maybe_smart_route_reply(cfg, "3 dwon")
    assert nudge is not None
    assert is_latest_batch_replied(cfg) is False

    fixed = maybe_smart_route_reply(cfg, "3 down")
    assert fixed is not None
    assert fixed["confirmed_count"] == 1
    assert is_latest_batch_replied(cfg) is True


# --- Conversational fallthrough survives (required test b) ----------------


def test_prose_with_leading_digit_falls_through(tmp_path: Path) -> None:
    """Ordinary prose that happens to start with an in-batch digit → silence."""
    cfg = _config(tmp_path)
    _seed_email_batch(cfg, count=5, message_ids=[100])

    result = maybe_smart_route_reply(cfg, "2 things I wanted to say")

    assert result is None                          # falls through to normal chat
    assert is_latest_batch_replied(cfg) is False


def test_short_nonverb_falls_through(tmp_path: Path) -> None:
    """A short in-batch fragment whose token is nowhere near a verb → silence."""
    cfg = _config(tmp_path)
    _seed_email_batch(cfg, count=5, message_ids=[100])

    result = maybe_smart_route_reply(cfg, "2 dogs")

    assert result is None
    assert is_latest_batch_replied(cfg) is False


def test_out_of_range_digit_falls_through(tmp_path: Path) -> None:
    """A verb typo whose leading digit isn't in the batch → silence."""
    cfg = _config(tmp_path)
    _seed_attribution_batch(cfg, count=1, message_ids=[100])  # only item 1

    result = maybe_smart_route_reply(cfg, "9 confrim")

    assert result is None
    assert is_latest_batch_replied(cfg) is False


def test_dotted_bullet_nonverb_falls_through(tmp_path: Path) -> None:
    """Regression pin for the pre-existing false-positive: "1. coffee" → silence."""
    cfg = _config(tmp_path)
    _seed_email_batch(cfg, count=1, message_ids=[100])

    result = maybe_smart_route_reply(cfg, "1. coffee")

    assert result is None
    assert is_latest_batch_replied(cfg) is False


# --- Discriminator unit tests --------------------------------------------


def test_osa_distance_basics() -> None:
    assert _osa_distance("confrim", "confirm") == 1   # adjacent transposition
    assert _osa_distance("dwon", "down") == 1
    assert _osa_distance("shwo", "show") == 1
    assert _osa_distance("dogs", "down") == 2         # two substitutions
    assert _osa_distance("down", "down") == 0
    assert _osa_distance("", "down") == 4


def test_is_calibration_verb_typo() -> None:
    email = {"same", "ditto", "high", "medium", "low", "spam", "up", "down", "keep"}
    confirm = {"confirm", "reject"}
    assert _is_calibration_verb_typo("dwon", email) is True
    assert _is_calibration_verb_typo("confrim", confirm) is True
    assert _is_calibration_verb_typo("rejcet", confirm) is True
    # Negatives — ordinary words must never read as a verb typo.
    assert _is_calibration_verb_typo("dogs", email) is False
    assert _is_calibration_verb_typo("coffee", email) is False
    assert _is_calibration_verb_typo("things", email) is False
    # Sub-3-char tokens never match (everything is "close" at that length).
    assert _is_calibration_verb_typo("us", email) is False


def test_detect_mistyped_calibration_direct(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_email_batch(cfg, count=3, message_ids=[100])
    assert _detect_mistyped_calibration("3 dwon", cfg) is True
    assert _detect_mistyped_calibration("2 things I wanted to say", cfg) is False
    assert _detect_mistyped_calibration("2 dogs", cfg) is False
    assert _detect_mistyped_calibration("3 down", cfg) is False  # parses cleanly, not a typo

"""R4 — the Daily Sync voice-calibration review section + its config contract.

Two families here. The SECTION tests pin what the operator reads (including the
intentionally-left-blank sentinel, so a quiet loop is distinguishable from a
broken one). The CONFIG tests pin the single-source path contract by driving
BOTH REAL LOADERS over one production-shaped dict — the pin shape
``test_stt_vocab_section`` had to invent after a wrong-key derive shipped green
against twenty-eight fixtures that all agreed with the bug.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import structlog

from alfred.daily_sync import assembler, calibration_section
from alfred.daily_sync.config import load_from_unified
from alfred.telegram.calibration import Proposal
from alfred.telegram import calibration_store


TODAY = date(2026, 8, 21)


def _raw(tmp_path: Path, *, enabled=True, capture=True, extra=None) -> dict:
    """A production-shaped unified config whose telegram.calibration points at tmp_path.

    Keyed on ``telegram`` — the schema's own key (``telegram/config.py`` reads
    ``raw.get("telegram")`` and config.yaml.example ships ``telegram:``) — and
    carrying the fields that loader REQUIRES (bot_token / allowed_users /
    instance.name) so ONE dict can drive both loaders.
    """
    raw = {
        "vault": {"path": str(tmp_path / "vault")},
        "telegram": {
            "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
            "allowed_users": [1],
            "instance": {"name": "test-instance"},
            "primary_users": ["person/Andrew Newton"],
            "calibration": {
                "capture_enabled": capture,
                "pending_path": str(tmp_path / "pending.jsonl"),
                "decided_path": str(tmp_path / "decided.jsonl"),
            },
        },
        "daily_sync": {
            "enabled": True,
            "calibration_review": {"enabled": enabled},
        },
    }
    if extra:
        raw["daily_sync"]["calibration_review"].update(extra)
    return raw


def _seed(tmp_path: Path, *proposals: Proposal) -> list:
    return calibration_store.record_proposals(
        str(tmp_path / "pending.jsonl"),
        str(tmp_path / "decided.jsonl"),
        list(proposals),
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    assembler.clear_providers()
    calibration_section.consume_last_batch()


# ---------------------------------------------------------------------------
# The section
# ---------------------------------------------------------------------------


def test_section_is_omitted_when_disabled(tmp_path: Path) -> None:
    config = load_from_unified(_raw(tmp_path, enabled=False))
    assert calibration_section.calibration_review_section(config, TODAY) is None


def test_enabled_but_empty_renders_the_ILB_sentinel(tmp_path: Path) -> None:
    """Silence is indistinguishable from broken — an enabled-but-quiet section
    SAYS it ran and found nothing.

    Paired with the populated case below so this is a real rendering rather than
    a section that can only ever render its empty state.
    """
    config = load_from_unified(_raw(tmp_path))
    with structlog.testing.capture_logs() as captured:
        out = calibration_section.calibration_review_section(config, TODAY)

    assert out is not None
    assert "Voice calibration review" in out
    assert "No calibration proposals pending." in out
    events = [c for c in captured if c.get("event") == "calibration_review.no_proposals"]
    assert len(events) == 1


def test_pending_proposals_render_with_the_cli_as_the_only_door(tmp_path: Path) -> None:
    """The populated case — and the instruction names the CLI, not a reply.

    The Daily Sync REPLY dispatcher has had zero production callers since bot.py
    was deleted; four existing sections still print reply instructions nobody can
    act on. This section must never join them, so the rendered text is asserted
    to direct at the CLI verb.
    """
    _seed(
        tmp_path,
        Proposal("Communication Style", "Prefers bottom-line-up-front.", 0.9, "session/A"),
        Proposal("Workflow Preferences", "Batches review into the morning.", 0.7, "session/B"),
    )
    config = load_from_unified(_raw(tmp_path))
    with structlog.testing.capture_logs() as captured:
        out = calibration_section.calibration_review_section(config, TODAY, start_index=4)

    assert out is not None
    assert "Voice calibration review (2 items)" in out
    assert "Prefers bottom-line-up-front." in out
    assert "Batches review into the morning." in out
    # GLOBAL numbering continues from the assembler's start_index.
    assert "4. [Communication Style]" in out
    assert "5. [Workflow Preferences]" in out
    # The CLI is the door.
    assert "alfred voice-calibration approve" in out
    assert "alfred voice-calibration reject" in out
    # And the no-timeout promise is stated where the operator reads it.
    assert "no timeout" in out

    events = [c for c in captured if c.get("event") == "calibration_review.surfaced"]
    assert len(events) == 1
    assert events[0]["count"] == 2


def test_the_batch_holder_carries_the_rendered_items(tmp_path: Path) -> None:
    """``peek_last_batch_count`` feeds the assembler's continuous numbering."""
    _seed(tmp_path, Proposal("Communication Style", "One.", 0.9, "session/A"))
    config = load_from_unified(_raw(tmp_path))
    calibration_section.calibration_review_section(config, TODAY)
    assert calibration_section.peek_last_batch_count() == 1
    batch = calibration_section.consume_last_batch()
    assert batch[0].bullet == "One."
    # Consuming clears — the next fire must not re-serve a stale batch.
    assert calibration_section.peek_last_batch_count() == 0


def test_a_disabled_section_clears_any_stale_batch(tmp_path: Path) -> None:
    """Turning the section off must not leave the previous fire's items live."""
    _seed(tmp_path, Proposal("Communication Style", "One.", 0.9, "session/A"))
    calibration_section.calibration_review_section(load_from_unified(_raw(tmp_path)), TODAY)
    assert calibration_section.peek_last_batch_count() == 1

    calibration_section.calibration_review_section(
        load_from_unified(_raw(tmp_path, enabled=False)), TODAY
    )
    assert calibration_section.peek_last_batch_count() == 0


def test_a_decided_proposal_leaves_the_review_list(tmp_path: Path) -> None:
    rows = _seed(
        tmp_path,
        Proposal("Communication Style", "One.", 0.9, "session/A"),
        Proposal("Communication Style", "Two.", 0.9, "session/A"),
    )
    config = load_from_unified(_raw(tmp_path))
    calibration_store.reject_proposal(config.calibration_review, rows[0].proposal_id,
                                      operator="andrew")
    out = calibration_section.calibration_review_section(config, TODAY)
    assert "One." not in out
    assert "Two." in out


def test_register_is_idempotent(tmp_path: Path) -> None:
    calibration_section.register()
    calibration_section.register()
    assert assembler.registered_providers().count("calibration_review") == 1


# ---------------------------------------------------------------------------
# The single-source config contract — BOTH real loaders, one dict
# ---------------------------------------------------------------------------


def test_BOTH_loaders_agree_on_one_production_shaped_config(tmp_path: Path) -> None:
    """The write side and the read side must resolve the SAME two files.

    The web session-close WRITES ``pending`` off ``telegram.calibration``; this
    section and the CLI READ it off ``daily_sync.calibration_review``. A fixture
    written by this file could make either side agree with a bug, so this drives
    BOTH REAL LOADERS over ONE raw dict shaped like config.yaml.example. It
    cannot go green while the two read different top-level keys.
    """
    from alfred.telegram.config import load_from_unified as load_talker

    pending = str(tmp_path / "CUSTOM_pending.jsonl")
    decided = str(tmp_path / "CUSTOM_decided.jsonl")
    raw = _raw(tmp_path)
    raw["telegram"]["calibration"].update(
        {"pending_path": pending, "decided_path": decided}
    )

    write = load_talker(raw).calibration     # what capture writes
    read = load_from_unified(raw).calibration_review   # what review + CLI read

    assert write.pending_path == read.pending_path == pending
    assert write.decided_path == read.decided_path == decided


def test_both_loaders_agree_on_the_DEFAULTS_too(tmp_path: Path) -> None:
    """The floor case. A shared fallback constant can hide a key mismatch — both
    sides land on the default and look identical — so the override case above is
    the load-bearing one and this asserts they do not diverge unconfigured."""
    from alfred.telegram.config import load_from_unified as load_talker

    raw = _raw(tmp_path)
    raw["telegram"]["calibration"].pop("pending_path", None)
    raw["telegram"]["calibration"].pop("decided_path", None)

    write = load_talker(raw).calibration
    read = load_from_unified(raw).calibration_review
    assert write.pending_path == read.pending_path
    assert write.decided_path == read.decided_path


def test_an_explicit_daily_sync_override_still_wins(tmp_path: Path) -> None:
    """The intentional, non-silent split — an operator may point the review at a
    different file, and it must not be silently overwritten by the derive."""
    override = str(tmp_path / "OVERRIDE_pending.jsonl")
    raw = _raw(tmp_path, extra={"pending_path": override})
    assert load_from_unified(raw).calibration_review.pending_path == override


def test_an_absent_telegram_block_falls_back_to_the_shared_constants(
    tmp_path: Path,
) -> None:
    from alfred.telegram.config import (
        DEFAULT_CALIBRATION_DECIDED_PATH,
        DEFAULT_CALIBRATION_PENDING_PATH,
    )

    raw = _raw(tmp_path)
    raw.pop("telegram")
    cr = load_from_unified(raw).calibration_review
    assert cr.pending_path == DEFAULT_CALIBRATION_PENDING_PATH
    assert cr.decided_path == DEFAULT_CALIBRATION_DECIDED_PATH


def test_both_switches_default_off(tmp_path: Path) -> None:
    """A new judgment surface is opt-in per instance, on BOTH halves.

    Capture and review are separate decisions: an instance may accumulate
    proposals without showing the morning card, or show the card while capture
    is off (the queue simply stays empty).
    """
    from alfred.telegram.config import load_from_unified as load_talker

    raw = {
        "vault": {"path": str(tmp_path / "vault")},
        "telegram": {
            "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
            "allowed_users": [1],
            "instance": {"name": "test-instance"},
        },
        "daily_sync": {"enabled": True},
    }
    assert load_talker(raw).calibration.capture_enabled is False
    assert load_talker(raw).calibration.inject_enabled is False
    assert load_from_unified(raw).calibration_review.enabled is False

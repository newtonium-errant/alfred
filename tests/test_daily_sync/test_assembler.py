"""Tests for the Daily Sync assembler + reply parser.

Covers:
- Provider registration + render order (priority sort).
- Assembled message includes registered sections.
- Empty sync (all providers return None) → header-only "No items today".
- Provider exceptions don't crash the assembly.
- Reply parser:
    - Whole-message ack ("✅", "ok", "all good", emoji-only).
    - Multi-item shorthand ("2 down, 4 spam").
    - Item with explicit tier + free-text note.
    - Mixed forms ("1 ok, 3 down, 5: high — RRTS customer").
    - Empty / unparseable replies.
    - Edge cases: bullets, leading whitespace, period-terminated tokens.
- apply_modifier saturation behavior.
"""

from __future__ import annotations

from datetime import date

import pytest

from alfred.daily_sync.assembler import (
    EMPTY_SYNC_BODY,
    apply_modifier,
    assemble_message,
    clear_providers,
    parse_reply,
    register_provider,
    registered_providers,
)
from alfred.daily_sync.config import DailySyncConfig


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_providers()
    yield
    clear_providers()


@pytest.fixture
def config() -> DailySyncConfig:
    return DailySyncConfig(enabled=True)


# --- Registry --------------------------------------------------------------


def test_register_provider_orders_by_priority(config):
    register_provider("low_pri", priority=30, provider=lambda c, t: "low")
    register_provider("high_pri", priority=10, provider=lambda c, t: "high")
    register_provider("mid_pri", priority=20, provider=lambda c, t: "mid")
    assert registered_providers() == ["high_pri", "mid_pri", "low_pri"]


def test_register_provider_duplicate_name_raises(config):
    register_provider("a", priority=10, provider=lambda c, t: "x")
    with pytest.raises(ValueError, match="already registered"):
        register_provider("a", priority=20, provider=lambda c, t: "y")


# --- Assembler -------------------------------------------------------------


def test_assemble_with_one_provider_includes_section(config):
    register_provider(
        "email", priority=10,
        provider=lambda c, t: "## Email calibration\n1. test",
    )
    out = assemble_message(config, date(2026, 4, 22))
    assert "Daily Sync — 2026-04-22" in out
    assert "## Email calibration" in out
    assert "1. test" in out


def test_assemble_skips_none_returns(config):
    register_provider("a", priority=10, provider=lambda c, t: "## A\nbody")
    register_provider("b", priority=20, provider=lambda c, t: None)
    register_provider("c", priority=30, provider=lambda c, t: "## C\nbody")
    out = assemble_message(config, date(2026, 4, 22))
    assert "## A" in out
    assert "## C" in out
    assert "## B" not in out


def test_assemble_empty_returns_header_only(config):
    register_provider("a", priority=10, provider=lambda c, t: None)
    register_provider("b", priority=20, provider=lambda c, t: None)
    out = assemble_message(config, date(2026, 4, 22))
    assert out == EMPTY_SYNC_BODY.format(date="2026-04-22")
    assert "No items today" in out


def test_assemble_with_no_providers_returns_empty_body(config):
    out = assemble_message(config, date(2026, 4, 22))
    assert "No items today" in out


def test_assemble_provider_exception_does_not_crash(config):
    def bad_provider(c, t):
        raise RuntimeError("provider blew up")

    register_provider("good", priority=10, provider=lambda c, t: "## ok\nbody")
    register_provider("bad", priority=20, provider=bad_provider)
    out = assemble_message(config, date(2026, 4, 22))
    assert "## ok" in out
    assert "[bad] section provider failed: RuntimeError: provider blew up" in out


# --- Reply parser ----------------------------------------------------------


def test_parse_reply_emoji_ok():
    result = parse_reply("✅")
    assert result.all_ok is True
    assert result.corrections == []
    assert result.unparsed == []


def test_parse_reply_word_ok():
    for token in ("ok", "okay", "all good", "looks good", "all ok"):
        result = parse_reply(token)
        assert result.all_ok is True, f"failed for {token!r}"


def test_parse_reply_simple_modifier():
    result = parse_reply("2 down")
    assert result.all_ok is False
    assert len(result.corrections) == 1
    c = result.corrections[0]
    assert c.item_number == 2
    assert c.modifier == "down"
    assert c.new_tier is None
    assert c.note == ""


def test_parse_reply_explicit_tier():
    result = parse_reply("3 spam")
    assert len(result.corrections) == 1
    assert result.corrections[0].item_number == 3
    assert result.corrections[0].new_tier == "spam"
    assert result.corrections[0].modifier is None


def test_parse_reply_multi_item():
    result = parse_reply("2 down, 4 spam")
    assert len(result.corrections) == 2
    assert result.corrections[0].item_number == 2
    assert result.corrections[0].modifier == "down"
    assert result.corrections[1].item_number == 4
    assert result.corrections[1].new_tier == "spam"


def test_parse_reply_with_note():
    result = parse_reply("2: actually high — Jamie was waiting")
    assert len(result.corrections) == 1
    c = result.corrections[0]
    assert c.item_number == 2
    assert c.new_tier == "high"
    assert "Jamie was waiting" in c.note


def test_parse_reply_mixed_forms():
    result = parse_reply("1 ok, 3 down, 5: high — RRTS customer")
    assert len(result.corrections) == 3
    assert result.corrections[0].item_number == 1
    assert result.corrections[0].ok is True
    assert result.corrections[1].item_number == 3
    assert result.corrections[1].modifier == "down"
    assert result.corrections[2].item_number == 5
    assert result.corrections[2].new_tier == "high"
    assert "RRTS customer" in result.corrections[2].note


def test_parse_reply_empty_input():
    result = parse_reply("")
    assert result.all_ok is False
    assert result.corrections == []
    assert result.unparsed == []


def test_parse_reply_garbage_collects_unparsed():
    result = parse_reply("just rambling without numbers")
    assert result.all_ok is False
    assert result.corrections == []
    assert result.unparsed  # at least one fragment captured


def test_parse_reply_partial_unparsed():
    result = parse_reply("2 down, what was item 4 about")
    # item 2 parses; the second fragment doesn't match the regex
    assert any(c.item_number == 2 for c in result.corrections)
    assert result.unparsed  # second fragment landed in unparsed


def test_parse_reply_tolerates_leading_bullet():
    result = parse_reply("- 2 down")
    assert len(result.corrections) == 1
    assert result.corrections[0].item_number == 2
    assert result.corrections[0].modifier == "down"


def test_parse_reply_med_alias():
    result = parse_reply("2 med")
    assert result.corrections[0].new_tier == "medium"


# --- Numbered-list reply shape (1. Down / 2. Spam / 3. up) -----------------
#
# Autocorrect, dictation, and voice transcripts naturally produce
# numbered-list format with a period after the digit. The separator
# class in _FRAGMENT_RE accepts ``.`` so these parse as first-class
# fragments alongside ``1 down`` / ``1: down`` / ``1 - down``.


def test_parse_reply_numbered_list_modifier():
    result = parse_reply("1. Down")
    assert len(result.corrections) == 1
    c = result.corrections[0]
    assert c.item_number == 1
    assert c.modifier == "down"
    assert c.new_tier is None
    assert c.note == ""


def test_parse_reply_numbered_list_tier_spam():
    result = parse_reply("1. Spam")
    assert len(result.corrections) == 1
    c = result.corrections[0]
    assert c.item_number == 1
    assert c.new_tier == "spam"
    assert c.modifier is None


def test_parse_reply_numbered_list_modifier_up():
    result = parse_reply("1. up")
    assert len(result.corrections) == 1
    c = result.corrections[0]
    assert c.item_number == 1
    assert c.modifier == "up"
    assert c.new_tier is None


def test_parse_reply_numbered_list_tier_with_note():
    result = parse_reply("1. high — Jamie was waiting")
    assert len(result.corrections) == 1
    c = result.corrections[0]
    assert c.item_number == 1
    assert c.new_tier == "high"
    assert "Jamie was waiting" in c.note


def test_parse_reply_numbered_list_multi_item_newline_split():
    result = parse_reply("1. Down\n2. Spam\n3. up")
    assert len(result.corrections) == 3
    assert result.corrections[0].item_number == 1
    assert result.corrections[0].modifier == "down"
    assert result.corrections[1].item_number == 2
    assert result.corrections[1].new_tier == "spam"
    assert result.corrections[2].item_number == 3
    assert result.corrections[2].modifier == "up"
    assert result.unparsed == []


def test_parse_reply_existing_forms_still_pass():
    # Sanity check: the separator-class extension to include ``.`` must
    # not regress the bare / colon / dash forms Andrew already uses.
    bare = parse_reply("1 down")
    assert bare.corrections[0].item_number == 1
    assert bare.corrections[0].modifier == "down"

    colon = parse_reply("1: down")
    assert colon.corrections[0].item_number == 1
    assert colon.corrections[0].modifier == "down"

    dash = parse_reply("1 - down")
    assert dash.corrections[0].item_number == 1
    assert dash.corrections[0].modifier == "down"


# --- duplicate verb (Stage 1, 2026-05-15) ---------------------------------
#
# ``N duplicate`` resolves item N the same way as the previous item
# in the same reply (item N-1). ``N duplicate of M`` form is the
# explicit pointer. The dispatcher stamps the resulting corpus row
# with ``via="duplicate-of-M"`` so few-shot rotation can detect the
# "Andrew calls X a duplicate of Y" signal.


def test_parse_reply_duplicate_resolves_to_previous_item():
    result = parse_reply("4 spam, 5 duplicate")
    assert result.unparsed == []
    assert len(result.corrections) == 2
    item4 = result.corrections[0]
    item5 = result.corrections[1]
    assert item4.item_number == 4
    assert item4.new_tier == "spam"
    assert item5.item_number == 5
    # Resolved correction takes item 4's classification.
    assert item5.new_tier == "spam"
    assert item5.via == "duplicate-of-4"
    # Chain placeholders cleared after resolution.
    assert item5.duplicate_of_item is None


def test_parse_reply_duplicate_explicit_pointer():
    result = parse_reply("1 ok, 2 down, 5 duplicate of 1")
    assert result.unparsed == []
    assert len(result.corrections) == 3
    item5 = result.corrections[2]
    assert item5.item_number == 5
    # Resolved correction takes item 1's classification (ok=True).
    assert item5.ok is True
    assert item5.via == "duplicate-of-1"
    assert item5.duplicate_of_item is None


def test_parse_reply_duplicate_with_hash_pointer():
    result = parse_reply("1 high, 4 duplicate of #1")
    assert result.unparsed == []
    item4 = result.corrections[1]
    assert item4.new_tier == "high"
    assert item4.via == "duplicate-of-1"


def test_parse_reply_duplicate_with_no_prior_item_unparsed():
    # Defensive: bare "duplicate" with no prior correction in the
    # reply lands in unparsed. The brief flagged this as the path
    # operator probably never types alone — verify it doesn't crash
    # and produces a parseable error.
    result = parse_reply("3 duplicate")
    assert result.corrections == []
    assert len(result.unparsed) == 1
    assert "3" in result.unparsed[0]
    assert "duplicate" in result.unparsed[0].lower()


def test_parse_reply_duplicate_of_nonexistent_item_unparsed():
    # ``5 duplicate of 9`` when item 9 wasn't mentioned earlier
    # produces a parseable error, not a crash.
    result = parse_reply("1 ok, 5 duplicate of 9")
    assert len(result.corrections) == 1  # only item 1
    assert any("5" in u for u in result.unparsed)


def test_parse_reply_duplicate_with_trailing_note():
    # Trailing content after a dash is preserved as the new
    # correction's note, mirroring same-chain behavior.
    result = parse_reply("4 spam, 5 duplicate — same Headspace newsletter")
    assert result.unparsed == []
    item5 = result.corrections[1]
    assert item5.new_tier == "spam"
    assert item5.via == "duplicate-of-4"
    assert "Headspace" in item5.note


def test_parse_reply_duplicate_preserves_modifier_chain():
    # Source correction has a modifier (``down``); duplicate inherits it.
    result = parse_reply("2 down, 5 duplicate")
    assert result.unparsed == []
    item5 = result.corrections[1]
    assert item5.modifier == "down"
    assert item5.via == "duplicate-of-2"


# --- Modifier arithmetic ---------------------------------------------------


def test_apply_modifier_down_steps():
    assert apply_modifier("high", "down") == "medium"
    assert apply_modifier("medium", "down") == "low"
    # saturating at low
    assert apply_modifier("low", "down") == "low"


def test_apply_modifier_up_steps():
    assert apply_modifier("low", "up") == "medium"
    assert apply_modifier("medium", "up") == "high"
    # saturating at high
    assert apply_modifier("high", "up") == "high"


def test_apply_modifier_spam_saturates():
    assert apply_modifier("spam", "down") == "spam"
    assert apply_modifier("spam", "up") == "spam"


def test_apply_modifier_unknown_tier_conservative():
    assert apply_modifier("unclassified", "down") == "low"
    assert apply_modifier("", "up") == "high"

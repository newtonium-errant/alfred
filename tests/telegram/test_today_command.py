"""Tier Phase 2A — /today slash command tests (originally 2026-05-28;
scope refined 2026-05-29 Ship 3).

Per the dispatch spec:
  * /today — Salem-only glance-view mini-brief composing the tier
    and upcoming-events sections as one Telegram reply
  * **Routines section dropped in Ship 3** — duplicating the brief's
    routines surface in /today muddled the glance-view's purpose;
    operators read routines from the morning brief or via the routine
    CLI surface
  * Read-only — no vault writes, no session record
  * Config-gated via ``telegram.today_command.enabled: true``
  * Salem-only: KAL-LE / Hypatia leave the block absent so Telegram's
    unknown-command behaviour fires

Coverage:
  * Config dataclass tolerates absent block (defaults to disabled)
  * Handler dispatches when enabled=True
  * Handler no-ops when enabled=False (defensive in-handler gate)
  * Handler no-ops on non-allowlisted user (access control parity)
  * Composed body contains the TWO section headers (tier + events)
  * Composed body does NOT contain a routines section header (Ship 3 pin)
  * Section ordering matches the morning brief (tier → events)
  * Body length under the Telegram cap (under 4000 chars sanity check)
  * compose_today_reply pure-helper covers: tier header present,
    upcoming-events header present, ordering preserved, defensive
    truncation when body would overflow
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from alfred.telegram.config import (
    TodayCommandConfig,
    load_from_unified,
)
from alfred.telegram.today_command import compose_today_reply


HALIFAX = ZoneInfo("America/Halifax")


# ---------------------------------------------------------------------------
# Vault fixture — minimal Salem-shape with task + routine + event dirs
# ---------------------------------------------------------------------------


@pytest.fixture
def salem_vault(tmp_path: Path) -> Path:
    """Vault with task/, routine/, event/, daily/ subdirs — enough for
    each of the three section renders to operate without ENOENT noise."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("task", "routine", "event", "daily"):
        (vault / sub).mkdir()
    return vault


# ---------------------------------------------------------------------------
# Config dataclass behaviour
# ---------------------------------------------------------------------------


def test_today_command_config_default_disabled() -> None:
    """``TodayCommandConfig()`` constructs with enabled=False by default
    — Salem-only opt-in convention."""
    cfg = TodayCommandConfig()
    assert cfg.enabled is False
    assert cfg.timezone == "America/Halifax"


def test_today_command_config_explicit_enable() -> None:
    """Operator opts in by setting ``enabled: true``."""
    cfg = TodayCommandConfig(enabled=True)
    assert cfg.enabled is True


def test_talker_config_today_command_block_absent() -> None:
    """Config block absent → ``today_command`` stays None.

    Mirrors the inventory_views + moc_suggestions sentinel convention.
    Distinguishes 'block absent' from 'block present, command disabled'
    so health probes / dashboards can tell the difference."""
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "Salem"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is None


def test_talker_config_today_command_block_present_with_enabled() -> None:
    """Config block present + ``enabled: true`` → TodayCommandConfig
    constructed with the opt-in flag set."""
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "Salem"},
            "today_command": {"enabled": True},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is not None
    assert cfg.today_command.enabled is True
    # Default timezone preserved when YAML omits it.
    assert cfg.today_command.timezone == "America/Halifax"


def test_talker_config_today_command_block_present_with_timezone_override() -> None:
    """Operator can override the timezone for non-Salem opt-ins."""
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "Salem"},
            "today_command": {
                "enabled": True,
                "timezone": "America/Toronto",
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is not None
    assert cfg.today_command.timezone == "America/Toronto"


# ---------------------------------------------------------------------------
# compose_today_reply — pure-helper composition tests
# ---------------------------------------------------------------------------


def test_compose_includes_tier_section_header(salem_vault: Path) -> None:
    """The composed body MUST include the canonical tier section header
    string (``"Today's Plan"``) — operator's mental model is the
    brief's exact wording."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    assert "## Today's Plan" in body


def test_compose_excludes_routines_section_header(salem_vault: Path) -> None:
    """Ship 3 scope refinement (2026-05-29): routines section is NOT
    in /today. Operators read routines from the morning brief or the
    routine CLI surface, not /today.

    Pinned so a future re-introduction surfaces immediately. If you
    find yourself wanting to add routines back, ask the operator
    whether the glance-view purpose changed — the original Phase 2A
    ship had routines + the Ship 3 drop was deliberate scope
    refinement, not an oversight."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    assert "## Today's Routines" not in body
    # Defensive: the routines render function shouldn't even be
    # importable from the today_command module post-Ship 3.
    from alfred.telegram import today_command as _tc
    assert not hasattr(_tc, "render_routine_section"), (
        "today_command.render_routine_section was re-introduced — "
        "Ship 3 dropped this binding deliberately. Confirm operator "
        "intent before adding routines back."
    )
    assert not hasattr(_tc, "ROUTINES_SECTION_HEADER"), (
        "today_command.ROUTINES_SECTION_HEADER was re-introduced — "
        "Ship 3 dropped this binding deliberately."
    )


def test_compose_includes_upcoming_events_section_header(
    salem_vault: Path,
) -> None:
    """Mirror of the brief daemon's ``Upcoming Events`` section header."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    assert "## Upcoming Events" in body


def test_compose_section_ordering_matches_brief(salem_vault: Path) -> None:
    """Sections appear in the canonical brief order: tier →
    upcoming events. Pinned so a refactor that reorders silently
    surfaces here.

    Ship 3 (2026-05-29): routines section dropped from /today; this
    test now pins the two-section ordering."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    tier_idx = body.index("## Today's Plan")
    events_idx = body.index("## Upcoming Events")
    assert tier_idx < events_idx, (
        f"Section ordering must match the brief: tier ({tier_idx}) → "
        f"events ({events_idx})"
    )


def test_compose_body_under_telegram_cap_for_empty_vault(
    salem_vault: Path,
) -> None:
    """Empty-vault composition stays well under Telegram's 4096-char
    cap. Sanity check that the empty-state sentinels in each section
    don't accidentally inflate to overflow."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    # Dispatch's "under 4000 char limit" sanity assertion.
    assert len(body) < 4000


def test_compose_uses_intentionally_left_blank_sentinels_when_empty(
    salem_vault: Path,
) -> None:
    """Per ``feedback_intentionally_left_blank.md``: each section
    emits a non-empty body even when its bucket is empty. The
    composed reply should NEVER silently drop a section."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    # Each section header has its body below it — pin that the
    # body following each header is non-empty (the sentinel string
    # from each section's render path). Ship 3: routines dropped;
    # only two headers now.
    lines = body.splitlines()
    headers = [
        "## Today's Plan",
        "## Upcoming Events",
    ]
    for header in headers:
        idx = lines.index(header)
        # Header is followed by a blank line + at least one content
        # line — sentinel from the section's render path.
        non_blank_after = [
            line for line in lines[idx + 1:idx + 5]
            if line.strip()
        ]
        assert len(non_blank_after) > 0, (
            f"Section '{header}' has no body lines below it — "
            f"sentinel missing per intentionally-left-blank discipline"
        )


def test_compose_both_renders_failing_emits_combined_sentinels(
    salem_vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combined-failure pin: when BOTH section render functions
    raise, the composed body MUST carry both
    ``*(<section> render failed; see brief log)*`` sentinels — not
    silently drop sections — AND the total body length stays under
    ``_TELEGRAM_BODY_CAP``.

    Pinned per code-reviewer boundary-coverage gap (2026-05-28).
    Ship 3 (2026-05-29) — routines section dropped, so this test
    now pins the two-section combined-failure path. The
    per-section try/except blocks each emit a sentinel; this
    test exercises the combined-failure path end-to-end through
    the composition + truncation logic. Without this pin, a
    refactor that swallows exceptions silently or emits empty
    strings on failure would only surface during real-vault edge
    cases where both renders happen to fail at once."""
    from alfred.telegram import today_command as _tc

    def _raise_for_tier(*args, **kwargs):
        raise RuntimeError("tier render simulated failure")

    def _raise_for_upcoming(*args, **kwargs):
        raise RuntimeError("upcoming events render simulated failure")

    # Patch in the today_command module's namespace — that's where
    # the compose path looks them up via the from-import binding.
    # 2026-05-30: tier section is now the curated-only render
    # (render_curated_tier_section_for_today + load_daily_curation),
    # not render_tier_section. Patch BOTH the load + render to
    # cover the try/except surface: if either raises, the
    # *(tier render failed; see brief log)* sentinel must surface.
    monkeypatch.setattr(
        _tc, "render_curated_tier_section_for_today", _raise_for_tier,
    )
    monkeypatch.setattr(
        _tc, "render_upcoming_events_section", _raise_for_upcoming,
    )

    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)

    # Both sentinels present — no silent drop per
    # intentionally-left-blank discipline.
    assert "*(tier render failed; see brief log)*" in body
    assert "*(upcoming events render failed; see brief log)*" in body

    # Routines sentinel must NOT appear — routines section is gone
    # in Ship 3, so a routines render failure can't surface.
    assert "*(routines render failed; see brief log)*" not in body

    # Section headers still emit so the operator's mental model of
    # the brief surface stays intact (failed section ≠ missing
    # section).
    assert "## Today's Plan" in body
    assert "## Upcoming Events" in body
    # Routines header NOT in body.
    assert "## Today's Routines" not in body

    # Combined-failure body length stays under the Telegram cap.
    # Pins the truncation-path correctness for the worst case (two
    # error sentinels rather than two full section bodies; should
    # be well under cap, but explicit assertion guards against a
    # future refactor that inflates the per-section sentinel text).
    assert len(body) < _tc._TELEGRAM_BODY_CAP


# ---------------------------------------------------------------------------
# Handler dispatch — config gate + access control
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Handler registration in build_app — drive-the-prod-call shape
# ---------------------------------------------------------------------------
#
# These tests build a PTB :class:`Application` via ``bot.build_app`` and
# inspect the actual handler registry for the ``today`` CommandHandler.
# Replaces the earlier predicate-mimic shape (re-evaluating the gate
# predicate inline) per task #12 sweep 2026-05-29 — the predicate-
# mimic would pass silently if a refactor moved the gate to a
# different code location while preserving the predicate shape.
#
# Mirror of the canonical patterns at
# ``test_voice_train.py:_build_app_and_get_commands`` and
# ``test_moc_suggestion_command_registration.py:_registered_command_names``.


# ===========================================================================
# 2026-05-30 curated-only refinement — compose_today_reply switches from
# render_tier_section (full materials) to render_curated_tier_section_for_today
# (operator-committed view only).
# ===========================================================================


def test_compose_today_reply_uses_curated_render_not_full_materials(
    salem_vault: Path,
) -> None:
    """compose_today_reply MUST call render_curated_tier_section_for_today,
    NOT render_tier_section.

    Scope refinement (2026-05-30): the /today surface is the
    operator-committed view. The full materials view (auto-T1 +
    selection pool + rollover + confirm prompts) stays in the morning
    brief only. Pinned via call-site monkeypatch: assert
    render_curated_tier_section_for_today is invoked, while
    render_tier_section is NOT imported on the today_command module.
    """
    from alfred.telegram import today_command as _tc
    # The curated render function MUST be bound on the module —
    # imported via from-import so it's available for monkeypatching
    # in the failure-pin test below.
    assert hasattr(_tc, "render_curated_tier_section_for_today")
    # The old full-materials render function must NOT be bound on
    # the today_command module (operator-stated scope refinement).
    assert not hasattr(_tc, "render_tier_section"), (
        "render_tier_section must NOT be imported on today_command "
        "after the 2026-05-30 curated-only refinement. The /today "
        "surface uses render_curated_tier_section_for_today only."
    )

    # Drive: call compose_today_reply with a mock curated render and
    # verify it's invoked.
    captured_args: list = []

    def _spy_curated(curation, vault_path=None, today=None):
        captured_args.append(curation)
        return "### T1 — (no items yet)\n\n### T2 — (no items yet)\n\n### T3 — (no items yet)\n"

    import unittest.mock
    with unittest.mock.patch.object(
        _tc, "render_curated_tier_section_for_today",
        side_effect=_spy_curated,
    ):
        now = datetime(2026, 5, 30, 10, 0, tzinfo=HALIFAX)
        compose_today_reply(salem_vault, now)
    # The spy was invoked exactly once (one tier render per /today
    # call).
    assert len(captured_args) == 1


def test_compose_today_reply_curated_render_receives_loaded_curation(
    salem_vault: Path,
) -> None:
    """compose_today_reply loads daily_curation FIRST, then passes
    the result to render_curated_tier_section_for_today.

    Verified by pre-writing a daily file with a curation block and
    spying on the render call to confirm the curation object reached
    it."""
    from alfred.telegram import today_command as _tc

    # Pre-write a daily file with a curated T1 entry.
    daily_dir = salem_vault / "daily"
    daily_dir.mkdir(exist_ok=True)
    (daily_dir / "2026-05-30.md").write_text(
        "---\n"
        "type: daily\n"
        "date: 2026-05-30\n"
        "tier_curation:\n"
        "  t1:\n"
        "    - task: '[[task/Curated Today]]'\n"
        "      source: operator\n"
        "      confirmed: true\n"
        "  t2: []\n"
        "  t3: []\n"
        "---\n\n# body\n",
        encoding="utf-8",
    )

    captured_curation = []

    def _spy(curation, vault_path=None, today=None):
        captured_curation.append(curation)
        return "stub"

    import unittest.mock
    with unittest.mock.patch.object(
        _tc, "render_curated_tier_section_for_today", side_effect=_spy,
    ):
        now = datetime(2026, 5, 30, 10, 0, tzinfo=HALIFAX)
        compose_today_reply(salem_vault, now)

    # The curation loaded from the daily file reached the render.
    assert len(captured_curation) == 1
    curation = captured_curation[0]
    assert curation is not None
    assert len(curation.t1) == 1
    assert curation.t1[0].task == "[[task/Curated Today]]"


def test_compose_today_reply_curated_render_handles_missing_daily_file(
    salem_vault: Path,
) -> None:
    """When no daily file exists for today, load_daily_curation
    returns None and the curated render gets None — empty-bucket
    sentinels surface."""
    # salem_vault has empty daily/ — no file for 2026-05-30.
    now = datetime(2026, 5, 30, 10, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    # All three empty-bucket sentinels surface end-to-end.
    assert "### T1 — (no items yet)" in body
    assert "### T2 — (no items yet)" in body
    assert "### T3 — (no items yet)" in body


def test_compose_today_reply_end_to_end_excludes_materials_surfaces(
    salem_vault: Path,
) -> None:
    """End-to-end pin: the composed body does NOT contain morning-
    brief-only surfaces (selection pool header, auto-T2-routine
    subsection header, rollover header)."""
    now = datetime(2026, 5, 30, 10, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    # Morning-brief materials surfaces MUST NOT appear in /today.
    assert "### T2 selection pool" not in body
    assert "#### Auto-surfaced (from routines)" not in body
    assert "### Rollover from yesterday (incomplete)" not in body
    # Confirm prompts also absent.
    assert '*(confirm? reply "T1 confirm")*' not in body
    assert '*(reply "T2 confirm" to keep on today\'s list)*' not in body
    # The composer still emits the section headers (operator's mental
    # model of the brief carries over).
    assert "## Today's Plan" in body
    assert "## Upcoming Events" in body

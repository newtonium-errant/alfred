"""Sustained agent-backend outage → a needs-you health card (2026-08-15).

THE INCIDENT. The box's Claude account hit its weekly quota at 00:31Z; every
``claude -p`` structuring call failed for days, email processing quarantined,
the email classifier went silent. The signal that fired was a curator WARN in
the brief's health section and a ``health:curator`` card at FYI attention —
technically present, practically invisible. The operator discovered the outage
by noticing an EMPTY DECK, not from the signal.

The escalation spans two layers on purpose, and each half is pinned here:

  * the health layer decides SEVERITY (a sustained streak is a FAIL, not a
    WARN) — ``tests/health/test_curator_agent_failure_probe.py`` owns that
    boundary;
  * this module owns the FEED half: a FAIL health card is born at needs-you
    attention, a WARN one is not, and — the pin that guards the design —
    promoting attention must NOT cost the card its only dismiss path.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from alfred.brief.feed_producer import health_feed_items
from alfred.curator.health import CURATOR_AGENT_CONSEQUENCE
from alfred.daily_sync.action_router import (
    FEED_ACTIONS,
    STATUS_ACKED,
    actions_for,
)
from alfred.feed import FeedStore
from alfred.health.agent_failure import agent_failure_check
from alfred.feed.model import (
    ATTENTION_FYI,
    ATTENTION_NEEDS_YOU,
    MODE_FYI,
    SNAPSHOT_FINGERPRINT_FIELDS,
    STATE_ACKED,
)

# The real curator detail the escalated probe emits, so the card body under test
# is the one an operator would actually read at 6am.
#
# DERIVED, not transcribed (2026-08-22). It was a hand-copied literal, and when
# the severity mapping moved into ``alfred.health.agent_failure`` the real detail
# gained "(the structuring pipeline is DOWN)" while this string did not. Nothing
# went red — the assertions below test surviving substrings — so the comment
# above quietly became false and the fixture became a mirror of a detail the
# probe no longer emits. Calling the probe keeps the two in lockstep by
# construction: the feed half is about what the feed DOES with a card body, and
# it should read the body from whatever curator actually produces.
_OUTAGE_DETAIL = agent_failure_check(
    failure={
        "ts": "2026-08-15T04:00:00+00:00",
        "since": "2026-08-15T00:31:00+00:00",
        "kind": "quota_limited",
        "summary_tail": (
            "Exit code 1: stdout: You've hit your weekly limit · resets Aug 20"
        ),
        "consecutive": 4,
    },
    last_success_ts="2026-08-01T00:00:00+00:00",
    consequence=CURATOR_AGENT_CONSEQUENCE,
    state_path="(fixture)",
).detail


def _write_bit(vault: Path, summary: str, date_str: str = "2026-08-15") -> None:
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    for old in run.glob("Alfred BIT *.md"):
        old.unlink()
    (run / f"Alfred BIT {date_str}.md").write_text(
        "---\ntype: run\n---\n\n## Summary\n" + summary + "## Detail\n",
        encoding="utf-8",
    )


def _by_id(vault: Path) -> dict[str, Any]:
    items = health_feed_items(vault, instance="salem")
    assert items is not None, "BIT record present — extractor must not report failure"
    return {it.id: it for it in items}


# ---------------------------------------------------------------------------
# FAIL → needs-you, WARN → FYI (paired: neither half is asserted alone)
# ---------------------------------------------------------------------------


def test_fail_card_is_needs_you_and_warn_card_is_not(tmp_path: Path) -> None:
    """The escalation and its control in ONE record.

    Asserting only "FAIL is needs-you" would pass just as well against a build
    that promoted EVERY health card — which is the noise failure this design is
    balanced against. The WARN line is the positive control for the negative
    half: same producer, same run, different severity.
    """
    _write_bit(
        tmp_path,
        f"[FAIL] curator — {_OUTAGE_DETAIL}\n"
        "[WARN] surveyor  (92 ms) — ollama 404\n"
        "[OK] brief  (40 ms)\n"
        "[SKIP] mail\n",
    )
    by_id = _by_id(tmp_path)

    assert set(by_id) == {"health:curator", "health:surveyor"}  # ok + skip stay quiet
    assert by_id["health:curator"].attention == ATTENTION_NEEDS_YOU
    assert by_id["health:surveyor"].attention == ATTENTION_FYI


def test_fail_card_body_carries_the_outage_facts(tmp_path: Path) -> None:
    """A needs-you ring has to arrive with the reason attached — the class, the
    consequence, and the reset date the operator decides on."""
    _write_bit(tmp_path, f"[FAIL] curator — {_OUTAGE_DETAIL}\n")
    card = _by_id(tmp_path)["health:curator"]

    assert "quota-limited" in card.title
    assert "email intake is stopped" in card.title
    assert "resets Aug 20" in card.title
    assert card.evidence["status"] == "fail"
    assert card.evidence["tool"] == "curator"


def test_unknown_status_is_carded_but_not_escalated(tmp_path: Path) -> None:
    """Unknown statuses fail OPEN into a card (the health denylist doctrine) —
    but they do NOT ring. Failing open is about not MISSING news; escalation is
    a judgement only a real FAIL has earned."""
    _write_bit(tmp_path, "[WEIRD] curator — from the future\n")
    by_id = _by_id(tmp_path)

    assert "health:curator" in by_id, "unknown status must still surface (fail-open)"
    assert by_id["health:curator"].attention == ATTENTION_FYI


def test_recovery_retires_the_escalated_card(tmp_path: Path) -> None:
    """When the outage clears, the tool goes OK and its card goes ABSENT.

    Absence is what reconcile reads to retire the card, so this is the
    downgrade path end-to-end: the same tool, the next morning's BIT.
    """
    _write_bit(tmp_path, f"[FAIL] curator — {_OUTAGE_DETAIL}\n")
    assert _by_id(tmp_path)["health:curator"].attention == ATTENTION_NEEDS_YOU

    _write_bit(tmp_path, "[OK] curator  (120 ms)\n", date_str="2026-08-21")
    assert health_feed_items(tmp_path, instance="salem") == []


# ---------------------------------------------------------------------------
# THE DESIGN GUARD — promoting attention must not strand the card
# ---------------------------------------------------------------------------


def test_escalated_card_keeps_fyi_mode(tmp_path: Path) -> None:
    """Attention is promoted; MODE is deliberately not.

    ``isNeedsYouItem`` (web) is ``attention === 'needs_you' || mode ===
    'decide'``, so attention ALONE reaches the needs-you column and the push
    doorbell. Promoting mode as well would look equivalent and would break the
    card — see the two pins below for why.
    """
    _write_bit(tmp_path, f"[FAIL] curator — {_OUTAGE_DETAIL}\n")
    assert _by_id(tmp_path)["health:curator"].mode == MODE_FYI


def test_health_advertises_no_verbs_so_ack_is_the_only_exit() -> None:
    """The premise of the pin below, asserted rather than assumed.

    ``health`` has no ``FEED_ACTIONS`` entry, so it advertises nothing — the
    universal FYI ack is the card's ONLY dismiss path. That is precisely why
    the mode must stay FYI: the universal ack is gated on mode, not on the
    ceiling. If a future commit gives ``health`` real verbs, this pin fails and
    its author gets to re-decide the mode question deliberately.
    """
    assert "health" not in FEED_ACTIONS
    assert actions_for("health") == []


def test_escalated_card_is_still_ackable_through_the_router(tmp_path: Path) -> None:
    """THE write-live/read-dead pin, driven through the production act path.

    A needs-you card that cannot be cleared rings every morning forever — the
    2026-08-03 undismissable-health-card incident, with a doorbell. This drives
    the real ``act`` entry point against a REAL producer-built escalated card
    (not a hand-made FeedItem), so it fails if the promotion ever reaches mode.
    """
    from alfred.daily_sync.action_router import act
    from alfred.daily_sync.config import DailySyncConfig

    _write_bit(tmp_path, f"[FAIL] curator — {_OUTAGE_DETAIL}\n")
    card = _by_id(tmp_path)["health:curator"]
    assert card.attention == ATTENTION_NEEDS_YOU, "premise: the card IS escalated"

    store = FeedStore(str(tmp_path / "feed.jsonl"))
    store.upsert(card)

    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "ds_state.json")

    result = act(
        card.id,
        "ack",
        feed_store=store,
        config=cfg,
        vault_path=None,
        instance_name="salem",
        instance_scope="talker",
    )

    assert result.ok is True, f"escalated health card is unclearable: {result.status}"
    assert result.status == STATUS_ACKED
    assert store.load()[card.id].state == STATE_ACKED


def test_health_stays_an_episode_kind_so_a_live_outage_re_surfaces() -> None:
    """An acked outage card must come BACK while the outage is still running.

    ``health`` is absent from ``SNAPSHOT_FINGERPRINT_FIELDS``, so
    ``_revival_suppressed`` declines to suppress and reconcile revives it on
    the next fire. That is the correct grain for this feature and the reason
    the ack above is not a permanent silence: acking says "seen", and tomorrow
    the card returns if — and only if — the tool is still failing.
    """
    assert "health" not in SNAPSHOT_FINGERPRINT_FIELDS

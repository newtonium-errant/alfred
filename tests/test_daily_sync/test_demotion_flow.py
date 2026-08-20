"""#72 item 1 — the demotion proposal through its REAL surfaces.

Separate from tests/test_attribution_demotion.py on purpose. That file pins the
trigger, the queue and the override store in isolation, and every one of those
pins stays green if the section never registers, the daemon never persists the
batch, or the reply dispatcher never routes the verb. A propose-then-approve
feature is made of four parts that only fail TOGETHER at the seams:

    corpus → trigger → numbered section item → reply verb → persisted override

so these drive the section provider and ``handle_daily_sync_reply`` for real.

The one thing an isolated pin structurally cannot see: the approve path writes
the override AND marks the queue, in that order, and the operator's next feed is
built from the override. A test that stubbed the override store would pass
against a build that never wrote one.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.daily_sync import assembler, demotion_section
from alfred.daily_sync.attribution_corpus import (
    AttributionCorpusEntry,
    append_entry,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.demotion_proposals import (
    STATE_ACCEPTED,
    STATE_PENDING,
    STATE_REJECTED,
    TRIGGER_EVENT,
    iter_proposals,
    list_pending,
)
from alfred.daily_sync.feed_producer import build_feed_items
from alfred.daily_sync.reply_dispatch import handle_daily_sync_reply
from alfred.daily_sync.tier_override import load_overrides
from alfred.feed.model import (
    ATTENTION_NEEDS_YOU,
    KIND_DEFAULTS,
    MODE_DECIDE,
)

# DERIVED FROM WALL CLOCK, never a literal — every contest row in this file
# hangs off NOW, and the window that judges those rows is evaluated against
# REAL time, not a fixture instant: `demotion_proposals_section` →
# `build_batch` → `run_trigger(config)` with NO `now=` (demotion_section.py)
# → `attribution_quality_stats(..., now=None)` → `when = now or
# datetime.now(timezone.utc)` (attribution_quality.py) — the same unthreaded
# funnel as the attribution quality-line provider. The first draft froze NOW
# at 2026-08-10T12:00Z with rows 1–3 days back, a dated suite regression set
# to red at 2026-08-22T12:00Z, when the 2026-08-08 row's exit from the
# 14-day wall-clock window would drop the contest count below threshold 2
# and 12 of these tests would fail over CORRECT production behavior.
# Derived, the offsets keep their meaning forever: days 1–3 sit inside the
# 14-day window, days 30–31 outside. Windows in wall-clock-predicate tests
# are derived, never literal (gate rule, 2026-08-19).
#
# A function rather than a module constant ON PURPOSE: the derivation must
# read the SAME clock the predicate reads, at the same altitude — call time.
# A module-level `NOW = datetime.now(...)` binds at import, and any clock
# instrumentation that starts after import (freezegun census drives — see
# the lane-clock-bombs commit body) splits the two reads apart again.
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _config(tmp_path: Path, *, threshold: int = 2, window: int = 14) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.attribution.corpus_path = str(tmp_path / "attr_corpus.jsonl")
    cfg.attribution.quality_window_days = window
    cfg.attribution.demotion_threshold = threshold
    return cfg


def _contest(cfg: DailySyncConfig, *, marker: str, when: datetime, via: str) -> None:
    """Append one contest row against an entry confirmed via ``via``."""
    append_entry(cfg.attribution.corpus_path, AttributionCorpusEntry(
        type="attribution_contest",
        marker_id=marker,
        record_path=f"note/{marker}.md",
        agent="salem",
        section_title="Structured Summary",
        marker_date=when.isoformat(),
        andrew_action="contest",
        action_at=when.isoformat(),
        confirmed_via=via,
    ))


def _seed_batch(cfg: DailySyncConfig, demotion_items: list[dict]) -> None:
    save_state(cfg.state.path, {"last_batch": {
        "date": "2026-08-10",
        "message_ids": [100],
        "demotion_items": demotion_items,
    }})


def _events(captured: list[dict], event: str) -> list[dict]:
    return [c for c in captured if c.get("event") == event]


# ---------------------------------------------------------------------------
# The section provider — trigger + render, through the real entry point.
# ---------------------------------------------------------------------------


def test_the_section_renders_nothing_and_logs_why_on_a_healthy_instance(
    tmp_path: Path,
) -> None:
    """The steady state. ILB lives in the trigger event, not in a daily
    "nothing pending" line the operator would learn to skip past."""
    cfg = _config(tmp_path)
    _contest(cfg, marker="m1", when=_now() - timedelta(days=1), via="timeout_24h")

    with structlog.testing.capture_logs() as captured:
        body = demotion_section.demotion_proposals_section(cfg, date(2026, 8, 10))

    assert body is None
    assert len(_events(captured, TRIGGER_EVENT)) == 1
    assert _events(captured, TRIGGER_EVENT)[0]["proposed"] is False


def test_raising_the_card_moves_no_tier_and_touches_nothing_else(
    tmp_path: Path,
) -> None:
    """NEVER A SILENT FLIP — asserted where the operator would notice.

    An earlier version of this pin asked "did the override file I named in the
    fixture appear". It passed against a build whose trigger DID write an
    override, because the write went to a path the fixture had not named. The
    question a pin has to ask is not "did my file appear" but "did the tier
    move", and then separately "did anything at all get written out there".

    So: the card is raised, and then a feed is built through the same resolved
    path production reads — it must still come out at the code default. Plus a
    debris check over the whole tmp tree, which catches a write to any path the
    fixture did not anticipate.
    """
    cfg = _config(tmp_path, threshold=2)
    for i in range(3):
        _contest(cfg, marker=f"m{i}", when=_now() - timedelta(days=i + 1),
                 via="timeout_24h")
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    body = demotion_section.demotion_proposals_section(cfg, date(2026, 8, 10))
    assert body is not None, "the card must actually have been raised"

    # 1. The tier did not move, read through the production path.
    overrides = load_overrides(cfg.attribution.resolved_tier_override_path())
    assert overrides.tier_for("attribution") is None
    feed = build_feed_items(
        "attribution", [{"record_path": "note/A.md", "marker_id": "x"}],
        "salem", tier_overrides=overrides,
    )
    assert (feed[0].mode, feed[0].attention) == KIND_DEFAULTS["attribution"]

    # 2. The ONLY thing that appeared is the proposal queue.
    #
    # WHAT THIS DOES NOT COVER, stated so the next reader does not inherit the
    # assumption that it does. There is a THIRD evasion shape: a write OUTSIDE
    # tmp_path, to a hardcoded path (e.g. a literal "data/feed_tier_overrides
    # .json" relative to the run's cwd). Both assertions above are blind to it —
    # the first reads the configured path, the second only sees tmp_path.
    #
    # It is covered, but by the SUITE DEBRIS GUARD, not by this pin: that write
    # materialises a file inside the repo tree and the guard fails the run
    # (verified — exit 1 mutated vs exit 0 clean, same selection). The coverage
    # is real and it lives somewhere else.
    #
    # Deliberately NOT closed here, per the #72 gate ruling. A pin can only
    # catch a hardcoded path by naming that path in advance, which means
    # guessing which wrong path a future bug picks — and a pin built on a
    # correct guess reads afterwards like proof that the whole class is
    # covered. That illusion is worse than the gap: the debris guard catches
    # the class without guessing any member of it.
    #
    # RE-LOOK WHEN #74 LANDS. What makes this shape dangerous TODAY is that on
    # a default config the hardcoded literal and the resolved path COINCIDE
    # (corpus_path defaults cwd-relative under ./data, and both #72 paths derive
    # from its parent) — so such a write is a genuine silent flip, not just
    # debris. Once #74 anchors those literals to a per-instance data dir the
    # two stop coinciding, and the same bug degrades to a write nothing reads.
    # The residual changes shape at that point and this comment should be
    # re-read rather than trusted.
    created = {p for p in tmp_path.rglob("*") if p.is_file()} - before
    assert created == {Path(cfg.attribution.resolved_demotion_queue_path())}, (
        f"the trigger wrote something besides the proposal queue: {created}"
    )


def test_enough_contests_produce_a_numbered_card_naming_the_evidence(
    tmp_path: Path,
) -> None:
    """The card must carry the count and the span. The operator is being asked
    to change a tier he ruled on; "quality is low" would make approving it an
    act of faith."""
    cfg = _config(tmp_path, threshold=2)
    for i in range(3):
        _contest(cfg, marker=f"m{i}", when=_now() - timedelta(days=i + 1),
                 via="timeout_24h")

    body = demotion_section.demotion_proposals_section(
        cfg, date(2026, 8, 10), start_index=7,
    )

    assert body is not None
    assert "7." in body
    assert "3 times" in body
    assert "14 days" in body
    assert "`N confirm`" in body and "`N reject`" in body
    items = demotion_section.consume_last_batch()
    assert len(items) == 1
    assert items[0].item_number == 7
    assert items[0].demotion_contests == 3


def test_only_timeout_confirms_count_toward_the_card(tmp_path: Path) -> None:
    """Straight through the section, not through the stats helper.

    A contest the operator caught inside the FYI window is the current design
    WORKING; counting it would demote the tier for succeeding. Three such
    contests must not raise a card that a single timeout one would.
    """
    cfg = _config(tmp_path, threshold=2)
    for i, via in enumerate(("operator", "backfill", "")):
        _contest(cfg, marker=f"m{i}", when=_now() - timedelta(days=i + 1), via=via)

    assert demotion_section.demotion_proposals_section(cfg, date(2026, 8, 10)) is None
    assert demotion_section.consume_last_batch() == []


def test_contests_outside_the_window_do_not_raise_a_card(tmp_path: Path) -> None:
    cfg = _config(tmp_path, threshold=2, window=14)
    for i in range(4):
        _contest(cfg, marker=f"old{i}", when=_now() - timedelta(days=30 + i),
                 via="timeout_24h")
    assert demotion_section.demotion_proposals_section(cfg, date(2026, 8, 10)) is None


def test_no_corpus_configured_says_so_rather_than_failing_quiet(
    tmp_path: Path,
) -> None:
    """An instance that quietly never proposes looks identical to one with a
    clean record."""
    cfg = _config(tmp_path)
    cfg.attribution.corpus_path = ""
    with structlog.testing.capture_logs() as captured:
        assert demotion_section.demotion_proposals_section(cfg, date(2026, 8, 10)) is None
    assert len(_events(captured, "daily_sync.attribution.demotion_no_corpus")) == 1


def test_the_section_registers_at_24_directly_above_attribution() -> None:
    """The recorded priority decision, pinned against the NEIGHBOUR rather than
    against the literal 24 — the reason is adjacency to the evidence, so if
    attribution ever moves, this must move with it."""
    from alfred.daily_sync import attribution_section

    demotion_section.register()
    attribution_section.register()
    by_name = {e.name: e.priority for e in assembler._REGISTRY}
    assert "demotion_proposals" in by_name
    assert by_name["demotion_proposals"] == by_name["attribution_audit"] - 1
    # And it actually renders ahead of it, which is the thing the number is for.
    order = assembler.registered_providers()
    assert order.index("demotion_proposals") < order.index("attribution_audit")


# ---------------------------------------------------------------------------
# The reply — confirm.
# ---------------------------------------------------------------------------


def _pending_item(cfg: DailySyncConfig, num: int = 7) -> dict[str, Any]:
    """Raise a real proposal through the section, return its numbered item."""
    for i in range(3):
        _contest(cfg, marker=f"m{i}", when=_now() - timedelta(days=i + 1),
                 via="timeout_24h")
    demotion_section.demotion_proposals_section(cfg, date(2026, 8, 10), start_index=num)
    items = demotion_section.consume_last_batch()
    assert len(items) == 1
    return items[0].to_dict()


def test_confirm_writes_the_override_and_the_next_feed_is_built_from_it(
    tmp_path: Path,
) -> None:
    """The whole point, measured at the far end.

    Not "an override file appeared" — the pin is that a card built AFTER the
    approval comes out under needs-you, which is the only thing the operator
    would notice.
    """
    cfg = _config(tmp_path)
    item = _pending_item(cfg)
    _seed_batch(cfg, [item])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 confirm")

    assert result is not None
    assert result["demotion_count"] == 1
    overrides = load_overrides(cfg.attribution.resolved_tier_override_path())
    assert overrides.tier_for("attribution") == (MODE_DECIDE, ATTENTION_NEEDS_YOU)

    feed = build_feed_items(
        "attribution", [{"record_path": "note/A.md", "marker_id": "x"}],
        "salem", tier_overrides=overrides,
    )
    assert (feed[0].mode, feed[0].attention) == (MODE_DECIDE, ATTENTION_NEEDS_YOU)
    assert KIND_DEFAULTS["attribution"] != (MODE_DECIDE, ATTENTION_NEEDS_YOU), (
        "the code default must be untouched — otherwise this pin proves nothing"
    )


def test_confirm_marks_the_queue_row_accepted(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    item = _pending_item(cfg)
    _seed_batch(cfg, [item])
    handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 confirm")
    rows = iter_proposals(cfg.attribution.resolved_demotion_queue_path())
    assert [r.state for r in rows] == [STATE_ACCEPTED]
    assert rows[0].resolved_at


def test_the_confirmation_message_names_the_undo(tmp_path: Path) -> None:
    """The operator has just changed a standing tier with two words. The
    sentence telling him it worked is the only natural place to tell him how to
    put it back — a reversibility that lives only in a docstring is one he does
    not have."""
    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])
    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 confirm")
    assert "alfred tier-override clear attribution" in result["message"]


def test_after_approval_the_trigger_stops_re_asking(tmp_path: Path) -> None:
    """The contests are still inside the window after approval, so without the
    already-overridden suppression the same card returns tomorrow."""
    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])
    handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 confirm")

    assert demotion_section.demotion_proposals_section(cfg, date(2026, 8, 11)) is None
    assert list_pending(cfg.attribution.resolved_demotion_queue_path()) == []


# ---------------------------------------------------------------------------
# The reply — reject.
# ---------------------------------------------------------------------------


def test_reject_leaves_the_tier_alone_and_starts_the_cooldown(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 reject")

    assert result["demotion_count"] == 1
    assert load_overrides(
        cfg.attribution.resolved_tier_override_path(),
    ).tier_for("attribution") is None
    rows = iter_proposals(cfg.attribution.resolved_demotion_queue_path())
    assert [r.state for r in rows] == [STATE_REJECTED]
    assert rows[0].resolved_at, (
        "the rejection timestamp IS the cooldown clock — without it the same "
        "card returns tomorrow off the same evidence"
    )


def test_after_a_rejection_the_next_sync_does_not_re_ask(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])
    handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 reject")

    with structlog.testing.capture_logs() as captured:
        assert demotion_section.demotion_proposals_section(cfg, date(2026, 8, 11)) is None
    assert _events(captured, TRIGGER_EVENT)[0]["reason"] == "cooldown"


def test_the_reject_message_says_how_long_it_will_stay_quiet(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])
    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 reject")
    assert "14 days" in result["message"]


# ---------------------------------------------------------------------------
# The verbs it refuses.
# ---------------------------------------------------------------------------


def test_a_bare_ack_does_not_approve_a_tier_change(tmp_path: Path) -> None:
    """The load-bearing refusal.

    An "ok" acknowledging a batch of email items must never be read as
    approving a standing change to a feed tier. The whole reason this goes
    through propose-then-approve is that the operator says yes TO THIS
    QUESTION, by its number.
    """
    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])

    with structlog.testing.capture_logs() as captured:
        result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="✅")

    assert result["demotion_count"] == 0
    assert load_overrides(
        cfg.attribution.resolved_tier_override_path(),
    ).tier_for("attribution") is None
    assert list_pending(cfg.attribution.resolved_demotion_queue_path()), (
        "an unanswered proposal must stay pending, not be silently consumed"
    )
    assert len(_events(captured, "daily_sync.demotion.all_ok_skipped")) == 1


def test_a_tier_verb_on_a_demotion_item_is_refused_not_guessed(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])
    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="7 high")
    assert result["demotion_count"] == 0
    assert load_overrides(
        cfg.attribution.resolved_tier_override_path(),
    ).tier_for("attribution") is None


def test_the_calibration_hint_advertises_confirm_reject_for_a_demotion_batch(
    tmp_path: Path,
) -> None:
    """The near-miss nudge must name a verb that applies to THIS batch."""
    from alfred.daily_sync.reply_dispatch import (
        _batch_type_flags,
        _compose_calibration_hint,
    )

    cfg = _config(tmp_path)
    _seed_batch(cfg, [_pending_item(cfg)])
    flags = _batch_type_flags(cfg)
    assert flags["has_demotion"] is True
    assert "N confirm" in _compose_calibration_hint(**flags)

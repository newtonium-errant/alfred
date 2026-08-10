"""#72 items 1/2/3/5 — the demotion proposal, the tier override, the section line.

What these pin, and the failure each one is standing in front of:

1. NEVER A SILENT FLIP. The trigger's only power is to raise a question. If a
   counter crossing a threshold could move the tier by itself, the operator's
   ruling would be something the machine re-rules on, and the whole
   propose-then-approve channel would be decoration.

2. THE THREE SUPPRESSIONS. Already-overridden, one-at-a-time, and the
   post-rejection cooldown. Each one is a different way the operator gets asked
   the same question twice, and the cost of that is not a duplicate card — it is
   that he learns to dismiss the card unread.

3. THE OVERRIDE REACHES THE PRODUCER. A persisted decision nothing consults is
   the accepted-then-ignored failure. So the pin goes through
   ``build_feed_items``, not through the store module's own accessor.

4. THE COOLDOWN IS ONE WINDOW, NOT A FIXED NUMBER. Pinned against the window
   rather than against 14 days, because the arithmetic — not the number — is
   the reason: a shorter cooldown re-proposes off evidence still sitting inside
   the trailing window.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.daily_sync.attribution_quality import (
    AttributionQuality,
    render_section_line,
)
from alfred.daily_sync.config import AttributionConfig
from alfred.daily_sync.demotion_proposals import (
    REASON_ALREADY_OVERRIDDEN,
    REASON_BELOW_THRESHOLD,
    REASON_COOLDOWN,
    REASON_PENDING_EXISTS,
    REASON_PROPOSED,
    STATE_ACCEPTED,
    STATE_PENDING,
    STATE_REJECTED,
    TRIGGER_EVENT,
    DemotionProposal,
    append_proposal,
    cooldown_until,
    iter_proposals,
    list_pending,
    maybe_propose_demotion,
    resolve_proposal,
)
from alfred.daily_sync.feed_producer import build_feed_items
from alfred.daily_sync.tier_override import (
    TierOverride,
    clear_override,
    load_overrides,
    set_override,
)
from alfred.feed.model import (
    ATTENTION_FYI,
    ATTENTION_NEEDS_YOU,
    KIND_DEFAULTS,
    MODE_DECIDE,
    MODE_FYI,
)

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = 14


def _queue(tmp_path: Path) -> Path:
    return tmp_path / "demotion_queue.jsonl"


def _propose(queue: Path, **kw: Any):
    base = dict(
        demotion_contests=3, window_days=WINDOW, threshold=2,
        override_in_force=False, now=NOW,
    )
    base.update(kw)
    return maybe_propose_demotion(queue, **base)


def _events(captured: list[dict], event: str) -> list[dict]:
    return [c for c in captured if c.get("event") == event]


# ---------------------------------------------------------------------------
# 1. The trigger raises a QUESTION, and only one.
# ---------------------------------------------------------------------------


def test_crossing_the_threshold_raises_a_pending_proposal_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The evidence crosses the bar → ONE pending row. No tier moves.

    The second assertion is the load-bearing one: the override file is the only
    thing that changes a tier, and the trigger must not have written it.
    """
    queue = _queue(tmp_path)
    override_path = tmp_path / "overrides.json"

    proposal = _propose(queue, demotion_contests=3, threshold=2)

    assert proposal is not None
    rows = list_pending(queue)
    assert len(rows) == 1
    assert rows[0].state == STATE_PENDING
    assert rows[0].demotion_contests == 3
    assert not override_path.exists(), "the trigger moved a tier without being asked"
    assert load_overrides(override_path).tier_for("attribution") is None


def test_below_the_threshold_proposes_nothing_and_says_so(tmp_path: Path) -> None:
    """ILB — the quiet path emits the trigger event with the reason.

    This section renders nothing on a healthy day, so this log line is the ONLY
    thing distinguishing "evaluated, no case to answer" from "the trigger
    stopped running".
    """
    queue = _queue(tmp_path)
    with structlog.testing.capture_logs() as captured:
        assert _propose(queue, demotion_contests=1, threshold=2) is None

    assert list_pending(queue) == []
    matches = _events(captured, TRIGGER_EVENT)
    assert len(matches) == 1
    assert matches[0]["proposed"] is False
    assert matches[0]["reason"] == REASON_BELOW_THRESHOLD
    assert matches[0]["demotion_contests"] == 1
    assert matches[0]["threshold"] == 2


def test_exactly_at_the_threshold_proposes(tmp_path: Path) -> None:
    """``>=``, not ``>`` — a threshold of 2 fires on the second contest."""
    queue = _queue(tmp_path)
    with structlog.testing.capture_logs() as captured:
        assert _propose(queue, demotion_contests=2, threshold=2) is not None
    assert _events(captured, TRIGGER_EVENT)[0]["reason"] == REASON_PROPOSED


def test_a_pending_proposal_suppresses_a_second_one(tmp_path: Path) -> None:
    """One at a time. Two cards asking the same question are two chances to
    answer it differently."""
    queue = _queue(tmp_path)
    first = _propose(queue)
    assert first is not None

    with structlog.testing.capture_logs() as captured:
        assert _propose(queue, now=NOW + timedelta(days=1)) is None

    assert len(list_pending(queue)) == 1
    matches = _events(captured, TRIGGER_EVENT)
    assert matches[0]["reason"] == REASON_PENDING_EXISTS
    assert matches[0]["proposal_id"] == first.proposal_id


def test_an_override_already_in_force_suppresses(tmp_path: Path) -> None:
    """Without this the card re-raises every day after approval — approving does
    not remove the contests from the trailing window."""
    queue = _queue(tmp_path)
    with structlog.testing.capture_logs() as captured:
        assert _propose(queue, override_in_force=True) is None
    assert list_pending(queue) == []
    assert _events(captured, TRIGGER_EVENT)[0]["reason"] == REASON_ALREADY_OVERRIDDEN


# ---------------------------------------------------------------------------
# 2. The cooldown — one full window from the rejection.
# ---------------------------------------------------------------------------


def test_a_rejection_starts_a_cooldown_of_exactly_one_window(tmp_path: Path) -> None:
    """Pinned against ``window_days``, not against 14.

    The number is not the rule. The rule is that a rejected proposal leaves its
    evidence inside the trailing window, so anything shorter re-asks off
    evidence the operator has just declined to act on. Asserting a literal 14
    here would stay green if someone re-derived the cooldown from a constant.
    """
    queue = _queue(tmp_path)
    proposal = _propose(queue)
    assert proposal is not None
    rejected_at = NOW + timedelta(hours=1)
    assert resolve_proposal(
        queue, proposal.proposal_id, STATE_REJECTED,
        resolved_at=rejected_at.isoformat(),
    )

    for window in (7, 14, 30):
        assert cooldown_until(queue, "attribution", window) == (
            rejected_at + timedelta(days=window)
        )


def test_inside_the_cooldown_it_does_not_re_ask(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    proposal = _propose(queue)
    assert proposal is not None
    rejected_at = NOW + timedelta(hours=1)
    resolve_proposal(
        queue, proposal.proposal_id, STATE_REJECTED,
        resolved_at=rejected_at.isoformat(),
    )

    # One day short of the window — the evidence has not turned over yet.
    with structlog.testing.capture_logs() as captured:
        assert _propose(
            queue, now=rejected_at + timedelta(days=WINDOW - 1),
        ) is None
    assert list_pending(queue) == []
    matches = _events(captured, TRIGGER_EVENT)
    assert matches[0]["reason"] == REASON_COOLDOWN
    assert matches[0]["cooldown_until"] == (
        rejected_at + timedelta(days=WINDOW)
    ).isoformat()


def test_past_the_cooldown_it_asks_again(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    proposal = _propose(queue)
    assert proposal is not None
    rejected_at = NOW + timedelta(hours=1)
    resolve_proposal(
        queue, proposal.proposal_id, STATE_REJECTED,
        resolved_at=rejected_at.isoformat(),
    )
    later = rejected_at + timedelta(days=WINDOW, seconds=1)
    assert _propose(queue, now=later) is not None
    assert len(list_pending(queue)) == 1


def test_a_second_rejection_restarts_the_clock(tmp_path: Path) -> None:
    """Latest rejection, not first — otherwise the second no is served by the
    first one's remaining time and the third card arrives early."""
    queue = _queue(tmp_path)
    first_reject = NOW
    second_reject = NOW + timedelta(days=WINDOW + 5)
    for when in (first_reject, second_reject):
        append_proposal(queue, DemotionProposal(
            proposal_id=f"attribution-demotion-{when:%Y%m%d%H%M%S}",
            ts=when.isoformat(), state=STATE_REJECTED, kind="attribution",
            resolved_at=when.isoformat(),
        ))
    assert cooldown_until(queue, "attribution", WINDOW) == (
        second_reject + timedelta(days=WINDOW)
    )


def test_an_accepted_proposal_does_not_start_a_cooldown(tmp_path: Path) -> None:
    """Acceptance is suppressed by the override instead — a different mechanism
    with a different lifetime (it lasts until cleared, not for one window)."""
    queue = _queue(tmp_path)
    append_proposal(queue, DemotionProposal(
        proposal_id="p1", ts=NOW.isoformat(), state=STATE_ACCEPTED,
        kind="attribution", resolved_at=NOW.isoformat(),
    ))
    assert cooldown_until(queue, "attribution", WINDOW) is None


def test_a_rejection_with_an_unreadable_timestamp_does_not_block_forever(
    tmp_path: Path,
) -> None:
    """The safe direction is to let the question be re-asked — the operator can
    decline again — rather than silencing it permanently on a bad timestamp."""
    queue = _queue(tmp_path)
    append_proposal(queue, DemotionProposal(
        proposal_id="p1", ts=NOW.isoformat(), state=STATE_REJECTED,
        kind="attribution", resolved_at="not-a-timestamp",
    ))
    assert cooldown_until(queue, "attribution", WINDOW) is None
    assert _propose(queue) is not None


# ---------------------------------------------------------------------------
# 3. The queue itself — schema tolerance both directions.
# ---------------------------------------------------------------------------


def test_a_row_from_a_newer_build_loads_without_its_extra_field(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    queue.write_text(json.dumps({
        "proposal_id": "p1", "ts": NOW.isoformat(), "state": STATE_PENDING,
        "kind": "attribution", "demotion_contests": 4,
        "something_from_the_future": {"nested": True},
    }) + "\n", encoding="utf-8")
    rows = iter_proposals(queue)
    assert len(rows) == 1
    assert rows[0].demotion_contests == 4


def test_corrupt_and_unusable_rows_are_skipped_not_raised(tmp_path: Path) -> None:
    """A queue that dies on one bad line stops being read on the day something
    went wrong enough to write one."""
    queue = _queue(tmp_path)
    queue.write_text(
        "{not json\n"
        + json.dumps(["not", "an", "object"]) + "\n"
        + json.dumps({"ts": NOW.isoformat(), "state": STATE_PENDING}) + "\n"  # no id
        + json.dumps({"proposal_id": "p2", "state": "nonsense"}) + "\n"
        + json.dumps({
            "proposal_id": "good", "ts": NOW.isoformat(),
            "state": STATE_PENDING, "kind": "attribution",
        }) + "\n",
        encoding="utf-8",
    )
    rows = iter_proposals(queue)
    assert [r.proposal_id for r in rows] == ["good"]


def test_resolve_is_order_preserving_and_stamps_the_time(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    for i in range(3):
        append_proposal(queue, DemotionProposal(
            proposal_id=f"p{i}", ts=NOW.isoformat(), state=STATE_PENDING,
            kind="attribution",
        ))
    when = (NOW + timedelta(hours=2)).isoformat()
    assert resolve_proposal(queue, "p1", STATE_ACCEPTED, resolved_at=when)
    rows = iter_proposals(queue)
    assert [r.proposal_id for r in rows] == ["p0", "p1", "p2"]
    assert rows[1].state == STATE_ACCEPTED
    assert rows[1].resolved_at == when


def test_resolving_an_unknown_proposal_returns_false(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    append_proposal(queue, DemotionProposal(
        proposal_id="p0", ts=NOW.isoformat(), state=STATE_PENDING,
        kind="attribution",
    ))
    assert resolve_proposal(
        queue, "nope", STATE_ACCEPTED, resolved_at=NOW.isoformat(),
    ) is False


# ---------------------------------------------------------------------------
# 4. The override reaches the PRODUCER.
# ---------------------------------------------------------------------------


def _attribution_item(marker: str = "inf-1") -> dict[str, Any]:
    return {"record_path": "note/A.md", "marker_id": marker}


def test_without_an_override_attribution_is_glance_the_code_default(
    tmp_path: Path,
) -> None:
    """The baseline the override is measured against, read from KIND_DEFAULTS
    rather than restated — if the code default moves, this moves with it."""
    items = build_feed_items(
        "attribution", [_attribution_item()], "salem",
        tier_overrides=load_overrides(tmp_path / "absent.json"),
    )
    assert (items[0].mode, items[0].attention) == KIND_DEFAULTS["attribution"]
    assert (items[0].mode, items[0].attention) == (MODE_FYI, ATTENTION_FYI)


def test_an_approved_override_puts_the_whole_kind_back_under_needs_you(
    tmp_path: Path,
) -> None:
    """Through ``build_feed_items``, not through ``tier_for``.

    A persisted decision the producer never consults is the accepted-then-
    ignored failure, and a pin on the store's own accessor cannot see it.
    """
    path = tmp_path / "overrides.json"
    set_override(path, TierOverride(
        kind="attribution", mode=MODE_DECIDE, attention=ATTENTION_NEEDS_YOU,
        approved_at=NOW.isoformat(), reason="3 wrong auto-confirms in 14 days",
        proposal_id="p1",
    ))
    items = build_feed_items(
        "attribution", [_attribution_item("a"), _attribution_item("b")],
        "salem", tier_overrides=load_overrides(path),
    )
    assert len(items) == 2
    for item in items:
        assert (item.mode, item.attention) == (MODE_DECIDE, ATTENTION_NEEDS_YOU)


def test_the_override_does_not_edit_kind_defaults(tmp_path: Path) -> None:
    """The dict stays the CODE default — a default an operator decision can
    rewrite is not a default."""
    path = tmp_path / "overrides.json"
    set_override(path, TierOverride(
        kind="attribution", mode=MODE_DECIDE, attention=ATTENTION_NEEDS_YOU,
    ))
    build_feed_items(
        "attribution", [_attribution_item()], "salem",
        tier_overrides=load_overrides(path),
    )
    assert KIND_DEFAULTS["attribution"] == (MODE_FYI, ATTENTION_FYI)


def test_a_contested_item_stays_needs_you_with_no_override(tmp_path: Path) -> None:
    """#63a's per-ITEM promotion is untouched by #72's per-KIND layer."""
    item = {**_attribution_item(), "contested": True}
    items = build_feed_items(
        "attribution", [item], "salem",
        tier_overrides=load_overrides(tmp_path / "absent.json"),
    )
    assert (items[0].mode, items[0].attention) == (MODE_DECIDE, ATTENTION_NEEDS_YOU)


def test_clearing_the_override_returns_the_kind_to_its_code_default(
    tmp_path: Path,
) -> None:
    """The escape hatch, end to end, measured where it matters."""
    path = tmp_path / "overrides.json"
    set_override(path, TierOverride(
        kind="attribution", mode=MODE_DECIDE, attention=ATTENTION_NEEDS_YOU,
    ))
    assert clear_override(path, "attribution") is True
    items = build_feed_items(
        "attribution", [_attribution_item()], "salem",
        tier_overrides=load_overrides(path),
    )
    assert (items[0].mode, items[0].attention) == KIND_DEFAULTS["attribution"]


def test_clearing_a_kind_with_no_override_reports_it_rather_than_lying(
    tmp_path: Path,
) -> None:
    path = tmp_path / "overrides.json"
    with structlog.testing.capture_logs() as captured:
        assert clear_override(path, "attribution") is False
    assert len(_events(captured, "daily_sync.tier_override.clear_noop")) == 1


def test_an_override_on_a_kind_with_no_per_item_hook_still_applies(
    tmp_path: Path,
) -> None:
    """Otherwise the escape hatch is a silent no-op on every kind but
    attribution — an operator sets one, sees no error, and gets no change."""
    path = tmp_path / "overrides.json"
    set_override(path, TierOverride(
        kind="radar", mode=MODE_FYI, attention=ATTENTION_FYI,
    ))
    items = build_feed_items(
        "radar", [{"record_path": "note/R.md"}], "salem",
        tier_overrides=load_overrides(path),
    )
    assert (items[0].mode, items[0].attention) == (MODE_FYI, ATTENTION_FYI)


# ---------------------------------------------------------------------------
# 5. The override store — fail-open, and loud about it.
# ---------------------------------------------------------------------------


def test_a_corrupt_override_file_falls_back_to_code_defaults_loudly(
    tmp_path: Path,
) -> None:
    """One bad byte in a tuning file must not be able to take the morning
    surface down — but it must not be silent either."""
    path = tmp_path / "overrides.json"
    path.write_text("{not json at all", encoding="utf-8")
    with structlog.testing.capture_logs() as captured:
        overrides = load_overrides(path)
    assert overrides.unreadable is True
    assert overrides.tier_for("attribution") is None
    assert len(_events(captured, "daily_sync.tier_override.unreadable")) == 1

    items = build_feed_items(
        "attribution", [_attribution_item()], "salem", tier_overrides=overrides,
    )
    assert (items[0].mode, items[0].attention) == KIND_DEFAULTS["attribution"]


def test_a_row_with_an_unrecognised_tier_is_declined_and_counted(
    tmp_path: Path,
) -> None:
    """Schema tolerance is about surviving an unknown FIELD, not about accepting
    a value the feed has no lane for — a hand-edit is where such a value comes
    from, and this file is hand-editable by design."""
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"overrides": {
        "attribution": {"mode": "decide", "attention": "extremely_urgent"},
        "radar": {"mode": "fyi", "attention": "fyi"},
    }}), encoding="utf-8")
    with structlog.testing.capture_logs() as captured:
        overrides = load_overrides(path)
    assert overrides.tier_for("attribution") is None
    assert overrides.tier_for("radar") == (MODE_FYI, ATTENTION_FYI)
    assert overrides.declined == 1
    assert len(_events(captured, "daily_sync.tier_override.row_declined")) == 1


def test_an_override_row_from_a_newer_build_loads_without_its_extra_field(
    tmp_path: Path,
) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"overrides": {"attribution": {
        "mode": "decide", "attention": "needs_you", "future_field": 1,
    }}}), encoding="utf-8")
    assert load_overrides(path).tier_for("attribution") == (
        MODE_DECIDE, ATTENTION_NEEDS_YOU,
    )


def test_the_load_event_fires_on_the_empty_steady_state(tmp_path: Path) -> None:
    """ILB. An override that stopped being applied and a file that stopped being
    read produce the same feed; this line is what separates them."""
    with structlog.testing.capture_logs() as captured:
        load_overrides(tmp_path / "absent.json")
    matches = _events(captured, "daily_sync.tier_override.loaded")
    assert len(matches) == 1
    assert matches[0]["count"] == 0
    assert matches[0]["declined"] == 0


# ---------------------------------------------------------------------------
# 6. Path derivation — the #74 tie-in.
# ---------------------------------------------------------------------------


def test_both_new_paths_derive_from_the_corpus_paths_parent() -> None:
    """No new ``./data`` literals. They inherit whatever correctness
    ``corpus_path`` has and follow it when #74 anchors it."""
    cfg = AttributionConfig(corpus_path="/srv/salem/data/attr_corpus.jsonl")
    assert Path(cfg.resolved_demotion_queue_path()).parent == Path("/srv/salem/data")
    assert Path(cfg.resolved_tier_override_path()).parent == Path("/srv/salem/data")


def test_an_explicit_config_value_always_wins() -> None:
    cfg = AttributionConfig(
        corpus_path="/srv/salem/data/attr_corpus.jsonl",
        demotion_queue_path="/elsewhere/q.jsonl",
        tier_override_path="/elsewhere/o.json",
    )
    assert cfg.resolved_demotion_queue_path() == "/elsewhere/q.jsonl"
    assert cfg.resolved_tier_override_path() == "/elsewhere/o.json"


def test_the_two_derived_paths_are_distinct_files() -> None:
    cfg = AttributionConfig()
    assert cfg.resolved_demotion_queue_path() != cfg.resolved_tier_override_path()


# ---------------------------------------------------------------------------
# 7. The surfacing line (item 5) — four states, four sentences.
# ---------------------------------------------------------------------------


def _stats(counts: dict[str, int] | None, *, live: bool) -> AttributionQuality:
    q = AttributionQuality(window_days=WINDOW, section_tap_live=live)
    for k, v in (counts or {}).items():
        q.contests_by_section[k] = v
    return q


def test_the_dark_tap_says_nothing_has_named_a_section(tmp_path: Path) -> None:
    """NOT "no section stands out", which would claim the sections were measured
    and found even. The rider's None state, carried through to the words."""
    line = render_section_line(_stats({"unknown": 5}, live=False))
    assert "no contest has named a section" in line
    assert "stands out" not in line


def test_too_few_contests_declines_to_single_one_out() -> None:
    """One contest is 100% of one contest."""
    line = render_section_line(_stats({"Topics": 2}, live=True))
    assert "too few" in line
    assert "Topics" not in line


def test_a_flat_spread_reports_that_as_the_finding() -> None:
    line = render_section_line(
        _stats({"Topics": 2, "Decisions": 2, "Action Items": 2}, live=True),
    )
    assert "no single section stands out" in line
    assert "6 contests across 3 sections" in line


def test_a_real_standout_is_named_with_its_raw_counts() -> None:
    """Raw counts alongside the percent so a small denominator is visible in the
    sentence that reports it."""
    line = render_section_line(
        _stats({"Decisions": 4, "Topics": 1, "unknown": 1}, live=True),
    )
    assert "Decisions accounts for 4 of 6 contests (67%)" in line


def test_unknown_never_becomes_the_standout_but_stays_in_the_denominator() -> None:
    """The conservative direction: when most contests named no section, no
    section CAN cross the share gate, and the honest answer is that the data
    does not support naming one."""
    stats = _stats({"unknown": 8, "Topics": 3}, live=True)
    assert stats.standout_section() is None
    line = render_section_line(stats)
    assert "unknown" not in line
    assert "11 contests" in line


def test_the_standout_reaches_the_logged_quality_event() -> None:
    """The log carries the same answer the rendered line does."""
    stats = _stats({"Decisions": 4, "Topics": 1}, live=True)
    assert stats.to_dict()["standout_section"] == "Decisions"
    assert _stats({"Topics": 2}, live=True).to_dict()["standout_section"] is None
    assert _stats({"unknown": 9}, live=False).to_dict()["standout_section"] is None


def test_the_section_line_is_never_empty() -> None:
    """Every state gets a sentence. An absent line is the one thing this cannot
    do — it is the state a reader most reliably misreads as healthy."""
    for stats in (
        _stats(None, live=False),
        _stats({}, live=True),
        _stats({"unknown": 1}, live=True),
        _stats({"Topics": 9}, live=True),
    ):
        assert render_section_line(stats).strip()

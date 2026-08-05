"""#44 slice 3 — the ops-brief campaign line.

D2: the ETA is the headline. The rest of these pins are ILB — a background
campaign is the easiest place in this system for silent death to hide, because
nobody waits on any individual increment.
"""

from __future__ import annotations

from alfred.drip.brief_line import (
    CampaignProgress,
    render_campaign_line,
    render_drip_section,
)
from alfred.drip.runner import (
    STOP_BUDGET_EXHAUSTED,
    STOP_CIRCUIT_BREAKER,
    STOP_QUOTA_BLOCKED,
    STOP_WORKLIST_EMPTY,
)


def _p(**kw) -> CampaignProgress:
    base = dict(
        name="gmail_backlog", total=907, remaining=612, per_run=12,
        last_run_at="2026-08-04T09:00:00+00:00",
        last_stop_reason=STOP_BUDGET_EXHAUSTED,
    )
    base.update(kw)
    return CampaignProgress(**base)


# --- D2: the ETA headline ---------------------------------------------------


def test_eta_is_the_headline_number() -> None:
    """"612 left, ~51d at 12/day" is what makes a multi-week wait an INFORMED
    acceptance rather than a hope."""
    line = render_campaign_line(_p())
    assert "612 left" in line
    assert "~51d at 12/day" in line, "ceil(612/12) = 51"


def test_eta_falls_as_the_campaign_drains() -> None:
    """And a frozen ETA is the stall signal — nothing else on the line shows
    a campaign that stopped working."""
    assert _p(remaining=612).eta_days == 51
    assert _p(remaining=300).eta_days == 25
    assert _p(remaining=1).eta_days == 1
    assert _p(remaining=0).eta_days == 0


def test_zero_budget_says_why_rather_than_printing_a_number() -> None:
    """A guessed ETA is worse than an absent one: the number's whole job is to
    ground a decision, and an ungrounded figure cannot."""
    assert _p(per_run=0).eta_days is None
    assert "no ETA (budget is 0)" in render_campaign_line(_p(per_run=0))


# --- ILB: the four kinds of quiet ------------------------------------------


def test_never_run_is_distinct_from_nothing_to_do() -> None:
    """Same silence, opposite meanings: a campaign enabled yesterday that has
    not fired is a scheduling problem; one that drained everything is done.
    Collapsing them would hide the former."""
    never = render_campaign_line(_p(last_run_at=""))
    assert "not yet run" in never

    complete = render_campaign_line(_p(remaining=0))
    assert "complete" in complete
    assert "not yet run" not in complete


def test_quota_blocked_is_loud_not_slow(  ) -> None:
    """Three days blocked and three days of small increments produce the SAME
    percentage. Only one is a problem, so the line must distinguish them."""
    line = render_campaign_line(_p(last_stop_reason=STOP_QUOTA_BLOCKED))
    assert "QUOTA-BLOCKED" in line
    assert "not draining" in line


def test_budget_exhausted_reads_as_benign() -> None:
    """The success path most days. A reader must not parse "exhausted" as a
    fault — the operator ASKED for the budget to stop it."""
    line = render_campaign_line(_p(last_stop_reason=STOP_BUDGET_EXHAUSTED))
    assert "on budget" in line
    assert "⚠" not in line


def test_idle_with_items_remaining_is_flagged() -> None:
    """Nothing eligible but items left = everything is awaiting confirmation or
    retired at max_attempts. Both are real states; neither is progress, and a
    bare "idle" would read as fine."""
    line = render_campaign_line(
        _p(last_stop_reason=STOP_WORKLIST_EMPTY, remaining=40),
    )
    assert "idle" in line
    assert "nothing eligible" in line


def test_circuit_breaker_is_surfaced() -> None:
    line = render_campaign_line(_p(last_stop_reason=STOP_CIRCUIT_BREAKER))
    assert "⚠" in line and "repeated failures" in line


# --- the numbers that would otherwise be invisible ---------------------------


def test_awaiting_is_shown_so_remaining_does_not_look_stuck() -> None:
    """Dispatched-but-unconfirmed is neither done nor failed. Hiding it makes
    `remaining` look frozen for no visible reason."""
    assert "12 awaiting" in render_campaign_line(_p(awaiting=12))


def test_failures_and_weekly_spend_surface() -> None:
    line = render_campaign_line(_p(failed=3, spent_week=48, week_cap=60))
    assert "3 failed" in line
    assert "week 48/60" in line


def test_a_line_is_always_produced() -> None:
    """Never an empty string — an absent line is exactly the silence this
    section exists to break."""
    for p in (
        _p(), _p(remaining=0), _p(last_run_at=""), _p(per_run=0),
        _p(total=0, remaining=0), _p(last_stop_reason=STOP_QUOTA_BLOCKED),
    ):
        assert render_campaign_line(p).strip()


# --- deploy-inert -----------------------------------------------------------


def test_section_is_omitted_entirely_when_no_campaigns_configured() -> None:
    """An instance that never enabled drip shows no trace of it — omit, not an
    empty header."""
    assert render_drip_section([]) is None


def test_section_renders_one_line_per_campaign() -> None:
    section = render_drip_section([
        _p(name="gmail_backlog"),
        _p(name="link001_repair", total=2075, remaining=2075, per_run=200),
    ])
    assert section is not None
    assert section.startswith("## Campaigns")
    assert "gmail_backlog" in section and "link001_repair" in section
    assert "~11d at 200/day" in section, "ceil(2075/200) = 11"

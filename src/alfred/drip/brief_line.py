"""The ops-brief campaign line (#44 slice 3).

D2 as ratified: **ETA-at-current-budget is the headline number.** Not percent,
not a raw count — "612 left, ~51 days at 12/day" is what turns the operator's
own "it's okay if this takes a few weeks" into an INFORMED acceptance instead
of a hope. It is also the stall detector: an ETA that stops falling is a
campaign that stopped working, and nothing else on the line shows that.

Intentionally-left-blank throughout. A background campaign is the easiest place
in this system for silent death to hide, because nobody waits on any individual
increment — so:

* an enabled campaign with nothing to do says so ("nothing left to drain");
* a campaign that has never run says THAT, distinctly (never-run and
  nothing-to-do are different facts and only one needs attention);
* a quota-blocked campaign is called out LOUDLY rather than reading as slow
  progress — three days blocked and three days of small increments produce the
  same percentage, and only one is a problem.

Renders nothing at all when the feature is disabled: deploy-inert, no empty
header on instances that never turned it on.
"""

from __future__ import annotations

from dataclasses import dataclass

from .runner import (
    STOP_BUDGET_EXHAUSTED,
    STOP_CIRCUIT_BREAKER,
    STOP_DISABLED,
    STOP_QUOTA_BLOCKED,
    STOP_WORKLIST_EMPTY,
)


@dataclass
class CampaignProgress:
    """What the brief needs from one campaign. Built from the persisted state
    plus its config, so the line reports the LAST RUN rather than triggering a
    new one — a brief render must never do work."""

    name: str
    total: int = 0
    remaining: int = 0
    done: int = 0
    failed: int = 0
    awaiting: int = 0
    per_run: int = 0
    spent_week: int = 0
    week_cap: int = 0
    last_run_at: str = ""
    last_stop_reason: str = ""

    @property
    def pct(self) -> float:
        if self.total <= 0:
            return 100.0
        return round(100.0 * (self.total - self.remaining) / self.total, 1)

    @property
    def eta_days(self) -> int | None:
        """D2's headline. ``None`` when it cannot be computed HONESTLY.

        A guessed ETA is worse than an absent one — the number's whole job is to
        make a multi-week wait an informed choice, and an ungrounded figure
        can't do that. So: no budget, or a campaign that has never run, yields
        None and the line says why instead of printing a number.
        """
        if self.remaining <= 0:
            return 0
        if self.per_run <= 0:
            return None
        return -(-self.remaining // self.per_run)  # ceil


def render_campaign_line(p: CampaignProgress) -> str:
    """One line per campaign. Always returns text — never an empty string."""
    # Never run: distinct from "nothing to do". A campaign enabled yesterday
    # that has not fired once is a scheduling problem; a campaign that has
    # drained everything is finished. Same silence, opposite meanings.
    if not p.last_run_at:
        return (
            f"**{p.name}:** configured, not yet run "
            f"({p.total} item{'s' if p.total != 1 else ''} staged)."
        )

    if p.last_stop_reason == STOP_DISABLED:
        return f"**{p.name}:** disabled — {p.remaining} left, not draining."

    if p.remaining <= 0:
        return f"**{p.name}:** ✓ complete — all {p.total} drained."

    parts: list[str] = [f"{p.remaining} left", f"{p.pct}%"]

    eta = p.eta_days
    if eta is None:
        # ILB: say WHY there's no ETA rather than omitting the headline.
        parts.append("no ETA (budget is 0)")
    else:
        parts.append(f"~{eta}d at {p.per_run}/day")

    if p.awaiting:
        # Dispatched-but-unconfirmed is neither done nor failed, and hiding it
        # would make `remaining` look stuck for no visible reason.
        parts.append(f"{p.awaiting} awaiting")
    if p.failed:
        parts.append(f"{p.failed} failed")
    if p.week_cap > 0:
        parts.append(f"week {p.spent_week}/{p.week_cap}")

    line = f"**{p.name}:** " + " · ".join(parts)

    # The loud cases go LAST so they are the final thing read on the line.
    if p.last_stop_reason == STOP_QUOTA_BLOCKED:
        line += " — ⚠ QUOTA-BLOCKED, not draining"
    elif p.last_stop_reason == STOP_CIRCUIT_BREAKER:
        line += " — ⚠ stopped early on repeated failures"
    elif p.last_stop_reason == STOP_WORKLIST_EMPTY and p.remaining > 0:
        # Nothing to pick up, yet items remain: everything left is either
        # awaiting confirmation or retired at max_attempts. Both are real
        # states; neither is progress, and "idle" would read as fine.
        line += " — idle (nothing eligible to drain)"
    elif p.last_stop_reason == STOP_BUDGET_EXHAUSTED:
        # The success path, most days. Explicitly benign so a reader does not
        # parse "exhausted" as a fault.
        line += " — on budget"
    return line


def render_drip_section(progresses: list[CampaignProgress]) -> str | None:
    """The whole section, or ``None`` when the feature is off.

    ``None`` (omit) vs an empty header is the deploy-inert distinction: an
    instance that never enabled drip should show no trace of it, while an
    instance that enabled it and has nothing to say still gets its lines.
    """
    if not progresses:
        return None
    lines = ["## Campaigns", ""]
    lines.extend(render_campaign_line(p) for p in progresses)
    return "\n".join(lines)


__all__ = ["CampaignProgress", "render_campaign_line", "render_drip_section"]

"""``alfred drip`` subcommand handlers (#44b).

Two commands, both operator-facing:

* ``run`` — drain one budgeted increment per enabled campaign. This is the
  ignition the #44 machinery was missing; it is what a scheduler invokes.
* ``status`` — render the same lines the ops brief shows, on demand, without
  running anything. Deliberately the SAME renderer as the brief so the two
  surfaces can never disagree about a campaign's progress.

Budgets come from config on every path. That is the D1 gap this slice closes,
and it is why no call here passes a literal: a number typed at a call site is a
number the operator cannot change.
"""

from __future__ import annotations

import uuid
from datetime import date

import structlog

from .brief_line import render_campaign_line
from .config import DripConfig
from .runner import run_increment
from .state import campaign_state_path, load_state
from .wiring import DripConfigError, build_campaign, build_progress

log = structlog.get_logger(__name__)


def _select(config: DripConfig, campaign_name: str | None) -> dict:
    """Campaigns to act on, or a loud error naming what IS configured."""
    enabled = config.enabled_campaigns()
    if campaign_name is None:
        return enabled
    if campaign_name not in config.campaigns:
        raise DripConfigError(
            f"unknown campaign {campaign_name!r} — configured: "
            f"{sorted(config.campaigns) or '(none)'}"
        )
    if campaign_name not in enabled:
        raise DripConfigError(
            f"campaign {campaign_name!r} is configured but not enabled — set "
            f"drip.campaigns.{campaign_name}.enabled: true"
        )
    return {campaign_name: config.campaigns[campaign_name]}


def cmd_run(
    config: DripConfig,
    *,
    campaign_name: str | None = None,
    apply: bool = True,
    today: date | None = None,
) -> int:
    """Drain one increment per selected campaign. Returns a process exit code.

    ``today`` is passed EXPLICITLY into the runner on every call. Its parameter
    is annotated ``date = None``, and omitting it raises ``AttributeError`` on
    the first item that records spend — verified against the runner, not
    assumed. A caller that lets it default works fine against an empty cursor
    and dies once the campaign actually costs something, which is the worst
    available time to find out.
    """
    day = today or date.today()
    selected = _select(config, campaign_name)

    if not selected:
        # ILB: an instance with drip off, or with every campaign disabled, is a
        # legitimate steady state — and it must not look like a crashed run.
        print("Drip: no enabled campaigns — ran, nothing to drain.")
        log.info(
            "drip.run.no_enabled_campaigns",
            configured=sorted(config.campaigns),
            detail="ran, nothing to drain — no campaign is enabled",
        )
        return 0

    exit_code = 0
    for name, ccfg in sorted(selected.items()):
        try:
            campaign = build_campaign(name, ccfg, config)
        except DripConfigError as exc:
            # Loud, and does not abort the other campaigns: one misconfigured
            # campaign should not stop a healthy one from draining.
            print(f"Drip: campaign {name!r} could not start — {exc}")
            log.warning("drip.run.campaign_unbuildable", campaign=name, error=str(exc))
            exit_code = 1
            continue

        state_path = campaign_state_path(config.data_dir, config.instance, name)
        state = load_state(state_path, name)

        result = run_increment(
            campaign,
            state,
            state_path=state_path,
            max_items_per_run=ccfg.max_items_per_run,
            max_items_per_week=ccfg.max_items_per_week,
            max_attempts=ccfg.max_attempts,
            max_failures_per_run=ccfg.max_failures_per_run,
            max_awaiting_runs=ccfg.max_awaiting_runs,
            today=day,
            run_id=uuid.uuid4().hex[:8],
            apply=apply,
        )

        mode = "" if apply else " [dry run]"
        print(
            f"Drip {name}{mode}: attempted {result.attempted}, done "
            f"{result.done}, failed {result.failed}, dispatched "
            f"{result.dispatched}, blocked {result.blocked} — "
            f"{result.remaining}/{result.total} left ({result.stop_reason})"
        )
        for err in result.errors[:5]:
            print(f"  ! {err}")

    return exit_code


def cmd_status(config: DripConfig, *, today: date | None = None) -> int:
    """Render every CONFIGURED campaign's line — including disabled ones.

    Disabled campaigns are shown deliberately. "Not in the output" and "not
    draining" are different facts, and a campaign someone turned off two weeks
    ago is exactly the thing an operator needs to see when they wonder why a
    backlog is not moving.
    """
    day = today or date.today()

    if not config.campaigns:
        print("Drip: no campaigns configured.")
        log.info(
            "drip.status.no_campaigns",
            detail="ran, nothing to report — no drip campaigns configured",
        )
        return 0

    for name, ccfg in sorted(config.campaigns.items()):
        try:
            campaign = build_campaign(name, ccfg, config)
        except DripConfigError as exc:
            print(f"**{name}:** ⚠ misconfigured — {exc}")
            log.warning("drip.status.campaign_unbuildable", campaign=name, error=str(exc))
            continue
        state_path = campaign_state_path(config.data_dir, config.instance, name)
        state = load_state(state_path, name)
        progress = build_progress(name, campaign, state, ccfg, today=day)
        print(render_campaign_line(progress))
    return 0


__all__ = ["cmd_run", "cmd_status"]

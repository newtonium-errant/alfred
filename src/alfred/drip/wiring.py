"""Ignition — config to live campaign, persisted state to brief progress (#44b).

The #44 machinery had zero importers: a runner nothing called, campaigns nothing
built, a brief line nothing rendered. This module is the missing middle. It is
deliberately the ONLY place that knows how a config block becomes a campaign
object, so the CLI and the ops brief cannot drift into two different answers
about what "the gmail_backlog campaign" means.

Nothing here does campaign work. :func:`build_progress` reads a directory
listing or a work-list file to size the campaign, which is what the brief needs
to report progress — it never calls ``work()``, never writes state, and never
triggers a run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import structlog

from .brief_line import CampaignProgress
from .campaigns import GmailBacklogCampaign, Link001Campaign
# DripConfigError moved to .config in #66 so `state` can raise it without an
# import cycle (wiring imports state). Re-exported here — and listed in
# __all__ — because `from .wiring import DripConfigError` is the established
# import across cli.py, brief/daemon.py and the drip tests.
from .config import CampaignConfig, DripConfig, DripConfigError
from .runner import STOP_DISABLED
from .state import DONE, IN_FLIGHT, CampaignState

log = structlog.get_logger(__name__)


def load_worklist_file(path: Path | str) -> list[str]:
    """Read a FROZEN work-list: one item id per line.

    Blank lines and ``#`` comments are ignored so an operator can annotate the
    file. Order is preserved — the runner drains FIFO, so the file's order is
    the drain order.

    A missing file is an error rather than an empty list. An empty work-list and
    an absent one look identical downstream ("nothing left to drain"), and for a
    campaign whose removal branch is irreversible, "I could not find your list"
    must never render as "you are finished".
    """
    p = Path(path)
    if not p.is_file():
        raise DripConfigError(f"work-list file not found: {p}")
    items: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            items.append(s)
    return items


def build_campaign(name: str, cfg: CampaignConfig, drip: DripConfig) -> Any:
    """Construct the live campaign object for ``name``.

    Every required path is checked here and reported with the config key the
    operator actually needs to fix. None of them have defaults: a default
    staging directory would be one instance's path silently applied to another.
    """
    kind = cfg.kind or name
    vault_path = drip.vault_path
    if not vault_path:
        raise DripConfigError(
            f"campaign {name!r}: vault.path is unset — every campaign needs a "
            "vault to act on"
        )

    if kind == "gmail_backlog":
        if not cfg.staging_dir:
            raise DripConfigError(
                f"campaign {name!r}: drip.campaigns.{name}.staging_dir is "
                "required (the cohort staged OUTSIDE the watched inbox)"
            )
        if not cfg.inbox_dir:
            raise DripConfigError(
                f"campaign {name!r}: drip.campaigns.{name}.inbox_dir is "
                "required (the curator-watched inbox)"
            )
        return GmailBacklogCampaign(
            staging_dir=Path(cfg.staging_dir),
            inbox_dir=Path(cfg.inbox_dir),
            vault_path=Path(vault_path),
            name=name,
        )

    if kind == "link001_repair":
        if not cfg.worklist_path:
            raise DripConfigError(
                f"campaign {name!r}: drip.campaigns.{name}.worklist_path is "
                "required (the FROZEN <path>::<target>::<branch> list — D4a "
                "forbids re-deriving the branch per run)"
            )
        return Link001Campaign(
            worklist_items=load_worklist_file(cfg.worklist_path),
            vault_path=Path(vault_path),
            name=name,
        )

    raise DripConfigError(
        f"campaign {name!r}: unknown kind {kind!r} — expected one of "
        "gmail_backlog, link001_repair"
    )


def build_progress(
    name: str,
    campaign: Any,
    state: CampaignState,
    cfg: CampaignConfig,
    *,
    today: date,
) -> CampaignProgress:
    """Summarize a campaign for the brief line, from persisted state.

    ``total`` is the UNION of the current work-list and everything state has
    ever tracked, matching ``CampaignState.remaining``'s own union. Sizing from
    the work-list alone would shrink the denominator for any campaign whose
    ``work()`` removes the item from its own list — ``gmail_backlog`` moves the
    file out of staging — so a campaign would appear to make no progress while
    its percentage silently re-based under it.
    """
    worklist = campaign.worklist()
    total = len(set(worklist) | set(state.items))
    counts = state.counts()

    week_cap = 0
    if cfg.max_items_per_week > 0 and bool(campaign.spends_quota()):
        # The weekly cap is a CREDIT guard and binds only spending campaigns
        # (the runner agrees — see its week_headroom branch). Reporting a cap
        # that does not bind would invite the operator to tune a number with no
        # effect.
        week_cap = cfg.max_items_per_week

    progress = CampaignProgress(
        name=name,
        total=total,
        remaining=state.remaining(worklist),
        done=counts.get(DONE, 0),
        failed=counts.get("failed", 0),
        awaiting=counts.get(IN_FLIGHT, 0),
        per_run=cfg.max_items_per_run,
        spent_week=state.spend_in_week_ending(today),
        week_cap=week_cap,
        last_run_at=state.last_run_at,
        last_stop_reason=state.last_stop_reason,
    )
    if not cfg.enabled:
        # A disabled campaign reports as disabled even though the runner never
        # ran to record it. Without this the line would show the LAST run's stop
        # reason and read as though it were still draining.
        progress.last_stop_reason = STOP_DISABLED
    return progress


__all__ = [
    "DripConfigError",
    "build_campaign",
    "build_progress",
    "load_worklist_file",
]

"""Drip-drain — budgeted background campaigns (#44).

A campaign is a large, mechanical, resumable work-list drained in budget-capped
increments. Spec: ``~/reports/drip-drain-design-2026-08-04.md``.

Two concerns, deliberately in separate modules: ``state`` owns never-lose-work
(the 907 guard), ``runner`` owns pacing. Conflating them is what the design
argues against at length.
"""

from .runner import (
    STOP_BUDGET_EXHAUSTED,
    STOP_CIRCUIT_BREAKER,
    STOP_DISABLED,
    STOP_QUOTA_BLOCKED,
    STOP_REASONS,
    STOP_WORKLIST_EMPTY,
    Campaign,
    RunResult,
    run_increment,
)
from .state import (
    BLOCKED,
    DONE,
    FAILED,
    IN_FLIGHT,
    ITEM_STATES,
    PENDING,
    CampaignState,
    ItemState,
    campaign_state_path,
    load_state,
    save_state,
)

__all__ = [
    "BLOCKED", "DONE", "FAILED", "IN_FLIGHT", "ITEM_STATES", "PENDING",
    "STOP_BUDGET_EXHAUSTED", "STOP_CIRCUIT_BREAKER", "STOP_DISABLED",
    "STOP_QUOTA_BLOCKED", "STOP_REASONS", "STOP_WORKLIST_EMPTY",
    "Campaign", "CampaignState", "ItemState", "RunResult",
    "campaign_state_path", "load_state", "run_increment", "save_state",
]

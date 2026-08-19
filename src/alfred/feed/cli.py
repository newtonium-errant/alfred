"""``alfred feed`` subcommand handlers.

One verb today:

``repair``  re-derive the missing VERB on legacy verbless-acted items and
            append the verb-bearing state events. DRY-RUN BY DEFAULT — it
            prints the plan and writes nothing unless ``--apply`` is passed.

**Dry-run is the default rather than a flag you remember.** This command edits
the operator's own judgement history, and the failure mode it repairs (a verb
recorded that the operator never chose) is exactly the failure mode a careless
run would CREATE. So the safe direction is the one you get by typing nothing.

**Every run says what it did, including when it did nothing.** A store that
does not exist says so and exits clean; a plan with no writes says so; a
skipped item prints its id AND the reason it was skipped. On a one-shot repair
that runs once per instance and is then never thought about again, silence
would be indistinguishable from a repair that quietly matched nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from alfred.common.instance_paths import configured_logging_dir

from . import config as feed_config
from .repair import (
    MAX_JOIN_DELTA_SECONDS,
    JoinPrecisionError,
    apply_plan,
    build_plan,
    collect_act_events,
    collect_reconcile_events,
    is_repair_candidate,
)
from .store import FeedStore

log = structlog.get_logger(__name__)

#: Logs the repair reads. Act lines land in the TALKER's file (the transport
#: and the action router both run inside the talker daemon, per the runtime
#: -location legend); reconcile lines land in the producers' files. The
#: umbrella log is included because the orchestrator pipes child stdout there.
LOG_FILENAMES = ("talker.log", "daily_sync.log", "brief.log", "alfred.log")


def _emit(payload: dict[str, Any], wants_json: bool, *lines: str) -> None:
    """Print JSON or human lines. One place, so the two never diverge."""
    if wants_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    for line in lines:
        print(line)


def _log_paths(raw: dict[str, Any], override: str | None) -> list[Path]:
    log_dir = Path(override or configured_logging_dir(raw) or "./data")
    return [log_dir / name for name in LOG_FILENAMES]


def cmd_repair(
    raw: dict[str, Any],
    *,
    apply: bool = False,
    wants_json: bool = False,
    log_dir: str | None = None,
) -> int:
    """``alfred feed repair [--apply] [--json] [--log-dir DIR]``."""
    cfg = feed_config.load_from_unified(raw)
    store_path = Path(cfg.store_path) if cfg.store_path.strip() else None

    # ILB, absent-store half. Hypatia and VERA have no feed store at all, so
    # "no file" is the STEADY STATE for them, not a fault — say which instance
    # shape we are looking at and exit clean rather than printing nothing.
    if store_path is None or not store_path.is_file():
        where = str(store_path) if store_path is not None else "(unanchored)"
        log.info(
            "feed.repair.no_store",
            path=where,
            enabled=cfg.enabled,
            detail="no feed store on this instance — nothing to repair",
        )
        _emit(
            {
                "ok": True,
                "store_path": where,
                "candidates": 0,
                "planned_writes": 0,
                "applied": 0,
                "detail": "no feed store on this instance — nothing to repair",
            },
            wants_json,
            f"Feed store: {where}",
            "No feed store on this instance — nothing to repair.",
        )
        return 0

    store = FeedStore(store_path)
    items = store.load()
    candidates = {i: it for i, it in items.items() if is_repair_candidate(it)}

    paths = _log_paths(raw, log_dir)
    present = [p for p in paths if p.is_file()]
    missing = [p for p in paths if not p.is_file()]

    act_events = collect_act_events(present)
    reconcile_events = collect_reconcile_events(present)

    try:
        plan = build_plan(items, act_events, reconcile_events)
    except JoinPrecisionError as exc:
        log.warning(
            "feed.repair.join_precision_refused",
            error=str(exc),
            candidates=len(candidates),
        )
        _emit(
            {"ok": False, "reason": "join_precision", "detail": str(exc)},
            wants_json,
            "REFUSED — join precision check failed.",
            str(exc),
        )
        return 1

    applied = 0
    if apply and not plan.is_empty:
        applied = apply_plan(store, plan)

    log.info(
        "feed.repair.plan",
        store=str(store_path),
        candidates=len(candidates),
        backfills=len(plan.backfills),
        retirements=len(plan.retirements),
        skipped=len(plan.skipped),
        max_delta_seconds=round(plan.max_delta_seconds, 4),
        applied=applied,
        dry_run=not apply,
    )

    payload = {
        "ok": True,
        "store_path": str(store_path),
        "logs_read": [str(p) for p in present],
        "logs_missing": [str(p) for p in missing],
        "candidates": len(candidates),
        "backfills": [
            {"id": b.item_id, "verb": b.verb, "delta_seconds": round(b.delta_seconds, 4)}
            for b in plan.backfills
        ],
        "retirements": [{"id": r.item_id, "kind": r.kind} for r in plan.retirements],
        "skipped": [{"id": s.item_id, "reason": s.reason} for s in plan.skipped],
        "max_delta_seconds": round(plan.max_delta_seconds, 4),
        "planned_writes": plan.write_count,
        "applied": applied,
        "dry_run": not apply,
    }

    lines = [f"Feed store: {store_path}"]
    lines.append(
        "Logs read: " + (", ".join(str(p) for p in present) if present else "(none found)")
    )
    if missing:
        lines.append("Logs absent: " + ", ".join(str(p) for p in missing))
    lines.append(f"Verbless-acted candidates: {len(candidates)}")

    if plan.backfills:
        lines.append(f"Verb backfills ({len(plan.backfills)}):")
        lines.extend(
            f"  {b.item_id} -> {b.verb} (matched {b.delta_seconds:.3f}s from acted_at)"
            for b in plan.backfills
        )
        lines.append(
            f"  max join delta {plan.max_delta_seconds:.3f}s "
            f"(ceiling {MAX_JOIN_DELTA_SECONDS:.1f}s)"
        )
    else:
        lines.append("Verb backfills: none.")

    if plan.retirements:
        lines.append(f"Retirements to stamp ({len(plan.retirements)}):")
        lines.extend(f"  {r.item_id} (kind={r.kind})" for r in plan.retirements)
    else:
        lines.append("Retirements to stamp: none.")

    if plan.skipped:
        lines.append(f"Skipped ({len(plan.skipped)}) — left exactly as they are:")
        lines.extend(f"  {s.item_id}: {s.reason}" for s in plan.skipped)

    if plan.is_empty:
        # ILB: an already-repaired store is the EXPECTED second-run outcome.
        lines.append("Nothing to repair — every acted item already carries its verb.")
    elif apply:
        lines.append(f"APPLIED {applied} state event(s).")
    else:
        lines.append(
            f"DRY RUN — {plan.write_count} event(s) would be written. "
            "Nothing was written. Re-run with --apply to commit."
        )

    _emit(payload, wants_json, *lines)
    return 0

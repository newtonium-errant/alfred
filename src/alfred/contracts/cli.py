"""``alfred contract`` — operator + agent-gate CLI.

Agent gate (read-only): ``check`` (exit 0 iff buildable — the script
build-gate ``alfred contract check <id> && <build>``) + ``show`` (the loud
RATIFIED-vs-DO-NOT-BUILD banner) + ``list``. Operator authority: ``ratify``
/ ``reject`` — the CLI invocation IS the operator authority; agents have NO
CLI ratify path (and a ``ratify`` bus message by an agent is fail-closed
rejected upstream in the store).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from .config import ContractConfig, load_contract_config
from .schema import (
    STATE_COUNTERED,
    STATE_PROPOSED,
    find_gaps,
    find_overlaps,
    is_buildable,
    is_converged,
)
from .store import ContractStore

# Exit code for `check` when the contract is NOT buildable (the build-gate).
CHECK_NOT_BUILDABLE = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store(config: ContractConfig) -> ContractStore:
    return ContractStore(config.store_path, config.resolved_audit_path())


def _contract_json(c) -> dict[str, Any]:
    return {
        "contract_id": c.contract_id,
        "seam": c.seam,
        "state": c.state,
        "version": c.version,
        "buildable": is_buildable(c),
        "converged": is_converged(c),
        "interface": c.interface,
        "division_of_labor": [d.to_dict() for d in c.division_of_labor],
        "participants": [p.to_dict() for p in c.participants],
        "gaps": [g.item for g in find_gaps(c)],
        "overlaps": [{"item": i, "owners": o} for i, o in find_overlaps(c)],
    }


def cmd_list(args: argparse.Namespace, config: ContractConfig) -> int:
    store = _store(config)
    if getattr(args, "awaiting", False):
        contracts = store.list_awaiting()
    else:
        contracts = store.iter_contracts()
        state_filter = getattr(args, "state", "") or ""
        if state_filter:
            contracts = [c for c in contracts if c.state == state_filter]
    if getattr(args, "json", False):
        print(json.dumps([_contract_json(c) for c in contracts], indent=2))
        return 0
    if not contracts:
        # Intentionally-left-blank — explicit empty line.
        print("(no contracts)")
        return 0
    for c in contracts:
        flag = "BUILDABLE" if is_buildable(c) else c.state.upper()
        print(f"  {c.contract_id}  [{flag}] v{c.version}  {c.seam}")
    return 0


def cmd_show(args: argparse.Namespace, config: ContractConfig) -> int:
    store = _store(config)
    c = store.load(args.contract_id)
    if c is None:
        print(f"contract not found: {args.contract_id}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(_contract_json(c), indent=2))
        return 0
    # Loud banner — the agent's go/no-go signal.
    if is_buildable(c):
        print(f"STATUS: RATIFIED — safe to build against v{c.version}")
    else:
        awaiting = (
            "operator ratification" if is_converged(c)
            else "participant convergence"
        )
        print(
            f"STATUS: {c.state.upper()} — DO NOT BUILD. "
            f"Interface not locked (v{c.version}, awaiting {awaiting})."
        )
    print(f"contract: {c.contract_id}")
    print(f"seam: {c.seam}")
    print("participants:")
    for p in c.participants:
        acc = "—" if p.accepted_version is None else f"v{p.accepted_version}"
        print(f"  - {p.project}/{p.agent} ({p.role}) accepted={acc}")
    print("division of labor:")
    for d in c.division_of_labor:
        owner = d.owner or "(GAP — unowned)"
        print(f"  - {d.item} → {owner} [{d.status}]")
    gaps = find_gaps(c)
    if gaps:
        print("GAPS: " + ", ".join(g.item for g in gaps))
    overlaps = find_overlaps(c)
    if overlaps:
        print("OVERLAPS: " + "; ".join(f"{i}={o}" for i, o in overlaps))
    if c.body.strip():
        print("\n--- interface spec ---")
        print(c.body.strip())
    return 0


def cmd_check(args: argparse.Namespace, config: ContractConfig) -> int:
    """The build-gate: exit 0 iff buildable, else CHECK_NOT_BUILDABLE."""
    store = _store(config)
    c = store.load(args.contract_id)
    if c is None:
        print(f"contract not found: {args.contract_id}", file=sys.stderr)
        return CHECK_NOT_BUILDABLE
    if is_buildable(c):
        print(f"BUILDABLE: {c.contract_id} ratified at v{c.version}")
        return 0
    print(
        f"NOT BUILDABLE: {c.contract_id} is {c.state} (v{c.version}) — "
        "do not build against an unratified contract",
        file=sys.stderr,
    )
    return CHECK_NOT_BUILDABLE


def _emit_operator_decision_notices(
    raw: dict, c, kind: str, note: str,
) -> list[str]:
    """Write an fyi notice to EVERY participant's inbox (both sides learn
    the operator ratified/rejected). Best-effort; returns notified projects."""
    try:
        from alfred.contracts.router import _write_notice
        from alfred.msgbus.config import load_message_bus_config
    except Exception:  # noqa: BLE001 — bus not present → skip notices
        return []
    registry = load_message_bus_config(raw).registry()
    subject = f"[contract] {c.seam} — {kind} → {c.state} (v{c.version})"
    body = (
        f"contract_id: {c.contract_id}\nstate: {c.state}\n"
        f"operator {kind}" + (f": {note}" if note else "")
    )
    notified: list[str] = []
    for p in c.participants:
        inbox = registry.inbox_for(p.project)
        if inbox is None:
            continue
        try:
            _write_notice(inbox, p.project, subject, body)
            notified.append(p.project)
        except OSError:
            pass
    return notified


def cmd_ratify(
    args: argparse.Namespace, config: ContractConfig, raw: dict,
) -> int:
    """OPERATOR-only ratify. Gated on convergence unless ``--force`` (the
    operator deadlock tiebreaker)."""
    store = _store(config)
    c = store.load(args.contract_id)
    if c is None:
        print(f"contract not found: {args.contract_id}", file=sys.stderr)
        return 1
    if not is_converged(c) and not getattr(args, "force", False):
        print(
            f"NOT CONVERGED: {c.contract_id} — not all participants accepted "
            f"v{c.version}. Re-run with --force to ratify anyway (operator "
            "deadlock tiebreaker).",
            file=sys.stderr,
        )
        return 1
    note = getattr(args, "note", "") or ""
    result = store.apply_message(
        {
            "kind": "ratify",
            "contract_id": c.contract_id,
            "from": config.operator_id,
            "correlation_id": f"op-ratify-{_now_iso()}",
            "note": note,
        },
        "",
        actor=config.operator_id,
        actor_is_operator=True,
    )
    if not result.ok:
        print(f"ratify failed: {result.reason}", file=sys.stderr)
        return 1
    notified = _emit_operator_decision_notices(raw, result.contract, "ratify", note)
    print(
        f"RATIFIED {c.contract_id} at v{result.contract.version}. "
        f"Both agents' `alfred contract check` now exit 0. "
        f"Notified: {notified or '(no inboxes)'}"
    )
    return 0


def cmd_reject(
    args: argparse.Namespace, config: ContractConfig, raw: dict,
) -> int:
    """OPERATOR-only reject → back to countered (with the reason)."""
    store = _store(config)
    c = store.load(args.contract_id)
    if c is None:
        print(f"contract not found: {args.contract_id}", file=sys.stderr)
        return 1
    note = getattr(args, "note", "") or ""
    result = store.apply_message(
        {
            "kind": "reject",
            "contract_id": c.contract_id,
            "from": config.operator_id,
            "correlation_id": f"op-reject-{_now_iso()}",
            "note": note,
        },
        "",
        actor=config.operator_id,
        actor_is_operator=True,
    )
    if not result.ok:
        print(f"reject failed: {result.reason}", file=sys.stderr)
        return 1
    notified = _emit_operator_decision_notices(raw, result.contract, "reject", note)
    print(
        f"REJECTED {c.contract_id} → {result.contract.state}. "
        f"Notified: {notified or '(no inboxes)'}"
    )
    return 0


def build_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register ``alfred contract {list|show|check|ratify|reject}``."""
    p = sub.add_parser(
        "contract",
        help=(
            "Layer-2 contracts — list/show/check (agent build-gate) + "
            "ratify/reject (operator)"
        ),
    )
    c_sub = p.add_subparsers(dest="contract_cmd")

    c_list = c_sub.add_parser("list", help="List contracts")
    c_list.add_argument("--state", default="", help="Filter by state")
    c_list.add_argument(
        "--awaiting", action="store_true", default=False,
        help="Only contracts needing the operator (converged/blocked)",
    )
    c_list.add_argument("--json", action="store_true", default=False)

    c_show = c_sub.add_parser("show", help="Show a contract (RATIFIED banner)")
    c_show.add_argument("contract_id")
    c_show.add_argument("--json", action="store_true", default=False)

    c_check = c_sub.add_parser(
        "check", help="Build-gate: exit 0 iff ratified (buildable)",
    )
    c_check.add_argument("contract_id")

    c_ratify = c_sub.add_parser("ratify", help="OPERATOR: lock a contract")
    c_ratify.add_argument("contract_id")
    c_ratify.add_argument(
        "--force", action="store_true", default=False,
        help="Ratify even if not converged (deadlock tiebreaker)",
    )
    c_ratify.add_argument("--note", default="")

    c_reject = c_sub.add_parser("reject", help="OPERATOR: send back to countered")
    c_reject.add_argument("contract_id")
    c_reject.add_argument("--note", default="")


def dispatch(args: argparse.Namespace, raw: dict) -> int:
    """Run the selected ``alfred contract`` subcommand; returns the exit
    code (``check`` uses it as the build-gate)."""
    config = load_contract_config(raw)
    subcmd = getattr(args, "contract_cmd", None)
    if subcmd == "list":
        return cmd_list(args, config)
    if subcmd == "show":
        return cmd_show(args, config)
    if subcmd == "check":
        return cmd_check(args, config)
    if subcmd == "ratify":
        return cmd_ratify(args, config, raw)
    if subcmd == "reject":
        return cmd_reject(args, config, raw)
    print(
        "usage: alfred contract {list|show|check|ratify|reject}",
        file=sys.stderr,
    )
    return 1


__all__ = ["CHECK_NOT_BUILDABLE", "build_subparser", "dispatch"]

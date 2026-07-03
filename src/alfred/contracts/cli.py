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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ContractConfig, load_contract_config
from .schema import (
    STATE_COUNTERED,
    STATE_PROPOSED,
    find_gaps,
    find_overlaps,
    is_buildable,
    is_converged,
    mint_contract_id,
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


# ---------------------------------------------------------------------------
# Agent-side minting: propose / counter / accept
#
# These mirror ``alfred msg send``'s spool-write path (mint a message into the
# msgbus spool), but for CONTRACT_KINDS: they add the ``seam``/``contract_id``
# frontmatter + a YAML payload body (participants / division_of_labor) that the
# contract router re-parses. The router later APPLIES the file with
# ``actor_is_operator=False`` — so agents stay fail-closed (only the operator
# CLI ``ratify``/``reject`` carries operator authority; those are unchanged).
# ---------------------------------------------------------------------------


def _parse_participant_arg(spec: str) -> tuple[dict[str, str] | None, str]:
    """``project[:agent[:role]]`` → ``{project, agent, role}`` or an error."""
    parts = str(spec).split(":")
    if len(parts) > 3:
        return None, f"invalid --participant {spec!r} (want project[:agent[:role]])"
    project = parts[0].strip()
    if not project:
        return None, f"invalid --participant {spec!r} (empty project)"
    return {
        "project": project,
        "agent": (parts[1].strip() if len(parts) > 1 else ""),
        "role": (parts[2].strip() if len(parts) > 2 else ""),
    }, ""


def _parse_item_arg(spec: str) -> tuple[dict[str, str] | None, str]:
    """``item[:owner]`` → ``{item, owner}`` (empty owner ⇒ GAP) or an error."""
    item, _, owner = str(spec).partition(":")
    item = item.strip()
    if not item:
        return None, f"invalid --item {spec!r} (empty item name)"
    return {"item": item, "owner": owner.strip()}, ""


def _build_participants(
    args: argparse.Namespace, from_project: str, to_list: list[str],
) -> tuple[list[dict[str, str]] | None, str]:
    """Explicit ``--participant`` specs, else derive a producer (``--from``) +
    one consumer per ``--to`` project (default agent ``lead``)."""
    specs = getattr(args, "participant", None) or []
    if specs:
        out: list[dict[str, str]] = []
        for s in specs:
            parsed, err = _parse_participant_arg(s)
            if err:
                return None, "alfred contract: " + err
            out.append(parsed)
        return out, ""
    parts = [{"project": from_project, "agent": "lead", "role": "producer"}]
    for t in to_list:
        parts.append({"project": t, "agent": "lead", "role": "consumer"})
    return parts, ""


def _build_dol(args: argparse.Namespace) -> tuple[list[dict[str, str]] | None, str]:
    """``--item`` specs → division_of_labor rows (may be empty)."""
    out: list[dict[str, str]] = []
    for s in getattr(args, "item", None) or []:
        parsed, err = _parse_item_arg(s)
        if err:
            return None, "alfred contract: " + err
        out.append(parsed)
    return out, ""


def _mint_contract_spool_file(
    spool_path: str,
    *,
    from_project: str,
    to_project: str,
    kind: str,
    subject: str,
    correlation_id: str,
    created: str,
    seam: str = "",
    contract_id: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """Mint a CONTRACT_KINDS message into the msgbus spool. Returns
    ``(message_id, filename)`` or None (printed error). Mirrors
    ``record.write_message_file`` (atomic ``.tmp`` → replace) but injects the
    contract-only ``seam``/``contract_id`` frontmatter the plain
    ``MessageRecord`` drops."""
    import os as _os

    import frontmatter as _frontmatter
    import yaml as _yaml

    from alfred.msgbus.record import (
        MESSAGE_KINDS,
        MessageRecord,
        _frontmatter_dict,
        message_filename,
        validate_record,
    )
    from alfred.msgbus.router import mint_message_id
    from .schema import CONTRACT_KINDS

    body = ""
    if payload:
        body = _yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)

    record = MessageRecord(
        from_project=from_project,
        to_project=to_project,
        kind=kind,
        correlation_id=correlation_id,
        created=created,
        subject=subject,
        body=body,
    )
    record.id = mint_message_id(from_project, to_project, created, subject, body)

    errors = validate_record(record, valid_kinds=MESSAGE_KINDS | CONTRACT_KINDS)
    if errors:
        print(
            "alfred contract: invalid message — " + "; ".join(errors),
            file=sys.stderr,
        )
        return None

    fm = _frontmatter_dict(record)
    if seam:
        fm["seam"] = seam
    if contract_id:
        fm["contract_id"] = contract_id

    dest = Path(spool_path) / message_filename(record)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rendered = _frontmatter.dumps(_frontmatter.Post(body, **fm))
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(rendered)
    _os.replace(tmp, dest)
    return record.id, dest.name


def _mb_config(raw: dict):  # type: ignore[no-untyped-def]
    from alfred.msgbus.config import load_message_bus_config

    return load_message_bus_config(raw)


def _resolve_from(args: argparse.Namespace, mb_config) -> str:  # type: ignore[no-untyped-def]
    return (getattr(args, "from_project", "") or "") or mb_config.self_project


def cmd_propose(args: argparse.Namespace, config: ContractConfig, raw: dict) -> int:
    """Mint a ``kind: propose`` contract message into the spool. ``--and-accept``
    ALSO drops the proposer's own ``accept`` (convergence needs EVERY
    participant, and a propose does NOT self-accept the proposer)."""
    mb = _mb_config(raw)
    from_project = _resolve_from(args, mb)
    if not from_project:
        print("alfred contract propose: --from required (or set message_bus.self_project)", file=sys.stderr)
        return 1
    to_list = [t.strip() for t in str(args.to or "").split(",") if t.strip()]
    if not to_list:
        print("alfred contract propose: --to required (comma-separated project slugs)", file=sys.stderr)
        return 1
    if not (args.seam or "").strip():
        print("alfred contract propose: --seam required (the coordination seam slug)", file=sys.stderr)
        return 1
    if not (args.subject or "").strip():
        print("alfred contract propose: --subject required", file=sys.stderr)
        return 1
    participants, err = _build_participants(args, from_project, to_list)
    if err:
        print(err, file=sys.stderr)
        return 1
    dol, err = _build_dol(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    payload: dict[str, Any] = {"participants": participants}
    if dol:
        payload["division_of_labor"] = dol

    created = _now_iso()
    correlation_id = getattr(args, "correlation_id", "") or f"cnv-{uuid.uuid4().hex[:12]}"
    minted = _mint_contract_spool_file(
        mb.spool_path, from_project=from_project, to_project=to_list[0],
        kind="propose", subject=args.subject, correlation_id=correlation_id,
        created=created, seam=args.seam, payload=payload,
    )
    if minted is None:
        return 1
    msg_id, _fn = minted
    contract_id = mint_contract_id(args.seam, created)
    print(
        f"queued propose {msg_id} → {to_list[0]} "
        f"(seam={args.seam}, contract_id={contract_id}) [{correlation_id}]"
    )

    if getattr(args, "and_accept", False):
        # The proposer's own accept. It MUST sort AFTER the propose in the
        # spool (message_filename is <compact-ts>-… and _compact_ts is
        # second-granular) so the propose CREATES the contract before this
        # accept applies in the same route tick — else the accept hits
        # "contract not found" and the router archives it (never retried).
        accept_created = (
            datetime.fromisoformat(created) + timedelta(seconds=1)
        ).isoformat()
        acc = _mint_contract_spool_file(
            mb.spool_path, from_project=from_project, to_project=to_list[0],
            kind="accept", subject=f"accept: {contract_id}",
            correlation_id=correlation_id, created=accept_created,
            contract_id=contract_id,
        )
        if acc is None:
            return 1
        print(
            f"queued accept  {acc[0]} from {from_project} "
            f"(contract_id={contract_id}) [--and-accept, proposer self-accept]"
        )
    print("  apply with: alfred msg route-once")
    return 0


def _load_contract_for_mint(
    config: ContractConfig, contract_id: str, from_project: str, verb: str,
) -> tuple[Any, str] | None:
    """Load an existing contract for counter/accept; validate ``from`` is a
    participant + derive the msgbus ``to`` (a participant other than the
    actor). Returns ``(contract, to_project)`` or None (printed error)."""
    c = _store(config).load(contract_id)
    if c is None:
        print(f"alfred contract {verb}: contract not found: {contract_id}", file=sys.stderr)
        return None
    parts = {p.project for p in c.participants}
    if from_project not in parts:
        print(
            f"alfred contract {verb}: --from {from_project!r} is not a participant "
            f"of {contract_id} (participants: {sorted(parts)})",
            file=sys.stderr,
        )
        return None
    to_project = next((p.project for p in c.participants if p.project != from_project), "")
    if not to_project:
        print(
            f"alfred contract {verb}: {contract_id} has no counterparty to notify",
            file=sys.stderr,
        )
        return None
    return c, to_project


def cmd_counter(args: argparse.Namespace, config: ContractConfig, raw: dict) -> int:
    """Mint a ``kind: counter`` — re-state terms on an existing contract
    (division_of_labor REPLACED when given; participants additive)."""
    mb = _mb_config(raw)
    from_project = _resolve_from(args, mb)
    if not from_project:
        print("alfred contract counter: --from required (or set message_bus.self_project)", file=sys.stderr)
        return 1
    if not (args.contract_id or "").strip():
        print("alfred contract counter: --contract-id required", file=sys.stderr)
        return 1
    if not (args.subject or "").strip():
        print("alfred contract counter: --subject required", file=sys.stderr)
        return 1
    loaded = _load_contract_for_mint(config, args.contract_id, from_project, "counter")
    if loaded is None:
        return 1
    _c, to_project = loaded
    payload: dict[str, Any] = {}
    specs = getattr(args, "participant", None) or []
    if specs:
        parts_list = []
        for s in specs:
            parsed, err = _parse_participant_arg(s)
            if err:
                print("alfred contract: " + err, file=sys.stderr)
                return 1
            parts_list.append(parsed)
        payload["participants"] = parts_list
    dol, err = _build_dol(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    if dol:
        payload["division_of_labor"] = dol

    correlation_id = getattr(args, "correlation_id", "") or f"cnv-{uuid.uuid4().hex[:12]}"
    minted = _mint_contract_spool_file(
        mb.spool_path, from_project=from_project, to_project=to_project,
        kind="counter", subject=args.subject, correlation_id=correlation_id,
        created=_now_iso(), contract_id=args.contract_id, payload=payload or None,
    )
    if minted is None:
        return 1
    print(
        f"queued counter {minted[0]} from {from_project} "
        f"(contract_id={args.contract_id}) [{correlation_id}]"
    )
    print("  apply with: alfred msg route-once")
    return 0


def cmd_accept(args: argparse.Namespace, config: ContractConfig, raw: dict) -> int:
    """Mint a ``kind: accept`` — ``--from`` (the accepting participant) assents
    to the contract's CURRENT version. Convergence needs EVERY participant."""
    mb = _mb_config(raw)
    from_project = _resolve_from(args, mb)
    if not from_project:
        print("alfred contract accept: --from required (or set message_bus.self_project)", file=sys.stderr)
        return 1
    if not (args.contract_id or "").strip():
        print("alfred contract accept: --contract-id required", file=sys.stderr)
        return 1
    loaded = _load_contract_for_mint(config, args.contract_id, from_project, "accept")
    if loaded is None:
        return 1
    _c, to_project = loaded
    correlation_id = getattr(args, "correlation_id", "") or f"cnv-{uuid.uuid4().hex[:12]}"
    minted = _mint_contract_spool_file(
        mb.spool_path, from_project=from_project, to_project=to_project,
        kind="accept", subject=f"accept: {args.contract_id}",
        correlation_id=correlation_id, created=_now_iso(),
        contract_id=args.contract_id,
    )
    if minted is None:
        return 1
    print(
        f"queued accept {minted[0]} from {from_project} "
        f"(contract_id={args.contract_id}) [{correlation_id}]"
    )
    print("  apply with: alfred msg route-once")
    return 0


def build_subparser(sub: "argparse._SubParsersAction") -> None:
    """Register ``alfred contract {list|show|check|ratify|reject|propose|
    counter|accept}``."""
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

    # --- agent-side minting (into the msgbus spool) ---
    c_propose = c_sub.add_parser(
        "propose", help="AGENT: propose a new contract (into the msgbus spool)",
    )
    c_propose.add_argument("--from", dest="from_project", default="", help="proposing project (or message_bus.self_project)")
    c_propose.add_argument("--to", required=True, help="counterparty project slug(s), comma-separated")
    c_propose.add_argument("--seam", required=True, help="the coordination seam slug")
    c_propose.add_argument("--subject", required=True)
    c_propose.add_argument(
        "--participant", action="append", default=[], metavar="project[:agent[:role]]",
        help="explicit participant (repeatable); else derived from --from/--to",
    )
    c_propose.add_argument(
        "--item", action="append", default=[], metavar="name[:owner]",
        help="division_of_labor row (repeatable); empty owner ⇒ GAP",
    )
    c_propose.add_argument("--correlation-id", dest="correlation_id", default="")
    c_propose.add_argument(
        "--and-accept", dest="and_accept", action="store_true", default=False,
        help="also drop the proposer's own accept (convergence needs all participants)",
    )

    c_counter = c_sub.add_parser(
        "counter", help="AGENT: counter an existing contract (re-state terms)",
    )
    c_counter.add_argument("--from", dest="from_project", default="", help="countering project (must be a participant)")
    c_counter.add_argument("--contract-id", dest="contract_id", required=True)
    c_counter.add_argument("--subject", required=True)
    c_counter.add_argument("--participant", action="append", default=[], metavar="project[:agent[:role]]")
    c_counter.add_argument("--item", action="append", default=[], metavar="name[:owner]")
    c_counter.add_argument("--correlation-id", dest="correlation_id", default="")

    c_accept = c_sub.add_parser(
        "accept", help="AGENT: accept a contract's current version (from a participant)",
    )
    c_accept.add_argument("--from", dest="from_project", default="", help="accepting participant (or message_bus.self_project)")
    c_accept.add_argument("--contract-id", dest="contract_id", required=True)
    c_accept.add_argument("--correlation-id", dest="correlation_id", default="")


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
    if subcmd == "propose":
        return cmd_propose(args, config, raw)
    if subcmd == "counter":
        return cmd_counter(args, config, raw)
    if subcmd == "accept":
        return cmd_accept(args, config, raw)
    print(
        "usage: alfred contract "
        "{list|show|check|ratify|reject|propose|counter|accept}",
        file=sys.stderr,
    )
    return 1


__all__ = ["CHECK_NOT_BUILDABLE", "build_subparser", "dispatch"]

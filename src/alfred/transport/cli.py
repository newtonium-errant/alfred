"""Subcommand handlers for ``alfred transport``.

Subcommands:

- ``alfred transport status``       — queue depth, dead-letter count,
                                      health probe summary.
- ``alfred transport send-test``    — direct smoke test via the client.
- ``alfred transport queue``        — list pending scheduled sends.
- ``alfred transport dead-letter``  — list / retry / drop dead-letter
                                      entries.
- ``alfred transport rotate``       — generate a new 64-char hex token,
                                      update ``.env`` in place with a
                                      backup.

The talker must be restarted (``alfred down && alfred up``) after a
token rotation — the running daemon has the old token baked into
memory. The ``rotate`` command prints that reminder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from .config import TransportConfig, load_from_unified
from .state import TransportState


# --- helpers ---------------------------------------------------------------


def _load_transport(raw: dict[str, Any]) -> TransportConfig:
    return load_from_unified(raw)


def _load_state(raw: dict[str, Any]) -> TransportState:
    config = _load_transport(raw)
    state = TransportState.create(config.state.path)
    state.load()
    return state


# --- subcommands -----------------------------------------------------------


def cmd_status(raw: dict[str, Any], wants_json: bool = False) -> int:
    """Print queue depth + dead-letter count + probe health."""
    config = _load_transport(raw)
    state = _load_state(raw)

    from alfred.transport.health import health_check
    report = asyncio.run(health_check(raw, mode="quick"))

    payload: dict[str, Any] = {
        "host": config.server.host,
        "port": config.server.port,
        "pending_queue": len(state.pending_queue),
        "dead_letter": len(state.dead_letter),
        "send_log_recent": len(state.send_log),
        "health_status": report.status.value,
        "checks": [
            {
                "name": r.name,
                "status": r.status.value,
                "detail": r.detail,
            }
            for r in report.results
        ],
    }

    if wants_json:
        print(json.dumps(payload, indent=2))
        return 0

    print("=" * 60)
    print("TRANSPORT STATUS")
    print("=" * 60)
    print(f"  Server:          {config.server.host}:{config.server.port}")
    print(f"  Pending queue:   {len(state.pending_queue)}")
    print(f"  Dead-letter:     {len(state.dead_letter)}")
    print(f"  Send log (recent): {len(state.send_log)}")
    print(f"  Health:          {report.status.value}")
    for r in report.results:
        print(f"    [{r.status.value:4}] {r.name}: {r.detail}")
    return 0


def cmd_send_test(
    raw: dict[str, Any],
    user_id: int,
    text: str,
    wants_json: bool = False,
) -> int:
    """Direct smoke test via the client — emits a real outbound send."""
    from alfred.transport.client import send_outbound
    from alfred.transport.exceptions import TransportError

    try:
        result = asyncio.run(
            send_outbound(
                user_id=user_id, text=text, client_name="cli",
            ),
        )
    except TransportError as exc:
        if wants_json:
            print(json.dumps({"error": str(exc), "type": exc.__class__.__name__}, indent=2))
        else:
            print(f"Transport error: {exc}")
        return 1

    if wants_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Sent: id={result.get('id')} status={result.get('status')}")
        if result.get("telegram_message_id"):
            print(f"  telegram_message_id={result['telegram_message_id']}")
    return 0


def cmd_queue(raw: dict[str, Any], wants_json: bool = False) -> int:
    """List pending scheduled sends from the transport state file."""
    state = _load_state(raw)

    if wants_json:
        print(json.dumps(state.pending_queue, indent=2, default=str))
        return 0

    if not state.pending_queue:
        print("No pending scheduled sends.")
        return 0
    print(f"Pending scheduled sends ({len(state.pending_queue)}):")
    for entry in state.pending_queue:
        print(
            f"  - id={entry.get('id')} "
            f"user={entry.get('user_id')} "
            f"scheduled_at={entry.get('scheduled_at')}"
        )
        text = entry.get("text", "")
        preview = text if len(text) <= 80 else text[:77] + "..."
        print(f"      {preview}")
    return 0


def cmd_dead_letter(
    raw: dict[str, Any],
    action: str,
    entry_id: str | None = None,
    wants_json: bool = False,
) -> int:
    """``list`` / ``retry <id>`` / ``drop <id>`` over the dead-letter queue."""
    state = _load_state(raw)

    if action == "list":
        if wants_json:
            print(json.dumps(state.dead_letter, indent=2, default=str))
            return 0
        if not state.dead_letter:
            print("Dead-letter queue is empty.")
            return 0
        print(f"Dead-letter entries ({len(state.dead_letter)}):")
        for entry in state.dead_letter:
            print(
                f"  - id={entry.get('id')} "
                f"reason={entry.get('dead_letter_reason')} "
                f"at={entry.get('dead_lettered_at')}"
            )
            text = entry.get("text", "")
            preview = text if len(text) <= 80 else text[:77] + "..."
            print(f"      {preview}")
        return 0

    if action in {"retry", "drop"} and not entry_id:
        print(f"Usage: alfred transport dead-letter {action} <id>")
        return 1

    # Find the entry.
    matching = [e for e in state.dead_letter if e.get("id") == entry_id]
    if not matching:
        print(f"No dead-letter entry with id={entry_id}")
        return 1
    entry = matching[0]

    if action == "drop":
        state.dead_letter = [
            e for e in state.dead_letter if e.get("id") != entry_id
        ]
        state.save()
        print(f"Dropped dead-letter entry id={entry_id}")
        return 0

    if action == "retry":
        # Re-enqueue for immediate dispatch — next scheduler tick
        # picks it up. Drop from dead_letter first so it doesn't
        # duplicate if the retry succeeds.
        state.dead_letter = [
            e for e in state.dead_letter if e.get("id") != entry_id
        ]
        requeue = {
            k: v for k, v in entry.items()
            if k not in {"dead_letter_reason", "dead_lettered_at"}
        }
        # Ensure it fires next tick.
        requeue.pop("scheduled_at", None)
        state.enqueue(requeue)
        state.save()
        print(f"Re-enqueued dead-letter entry id={entry_id} for retry")
        return 0

    print(f"Unknown dead-letter action: {action}")
    return 1


def cmd_tail(
    raw: dict[str, Any],
    peer: str | None,
    limit: int,
    wants_json: bool,
) -> int:
    """Tail the canonical audit log, optionally filtered by peer."""
    from .canonical_audit import read_audit, resolve_audit_path

    audit_path = resolve_audit_path(raw)
    entries = read_audit(audit_path)
    if peer:
        entries = [e for e in entries if e.get("peer") == peer]
    tail = entries[-limit:] if limit > 0 else entries

    if wants_json:
        print(json.dumps(tail, indent=2, default=str))
        return 0

    if not tail:
        label = f" for peer {peer!r}" if peer else ""
        print(f"No canonical audit entries{label}.")
        return 0

    print(
        f"Showing last {len(tail)} canonical audit entries"
        + (f" for peer {peer!r}" if peer else "")
        + ":"
    )
    for e in tail:
        granted = ",".join(e.get("granted") or [])
        denied = ",".join(e.get("denied") or [])
        print(
            f"  {e.get('ts', '?')}  peer={e.get('peer'):<10}  "
            f"type={e.get('type'):<10} name={e.get('name')}  "
            f"granted=[{granted}]  denied=[{denied}]  "
            f"cid={e.get('correlation_id', '')}"
        )
    return 0


def cmd_rotate(raw: dict[str, Any], env_path: str = ".env") -> int:
    """Generate a new 64-char hex token, update ``.env`` in place.

    The running talker daemon does NOT pick up the change — the
    token is baked into its auth middleware config at startup.
    Operator must ``alfred down && alfred up`` after rotation.
    """
    new_token = secrets.token_hex(32)
    env_file = Path(env_path)

    if not env_file.exists():
        # Create a fresh .env with just the token.
        env_file.write_text(
            f"# Auto-created by `alfred transport rotate`\n"
            f"ALFRED_TRANSPORT_TOKEN={new_token}\n",
            encoding="utf-8",
        )
        print(f"Created {env_path} with new transport token.")
        print(f"New token: {new_token}")
        print(
            "\nRestart Alfred for the new token to take effect:"
            "\n  alfred down && alfred up"
        )
        return 0

    # Back up first — never edit .env without a recoverable copy.
    backup = env_file.with_suffix(env_file.suffix + ".bak")
    backup.write_text(env_file.read_text(encoding="utf-8"), encoding="utf-8")

    lines = env_file.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("ALFRED_TRANSPORT_TOKEN="):
            out.append(f"ALFRED_TRANSPORT_TOKEN={new_token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"ALFRED_TRANSPORT_TOKEN={new_token}")

    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Updated {env_path} with new transport token (backup: {backup}).")
    print(f"New token: {new_token}")
    print(
        "\nRestart Alfred for the new token to take effect:"
        "\n  alfred down && alfred up"
    )
    return 0


# --- argparse wiring --------------------------------------------------------


def cmd_propose_person(
    raw: dict[str, Any],
    *,
    peer: str,
    name: str,
    fields: list[str],
    source: str,
    self_name: str,
    wants_json: bool = False,
) -> int:
    """Run the KAL-LE-side resolve-or-propose flow against ``peer``.

    Parses ``--field key=value`` into a dict, invokes
    :func:`alfred.transport.client.resolve_or_propose_canonical_person`,
    and prints the structured outcome. Exit code 0 regardless of
    ``found``/``pending`` — the outcome is informational, not a failure
    signal. Non-zero exits reserved for transport errors (401, 5xx,
    peer unreachable).
    """
    from .client import resolve_or_propose_canonical_person
    from .config import load_from_unified
    from .exceptions import TransportError

    parsed_fields: dict[str, str] = {}
    for kv in fields:
        if "=" not in kv:
            print(f"--field expects key=value shape (got {kv!r})", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        parsed_fields[k.strip()] = v.strip()

    config = load_from_unified(raw)
    try:
        result = asyncio.run(resolve_or_propose_canonical_person(
            peer, name,
            proposed_fields=parsed_fields or None,
            source=source,
            config=config,
            self_name=self_name,
        ))
    except TransportError as exc:
        print(f"Transport error: {exc}", file=sys.stderr)
        return 1

    if wants_json:
        print(json.dumps(result, default=str))
    else:
        status = result.get("status", "?")
        print(f"status: {status}")
        if status == "found":
            fm = result.get("frontmatter", {})
            granted = result.get("granted", [])
            print(f"granted fields: {granted}")
            for k, v in fm.items():
                print(f"  {k}: {v}")
        elif status == "pending":
            cid = result.get("correlation_id", "")
            print(f"proposal queued on {peer} — correlation_id={cid}")
            print("Andrew will confirm or reject in the next Daily Sync.")
    return 0


def build_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``alfred transport ...`` subcommand tree.

    Called from ``alfred.cli.build_parser`` so ``alfred transport``
    gets a proper help surface alongside the other tool CLIs.
    """
    transport_p = subparsers.add_parser(
        "transport",
        help="Outbound-push transport (HTTP server inside the talker)",
    )
    t_sub = transport_p.add_subparsers(dest="transport_cmd")

    status_p = t_sub.add_parser("status", help="Show queue + health")
    status_p.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )

    send_p = t_sub.add_parser(
        "send-test", help="Smoke test: send one message via the transport",
    )
    send_p.add_argument("user_id", type=int, help="Telegram user_id")
    send_p.add_argument("text", help="Message text")
    send_p.add_argument("--json", action="store_true", default=False)

    queue_p = t_sub.add_parser("queue", help="List pending scheduled sends")
    queue_p.add_argument("--json", action="store_true", default=False)

    dl_p = t_sub.add_parser(
        "dead-letter", help="Inspect / retry / drop dead-letter entries",
    )
    dl_p.add_argument(
        "action", choices=["list", "retry", "drop"],
        help="Action to take",
    )
    dl_p.add_argument(
        "entry_id", nargs="?", default=None,
        help="Entry ID (required for retry/drop)",
    )
    dl_p.add_argument("--json", action="store_true", default=False)

    t_sub.add_parser(
        "rotate",
        help="Generate a new transport token, update .env in place",
    )

    # Stage 3.5: tail peer-specific audit / correlation-id logs.
    tail_p = t_sub.add_parser(
        "tail",
        help="Tail the canonical audit log (optionally filtered by peer)",
    )
    tail_p.add_argument(
        "--peer", default=None,
        help="Filter audit entries to a specific peer (e.g. --peer kal-le)",
    )
    tail_p.add_argument(
        "--limit", type=int, default=50,
        help="Number of entries to show (default: 50)",
    )
    tail_p.add_argument("--json", action="store_true", default=False)

    # Propose-person c3: manual test trigger for the KAL-LE-side
    # canonical-person resolver. Runs
    # :func:`alfred.transport.client.resolve_or_propose_canonical_person`
    # against a named peer (typically ``salem``) so operators can exercise
    # the GET-then-propose flow without wiring up a full LLM tool call.
    # TODO: replace once KAL-LE's conversation loop has a native
    # canonical-person call site — this subcommand is the interim
    # reachable trigger point.
    propose_p = t_sub.add_parser(
        "propose-person",
        help="Query-or-propose a canonical person on a peer (c3 test trigger)",
    )
    propose_p.add_argument("peer", help="Peer name (e.g. salem)")
    propose_p.add_argument("name", help="Canonical person name")
    propose_p.add_argument(
        "--field",
        action="append",
        default=[],
        help=(
            "key=value field to propose (repeatable). "
            "e.g. --field description='NP colleague'"
        ),
    )
    propose_p.add_argument(
        "--source", default="",
        help="Free-text source attribution for the proposal audit trail",
    )
    propose_p.add_argument(
        "--self", dest="self_name", default="kal-le",
        help="This instance's name (default: kal-le)",
    )
    propose_p.add_argument("--json", action="store_true", default=False)

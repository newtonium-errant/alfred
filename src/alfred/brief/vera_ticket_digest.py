"""VERA ticket digest assembler — open-ticket snapshot for Salem's brief.

VERA P2 (2026-06-09, project_vera_ops_assistant.md). VERA files RRTS
website tickets (Ben's bug reports + enhancement ideas) into its own
vault's ``ticket/`` directory. Each morning at 05:45 ADT — 15 minutes
before Salem's 06:00 brief — the brief-digest pusher daemon
(``kalle_brief_daemon`` with ``source: tickets``) calls
:func:`assemble_ticket_digest` to render a one-slide snapshot of every
OPEN ticket, pushes it to Salem's ``/peer/brief_digest`` endpoint, and
Salem's brief renders it under ``### VERA Update``.

Snapshot semantics (operator-ratified 2026-06-09): each morning's digest
is a SNAPSHOT of all tickets whose ``status`` is ``open`` or
``in_progress``, newest-first. NO high-water-mark, NO "new since last
digest" state — a ticket re-surfaces every morning until Ben/Andrew
flips it to ``resolved`` / ``closed`` / ``wont_fix``. This is the
"re-surface until resolved" behaviour: the brief is a live worklist, not
a one-shot notification. Empty set → an intentionally-left-blank digest
("No open tickets.") so Salem renders "ran, nothing open" rather than an
ambiguous absence (per ``feedback_intentionally_left_blank.md``).

Distinct from ``kalle_digest`` (git-activity/BIT posture). The pusher
daemon selects between them via ``brief_digest_push.source`` — KAL-LE's
``git_activity`` default stays byte-identical; VERA sets ``tickets``.

Forward-status tails (pipeline c5): each rendered open-ticket line ends
with its VERA→KAL-LE→GitHub pipeline status — ``→ GH#<n>`` once linked,
``· forward pending`` before the forwarder reaches it, or the loud
``· forward FAILED ×<attempts> (retrying)`` when the forwarder state
shows repeated attempts without a linked issue. Everything else about
the snapshot (selection, ordering, header/footer, the "No open
tickets" ILB line) is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import frontmatter

from .utils import get_logger

log = get_logger(__name__)


# Ticket statuses that count as OPEN (surface in the digest). Mirrors the
# ``ticket`` TypeDefinition's status set in ``vault/schema.py`` minus the
# terminal states (resolved / closed / wont_fix). Kept as a module
# constant so the digest selection + any future caller share one source
# of truth.
OPEN_TICKET_STATUSES: frozenset[str] = frozenset({"open", "in_progress"})


@dataclass
class TicketDigestItem:
    """One open ticket's digest-relevant fields.

    Sorting key is ``created`` (ISO string) for newest-first ordering;
    ``filename`` breaks ties deterministically so a same-day batch of
    tickets renders in a stable order between brief runs.
    """

    title: str
    ticket_type: str
    priority: str
    status: str
    created: str
    filename: str
    # Pipeline fields (c5 forward-status tails). ``ticket_uid`` is the
    # forwarder-state join key; ``github_issue`` is the c4 link-back.
    # Defaults keep pre-pipeline records valid.
    ticket_uid: str = ""
    github_issue: int | None = None


def _scan_open_tickets(vault_path: Path) -> list[TicketDigestItem]:
    """Scan ``<vault>/ticket/`` for open tickets, newest-first.

    Reads every ``*.md`` under ``ticket/``, parses frontmatter
    defensively (a single malformed record logs + is skipped rather
    than failing the whole digest — mirrors ``peer_digests._scan_peer_
    digests``), and selects records whose ``status`` is in
    :data:`OPEN_TICKET_STATUSES`.

    Returns the items sorted newest-first by ``created`` (ISO string),
    with ``filename`` as the deterministic tie-breaker. An absent
    ``ticket/`` directory returns an empty list (the empty-snapshot case
    the caller renders as the intentionally-left-blank digest).
    """
    ticket_dir = vault_path / "ticket"
    if not ticket_dir.exists():
        log.info("vera.ticket_digest.no_ticket_dir", vault_path=str(vault_path))
        return []

    out: list[TicketDigestItem] = []
    for md_file in sorted(ticket_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except Exception as exc:  # noqa: BLE001 — one bad record never fails the digest
            log.warning(
                "vera.ticket_digest.parse_failed",
                path=str(md_file),
                error=str(exc),
            )
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "ticket":
            continue
        status = str(fm.get("status") or "")
        if status not in OPEN_TICKET_STATUSES:
            continue
        # ``created`` may parse as a date object (PyYAML) or a string —
        # normalise to ISO string for sorting + display.
        created = fm.get("created")
        created_iso = str(created) if created is not None else ""
        # ``title`` is the ticket name_field; fall back to the file stem
        # so a record missing the field still renders something usable.
        title = str(fm.get("title") or md_file.stem)
        # Pipeline fields — defensively typed (frontmatter is operator/
        # agent-written; a stray string github_issue must not crash the
        # digest).
        uid_raw = fm.get("ticket_uid")
        ticket_uid = uid_raw if isinstance(uid_raw, str) else ""
        gh_raw = fm.get("github_issue")
        github_issue: int | None = None
        if gh_raw is not None and not isinstance(gh_raw, bool):
            try:
                github_issue = int(gh_raw)
            except (TypeError, ValueError):
                github_issue = None
        out.append(
            TicketDigestItem(
                title=title,
                ticket_type=str(fm.get("ticket_type") or "ticket"),
                priority=str(fm.get("priority") or "unset"),
                status=status,
                created=created_iso,
                filename=md_file.name,
                ticket_uid=ticket_uid,
                github_issue=github_issue,
            )
        )

    # Newest-first by created date; filename breaks ties deterministically.
    out.sort(key=lambda t: (t.created, t.filename), reverse=True)
    return out


def _forward_status_tail(
    item: TicketDigestItem,
    forward_entries: dict[str, Any],
) -> str:
    """One ticket's pipeline forward-status tail (c5).

    Precedence:
      1. The record carries ``github_issue`` (the c4 link-back) →
         ``→ GH#<n>``.
      2. The forwarder state knows an issue for this uid but the
         record doesn't (link-back write failed —
         ``ticket_forward.link_back_write_failed``) → ``→ GH#<n>``
         from state, so a one-off write failure never renders as a
         permanent misleading "pending".
      3. State shows attempts >= 2 without an issue → the loud
         ``· forward FAILED ×<attempts> (retrying)``.
      4. Everything else (no state file, no entry yet, first attempt
         in flight) → ``· forward pending``.
    """
    if item.github_issue is not None:
        return f" → GH#{item.github_issue}"
    entry = forward_entries.get(item.ticket_uid) if item.ticket_uid else None
    if entry is not None:
        issue_number = getattr(entry, "issue_number", None)
        if issue_number is not None:
            return f" → GH#{issue_number}"
        attempts = getattr(entry, "attempts", 0) or 0
        if attempts >= 2:
            return f" · forward FAILED ×{attempts} (retrying)"
    return " · forward pending"


def _render_ticket_digest_markdown(
    items: list[TicketDigestItem],
    today: date,
    forward_entries: dict[str, Any] | None = None,
) -> str:
    """Render the open-ticket snapshot as plain-text markdown.

    Format (plain text, no emoji per operator decision F), each line
    ending with its pipeline forward-status tail (c5):

        3 open tickets (as of 2026-06-09):

        - bug · Login button broken on checkout · priority: high · open → GH#7
        - enhancement · Add CSV export to driver report · priority: medium · open · forward pending
        - bug · Map pin misaligned on mobile · priority: low · in_progress · forward FAILED ×2 (retrying)

        Review in Obsidian: ticket/ in the VERA vault.

    Empty set → the intentionally-left-blank body so Salem's brief
    renders "ran, nothing open" rather than an ambiguous blank section.
    """
    today_iso = today.isoformat()
    if not items:
        return f"No open tickets (as of {today_iso})."

    count = len(items)
    noun = "ticket" if count == 1 else "tickets"
    lines = [f"{count} open {noun} (as of {today_iso}):", ""]
    for item in items:
        lines.append(
            f"- {item.ticket_type} · {item.title} · "
            f"priority: {item.priority} · {item.status}"
            f"{_forward_status_tail(item, forward_entries or {})}"
        )
    lines.append("")
    lines.append("Review in Obsidian: ticket/ in the VERA vault.")
    return "\n".join(lines)


def assemble_ticket_digest(
    *,
    today: date | None = None,
    vault_path: Path,
    forward_state_path: str | Path | None = None,
) -> str:
    """Build today's open-ticket snapshot digest markdown.

    The VERA-side counterpart to ``kalle_digest.assemble_digest``. The
    brief-digest pusher daemon calls this when
    ``brief_digest_push.source == "tickets"``; the result is pushed to
    Salem's ``/peer/brief_digest`` and rendered under ``### VERA Update``.

    Args:
        today: Date to anchor the digest header. ``None`` resolves to
            ``date.today()`` — the daemon passes its tz-aware local date.
        vault_path: VERA's vault root (holds the ``ticket/`` directory).
        forward_state_path: The c4 forwarder's state file (from the
            ``ticket_forward`` config section) for the forward-status
            tails. ``None`` or an absent file degrades to "forward
            pending" tails — never a crash (the loader treats a missing
            file as an empty state).

    Returns:
        The one-slide markdown body. Never empty — the empty-snapshot
        case returns the intentionally-left-blank "No open tickets" line.
    """
    today = today or date.today()
    items = _scan_open_tickets(vault_path)
    forward_entries: dict[str, Any] = {}
    if forward_state_path:
        from alfred.transport.ticket_forward import TicketForwardState
        forward_entries = TicketForwardState.load(forward_state_path).entries
    log.info(
        "vera.ticket_digest.assembled",
        date=today.isoformat(),
        open_count=len(items),
    )
    return _render_ticket_digest_markdown(items, today, forward_entries)

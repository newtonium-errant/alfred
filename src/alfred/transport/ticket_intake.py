"""KAL-LE ticket-intake config + state (pipeline c3).

The receiving half of the ratified VERA→KAL-LE→GitHub ticket pipeline
(2026-06-11, R1/R3): VERA pushes ``kind=ticket`` over the peer
protocol → KAL-LE records the ticket in its vault (backlog keeper)
and posts the GitHub issue with the auto-fix label — ALL deterministic
(no LLM anywhere in this path). The sync ack carries the outcome;
GitHub-down means record-then-pending-ack, and VERA's re-push is the
single retry mechanism (no separate retry daemon, no back-channel).

This module holds the intake's CONFIG + STATE only. The HTTP handler
lives in :mod:`alfred.transport.peer_handlers` (the ``kind=ticket``
in-transport branch), wired at daemon startup via
``register_ticket_intake``.

Config home decision (c3): ``labels`` + ``label_map`` live on the
``github:`` section (where c1 put them, on
:class:`alfred.integrations.github_ops.GitHubOpsConfig`) — ONE home,
no duplication. The ``ticket_intake:`` section carries only
``enabled`` + ``state.path``.

State semantics: one entry per ``ticket_uid``. The state file is
deletable bookkeeping (per CLAUDE.md) — the intake handler's
marker-search guard (``issue_search_marker``) recovers issue linkage
after a state deletion so duplicates are never minted. The
effectiveness-loop fields (``pr_number`` / ``pr_state`` /
``disposition`` / ``ticket_to_pr_latency_days`` /
``outcome_checked_at``) are FILLED by the c5 digest loop
(``kalle_digest.assemble_ticket_pipeline_section``) — c3 writes them
as defaults and never touches them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# Tool-scoped default per the CLAUDE.md state-path rule — sharing a
# generic ``state.json`` default across tools lets one tool silently
# load another's state file.
DEFAULT_TICKET_INTAKE_STATE_PATH = "./data/ticket_intake_state.json"

# Wire-format pin for ``payload.ticket_uid``. The uid flows verbatim
# into three injection-sensitive sinks: the GitHub search phrase
# (``issue_search_marker`` quotes it into ``q=``), the HTML dedupe
# marker comment (``issue_marker``), and the uid-suffixed vault record
# filename (the title-collision path in ``_record_kalle_ticket``).
# Constraining to a filename- and search-safe charset at the schema
# gate keeps all three sinks clean. The forwarder's minted shape
# (``vera-YYYYMMDD-<8 hex>``) conforms — pinned in
# ``tests/test_ticket_forward.py``'s mint-stability test.
TICKET_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class TicketIntakeConfig:
    """Typed view of the ``ticket_intake:`` config section (KAL-LE-only).

    Deliberately tiny — the GitHub side (repo / pat / labels /
    label_map / audit log) all lives on the ``github:`` section per the
    one-home decision in the module docstring.
    """

    enabled: bool = False
    state_path: str = DEFAULT_TICKET_INTAKE_STATE_PATH


def load_ticket_intake_config(raw: dict[str, Any]) -> TicketIntakeConfig:
    """Build :class:`TicketIntakeConfig` from the unified config dict.

    Tolerant defaults: an absent / malformed section returns the
    disabled default config (the daemon then skips registration and
    ``kind=ticket`` answers 501).
    """
    section = raw.get("ticket_intake") or {}
    if not isinstance(section, dict):
        return TicketIntakeConfig()

    state_raw = section.get("state") or {}
    state_path = ""
    if isinstance(state_raw, dict):
        state_path = str(state_raw.get("path", "") or "")

    return TicketIntakeConfig(
        enabled=bool(section.get("enabled", False)),
        state_path=state_path or DEFAULT_TICKET_INTAKE_STATE_PATH,
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class TicketIntakeEntry:
    """Per-ticket intake bookkeeping, keyed by ``ticket_uid``.

    Lifecycle written by c3:
      * ``recorded_at`` / ``kalle_relpath`` — set the moment the vault
        record lands (BEFORE the GitHub post, so a crash between
        record and post resumes in the pending-retry path rather than
        duplicate-recording).
      * ``issue_number`` / ``issue_url`` / ``issue_created_at`` — set
        when the GitHub issue is created (or adopted via the marker
        search). ``issue_number is None`` == issue still pending.
      * ``retry_count`` — incremented on every GitHub failure that
        acked ``recorded_issue_pending``.
      * ``ticket_type`` — the wire frontmatter's ``ticket_type``
        (schema-gated non-empty by the handler), captured at record
        time as the c5 scoreboard's split key. Additive +
        backward-safe via the schema-tolerance loader: entries
        recorded before this field existed carry ``""`` and bucket
        under "unspecified" in the digest scoreboard.

    Filled by c5 (the digest effectiveness loop,
    ``kalle_digest.assemble_ticket_pipeline_section``):
      * ``pr_number`` / ``pr_state`` — the linked PR, once one exists.
      * ``disposition`` — ``""`` until set; vocabulary
        ``merged_clean | merged_after_rework | closed_unmerged |
        stalled`` (terminal = the merged_*/closed_unmerged three;
        ``stalled`` keeps re-checking).
      * ``ticket_to_pr_latency_days`` — float days from issue creation
        to PR merge (merged dispositions only).
      * ``outcome_checked_at`` — last time c5 evaluated this entry.
    """

    recorded_at: str = ""
    kalle_relpath: str = ""
    issue_number: int | None = None
    issue_url: str = ""
    issue_created_at: str = ""
    retry_count: int = 0
    ticket_type: str = ""
    # --- c5 effectiveness-loop capture fields (reserved; c3 never writes) ---
    pr_number: int | None = None
    pr_state: str = ""
    disposition: str = ""
    ticket_to_pr_latency_days: float | None = None
    outcome_checked_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "TicketIntakeEntry":
        """Load-time schema-tolerance contract (per CLAUDE.md)."""
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TicketIntakeState:
    """Intake state — ``ticket_uid`` → :class:`TicketIntakeEntry`.

    JSON file shape::

        {"entries": {"<ticket_uid>": {...entry fields...}, ...}}

    Atomic save (``.tmp`` → rename). Load is defensive: a missing
    file is an empty state; a corrupt file logs + starts empty (the
    marker-search guard in the handler recovers issue linkage, so a
    lost state file degrades to extra GitHub searches, never to
    duplicate issues).
    """

    path: Path
    entries: dict[str, TicketIntakeEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TicketIntakeState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "ticket_intake.state_load_failed",
                path=str(p),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return cls(path=p)
        entries_raw = data.get("entries") if isinstance(data, dict) else None
        entries: dict[str, TicketIntakeEntry] = {}
        if isinstance(entries_raw, dict):
            for uid, entry_data in entries_raw.items():
                if isinstance(entry_data, dict):
                    entries[str(uid)] = TicketIntakeEntry.from_dict(entry_data)
        return cls(path=p, entries=entries)

    def save(self) -> None:
        """Atomic write — ``.tmp`` then ``os.replace`` rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {
                uid: entry.to_dict() for uid, entry in self.entries.items()
            },
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, self.path)


__all__ = [
    "DEFAULT_TICKET_INTAKE_STATE_PATH",
    "TICKET_UID_RE",
    "TicketIntakeConfig",
    "TicketIntakeEntry",
    "TicketIntakeState",
    "load_ticket_intake_config",
]

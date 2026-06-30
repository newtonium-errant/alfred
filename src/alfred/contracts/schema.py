"""Contract schema + the pure state-machine helpers.

The contract is a SINGLE canonical frontmatter+markdown file (NOT a vault
``KNOWN_TYPES`` record). Frontmatter = machine state; body = the
human-readable interface spec. All dataclasses carry the ``from_dict``
schema-tolerance filter — AND per-LIST-item ``from_dict`` for the embedded
``participants`` / ``division_of_labor`` / ``history`` lists (a contract
written by a newer build with extra per-item fields must still load on an
older build; top-level filtering alone misses the nested lists).

Every helper here is PURE + unit-testable without the bus or the store.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any


# The six negotiation kinds the bus router hands to ``contracts.router``
# instead of plain inbox-routing.
CONTRACT_KINDS: frozenset[str] = frozenset(
    {"propose", "counter", "accept", "ratify", "reject", "block"},
)

# Authority split: these are OPERATOR-ONLY — an agent emitting them is
# fail-closed rejected (the CLI invocation IS the operator authority).
_OPERATOR_ONLY_KINDS: frozenset[str] = frozenset({"ratify", "reject"})

# Contract states.
STATE_DRAFT = "draft"
STATE_PROPOSED = "proposed"
STATE_COUNTERED = "countered"
STATE_RATIFIED = "ratified"
STATE_BLOCKED = "blocked"
STATE_SUPERSEDED = "superseded"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Participant:
    """One side of the seam. ``accepted_version`` is the last contract
    version this agent ``accept``ed (None = not yet) — convergence = all
    participants accepted the CURRENT version."""

    project: str = ""
    agent: str = ""
    role: str = ""  # producer | consumer
    accepted_version: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Participant":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DivisionItem:
    """One who-builds-what row. ``owner == ""`` is a GAP (nobody owns it),
    surfaced at agreement-time (the exact failure this kills)."""

    item: str = ""
    owner: str = ""  # project slug; "" => GAP
    status: str = "todo"  # todo | building | built

    @classmethod
    def from_dict(cls, data: dict) -> "DivisionItem":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Transition:
    """One operator-facing history row + forensic audit mirror row."""

    ts: str = ""
    from_state: str = ""
    to_state: str = ""
    actor: str = ""
    kind: str = ""
    correlation_id: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Transition":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Contract:
    """The versioned contract artifact (one per seam)."""

    contract_id: str = ""
    seam: str = ""
    state: str = STATE_DRAFT
    version: int = 1
    participants: list[Participant] = field(default_factory=list)
    interface: dict = field(default_factory=dict)
    division_of_labor: list[DivisionItem] = field(default_factory=list)
    history: list[Transition] = field(default_factory=list)
    thread_correlation_id: str = ""
    created: str = ""
    updated: str = ""
    ratified_at: str = ""
    ratified_by: str = ""
    supersedes: str = ""
    superseded_by: str = ""
    blocked_reason: str = ""
    body: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Contract":
        """Top-level schema-tolerance + PER-LIST-ITEM tolerance for the
        embedded participants / division_of_labor / history lists."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["participants"] = [
            Participant.from_dict(p)
            for p in (data.get("participants") or [])
            if isinstance(p, dict)
        ]
        known["division_of_labor"] = [
            DivisionItem.from_dict(d)
            for d in (data.get("division_of_labor") or [])
            if isinstance(d, dict)
        ]
        known["history"] = [
            Transition.from_dict(t)
            for t in (data.get("history") or [])
            if isinstance(t, dict)
        ]
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        """Frontmatter dict (everything except the markdown ``body``)."""
        d = asdict(self)
        d.pop("body", None)
        return d


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _slug(seam: str) -> str:
    s = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(seam))
    s = "-".join(part for part in s.split("-") if part)
    return s[:40] or "seam"


def mint_contract_id(seam: str, created: str) -> str:
    """Readable + stable id — ``contract-<seam-slug>-<sha6>``.

    Pure + pinned-stable by test (mirrors ``mint_ticket_uid``)."""
    digest = hashlib.sha256(
        f"{seam}|{created}".encode("utf-8"),
    ).hexdigest()[:6]
    return f"contract-{_slug(seam)}-{digest}"


# The legal (from_state, kind) → to_state table (state dimension only;
# authority is layered in :func:`legal_transition`). ``accept`` is a no-op
# on state (sets accepted_version in the store) so it maps to itself.
LEGAL_TRANSITIONS: dict[tuple[str, str], str] = {
    (STATE_DRAFT, "propose"): STATE_PROPOSED,
    (STATE_PROPOSED, "counter"): STATE_COUNTERED,
    (STATE_COUNTERED, "counter"): STATE_COUNTERED,
    (STATE_COUNTERED, "propose"): STATE_PROPOSED,   # clean re-propose
    (STATE_BLOCKED, "counter"): STATE_COUNTERED,    # resolve a block
    (STATE_PROPOSED, "accept"): STATE_PROPOSED,     # unchanged (sets ver)
    (STATE_COUNTERED, "accept"): STATE_COUNTERED,   # unchanged (sets ver)
    (STATE_PROPOSED, "ratify"): STATE_RATIFIED,
    (STATE_COUNTERED, "ratify"): STATE_RATIFIED,
    (STATE_PROPOSED, "reject"): STATE_COUNTERED,
    (STATE_COUNTERED, "reject"): STATE_COUNTERED,
    (STATE_PROPOSED, "block"): STATE_BLOCKED,
    (STATE_COUNTERED, "block"): STATE_BLOCKED,
    (STATE_RATIFIED, "block"): STATE_BLOCKED,
}


def legal_transition(
    from_state: str, kind: str, actor_is_operator: bool
) -> tuple[bool, str, str]:
    """Return ``(ok, to_state, reason)``.

    Authority is enforced FIRST: ``ratify``/``reject`` are operator-only —
    an agent attempting one is fail-closed rejected (the load-bearing
    guard). Then the state dimension is consulted via
    :data:`LEGAL_TRANSITIONS`."""
    if kind in _OPERATOR_ONLY_KINDS and not actor_is_operator:
        return (
            False, from_state,
            f"{kind} is operator-only — an agent has no authority to {kind}",
        )
    key = (from_state, kind)
    if key in LEGAL_TRANSITIONS:
        return True, LEGAL_TRANSITIONS[key], ""
    return False, from_state, f"illegal transition: {kind} from {from_state}"


def is_buildable(c: Contract) -> bool:
    """THE field agents check before pouring concrete on a shared seam:
    the contract is ratified and not superseded."""
    return c.state == STATE_RATIFIED and not c.superseded_by


def is_converged(c: Contract) -> bool:
    """Every participant has accepted the CURRENT version. An empty
    participants list is NOT converged (nothing has agreed)."""
    if not c.participants:
        return False
    return all(p.accepted_version == c.version for p in c.participants)


def find_gaps(c: Contract) -> list[DivisionItem]:
    """Division-of-labor rows nobody owns (``owner == ""``) — surfaced at
    agreement-time, not integration-time."""
    return [d for d in c.division_of_labor if not d.owner]


def find_overlaps(c: Contract) -> list[tuple[str, list[str]]]:
    """Items claimed by MORE THAN ONE distinct owner. Returns
    ``[(item, [owners...])]`` for each over-claimed row."""
    by_item: dict[str, list[str]] = {}
    for d in c.division_of_labor:
        if d.owner:
            by_item.setdefault(d.item, [])
            if d.owner not in by_item[d.item]:
                by_item[d.item].append(d.owner)
    return [(item, owners) for item, owners in by_item.items() if len(owners) > 1]


__all__ = [
    "CONTRACT_KINDS",
    "Contract",
    "DivisionItem",
    "LEGAL_TRANSITIONS",
    "Participant",
    "STATE_BLOCKED",
    "STATE_COUNTERED",
    "STATE_DRAFT",
    "STATE_PROPOSED",
    "STATE_RATIFIED",
    "STATE_SUPERSEDED",
    "Transition",
    "find_gaps",
    "find_overlaps",
    "is_buildable",
    "is_converged",
    "legal_transition",
    "mint_contract_id",
]

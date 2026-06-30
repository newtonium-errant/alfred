"""Contract store + the single deterministic ``apply_message`` apply point.

SERIAL-APPLY assumption: ``apply_message`` rewrites the whole contract
file; this is safe ONLY because the KAL-LE routing daemon is single-process
serial (like ``ticket_forward``). If routing ever parallelizes, contract
writes need a lock — documented here loudly.

``apply_message`` is the ONE place that validates kind + actor + transition
(via :func:`legal_transition`), mutates the contract, appends a
:class:`Transition` to ``history`` AND mirrors it to the append-only
``contract_audit.jsonl``, then atomic-saves. It returns an
:class:`ApplyResult` the router uses to decide notices + the operator ping.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import structlog
import yaml

from .audit import append_contract_audit
from .schema import (
    CONTRACT_KINDS,
    STATE_BLOCKED,
    STATE_COUNTERED,
    STATE_DRAFT,
    STATE_PROPOSED,
    STATE_RATIFIED,
    Contract,
    DivisionItem,
    Participant,
    Transition,
    is_converged,
    legal_transition,
    mint_contract_id,
)

log = structlog.get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ApplyResult:
    """Outcome of one :meth:`ContractStore.apply_message`. The router reads
    ``converged`` / ``contract.state`` to decide the counterparty notice +
    the operator ping; ``ok=False`` carries the rejection ``reason``."""

    ok: bool
    contract: Contract | None = None
    reason: str = ""
    converged: bool = False
    new_contract: bool = False
    from_state: str = ""
    to_state: str = ""
    kind: str = ""
    actor: str = ""


def _parse_payload(body: str) -> dict[str, Any]:
    """Parse the message body as a YAML contract payload (interface /
    division_of_labor / participants / claims / blocked_reason / note).
    A non-mapping or unparseable body → empty payload (the message still
    drives the state transition; it just carries no structured update)."""
    try:
        data = yaml.safe_load(body or "")
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_participants(payload: dict[str, Any]) -> list[Participant]:
    out: list[Participant] = []
    for p in payload.get("participants") or []:
        if isinstance(p, dict):
            out.append(Participant.from_dict(p))
    return out


def _parse_dol(payload: dict[str, Any]) -> list[DivisionItem]:
    out: list[DivisionItem] = []
    for d in payload.get("division_of_labor") or []:
        if isinstance(d, dict):
            out.append(DivisionItem.from_dict(d))
    return out


class ContractStore:
    """``.md``-per-contract store under ``store_dir``."""

    def __init__(self, store_dir: str | Path, audit_path: str | Path = "") -> None:
        self.store_dir = Path(store_dir)
        self.audit_path = str(audit_path) or str(
            self.store_dir / "contract_audit.jsonl"
        )

    def _path(self, contract_id: str) -> Path:
        return self.store_dir / f"{contract_id}.md"

    def load(self, contract_id: str) -> Contract | None:
        p = self._path(contract_id)
        if not p.exists():
            return None
        post = frontmatter.load(str(p))
        data = dict(post.metadata or {})
        data["body"] = post.content or ""
        return Contract.from_dict(data)

    def save(self, contract: Contract) -> None:
        """Atomic write — frontmatter.dumps + ``.tmp`` → ``os.replace``."""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(contract.body or "", **contract.to_dict())
        rendered = frontmatter.dumps(post)
        p = self._path(contract.contract_id)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp, p)

    def iter_contracts(self) -> list[Contract]:
        if not self.store_dir.exists():
            return []
        out: list[Contract] = []
        for f in sorted(self.store_dir.glob("*.md")):
            try:
                c = self.load(f.stem)
            except Exception as exc:  # noqa: BLE001 — one bad file never kills the list
                log.warning(
                    "contracts.load_failed",
                    path=str(f),
                    error_type=exc.__class__.__name__,
                )
                continue
            if c is not None:
                out.append(c)
        return out

    def list_awaiting(self) -> list[Contract]:
        """Contracts needing the operator: converged-but-unratified OR
        blocked. The brief's awaiting-section batch source."""
        out: list[Contract] = []
        for c in self.iter_contracts():
            if c.state == STATE_BLOCKED:
                out.append(c)
            elif (
                is_converged(c)
                and c.state in (STATE_PROPOSED, STATE_COUNTERED)
            ):
                out.append(c)
        return out

    def _audit(
        self, c: Contract, kind: str, from_state: str, to_state: str,
        actor: str, outcome: str, correlation_id: str, reason: str,
    ) -> None:
        append_contract_audit(
            self.audit_path,
            contract_id=c.contract_id,
            kind=kind,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            outcome=outcome,
            version=c.version,
            correlation_id=correlation_id,
            reason=reason,
        )

    def apply_message(
        self,
        msg_fm: dict[str, Any],
        body: str,
        *,
        actor: str,
        actor_is_operator: bool,
    ) -> ApplyResult:
        """The single deterministic apply point (see module docstring)."""
        kind = str(msg_fm.get("kind") or "")
        if kind not in CONTRACT_KINDS:
            return ApplyResult(False, reason=f"not a contract kind: {kind!r}")
        contract_id = str(msg_fm.get("contract_id") or "")
        correlation_id = str(msg_fm.get("correlation_id") or "")
        payload = _parse_payload(body)

        # NEW contract (first propose, no contract_id) — the proposer
        # self-declares the participants, so no participant-authority check.
        if kind == "propose" and not contract_id:
            seam = str(msg_fm.get("seam") or "")
            if not seam:
                return ApplyResult(
                    False,
                    reason="a first propose (no contract_id) requires a seam",
                )
            created = str(msg_fm.get("created") or _now_iso())
            contract_id = mint_contract_id(seam, created)
            c = Contract(
                contract_id=contract_id, seam=seam, state=STATE_DRAFT,
                version=1, created=created,
                thread_correlation_id=correlation_id,
            )
            c.participants = _parse_participants(payload)
            new_contract = True
        else:
            if not contract_id:
                return ApplyResult(
                    False, reason=f"{kind} requires a contract_id",
                )
            loaded = self.load(contract_id)
            if loaded is None:
                return ApplyResult(
                    False, reason=f"contract not found: {contract_id}",
                )
            c = loaded
            new_contract = False

        from_state = c.state
        ok, to_state, reason = legal_transition(
            from_state, kind, actor_is_operator,
        )
        if not ok:
            # Fail-closed (incl. illegal-ratify-by-agent) — audited.
            self._audit(
                c, kind, from_state, from_state, actor, "rejected",
                correlation_id, reason,
            )
            log.warning(
                "contracts.transition_rejected",
                contract_id=c.contract_id, kind=kind,
                from_state=from_state, actor=actor, reason=reason,
            )
            return ApplyResult(
                False, contract=c, reason=reason,
                from_state=from_state, kind=kind, actor=actor,
            )

        # Authority for agent kinds: the actor must be a listed participant.
        # ``propose`` is included so a NON-PARTICIPANT cannot re-propose on an
        # EXISTING contract (which would bump the version + overwrite the
        # interface/division_of_labor = term injection / convergence reset).
        # ``not new_contract`` exempts the FIRST propose only — a genuinely
        # new contract has no participants yet, so the proposer self-declares
        # (counter/accept/block always operate on an existing contract, so
        # ``not new_contract`` is always True for them — no behavior change).
        if (
            not new_contract
            and kind in ("counter", "accept", "block", "propose")
            and not actor_is_operator
        ):
            actor_project = actor.split("/")[0]
            if actor_project not in {p.project for p in c.participants}:
                reason = f"actor {actor!r} is not a contract participant"
                self._audit(
                    c, kind, from_state, from_state, actor, "rejected",
                    correlation_id, reason,
                )
                log.warning(
                    "contracts.authority_rejected",
                    contract_id=c.contract_id, kind=kind, actor=actor,
                )
                return ApplyResult(
                    False, contract=c, reason=reason,
                    from_state=from_state, kind=kind, actor=actor,
                )

        # --- apply the kind's effect ---
        bumped = False
        if kind == "counter" or (kind == "propose" and from_state == STATE_COUNTERED):
            # An interface mutation bumps the version (stales prior accepts).
            c.version += 1
            bumped = True
            self._apply_interface_and_dol(c, payload)
        elif kind == "propose":  # first propose (draft → proposed), version 1
            self._apply_interface_and_dol(c, payload)
        elif kind == "accept":
            # Assent to the CURRENT version + (optionally) claim a GAP row.
            # A gap-fill does NOT bump the version (it splits labor within
            # the already-agreed interface, not the terms).
            self._apply_claims(c, payload, actor)
            self._set_accepted_version(c, actor)
        elif kind == "block":
            c.blocked_reason = str(
                payload.get("blocked_reason")
                or msg_fm.get("blocked_reason") or "",
            )
        elif kind == "ratify":
            c.ratified_at = _now_iso()
            c.ratified_by = actor
        elif kind == "reject":
            pass  # the reason is recorded in the history note below

        c.state = to_state
        c.updated = _now_iso()
        note = str(payload.get("note") or msg_fm.get("note") or "")
        c.history.append(Transition(
            ts=c.updated, from_state=from_state, to_state=to_state,
            actor=actor, kind=kind, correlation_id=correlation_id, note=note,
        ))
        self.save(c)
        self._audit(
            c, kind, from_state, to_state, actor, "applied",
            correlation_id, note,
        )
        log.info(
            "contracts.applied",
            contract_id=c.contract_id, kind=kind,
            from_state=from_state, to_state=to_state,
            version=c.version, version_bumped=bumped, actor=actor,
        )
        return ApplyResult(
            True, contract=c, converged=is_converged(c),
            new_contract=new_contract, from_state=from_state,
            to_state=to_state, kind=kind, actor=actor,
        )

    # --- effect helpers ---

    @staticmethod
    def _apply_interface_and_dol(c: Contract, payload: dict[str, Any]) -> None:
        if isinstance(payload.get("interface"), dict):
            c.interface = payload["interface"]
        if "division_of_labor" in payload:
            c.division_of_labor = _parse_dol(payload)
        if payload.get("participants"):
            # Additive: a counter may introduce a participant; existing ones
            # keep their accepted_version (the version bump stales it anyway).
            existing = {p.project for p in c.participants}
            for p in _parse_participants(payload):
                if p.project not in existing:
                    c.participants.append(p)

    @staticmethod
    def _apply_claims(c: Contract, payload: dict[str, Any], actor: str) -> None:
        """An ``accept`` may CLAIM gap rows: set owner=actor on listed items
        that are currently unowned. Never reassigns an owned row."""
        claims = payload.get("claims") or []
        if not isinstance(claims, list):
            return
        actor_project = actor.split("/")[0]
        wanted = {str(c0) for c0 in claims}
        for d in c.division_of_labor:
            if d.item in wanted and not d.owner:
                d.owner = actor_project

    @staticmethod
    def _set_accepted_version(c: Contract, actor: str) -> None:
        actor_project = actor.split("/")[0]
        for p in c.participants:
            if p.project == actor_project:
                p.accepted_version = c.version
                return


__all__ = ["ApplyResult", "ContractStore"]

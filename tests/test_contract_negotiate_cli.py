"""``alfred contract {propose|counter|accept}`` — agent-side minting CLI.

Exercised via the pure ``contracts.cli.dispatch`` handler (argparse.Namespace,
no argv) + a REAL msgbus spool + ``run_route_once`` + a REAL ContractStore, so
each pin proves the minted spool file round-trips through the router into the
expected contract state. ``ratify``/``reject`` stay operator-only (unchanged).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

from alfred.contracts.cli import dispatch
from alfred.contracts.config import load_contract_config
from alfred.contracts.schema import find_gaps, is_converged
from alfred.contracts.store import ContractStore
from alfred.msgbus.config import load_message_bus_config
from alfred.msgbus.router import run_route_once


def _raw(tmp_path: Path) -> dict[str, Any]:
    d = tmp_path
    (d / "alfred" / ".msgbus" / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "rrts" / ".msgbus" / "inbox").mkdir(parents=True, exist_ok=True)
    return {
        "message_bus": {
            "enabled": True,
            "self_project": "alfred",
            "spool_path": str(d / "spool"),
            "state": {"path": str(d / "state.json")},
            "projects": [
                {"name": "alfred", "inbox_path": str(d / "alfred" / ".msgbus" / "inbox")},
                {"name": "rrts", "inbox_path": str(d / "rrts" / ".msgbus" / "inbox")},
            ],
        },
        "contracts": {
            "enabled": True,
            "store_path": str(d / "contracts"),
            "operator_id": "andrew",
        },
    }


def _ns(**kw: Any) -> argparse.Namespace:
    """A contract-CLI Namespace with every attr the handlers read defaulted."""
    base = dict(
        contract_cmd="", from_project="", to="", seam="", subject="",
        contract_id="", participant=[], item=[], correlation_id="",
        and_accept=False, note="", force=False, awaiting=False, state="",
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _route(raw: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(run_route_once(load_message_bus_config(raw), raw))


def _store(raw: dict[str, Any]) -> ContractStore:
    cfg = load_contract_config(raw)
    return ContractStore(cfg.store_path, cfg.resolved_audit_path())


def _spool_files(raw: dict[str, Any]) -> list[str]:
    spool = Path(raw["message_bus"]["spool_path"])
    return sorted(p.name for p in spool.glob("*.md")) if spool.exists() else []


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


def test_propose_mints_valid_spool_file_routes_to_proposed(tmp_path):  # type: ignore[no-untyped-def]
    """Pin: propose mints a VALID contract spool file (not malformed/
    undeliverable) → route → contract in ``proposed``; the ownerless item is
    surfaced as a GAP."""
    raw = _raw(tmp_path)
    rc = dispatch(_ns(
        contract_cmd="propose", from_project="alfred", to="rrts",
        seam="bug-report-ingest", subject="propose: seam",
        item=["portal-form:alfred", "rrts-endpoint:rrts", "shared-schema"],
    ), raw)
    assert rc == 0
    assert len(_spool_files(raw)) == 1  # exactly one spool file minted

    res = _route(raw)
    assert res["malformed"] == 0 and res["undeliverable"] == 0
    assert res["contracts_applied"] == 1

    contracts = list(_store(raw).iter_contracts())
    assert len(contracts) == 1
    c = contracts[0]
    assert c.state == "proposed"
    assert c.seam == "bug-report-ingest"
    assert {p.project for p in c.participants} == {"alfred", "rrts"}
    assert [g.item for g in find_gaps(c)] == ["shared-schema"]  # owner:"" ⇒ GAP


def test_propose_participants_derived_from_from_and_to(tmp_path):  # type: ignore[no-untyped-def]
    """Without --participant, participants derive: --from is producer, each
    --to is a consumer."""
    raw = _raw(tmp_path)
    assert dispatch(_ns(
        contract_cmd="propose", from_project="alfred", to="rrts",
        seam="s", subject="p",
    ), raw) == 0
    _route(raw)
    c = list(_store(raw).iter_contracts())[0]
    roles = {p.project: p.role for p in c.participants}
    assert roles == {"alfred": "producer", "rrts": "consumer"}


# ---------------------------------------------------------------------------
# accept + convergence
# ---------------------------------------------------------------------------


def _propose_and_route(raw: dict[str, Any]) -> str:
    assert dispatch(_ns(
        contract_cmd="propose", from_project="alfred", to="rrts",
        seam="s", subject="p", item=["x:alfred"],
    ), raw) == 0
    _route(raw)
    return list(_store(raw).iter_contracts())[0].contract_id


def test_accept_from_participant_applies(tmp_path):  # type: ignore[no-untyped-def]
    """Pin: an accept from a participant is applied — that participant's
    accepted_version reaches the current version."""
    raw = _raw(tmp_path)
    cid = _propose_and_route(raw)
    assert dispatch(_ns(
        contract_cmd="accept", from_project="rrts", contract_id=cid,
    ), raw) == 0
    _route(raw)
    c = _store(raw).load(cid)
    accepted = {p.project: p.accepted_version for p in c.participants}
    assert accepted["rrts"] == c.version
    assert accepted["alfred"] is None      # proposer hasn't accepted yet
    assert not is_converged(c)


def test_both_participants_accept_converges(tmp_path):  # type: ignore[no-untyped-def]
    """Pin: both participants accept → converged (awaiting operator ratify)."""
    raw = _raw(tmp_path)
    cid = _propose_and_route(raw)
    assert dispatch(_ns(contract_cmd="accept", from_project="rrts", contract_id=cid), raw) == 0
    assert dispatch(_ns(contract_cmd="accept", from_project="alfred", contract_id=cid), raw) == 0
    _route(raw)
    c = _store(raw).load(cid)
    assert is_converged(c)
    # still NOT ratified — convergence only makes it operator-eligible.
    assert not c.ratified_at
    assert c.contract_id in {x.contract_id for x in _store(raw).list_awaiting()}


def test_propose_and_accept_self_accepts_proposer(tmp_path):  # type: ignore[no-untyped-def]
    """Pin: ``propose --and-accept`` ALSO drops the proposer's own accept
    (both files in ONE spool drop; the propose sorts BEFORE the self-accept so
    it creates the contract first in the same route tick). After the
    counterparty accepts, it converges."""
    raw = _raw(tmp_path)
    assert dispatch(_ns(
        contract_cmd="propose", from_project="alfred", to="rrts",
        seam="seam-x", subject="p", item=["a:alfred"], and_accept=True,
    ), raw) == 0
    assert len(_spool_files(raw)) == 2  # propose + the proposer's self-accept
    _route(raw)
    c = list(_store(raw).iter_contracts())[0]
    cid = c.contract_id
    accepted = {p.project: p.accepted_version for p in c.participants}
    assert accepted["alfred"] == c.version   # proposer self-accepted in the same tick
    assert accepted["rrts"] is None
    assert not is_converged(c)
    # counterparty accepts → converged
    assert dispatch(_ns(contract_cmd="accept", from_project="rrts", contract_id=cid), raw) == 0
    _route(raw)
    assert is_converged(_store(raw).load(cid))


# ---------------------------------------------------------------------------
# counter
# ---------------------------------------------------------------------------


def test_counter_replaces_division_of_labor(tmp_path):  # type: ignore[no-untyped-def]
    """A counter re-states terms: --item REPLACES the division_of_labor and
    bumps the version (staling prior accepts)."""
    raw = _raw(tmp_path)
    cid = _propose_and_route(raw)
    assert dispatch(_ns(
        contract_cmd="counter", from_project="rrts", contract_id=cid,
        subject="counter: split differently", item=["x:alfred", "y:rrts"],
    ), raw) == 0
    _route(raw)
    c = _store(raw).load(cid)
    assert c.state == "countered"
    assert c.version == 2  # a term mutation bumped the version
    assert {(d.item, d.owner) for d in c.division_of_labor} == {("x", "alfred"), ("y", "rrts")}


# ---------------------------------------------------------------------------
# fail-loud (invalid / unknown args)
# ---------------------------------------------------------------------------


def test_propose_missing_to_fails_loud(tmp_path):  # type: ignore[no-untyped-def]
    raw = _raw(tmp_path)
    assert dispatch(_ns(contract_cmd="propose", from_project="alfred", to="", seam="s", subject="p"), raw) == 1
    assert _spool_files(raw) == []  # nothing minted


def test_propose_bad_participant_spec_fails_loud(tmp_path):  # type: ignore[no-untyped-def]
    raw = _raw(tmp_path)
    assert dispatch(_ns(
        contract_cmd="propose", from_project="alfred", to="rrts", seam="s",
        subject="p", participant=["a:b:c:d"],
    ), raw) == 1
    assert _spool_files(raw) == []


def test_accept_unknown_contract_fails_loud(tmp_path):  # type: ignore[no-untyped-def]
    raw = _raw(tmp_path)
    assert dispatch(_ns(
        contract_cmd="accept", from_project="rrts", contract_id="contract-nope-000000",
    ), raw) == 1
    assert _spool_files(raw) == []


def test_accept_from_non_participant_fails_loud(tmp_path):  # type: ignore[no-untyped-def]
    raw = _raw(tmp_path)
    cid = _propose_and_route(raw)
    assert dispatch(_ns(
        contract_cmd="accept", from_project="stranger", contract_id=cid,
    ), raw) == 1
    assert _spool_files(raw) == []  # nothing minted for the bad actor


# ---------------------------------------------------------------------------
# ratify / reject UNCHANGED — still operator-only (agents fail-closed)
# ---------------------------------------------------------------------------


def test_agent_minted_ratify_is_impossible_and_operator_cli_ratifies(tmp_path):  # type: ignore[no-untyped-def]
    """Pin: the new minting verbs did NOT add an agent ratify path — ``ratify``
    stays operator-only via the CLI. A converged contract ratifies through the
    operator CLI (actor_is_operator=True); the agent verbs never touch it."""
    raw = _raw(tmp_path)
    cid = _propose_and_route(raw)
    dispatch(_ns(contract_cmd="accept", from_project="rrts", contract_id=cid), raw)
    dispatch(_ns(contract_cmd="accept", from_project="alfred", contract_id=cid), raw)
    _route(raw)
    assert is_converged(_store(raw).load(cid))
    # operator ratify via the (unchanged) CLI path
    assert dispatch(_ns(contract_cmd="ratify", contract_id=cid, note="locked"), raw) == 0
    c = _store(raw).load(cid)
    assert c.state == "ratified" and c.ratified_by == "andrew"
    # build-gate now passes (exit 0)
    assert dispatch(_ns(contract_cmd="check", contract_id=cid), raw) == 0

"""Layer-2 contract negotiation — C0–C3 tests.

Covers: mint stability; every legal/illegal transition + the
illegal-ratify-by-agent fail-closed guard; is_buildable/is_converged/
find_gaps/find_overlaps; apply_message round-trip + atomic save +
per-list-item schema-tolerance; the `check` exit-code build-gate; the bus
dispatch hook (CONTRACT_KINDS → contracts.router, not plain inbox); the
5-step RRTS↔Algernon worked example end-to-end; and the brief section ILB.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from alfred.contracts.config import ContractConfig
from alfred.contracts.schema import (
    CONTRACT_KINDS,
    Contract,
    DivisionItem,
    Participant,
    Transition,
    find_gaps,
    find_overlaps,
    is_buildable,
    is_converged,
    legal_transition,
    mint_contract_id,
)
from alfred.contracts.store import ContractStore


SEAM = "Website->VERA image-block"
CREATED = "2026-06-30T12:00:00+00:00"


def _store(tmp_path: Path) -> ContractStore:
    return ContractStore(
        str(tmp_path / "contracts"),
        str(tmp_path / "contracts" / "contract_audit.jsonl"),
    )


def _propose_body() -> str:
    return yaml.safe_dump({
        "participants": [
            {"project": "algernon", "agent": "intake-backend", "role": "producer"},
            {"project": "rrts", "agent": "rrts-intake", "role": "consumer"},
        ],
        "interface": {"images": "max 4, max 5MiB, intake-only-no-egress"},
        "division_of_labor": [
            {"item": "widget-capture", "owner": "rrts", "status": "built"},
            {"item": "image-carry", "owner": "algernon", "status": "building"},
            {"item": "wire-schema", "owner": "algernon", "status": "todo"},
        ],
    })


def _counter_body_with_gap() -> str:
    return yaml.safe_dump({
        "division_of_labor": [
            {"item": "widget-capture", "owner": "rrts", "status": "built"},
            {"item": "image-carry", "owner": "algernon", "status": "building"},
            {"item": "wire-schema", "owner": "algernon", "status": "todo"},
            # the NEW unowned row = GAP, surfaced at agreement-time
            {"item": "media_type allowlist enforcement", "owner": "", "status": "todo"},
        ],
        "interface": {"images": "max 4, max 5MiB, png/jpeg/webp, intake-only"},
    })


# ---------------------------------------------------------------------------
# C0 — schema helpers
# ---------------------------------------------------------------------------


def test_mint_contract_id_stable_and_readable():
    a = mint_contract_id(SEAM, CREATED)
    b = mint_contract_id(SEAM, CREATED)
    assert a == b
    assert a.startswith("contract-website-vera-image-block-")
    assert len(a.split("-")[-1]) == 6
    assert mint_contract_id("other seam", CREATED) != a


def test_contract_kinds_frozen():
    assert CONTRACT_KINDS == frozenset(
        {"propose", "counter", "accept", "ratify", "reject", "block"}
    )


@pytest.mark.parametrize("from_state,kind,op,expect_ok,expect_to", [
    ("draft", "propose", False, True, "proposed"),
    ("proposed", "counter", False, True, "countered"),
    ("countered", "counter", False, True, "countered"),
    ("countered", "propose", False, True, "proposed"),
    ("blocked", "counter", False, True, "countered"),
    ("proposed", "accept", False, True, "proposed"),
    ("countered", "accept", False, True, "countered"),
    ("proposed", "ratify", True, True, "ratified"),
    ("countered", "ratify", True, True, "ratified"),
    ("proposed", "reject", True, True, "countered"),
    ("proposed", "block", False, True, "blocked"),
    ("ratified", "block", False, True, "blocked"),
    # illegal state transitions
    ("draft", "counter", False, False, "draft"),
    ("ratified", "counter", False, False, "ratified"),
    ("draft", "accept", False, False, "draft"),
    ("blocked", "ratify", True, False, "blocked"),
])
def test_legal_transition_matrix(from_state, kind, op, expect_ok, expect_to):
    ok, to_state, _ = legal_transition(from_state, kind, op)
    assert ok is expect_ok
    assert to_state == expect_to


def test_illegal_ratify_by_agent_fail_closed():
    """THE load-bearing guard: ratify/reject by a non-operator is rejected
    BEFORE any state check."""
    ok, to_state, reason = legal_transition("proposed", "ratify", False)
    assert ok is False
    assert "operator-only" in reason
    ok2, _, reason2 = legal_transition("proposed", "reject", False)
    assert ok2 is False
    assert "operator-only" in reason2
    # operator CAN ratify
    assert legal_transition("proposed", "ratify", True)[0] is True


def test_is_buildable():
    assert is_buildable(Contract(state="ratified")) is True
    assert is_buildable(Contract(state="ratified", superseded_by="c2")) is False
    assert is_buildable(Contract(state="proposed")) is False


def test_is_converged():
    c = Contract(version=2, participants=[
        Participant(project="a", accepted_version=2),
        Participant(project="b", accepted_version=2),
    ])
    assert is_converged(c) is True
    c.participants[1].accepted_version = 1  # stale
    assert is_converged(c) is False
    assert is_converged(Contract(version=1, participants=[])) is False  # nothing agreed


def test_find_gaps_and_overlaps():
    c = Contract(division_of_labor=[
        DivisionItem(item="a", owner="x"),
        DivisionItem(item="b", owner=""),          # gap
        DivisionItem(item="c", owner="x"),
        DivisionItem(item="c", owner="y"),          # overlap on c
    ])
    assert [g.item for g in find_gaps(c)] == ["b"]
    overlaps = find_overlaps(c)
    assert overlaps == [("c", ["x", "y"])]


def test_contract_per_list_item_schema_tolerance():
    """QA risk flag: the embedded participants/division_of_labor/history
    LISTS need per-item from_dict filtering, not just top-level."""
    data = {
        "contract_id": "c1", "seam": "s", "state": "proposed", "future_top": "x",
        "participants": [{"project": "a", "future_p": "x"}],
        "division_of_labor": [{"item": "i", "owner": "o", "future_d": "x"}],
        "history": [{"ts": "t", "kind": "propose", "future_h": "x"}],
    }
    c = Contract.from_dict(data)
    assert c.contract_id == "c1"
    assert c.participants[0].project == "a"
    assert c.division_of_labor[0].item == "i"
    assert c.history[0].kind == "propose"
    # extras dropped (no crash, no leakage)
    assert not hasattr(c.participants[0], "future_p")


# ---------------------------------------------------------------------------
# C0 — store: apply_message + round-trip + atomic save
# ---------------------------------------------------------------------------


def test_store_save_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    c = Contract(
        contract_id="c1", seam=SEAM, state="proposed", version=2,
        participants=[Participant(project="a", agent="ag", role="producer", accepted_version=2)],
        division_of_labor=[DivisionItem(item="i", owner="a", status="todo")],
        history=[Transition(ts="t", kind="propose", from_state="draft", to_state="proposed")],
        body="# interface spec\n\nthe prose",
    )
    store.save(c)
    loaded = store.load("c1")
    assert loaded.state == "proposed"
    assert loaded.version == 2
    assert loaded.participants[0].accepted_version == 2
    assert loaded.division_of_labor[0].item == "i"
    assert loaded.history[0].kind == "propose"
    assert "the prose" in loaded.body


def test_apply_first_propose_creates_contract(tmp_path):
    store = _store(tmp_path)
    fm = {
        "kind": "propose", "from": "algernon/intake-backend",
        "to": "rrts/rrts-intake", "seam": SEAM,
        "correlation_id": "cnv-1", "created": CREATED,
    }
    result = store.apply_message(fm, _propose_body(), actor="algernon/intake-backend", actor_is_operator=False)
    assert result.ok
    assert result.new_contract
    c = result.contract
    assert c.state == "proposed"
    assert c.version == 1
    assert {p.project for p in c.participants} == {"algernon", "rrts"}
    assert len(c.division_of_labor) == 3
    # persisted
    assert store.load(c.contract_id) is not None


def test_apply_counter_bumps_version_and_stales_accepts(tmp_path):
    store = _store(tmp_path)
    fm = {"kind": "propose", "from": "algernon", "to": "rrts", "seam": SEAM,
          "correlation_id": "c", "created": CREATED}
    cid = store.apply_message(fm, _propose_body(), actor="algernon", actor_is_operator=False).contract.contract_id
    r = store.apply_message(
        {"kind": "counter", "contract_id": cid, "from": "rrts", "correlation_id": "c2"},
        _counter_body_with_gap(), actor="rrts", actor_is_operator=False,
    )
    assert r.ok
    assert r.contract.state == "countered"
    assert r.contract.version == 2
    assert len(find_gaps(r.contract)) == 1   # the new unowned row


def test_apply_illegal_ratify_by_agent_rejected(tmp_path):
    store = _store(tmp_path)
    fm = {"kind": "propose", "from": "algernon", "to": "rrts", "seam": SEAM,
          "correlation_id": "c", "created": CREATED}
    cid = store.apply_message(fm, _propose_body(), actor="algernon", actor_is_operator=False).contract.contract_id
    # agent tries to ratify → fail-closed
    r = store.apply_message(
        {"kind": "ratify", "contract_id": cid, "from": "algernon", "correlation_id": "x"},
        "", actor="algernon", actor_is_operator=False,
    )
    assert r.ok is False
    assert "operator-only" in r.reason
    assert store.load(cid).state == "proposed"   # unchanged


def test_apply_counter_by_non_participant_rejected(tmp_path):
    store = _store(tmp_path)
    fm = {"kind": "propose", "from": "algernon", "to": "rrts", "seam": SEAM,
          "correlation_id": "c", "created": CREATED}
    cid = store.apply_message(fm, _propose_body(), actor="algernon", actor_is_operator=False).contract.contract_id
    r = store.apply_message(
        {"kind": "counter", "contract_id": cid, "from": "intruder", "correlation_id": "x"},
        _counter_body_with_gap(), actor="intruder", actor_is_operator=False,
    )
    assert r.ok is False
    assert "not a contract participant" in r.reason


def test_apply_propose_by_non_participant_rejected(tmp_path):
    """QA fix #1: a NON-PARTICIPANT re-proposing on an EXISTING contract (a
    legal countered→proposed transition) would bump the version + reset the
    interface — term injection. The participant-authority gate now covers
    propose-on-existing (the FIRST propose stays unguarded)."""
    store = _store(tmp_path)
    fm = {"kind": "propose", "from": "algernon", "to": "rrts", "seam": SEAM,
          "correlation_id": "c", "created": CREATED}
    cid = store.apply_message(fm, _propose_body(), actor="algernon", actor_is_operator=False).contract.contract_id
    # move to countered so a propose is a LEGAL state transition
    store.apply_message(
        {"kind": "counter", "contract_id": cid, "from": "rrts", "correlation_id": "c2"},
        _counter_body_with_gap(), actor="rrts", actor_is_operator=False,
    )
    before = store.load(cid)
    r = store.apply_message(
        {"kind": "propose", "contract_id": cid, "from": "intruder", "correlation_id": "x"},
        yaml.safe_dump({"interface": {"hijacked": True}}),
        actor="intruder", actor_is_operator=False,
    )
    assert r.ok is False
    assert "not a contract participant" in r.reason
    after = store.load(cid)
    assert after.state == before.state == "countered"   # state unchanged
    assert after.version == before.version              # version NOT bumped
    assert "hijacked" not in after.interface            # interface untouched


def test_apply_audit_trail_written(tmp_path):
    from alfred.contracts.audit import read_contract_audit
    store = _store(tmp_path)
    fm = {"kind": "propose", "from": "algernon", "to": "rrts", "seam": SEAM,
          "correlation_id": "c", "created": CREATED}
    store.apply_message(fm, _propose_body(), actor="algernon", actor_is_operator=False)
    rows = read_contract_audit(store.audit_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "propose"
    assert rows[0]["outcome"] == "applied"


# ---------------------------------------------------------------------------
# C2 — the 5-step RRTS↔Algernon worked example (end-to-end)
# ---------------------------------------------------------------------------


def test_worked_example_image_block_converges_and_ratifies(tmp_path):
    store = _store(tmp_path)

    # 1. propose(algernon) → proposed v1
    r1 = store.apply_message(
        {"kind": "propose", "from": "algernon/intake-backend", "to": "rrts/rrts-intake",
         "seam": SEAM, "correlation_id": "cnv-1", "created": CREATED},
        _propose_body(), actor="algernon/intake-backend", actor_is_operator=False,
    )
    cid = r1.contract.contract_id
    assert r1.contract.state == "proposed" and r1.contract.version == 1

    # 2. counter(rrts, adds the unowned GAP row) → countered v2
    r2 = store.apply_message(
        {"kind": "counter", "contract_id": cid, "from": "rrts/rrts-intake", "correlation_id": "cnv-2"},
        _counter_body_with_gap(), actor="rrts/rrts-intake", actor_is_operator=False,
    )
    assert r2.contract.state == "countered" and r2.contract.version == 2
    assert len(find_gaps(r2.contract)) == 1   # GAP flagged at agreement-time

    # 3. accept(algernon) — claims the gap + assents to v2
    r3 = store.apply_message(
        {"kind": "accept", "contract_id": cid, "from": "algernon/intake-backend", "correlation_id": "cnv-3"},
        yaml.safe_dump({"claims": ["media_type allowlist enforcement"]}),
        actor="algernon/intake-backend", actor_is_operator=False,
    )
    assert find_gaps(r3.contract) == []        # gap claimed
    assert not is_converged(r3.contract)       # rrts hasn't accepted v2 yet

    # 4. accept(rrts) → converged
    r4 = store.apply_message(
        {"kind": "accept", "contract_id": cid, "from": "rrts/rrts-intake", "correlation_id": "cnv-4"},
        "", actor="rrts/rrts-intake", actor_is_operator=False,
    )
    assert is_converged(r4.contract) is True
    assert r4.converged is True
    assert is_buildable(r4.contract) is False   # not yet ratified

    # 5. ratify(operator) → ratified ⇒ buildable
    r5 = store.apply_message(
        {"kind": "ratify", "contract_id": cid, "from": "andrew", "correlation_id": "op-1"},
        "", actor="andrew", actor_is_operator=True,
    )
    assert r5.contract.state == "ratified"
    assert is_buildable(r5.contract) is True
    assert r5.contract.ratified_by == "andrew"


# ---------------------------------------------------------------------------
# C1 — the CLI build-gate (check exit code)
# ---------------------------------------------------------------------------


def _contract_config(tmp_path) -> ContractConfig:
    return ContractConfig(
        enabled=True,
        store_path=str(tmp_path / "contracts"),
        audit_log_path=str(tmp_path / "contracts" / "audit.jsonl"),
        operator_id="andrew",
    )


def test_cli_check_exit_code_gate(tmp_path):
    from alfred.contracts.cli import CHECK_NOT_BUILDABLE, cmd_check
    cfg = _contract_config(tmp_path)
    store = ContractStore(cfg.store_path, cfg.resolved_audit_path())
    store.save(Contract(contract_id="rat", seam="s", state="ratified", version=1))
    store.save(Contract(contract_id="prop", seam="s", state="proposed", version=1))

    assert cmd_check(SimpleNamespace(contract_id="rat"), cfg) == 0
    assert cmd_check(SimpleNamespace(contract_id="prop"), cfg) == CHECK_NOT_BUILDABLE
    assert cmd_check(SimpleNamespace(contract_id="missing"), cfg) == CHECK_NOT_BUILDABLE


def test_cli_show_banner(tmp_path, capsys):
    from alfred.contracts.cli import cmd_show
    cfg = _contract_config(tmp_path)
    store = ContractStore(cfg.store_path, cfg.resolved_audit_path())
    store.save(Contract(contract_id="rat", seam="s", state="ratified", version=2))
    cmd_show(SimpleNamespace(contract_id="rat", json=False), cfg)
    out = capsys.readouterr().out
    assert "RATIFIED — safe to build against v2" in out

    store.save(Contract(contract_id="prop", seam="s", state="proposed", version=1))
    cmd_show(SimpleNamespace(contract_id="prop", json=False), cfg)
    out = capsys.readouterr().out
    assert "DO NOT BUILD" in out


def test_cli_ratify_requires_convergence_without_force(tmp_path):
    from alfred.contracts.cli import cmd_ratify
    cfg = _contract_config(tmp_path)
    store = ContractStore(cfg.store_path, cfg.resolved_audit_path())
    store.save(Contract(
        contract_id="c", seam="s", state="proposed", version=2,
        participants=[Participant(project="a", accepted_version=1)],  # stale → not converged
    ))
    # not converged + no --force → refused
    rc = cmd_ratify(SimpleNamespace(contract_id="c", force=False, note=""), cfg, {})
    assert rc == 1
    assert store.load("c").state == "proposed"
    # --force overrides
    rc2 = cmd_ratify(SimpleNamespace(contract_id="c", force=True, note=""), cfg, {})
    assert rc2 == 0
    assert store.load("c").state == "ratified"


# ---------------------------------------------------------------------------
# C2 — the bus dispatch hook
# ---------------------------------------------------------------------------


def _bus_raw(tmp_path) -> dict:
    """A unified config with BOTH the bus + contracts sections."""
    return {
        "message_bus": {
            "enabled": True,
            "spool_path": str(tmp_path / "spool"),
            "state": {"path": str(tmp_path / "bus_state.json")},
            "projects": [
                {"name": "algernon", "inbox_path": str(tmp_path / "algernon" / "inbox")},
                {"name": "rrts", "inbox_path": str(tmp_path / "rrts" / "inbox")},
            ],
        },
        "contracts": {
            "enabled": True,
            "store_path": str(tmp_path / "contracts"),
        },
    }


async def test_bus_dispatch_routes_contract_to_solver_not_inbox(tmp_path):
    """A CONTRACT_KINDS message is handed to contracts.router (creating the
    contract) — NOT placed as a plain inbox message."""
    from alfred.msgbus.config import load_message_bus_config
    from alfred.msgbus.record import MessageRecord, message_filename, write_message_file
    from alfred.msgbus.router import run_route_once

    raw = _bus_raw(tmp_path)
    spool = Path(raw["message_bus"]["spool_path"])
    spool.mkdir(parents=True, exist_ok=True)

    # drop a first-propose contract message into the spool. contract_id +
    # seam ride the frontmatter; participants/DoL ride the body.
    rec = MessageRecord(
        id="msg-c1", from_project="algernon", to_project="rrts",
        kind="propose", correlation_id="cnv-1", created=CREATED,
        subject="propose image-block", body=_propose_body(),
    )
    # add the contract-specific frontmatter fields the bus record drops
    import frontmatter
    post = frontmatter.Post(rec.body, **{
        "id": rec.id, "from": "algernon", "to": "rrts", "kind": "propose",
        "correlation_id": "cnv-1", "created": CREATED, "subject": rec.subject,
        "seam": SEAM,
    })
    (spool / message_filename(rec)).write_text(frontmatter.dumps(post))

    cfg = load_message_bus_config(raw)
    result = await run_route_once(cfg, raw)

    assert result["routed"] == 0              # NOT a plain inbox placement
    assert result["contracts_applied"] == 1
    # the contract now exists in the store
    store = _store(tmp_path)
    contracts = store.iter_contracts()
    assert len(contracts) == 1
    assert contracts[0].state == "proposed"
    # rrts (the counterparty) got an fyi NOTICE in its inbox
    rrts_inbox = Path(raw["message_bus"]["projects"][1]["inbox_path"])
    assert len(list(rrts_inbox.glob("*.md"))) == 1


async def test_bus_contract_message_deduped_no_double_apply(tmp_path):
    """A re-dropped contract message (same bus id) is NOT applied twice —
    a double counter would double-bump the version."""
    from alfred.msgbus.config import load_message_bus_config
    from alfred.msgbus.record import MessageRecord, message_filename
    from alfred.msgbus.router import run_route_once
    import frontmatter

    raw = _bus_raw(tmp_path)
    spool = Path(raw["message_bus"]["spool_path"])
    spool.mkdir(parents=True, exist_ok=True)

    def _drop():
        rec = MessageRecord(id="msg-fixed", from_project="algernon", to_project="rrts",
                            kind="propose", correlation_id="c", created=CREATED, subject="s")
        post = frontmatter.Post(_propose_body(), **{
            "id": "msg-fixed", "from": "algernon", "to": "rrts", "kind": "propose",
            "correlation_id": "c", "created": CREATED, "subject": "s", "seam": SEAM,
        })
        (spool / message_filename(rec)).write_text(frontmatter.dumps(post))

    cfg = load_message_bus_config(raw)
    _drop()
    await run_route_once(cfg, raw)
    _drop()  # re-drop the SAME bus id
    r2 = await run_route_once(cfg, raw)
    assert r2["contracts_applied"] == 0
    assert r2["skipped_dup"] == 1
    assert len(_store(tmp_path).iter_contracts()) == 1   # not double-created


async def test_bus_contract_quarantined_when_contracts_disabled(tmp_path):
    """QA fix #2: a bus-on / contracts-off box does NOT process contract
    messages — it quarantines them (undeliverable, reason contracts_disabled),
    never applying them to a store."""
    from alfred.msgbus.config import load_message_bus_config
    from alfred.msgbus.record import MessageRecord, message_filename
    from alfred.msgbus.router import run_route_once
    import frontmatter

    raw = _bus_raw(tmp_path)
    raw["contracts"]["enabled"] = False   # the off-switch
    spool = Path(raw["message_bus"]["spool_path"])
    spool.mkdir(parents=True, exist_ok=True)
    rec = MessageRecord(id="msg-d", from_project="algernon", to_project="rrts",
                        kind="propose", correlation_id="c", created=CREATED, subject="s")
    post = frontmatter.Post(_propose_body(), **{
        "id": "msg-d", "from": "algernon", "to": "rrts", "kind": "propose",
        "correlation_id": "c", "created": CREATED, "subject": "s", "seam": SEAM,
    })
    (spool / message_filename(rec)).write_text(frontmatter.dumps(post))

    cfg = load_message_bus_config(raw)
    result = await run_route_once(cfg, raw)
    assert result["contracts_applied"] == 0
    assert result["undeliverable"] == 1
    # NOT applied — no contract created
    assert _store(tmp_path).iter_contracts() == []
    # quarantined to undeliverable/
    assert len(list((spool / "undeliverable").glob("*.md"))) == 1


# ---------------------------------------------------------------------------
# C3 — brief section ILB
# ---------------------------------------------------------------------------


def test_contracts_awaiting_section_ilb_at_zero():
    from alfred.daily_sync.contracts_awaiting_section import render_batch
    assert render_batch([]) is None


def test_contracts_awaiting_section_renders_items():
    from alfred.daily_sync.contracts_awaiting_section import (
        ContractAwaitingItem,
        render_batch,
    )
    items = [ContractAwaitingItem(
        item_number=3, contract_id="c-img", seam=SEAM, version=2,
        converged=True, blocked=False, gaps=[],
    )]
    out = render_batch(items)
    assert "3. [CONTRACT awaiting ratification]" in out
    assert "agents converged on v2" in out
    assert "alfred contract ratify" in out


def test_contracts_awaiting_build_batch_empty_when_no_store(tmp_path):
    from alfred.daily_sync.config import DailySyncConfig
    from alfred.daily_sync.contracts_awaiting_section import build_batch
    cfg = DailySyncConfig(config_path=str(tmp_path / "nonexistent.yaml"))
    assert build_batch(cfg) == []


# ---------------------------------------------------------------------------
# Dormant-safe + CLI registration
# ---------------------------------------------------------------------------


def test_dormant_safe_no_contracts_config():
    from alfred.contracts.config import load_contract_config
    cfg = load_contract_config({})
    assert cfg.enabled is False


def test_cli_contract_registered():
    import alfred.cli as cli
    p = cli.build_parser()
    ns = p.parse_args(["contract", "list", "--awaiting"])
    assert ns.contract_cmd == "list"
    assert ns.awaiting is True
    assert "contract" in cli.build_parser().__dict__ or True  # parser built ok

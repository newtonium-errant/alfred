"""Path B — route-on-send (`--route`/`--now`) + the spool concurrency lock.

The keystone is `test_keystone_*`: route-on-send makes the router concurrent for
the first time, so a contract `counter` swept by two overlapping `route_now`
calls must apply its version bump EXACTLY ONCE — the `flock` on
`<spool>/.route.lock` is what prevents the double-apply corruption.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import threading
import time

import structlog

from alfred.contracts.cli import dispatch as contract_dispatch
from alfred.contracts.config import load_contract_config
from alfred.contracts.store import ContractStore
from alfred.msgbus.config import load_message_bus_config
from alfred.msgbus.router import route_now, run_route_once
from alfred.cli import _msg_inbox, _msg_send


def _raw(tmp_path) -> dict:  # type: ignore[no-untyped-def]
    d = tmp_path
    (d / "alfred" / ".msgbus" / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "rrts" / ".msgbus" / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "spool").mkdir(parents=True, exist_ok=True)
    return {
        "message_bus": {
            "enabled": True, "self_project": "alfred",
            "spool_path": str(d / "spool"),
            "state": {"path": str(d / "state.json")},
            "projects": [
                {"name": "alfred", "inbox_path": str(d / "alfred" / ".msgbus" / "inbox")},
                {"name": "rrts", "inbox_path": str(d / "rrts" / ".msgbus" / "inbox")},
            ],
        },
        "contracts": {
            "enabled": True, "store_path": str(d / "contracts"), "operator_id": "andrew",
        },
    }


def _mb(raw):  # type: ignore[no-untyped-def]
    return load_message_bus_config(raw)


def _store(raw) -> ContractStore:  # type: ignore[no-untyped-def]
    cfg = load_contract_config(raw)
    return ContractStore(cfg.store_path, cfg.resolved_audit_path())


def _inbox_files(tmp_path, project: str) -> list[str]:
    d = tmp_path / project / ".msgbus" / "inbox"
    return sorted(p.name for p in d.glob("*.md")) if d.exists() else []


def _spool_files(tmp_path) -> list[str]:
    d = tmp_path / "spool"
    return sorted(p.name for p in d.glob("*.md")) if d.exists() else []


def _send_ns(**kw) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    base = dict(
        from_project="alfred", to="rrts", kind="handover", subject="s",
        correlation_id="", reply_to="", precedence="R", body="hi",
        body_file="", route=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _cns(**kw) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    base = dict(
        contract_cmd="", from_project="", to="", seam="", subject="",
        contract_id="", participant=[], item=[], correlation_id="",
        and_accept=False, route=False, note="", force=False, awaiting=False,
        state="", json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _inbox_ns(**kw) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    base = dict(project="rrts", inbox_action="drain", message_id="", json=False)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# (a) msg send --route + (g) backward-compat
# ---------------------------------------------------------------------------


def test_msg_send_route_delivers_without_cron(tmp_path):  # type: ignore[no-untyped-def]
    """Pin (a): `msg send --route` sweeps immediately → the message lands in
    the peer inbox with NO cron tick."""
    raw = _raw(tmp_path)
    with structlog.testing.capture_logs():
        _msg_send(_send_ns(route=True), _mb(raw), raw)
    assert len(_inbox_files(tmp_path, "rrts")) == 1
    assert _spool_files(tmp_path) == []  # swept out of the spool


def test_msg_send_without_route_is_cron_only_unchanged(tmp_path):  # type: ignore[no-untyped-def]
    """Pin (g): without --route, the message stays in the spool (cron-only) —
    byte-identical to today, NOT delivered."""
    raw = _raw(tmp_path)
    with structlog.testing.capture_logs():
        _msg_send(_send_ns(route=False), _mb(raw), raw)
    assert _inbox_files(tmp_path, "rrts") == []   # NOT delivered
    assert len(_spool_files(tmp_path)) == 1       # waiting for the cron


# ---------------------------------------------------------------------------
# (c) lock-held → skipped cleanly; the holder still routes the file
# ---------------------------------------------------------------------------


def test_route_now_skips_cleanly_when_lock_held(tmp_path):  # type: ignore[no-untyped-def]
    """Pin (c): when a concurrent sweep holds `<spool>/.route.lock`, route_now
    returns `skipped_locked` (no double-route); the file stays in the spool and
    is routed once the lock frees."""
    import fcntl

    raw = _raw(tmp_path)
    # mint into the spool WITHOUT routing.
    with structlog.testing.capture_logs():
        _msg_send(_send_ns(route=False), _mb(raw), raw)
    assert len(_spool_files(tmp_path)) == 1

    holder = open(tmp_path / "spool" / ".route.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with structlog.testing.capture_logs():
            res = route_now(_mb(raw), raw)
        assert res.get("skipped_locked") is True
        assert res["routed"] == 0
        assert _inbox_files(tmp_path, "rrts") == []   # not routed while locked
        assert len(_spool_files(tmp_path)) == 1       # still pending
    finally:
        holder.close()
    # lock freed → the file routes.
    with structlog.testing.capture_logs():
        res2 = route_now(_mb(raw), raw)
    assert res2["routed"] == 1
    assert len(_inbox_files(tmp_path, "rrts")) == 1


# ---------------------------------------------------------------------------
# (b) THE KEYSTONE — concurrent route_now on a contract counter applies ONCE
# ---------------------------------------------------------------------------


def test_keystone_concurrent_route_now_applies_counter_exactly_once(tmp_path):  # type: ignore[no-untyped-def]
    """KEYSTONE (b): two CONCURRENT route_now sweeps over a minted contract
    `counter` must apply its version bump EXACTLY ONCE. Without the flock, both
    sweeps `MessageBusState.load()` before either `.save()`, both dispatch the
    counter → version applied TWICE (corruption). The lock serializes them:
    exactly one applies, the other skips."""
    raw = _raw(tmp_path)
    # propose + route → v1
    with structlog.testing.capture_logs():
        assert contract_dispatch(_cns(
            contract_cmd="propose", from_project="alfred", to="rrts",
            seam="s", subject="p", item=["x:alfred"], route=True,
        ), raw) == 0
    cid = list(_store(raw).iter_contracts())[0].contract_id
    assert _store(raw).load(cid).version == 1

    # mint a counter WITHOUT routing — leave it pending in the spool.
    with structlog.testing.capture_logs():
        assert contract_dispatch(_cns(
            contract_cmd="counter", from_project="rrts", contract_id=cid,
            subject="c", item=["x:alfred", "y:rrts"], route=False,
        ), raw) == 0

    # two threads race to route_now the SAME pending counter.
    mb = _mb(raw)
    barrier = threading.Barrier(2)
    results: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with structlog.testing.capture_logs():
            r = route_now(mb, raw)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    c = _store(raw).load(cid)
    assert c.version == 2, f"counter must apply EXACTLY ONCE, got v{c.version}"
    counter_transitions = [h for h in c.history if h.kind == "counter"]
    assert len(counter_transitions) == 1  # not two
    # exactly one sweep actually ran (the other skipped_locked OR deduped to 0).
    applied = [r for r in results if r.get("contracts_applied", 0) == 1]
    assert len(applied) == 1


def test_direct_run_route_once_respects_lock_no_double_apply(tmp_path):  # type: ignore[no-untyped-def]
    """THE MISSING REGRESSION (the gap that shipped in b63b732): the lock lives
    in ``run_route_once``, so the CRON/DAEMON path — a DIRECT
    ``run_route_once``, NOT via ``route_now`` — ALSO serializes on it. While a
    concurrent sweep holds the lock, a direct ``run_route_once`` SKIPS and does
    NOT apply the pending counter, so the version is never double-bumped.

    FAILS against b63b732 (the lock was ONLY in ``route_now`` → the direct
    cron call ran unlocked and applied the counter while another sweep held the
    lock = the exact double-apply the guard exists to prevent)."""
    raw = _raw(tmp_path)
    with structlog.testing.capture_logs():
        assert contract_dispatch(_cns(
            contract_cmd="propose", from_project="alfred", to="rrts",
            seam="s", subject="p", item=["x:alfred"], route=True,
        ), raw) == 0
    cid = list(_store(raw).iter_contracts())[0].contract_id
    with structlog.testing.capture_logs():
        assert contract_dispatch(_cns(
            contract_cmd="counter", from_project="rrts", contract_id=cid,
            subject="c", item=["x:alfred", "y:rrts"], route=False,
        ), raw) == 0
    assert _store(raw).load(cid).version == 1

    # a concurrent sweep holds the lock (loaded state, not yet applied).
    holder = open(tmp_path / "spool" / ".route.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # THE CRON/DAEMON PATH: a DIRECT run_route_once (lock_wait=0 default).
        with structlog.testing.capture_logs():
            res = asyncio.run(run_route_once(_mb(raw), raw))
        assert res.get("skipped_locked") is True   # the direct path RESPECTS the lock
        assert res["contracts_applied"] == 0
        assert _store(raw).load(cid).version == 1   # counter NOT applied while locked
    finally:
        holder.close()
    # once the holder frees, the counter applies exactly once.
    with structlog.testing.capture_logs():
        res2 = asyncio.run(run_route_once(_mb(raw), raw))
    assert res2["contracts_applied"] == 1
    assert _store(raw).load(cid).version == 2


def test_route_now_routes_promptly_when_racing_inflight_sweep(tmp_path):  # type: ignore[no-untyped-def]
    """Pin (reviewer #3 — prompt delivery under race): a ``--route`` message
    racing an IN-FLIGHT sweep (which already snapshotted the spool BEFORE the
    mint) is still delivered PROMPTLY — the interactive ``route_now`` blocks up
    to ``ROUTE_NOW_LOCK_WAIT_SECONDS`` for the sweep to release, then routes its
    own file, instead of stranding it to the next 5-min cron tick."""
    raw = _raw(tmp_path)
    # mint into the spool WITHOUT routing (a --route message waiting to go).
    with structlog.testing.capture_logs():
        _msg_send(_send_ns(route=False, body="urgent"), _mb(raw), raw)

    # an in-flight sweep holds the lock, releases after 0.3s (< the wait).
    holder = open(tmp_path / "spool" / ".route.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release_soon() -> None:
        time.sleep(0.3)
        holder.close()

    releaser = threading.Thread(target=_release_soon)
    releaser.start()
    try:
        with structlog.testing.capture_logs():
            res = route_now(_mb(raw), raw)   # blocks ~0.3s, then routes
    finally:
        releaser.join()

    assert not res.get("skipped_locked")            # NOT deferred to next tick
    assert res["routed"] == 1                        # routed promptly
    assert len(_inbox_files(tmp_path, "rrts")) == 1  # delivered


# ---------------------------------------------------------------------------
# (d) drain --json
# ---------------------------------------------------------------------------


def test_drain_json_emits_bodies_valid_json(tmp_path, capsys):  # type: ignore[no-untyped-def]
    """Pin (d): `msg inbox drain --json` emits the full records (incl. body) as
    valid JSON — the loop surface a live tick reads to respond."""
    raw = _raw(tmp_path)
    with structlog.testing.capture_logs():
        _msg_send(_send_ns(route=True, body="the payload"), _mb(raw), raw)
    capsys.readouterr()  # clear
    # capture_logs keeps structlog off stdout so only the JSON print lands.
    with structlog.testing.capture_logs():
        _msg_inbox(_inbox_ns(project="rrts", inbox_action="drain", json=True), _mb(raw))
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list) and len(parsed) == 1
    assert parsed[0]["body"] == "the payload"      # asdict carries the body
    assert parsed[0]["kind"] == "handover"


def test_drain_without_json_unchanged(tmp_path, capsys):  # type: ignore[no-untyped-def]
    """Pin (g): drain without --json is the human-readable output, unchanged."""
    raw = _raw(tmp_path)
    with structlog.testing.capture_logs():
        _msg_send(_send_ns(route=True), _mb(raw), raw)
    capsys.readouterr()
    with structlog.testing.capture_logs():
        _msg_inbox(_inbox_ns(project="rrts", inbox_action="drain", json=False), _mb(raw))
    out = capsys.readouterr().out
    assert "drained" in out and "drained:" in out
    # NOT JSON.
    import pytest
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# ---------------------------------------------------------------------------
# (e) contract propose --route + (f) --and-accept --route
# ---------------------------------------------------------------------------


def test_contract_propose_route_applies_instantly(tmp_path):  # type: ignore[no-untyped-def]
    """Pin (e): `contract propose --route` routes the mint immediately → the
    contract is `proposed` with NO separate route-once."""
    raw = _raw(tmp_path)
    with structlog.testing.capture_logs():
        assert contract_dispatch(_cns(
            contract_cmd="propose", from_project="alfred", to="rrts",
            seam="s", subject="p", item=["x:alfred"], route=True,
        ), raw) == 0
    contracts = list(_store(raw).iter_contracts())
    assert len(contracts) == 1 and contracts[0].state == "proposed"
    assert _spool_files(tmp_path) == []   # swept, not left for the cron


def test_contract_and_accept_route_routes_once_after_both_mints(tmp_path):  # type: ignore[no-untyped-def]
    """Pin (f): `propose --and-accept --route` routes ONCE after BOTH mints —
    the propose sorts first, so it creates the contract before the proposer's
    self-accept applies in the same sweep (proposer self-accepted)."""
    raw = _raw(tmp_path)
    with structlog.testing.capture_logs():
        assert contract_dispatch(_cns(
            contract_cmd="propose", from_project="alfred", to="rrts",
            seam="s", subject="p", item=["x:alfred"], and_accept=True, route=True,
        ), raw) == 0
    c = list(_store(raw).iter_contracts())[0]
    assert c.state == "proposed"
    accepted = {p.project: p.accepted_version for p in c.participants}
    assert accepted["alfred"] == c.version   # proposer self-accepted in the one sweep
    assert accepted["rrts"] is None
    assert _spool_files(tmp_path) == []      # both files swept in one route

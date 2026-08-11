"""#83 — one drip run at a time, per instance.

WHAT THESE PIN, AND WHY IT NEEDED PINNING. Before #83 the only ignition was an
hourly timer, so drip runs were serialized by having a single caller. The batch
submit route adds a second ignition (an on-submit kick), which makes concurrent
runs reachable. They are not safe: ``run_increment`` claims an item before
working it, but the claim is a crash-visibility marker, not mutual exclusion —
two processes that load the state file before either writes both see every item
PENDING and both work it.

That was MEASURED on this tree before the fix: two concurrent ``run_increment``
calls over one 6-item work-list produced 12 ``work()`` calls, every item
processed exactly twice, with both runs reporting a clean ``done=6``. The
observability said nothing at all, which is what makes it worth a pin rather
than a comment.

``test_two_processes_never_overlap_inside_the_lock`` is the mechanism proof and
the one to run a mutation against: delete the ``flock`` call in
``run_lock`` and it fails. The ``cmd_run`` pins prove the mechanism is actually
WIRED at the production entry point — a lock nothing calls is the failure mode
per-layer unit tests cannot see.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest
import structlog

from alfred.drip.cli import cmd_run
from alfred.drip import cli as drip_cli
from alfred.drip.config import CampaignConfig, DripConfig
from alfred.drip.run_lock import run_lock, run_lock_path
from alfred.drip.state import campaign_state_path
from alfred.drip.wiring import DripConfigError

_INSTANCE = "Salem"


# ---------------------------------------------------------------------------
# Path derivation
# ---------------------------------------------------------------------------


def test_the_lock_sits_beside_the_state_it_protects(tmp_path: Path) -> None:
    """Lock and cursor must share a directory — by construction, not by copy.

    A lock derived from a drifted slug would guard a directory no runner writes
    to, and would fail OPEN. Deriving both from ``drip_instance_dir`` is what
    makes that undriftable, so this asserts the shared parent rather than a
    literal path.
    """
    lock = run_lock_path(tmp_path, _INSTANCE)
    state = campaign_state_path(tmp_path, _INSTANCE, "batch_image")
    assert lock.parent == state.parent
    assert lock.name == ".run.lock"


def test_the_lock_path_is_instance_scoped(tmp_path: Path) -> None:
    """Two instances on one box must not share one lock.

    Sharing it would be worse than no lock: KAL-LE's run would block Salem's
    while neither protects the other's cursor.
    """
    assert run_lock_path(tmp_path, "Salem") != run_lock_path(tmp_path, "KAL-LE")


def test_a_blank_instance_is_refused_not_defaulted(tmp_path: Path) -> None:
    """An unscoped path is shared across every instance on the box."""
    with pytest.raises(DripConfigError) as exc:
        run_lock_path(tmp_path, "   ")
    assert "instance name" in str(exc.value)


# ---------------------------------------------------------------------------
# The mechanism, under real concurrency
# ---------------------------------------------------------------------------


def _hold_and_record(data_dir: str, instance: str, marker: str) -> None:
    """Take the lock, record an enter/exit pair around a real time window."""
    log_file = Path(data_dir) / "critical.log"
    with run_lock(data_dir, instance) as acquired:
        if not acquired:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"skipped {marker}\n")
            return
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"enter {marker}\n")
        # A window wide enough that an unguarded second process lands inside it.
        time.sleep(0.35)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"exit {marker}\n")


def test_two_processes_never_overlap_inside_the_lock(tmp_path: Path) -> None:
    """THE mechanism pin. Mutation: drop the flock in ``run_lock`` -> fails.

    Asserts on the SHAPE of the interleaving, not just a count: a second
    process either waits its turn or stands down, so ``enter`` is never seen
    twice before the matching ``exit``. A count alone would pass against a
    build where both ran and one crashed.
    """
    ctx = mp.get_context("fork")
    procs = [
        ctx.Process(target=_hold_and_record, args=(str(tmp_path), _INSTANCE, m))
        for m in ("A", "B")
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    lines = (tmp_path / "critical.log").read_text().splitlines()
    # Exactly one process did the work; the other stood down (non-blocking).
    assert sum(1 for line in lines if line.startswith("enter")) == 1, lines
    assert sum(1 for line in lines if line.startswith("skipped")) == 1, lines
    # And the one that ran completed its section.
    assert sum(1 for line in lines if line.startswith("exit")) == 1, lines


def test_the_lock_is_released_so_the_next_run_proceeds(tmp_path: Path) -> None:
    """A held-then-released lock must not wedge every later run.

    The failure this guards is a lock leaked on the success path, which looks
    fine for one run and silently stops the campaign forever after.
    """
    with run_lock(tmp_path, _INSTANCE) as first:
        assert first is True
    with run_lock(tmp_path, _INSTANCE) as second:
        assert second is True


def test_a_second_holder_is_refused_while_the_first_holds(tmp_path: Path) -> None:
    """flock is per open-file-description, so this holds within one process."""
    with run_lock(tmp_path, _INSTANCE) as first:
        assert first is True
        with run_lock(tmp_path, _INSTANCE) as second:
            assert second is False


def test_standing_down_is_logged_with_the_lock_path(tmp_path: Path) -> None:
    """ILB: 'another run holds it' must be greppable, not inferred from silence."""
    with run_lock(tmp_path, _INSTANCE):
        with structlog.testing.capture_logs() as captured:
            with run_lock(tmp_path, _INSTANCE) as second:
                assert second is False
    busy = [c for c in captured if c.get("event") == "drip.run_lock.busy"]
    assert len(busy) == 1
    assert busy[0]["lock_path"] == str(run_lock_path(tmp_path, _INSTANCE))
    assert "standing down" in busy[0]["detail"]


# ---------------------------------------------------------------------------
# The wiring — cmd_run, the production entry point
# ---------------------------------------------------------------------------


class _RecordingCampaign:
    """A campaign that records every item it was asked to work."""

    name = "batch_image"

    def __init__(self, sink: Path) -> None:
        self.sink = sink

    def worklist(self) -> list[str]:
        return ["item-1", "item-2"]

    def work(self, item_id: str) -> None:
        with open(self.sink, "a", encoding="utf-8") as f:
            f.write(f"{item_id}\n")

    def verify(self, item_id: str) -> bool:
        return self.sink.exists() and item_id in self.sink.read_text()

    def spends_quota(self) -> bool:
        return True

    def verify_is_async(self) -> bool:
        return False


def _config(tmp_path: Path) -> DripConfig:
    return DripConfig(
        enabled=True,
        vault_path=str(tmp_path / "vault"),
        data_dir=str(tmp_path),
        instance=_INSTANCE,
        campaigns={"batch_image": CampaignConfig(
            kind="batch_image", enabled=True, api_key="DUMMY_ANTHROPIC_TEST_KEY",
        )},
    )


@pytest.fixture()
def _fake_campaign(monkeypatch, tmp_path: Path) -> Path:
    sink = tmp_path / "worked.log"
    monkeypatch.setattr(
        drip_cli, "build_campaign",
        lambda name, ccfg, cfg: _RecordingCampaign(sink),
    )
    return sink


def test_cmd_run_works_items_when_the_lock_is_free(
    _fake_campaign: Path, tmp_path: Path,
) -> None:
    """Baseline: the guard must not break the ordinary path."""
    assert cmd_run(_config(tmp_path), apply=True) == 0
    assert _fake_campaign.read_text().split() == ["item-1", "item-2"]


def test_cmd_run_stands_down_while_another_run_holds_the_lock(
    _fake_campaign: Path, tmp_path: Path, capsys,
) -> None:
    """THE wiring pin — and it asserts the VICTIM state, not just the exit code.

    "Returned 0" is satisfied by a build with no lock at all (the ordinary
    success path also returns 0), so the load-bearing assertion is that NOTHING
    WAS WORKED: the sink file must not exist. That is the fact that separates
    "stood down" from "ran anyway".
    """
    with run_lock(tmp_path, _INSTANCE) as held:
        assert held is True
        rc = cmd_run(_config(tmp_path), apply=True)

    assert rc == 0, "a stand-down is not a failure — the work is already covered"
    assert not _fake_campaign.exists(), (
        "cmd_run worked items while another run held the lock"
    )
    out = capsys.readouterr().out
    assert "another run is already in progress" in out
    assert "standing down" in out


def test_a_dry_run_ignores_the_lock(
    _fake_campaign: Path, tmp_path: Path, capsys,
) -> None:
    """A read-only 'what would run?' must not be refused for an hour.

    Asserts the dry run REPORTED (its campaign line reached stdout) rather than
    merely returning 0, which the stand-down path also does.
    """
    with run_lock(tmp_path, _INSTANCE):
        rc = cmd_run(_config(tmp_path), apply=False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry run]" in out
    assert "another run is already in progress" not in out
    # Dry runs claim nothing and spend nothing.
    assert not _fake_campaign.exists()

"""``drip repair-verify --apply`` takes the run lock (#89).

The demotion is a read-modify-write on the SAME per-campaign state file
``run_increment`` writes, and it was happening outside the lock the hourly
timer holds. The interleaving loses data in either direction and BOTH
processes report success — the exact signature ``run_lock`` was introduced
for.

The last test is a successor pin: any FUTURE caller of ``run_increment``
must be inside the lock. One caller is easy to keep honest by reading it;
the second one is the one that gets it wrong.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import structlog

from alfred.drip import cli as drip_cli
from alfred.drip.config import DripConfig


class _Recorder:
    """Stands in for ``run_lock``, recording whether it was entered."""

    def __init__(self, acquired: bool = True):
        self.acquired = acquired
        self.calls: list[tuple] = []
        self.entered = 0

    def __call__(self, data_dir, instance):
        self.calls.append((data_dir, instance))
        return self

    def __enter__(self):
        self.entered += 1
        return self.acquired

    def __exit__(self, *a):
        return False


@pytest.fixture
def config(tmp_path):
    return DripConfig(
        data_dir=str(tmp_path / "data"),
        instance="testbox",
        campaigns={},
    )


@pytest.fixture(autouse=True)
def _no_campaigns(monkeypatch):
    """Default: one selected campaign that repairs nothing.

    The lock behaviour is what is under test, so the campaign body is stubbed
    down to the smallest thing that still exercises select -> lock -> pass.
    """
    monkeypatch.setattr(
        drip_cli, "_select_configured", lambda cfg, name: {"link001": object()},
    )
    monkeypatch.setattr(
        drip_cli, "build_campaign", lambda name, ccfg, cfg: object(),
    )
    monkeypatch.setattr(
        drip_cli, "campaign_state_path", lambda d, i, n: Path("/tmp/never-read"),
    )
    monkeypatch.setattr(drip_cli, "load_state", lambda p, n: object())

    class _Result:
        audited = 0
        demoted = 0
        confirmed = 0
        unverifiable = 0
        demoted_items: list = []
        unverifiable_items: list = []

    monkeypatch.setattr(
        drip_cli, "repair_false_dones", lambda c, s, apply: _Result(),
    )


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def test_apply_takes_the_run_lock(monkeypatch, config):
    lock = _Recorder(acquired=True)
    monkeypatch.setattr(drip_cli, "run_lock", lock)

    assert drip_cli.cmd_repair_verify(config, apply=True) == 0
    assert lock.entered == 1
    assert lock.calls == [(config.data_dir, config.instance)]


def test_a_dry_run_takes_NO_lock(monkeypatch, config):
    """A preview writes no state. Refusing to tell an operator what repair
    WOULD do because a run is in progress denies the answer at the moment it
    is most worth having — the same reasoning ``cmd_run`` applies."""
    lock = _Recorder(acquired=True)
    monkeypatch.setattr(drip_cli, "run_lock", lock)

    assert drip_cli.cmd_repair_verify(config, apply=False) == 0
    assert lock.entered == 0
    assert lock.calls == []


def test_a_busy_lock_stands_down_without_touching_state(monkeypatch, config):
    """FAIL-SAFE: no state is read or written when the lock is held."""
    lock = _Recorder(acquired=False)
    monkeypatch.setattr(drip_cli, "run_lock", lock)

    loads: list = []
    monkeypatch.setattr(
        drip_cli, "load_state",
        lambda p, n: loads.append(p) or object(),
    )
    saves: list = []
    monkeypatch.setattr(drip_cli, "save_state", lambda p, s: saves.append(p))

    assert drip_cli.cmd_repair_verify(config, apply=True) == 0
    assert loads == []
    assert saves == []


def test_the_busy_message_does_NOT_claim_the_work_is_covered(
    monkeypatch, config, capsys,
):
    """A skipped RUN is covered by the run holding the lock. A skipped
    REPAIR is not covered by anything — ``run`` never re-verifies done rows —
    so this must not borrow ``run``'s "its work covers this one" wording."""
    monkeypatch.setattr(drip_cli, "run_lock", _Recorder(acquired=False))

    with structlog.testing.capture_logs() as captured:
        drip_cli.cmd_repair_verify(config, apply=True)

    out = capsys.readouterr().out
    assert "WITHOUT repairing" in out
    assert "re-run this command" in out
    assert "covers this one" not in out

    events = [c for c in captured
              if c.get("event") == "drip.repair_verify.lock_busy"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert "NOT covered" in events[0]["detail"]


def test_a_busy_lock_still_exits_zero(monkeypatch, config):
    """Not a failure — the operator is told to re-issue, and a non-zero exit
    would make the hourly timer's logs look broken."""
    monkeypatch.setattr(drip_cli, "run_lock", _Recorder(acquired=False))
    assert drip_cli.cmd_repair_verify(config, apply=True) == 0


def test_no_campaigns_short_circuits_before_the_lock(monkeypatch, config):
    """Nothing to repair means nothing to serialize; taking the lock would
    make an empty no-op contend with a real run."""
    monkeypatch.setattr(drip_cli, "_select_configured", lambda cfg, name: {})
    lock = _Recorder(acquired=True)
    monkeypatch.setattr(drip_cli, "run_lock", lock)

    assert drip_cli.cmd_repair_verify(config, apply=True) == 0
    assert lock.entered == 0


def test_the_repair_body_is_not_duplicated():
    """One copy under the lock and one without is exactly how the two drift.
    The apply and dry-run paths must delegate to the SAME helper."""
    source = inspect.getsource(drip_cli.cmd_repair_verify)
    assert source.count("_repair_all(") == 2
    assert "def _repair_all" in inspect.getsource(drip_cli)


# ---------------------------------------------------------------------------
# The successor pin
# ---------------------------------------------------------------------------


def _enclosing_functions_with_run_lock(tree: ast.AST) -> dict[str, bool]:
    """For each function that calls ``run_increment``, whether that call is
    lexically inside a ``with run_lock(...)`` block."""
    results: dict[str, bool] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.fn_stack: list[str] = []
            self.lock_depth = 0

        def visit_FunctionDef(self, node):  # noqa: N802
            self.fn_stack.append(node.name)
            self.generic_visit(node)
            self.fn_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_With(self, node):  # noqa: N802
            guarded = any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "run_lock"
                for item in node.items
            )
            if guarded:
                self.lock_depth += 1
            self.generic_visit(node)
            if guarded:
                self.lock_depth -= 1

        def visit_Call(self, node):  # noqa: N802
            if isinstance(node.func, ast.Name) and node.func.id == "run_increment":
                name = self.fn_stack[-1] if self.fn_stack else "<module>"
                results[name] = self.lock_depth > 0
            self.generic_visit(node)

    Visitor().visit(tree)
    return results


def test_every_run_increment_caller_is_under_the_run_lock():
    """SUCCESSOR PIN. ``run_increment`` has ONE caller today and it is
    correct; the hazard is the SECOND one. Two concurrent increments over a
    6-item list produced 12 work() calls and both reported a clean done=6 —
    a doubled bill that nothing anywhere reported as a problem.

    ``_drain`` is the deliberate exception: it is the body ``cmd_run`` calls
    from INSIDE the lock for apply, and outside it only for a dry run that
    claims nothing and spends nothing. It is asserted explicitly rather than
    skipped silently, so a future edit that makes ``_drain`` reachable with
    apply=True outside the lock still has to come through this test.
    """
    tree = ast.parse(Path(drip_cli.__file__).read_text(encoding="utf-8"))
    callers = _enclosing_functions_with_run_lock(tree)

    assert callers, "run_increment has no callers in drip/cli.py — pin is stale"
    assert set(callers) == {"_drain"}, (
        f"NEW run_increment caller(s): {sorted(set(callers) - {'_drain'})}. "
        "Every caller that can apply must run inside `with run_lock(...)` — "
        "two concurrent increments process every item twice and both report "
        "success. Take the lock, then extend this pin."
    )

    # ``_drain``'s own guard: it is only ever CALLED from inside the lock on
    # the apply path. Pin that shape at its call sites in cmd_run.
    run_src = inspect.getsource(drip_cli.cmd_run)
    assert "_drain(selected, config, day=day, apply=False)" in run_src
    assert "with run_lock(" in run_src
    lock_pos = run_src.index("with run_lock(")
    apply_call = run_src.index("apply=True)")
    assert apply_call > lock_pos, (
        "_drain(apply=True) must appear INSIDE the run_lock block"
    )


def test_repair_verify_saves_state_only_under_the_lock():
    """The #89 defect in one assertion: ``save_state`` in the repair path
    must not be reachable outside the lock."""
    tree = ast.parse(Path(drip_cli.__file__).read_text(encoding="utf-8"))

    savers: dict[str, bool] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.fn_stack: list[str] = []

        def visit_FunctionDef(self, node):  # noqa: N802
            self.fn_stack.append(node.name)
            self.generic_visit(node)
            self.fn_stack.pop()

        def visit_Call(self, node):  # noqa: N802
            if isinstance(node.func, ast.Name) and node.func.id == "save_state":
                savers[self.fn_stack[-1] if self.fn_stack else "<module>"] = True
            self.generic_visit(node)

    Visitor().visit(tree)

    # The repair path's save lives in _repair_all, which cmd_repair_verify
    # only reaches with apply=True from inside the lock.
    assert "_repair_all" in savers, (
        "the repair save moved — re-point this pin at its new home and "
        "confirm the lock still wraps it"
    )
    assert "cmd_repair_verify" not in savers, (
        "save_state is back in cmd_repair_verify, which runs OUTSIDE the lock "
        "on the dry-run path"
    )

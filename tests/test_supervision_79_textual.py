"""#59 — Textual supervision path routes exit-79 through the shared policy.

The Textual live dashboard's ``_check_workers`` must handle a sovereign runtime
breach (exit 79 in a sovereign instance) by tearing the app down and latching a
breach flag — NOT restarting it up to 5× (the pre-#59 bug). The per-worker
decision is extracted into ``_decide_dead_worker`` so it is unit-testable
without a mounted app (the card-update render tail needs a live screen stack);
one integration test drives full ``_check_workers`` for the abort early-return
+ ``self.exit()``.

Skipped when Textual isn't installed — the Textual path only exists then (the
Rich behavioral pins in test_supervision_79_consistency.py + the classifier
truth table cover the policy regardless)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from alfred.orchestrator import (  # noqa: E402 — after importorskip by design
    CHILD_EXIT_ABORT_SOVEREIGN,
    CHILD_EXIT_DROP,
    CHILD_EXIT_RESTART,
)
from alfred.tui.app import AlfredApp  # noqa: E402


class _FakeDead:
    """multiprocessing.Process stand-in: already dead with a fixed exitcode."""

    def __init__(self, code: int | None) -> None:
        self._code = code

    def is_alive(self) -> bool:
        return False

    @property
    def exitcode(self) -> int | None:
        return self._code

    @property
    def pid(self) -> int | None:
        return None


def _app(tmp_path: Path, procs: dict, sovereign_enabled: bool) -> AlfredApp:
    app = AlfredApp(
        tools=list(procs),
        processes=procs,
        restart_counts={t: 0 for t in procs},
        start_process=lambda t: _FakeDead(0),
        sentinel_path=None,
        log_dir=tmp_path,
        state_dir=tmp_path,
        sovereign_enabled=sovereign_enabled,
    )
    # Stub the message-pump touch points so the decision is testable off-app.
    app.notify = lambda *a, **k: None
    return app


# ---------------------------------------------------------------------------
# _decide_dead_worker — the extracted, app-lifecycle-free decision
# ---------------------------------------------------------------------------


def test_textual_decide_aborts_on_sovereign_79(tmp_path: Path) -> None:
    app = _app(tmp_path, {"curator": _FakeDead(79)}, sovereign_enabled=True)
    w = app._data.workers["curator"]
    decision = app._decide_dead_worker("curator", w, 79, now=1.0)
    assert decision == CHILD_EXIT_ABORT_SOVEREIGN
    assert app._sovereign_breach is True
    assert app._restart_counts["curator"] == 0, "a breach must NOT restart"
    assert w.status == "stopped"


def test_textual_decide_drops_79_in_nonsovereign(tmp_path: Path) -> None:
    app = _app(tmp_path, {"curator": _FakeDead(79)}, sovereign_enabled=False)
    w = app._data.workers["curator"]
    decision = app._decide_dead_worker("curator", w, 79, now=1.0)
    assert decision == CHILD_EXIT_DROP
    assert app._sovereign_breach is False, "non-sovereign 79 must not breach"
    assert "curator" not in app._active_tools, "79 is no-restart → dropped"
    assert app._restart_counts["curator"] == 0


def test_textual_decide_drops_78(tmp_path: Path) -> None:
    app = _app(tmp_path, {"surveyor": _FakeDead(78)}, sovereign_enabled=True)
    w = app._data.workers["surveyor"]
    decision = app._decide_dead_worker("surveyor", w, 78, now=1.0)
    assert decision == CHILD_EXIT_DROP
    assert "surveyor" not in app._active_tools
    assert app._restart_counts["surveyor"] == 0
    assert app._sovereign_breach is False


def test_textual_decide_restarts_generic(tmp_path: Path) -> None:
    # Sovereign instance but a GENERIC exit (1) → still restarts (only 79 aborts).
    app = _app(tmp_path, {"curator": _FakeDead(1)}, sovereign_enabled=True)
    w = app._data.workers["curator"]
    decision = app._decide_dead_worker("curator", w, 1, now=1.0)
    assert decision == CHILD_EXIT_RESTART
    assert app._restart_counts["curator"] == 1, "generic crash → restart accounting"
    assert w.status == "restarting"
    assert app._sovereign_breach is False


# ---------------------------------------------------------------------------
# _check_workers integration — abort early-returns + calls self.exit()
# ---------------------------------------------------------------------------


def test_textual_check_workers_tears_down_on_sovereign_79(tmp_path: Path) -> None:
    """Full _check_workers on a sovereign 79: latches the breach AND calls
    self.exit() (ends app.run() → run_textual_dashboard returns the breach),
    without restarting. The abort path returns before the render tail, so this
    runs on a non-mounted app."""
    app = _app(tmp_path, {"curator": _FakeDead(79)}, sovereign_enabled=True)
    exit_calls: list[bool] = []
    app.exit = lambda *a, **k: exit_calls.append(True)

    app._check_workers()

    assert app._sovereign_breach is True
    assert exit_calls == [True], "sovereign breach must tear the app down"
    assert app._restart_counts["curator"] == 0, "must not restart on breach"

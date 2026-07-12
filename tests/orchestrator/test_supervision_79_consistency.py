"""#59 — one shared supervision policy across all three supervisors.

``classify_child_exit`` is the single source of truth; this pins its truth
table AND the Rich ``run_live_dashboard`` behavioral wiring (the Textual path
is pinned in ``tests/test_supervision_79_textual.py``; the plain ``run_all``
loop keeps the four #42 pins in ``test_sovereign_propagation.py``).

Rich is patched headless: ``Live`` → a dummy context manager, the three tail
threads → no-op stubs, so the test exercises the supervision decision without a
terminal or real threads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import alfred.dashboard as dashboard
from alfred.orchestrator import (
    CHILD_EXIT_ABORT_SOVEREIGN,
    CHILD_EXIT_DROP,
    CHILD_EXIT_RESTART,
    classify_child_exit,
)


# ---------------------------------------------------------------------------
# Pure classifier truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exit_code,sovereign_enabled,expected",
    [
        (78, True, CHILD_EXIT_DROP),       # missing deps → drop (regardless of sovereign)
        (78, False, CHILD_EXIT_DROP),
        (79, True, CHILD_EXIT_ABORT_SOVEREIGN),   # sovereign breach in a sovereign instance → abort-all
        (79, False, CHILD_EXIT_DROP),      # 79 in a NON-sovereign instance → drop (no abort)
        (0, True, CHILD_EXIT_RESTART),     # clean-but-unexpected return → restart
        (0, False, CHILD_EXIT_RESTART),
        (1, True, CHILD_EXIT_RESTART),     # generic crash → restart
        (1, False, CHILD_EXIT_RESTART),
        (255, False, CHILD_EXIT_RESTART),
        (None, True, CHILD_EXIT_RESTART),  # None (killed/unknown) → restart (matches _is_no_restart_exit)
        (None, False, CHILD_EXIT_RESTART),
    ],
)
def test_classify_child_exit_truth_table(exit_code, sovereign_enabled, expected) -> None:
    assert classify_child_exit(exit_code, sovereign_enabled=sovereign_enabled) == expected


def test_classify_child_exit_79_abort_requires_sovereign() -> None:
    """The abort verdict is the ONLY one gated on sovereign_enabled — 79 aborts
    iff sovereign, else it merely drops. This is the crux of the #59 fix."""
    assert classify_child_exit(79, sovereign_enabled=True) == CHILD_EXIT_ABORT_SOVEREIGN
    assert classify_child_exit(79, sovereign_enabled=False) == CHILD_EXIT_DROP


# ---------------------------------------------------------------------------
# Rich run_live_dashboard — headless harness
# ---------------------------------------------------------------------------


class _FakeDead:
    """A multiprocessing.Process stand-in that is already dead with a code."""

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


class _SentinelAfter:
    """Path-like whose ``.exists()`` returns False for the first ``after``
    calls, then True — deterministically ends the Live loop after N ticks with
    no wall-clock timing."""

    def __init__(self, after: int = 1) -> None:
        self._n = 0
        self._after = after

    def exists(self) -> bool:
        self._n += 1
        return self._n > self._after


class _DummyLive:
    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False

    def update(self, *a, **k) -> None:
        pass


class _DummyThread:
    def __init__(self, *a, **k) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def rich_headless(monkeypatch: pytest.MonkeyPatch):
    """Patch the Rich Live + the three tail threads so run_live_dashboard runs
    without a terminal or real background threads."""
    monkeypatch.setattr(dashboard, "Live", _DummyLive)
    monkeypatch.setattr(dashboard, "LogTailThread", _DummyThread)
    monkeypatch.setattr(dashboard, "AuditTailThread", _DummyThread)
    monkeypatch.setattr(dashboard, "StatReaderThread", _DummyThread)
    return None


def _run_rich(tmp_path, *, procs, sovereign_enabled, sentinel_path=None, started=None):
    return dashboard.run_live_dashboard(
        tools=list(procs.keys()),
        processes=procs,
        restart_counts={t: 0 for t in procs},
        start_process=lambda t: (started.append(t) if started is not None else None) or _FakeDead(0),
        sentinel_path=sentinel_path,
        log_dir=tmp_path,
        state_dir=tmp_path,
        sovereign_enabled=sovereign_enabled,
    )


def test_rich_dashboard_aborts_on_sovereign_79(tmp_path: Path, rich_headless) -> None:
    """Sovereign instance + a slot exits 79 → run_live_dashboard returns True
    (breach) and NEVER restarts (start_process not called). The breach breaks
    the loop immediately (no sentinel needed)."""
    started: list[str] = []
    result = _run_rich(
        tmp_path, procs={"curator": _FakeDead(79)}, sovereign_enabled=True,
        sentinel_path=None, started=started,
    )
    assert result is True, "sovereign 79 must return breach True"
    assert started == [], "a sovereign breach must NOT restart anything"


def test_rich_dashboard_drops_79_in_nonsovereign(tmp_path: Path, rich_headless) -> None:
    """NON-sovereign instance + a slot exits 79 → dropped (no restart), and the
    dashboard returns False (no false propagation). The sentinel ends the loop
    after the drop tick."""
    started: list[str] = []
    result = _run_rich(
        tmp_path, procs={"curator": _FakeDead(79)}, sovereign_enabled=False,
        sentinel_path=_SentinelAfter(after=1), started=started,
    )
    assert result is False, "non-sovereign 79 must NOT propagate a breach"
    assert started == [], "79 is always no-restart — even non-sovereign"


def test_rich_dashboard_drops_78(tmp_path: Path, rich_headless) -> None:
    """78 (missing deps) drops without restart in the Rich path too."""
    started: list[str] = []
    result = _run_rich(
        tmp_path, procs={"surveyor": _FakeDead(78)}, sovereign_enabled=True,
        sentinel_path=_SentinelAfter(after=1), started=started,
    )
    assert result is False
    assert started == []

"""Tests for ``alfred.janitor.issues.SweepResult`` robustness.

Scope: defense in depth on ``from_dict`` coercion of counter fields and
the ``daemon.deep_sweep_fix_mode`` observability log event introduced
after the 2026-04-21 "deep sweep fixed=None, deleted=None" investigation.

The daemon always writes integer counter fields; ``from_dict`` still
coerces ``None`` to ``0`` so stale or half-written state files can't
surface ``None`` values into status/history formatting.
"""

from __future__ import annotations

import inspect

from alfred.janitor import daemon
from alfred.janitor.issues import SweepResult


def test_from_dict_coerces_none_counters_to_zero() -> None:
    """A state dict with explicit ``None`` values for counter fields
    should round-trip as integer zeros, not propagate ``None``.

    ``.get(key, default)`` only returns the default when the key is
    MISSING — if the key is present with value ``None`` the None is
    returned. That used to turn ``alfred janitor history`` into a pretty
    formatter crash when printing the ``Fixed`` column. The fix uses
    ``value or 0`` so ``None`` collapses to ``0`` at load time.
    """
    d = {
        "sweep_id": "abc12345",
        "timestamp": "2026-04-21T05:52:24Z",
        "files_scanned": None,
        "files_skipped": None,
        "issues_found": None,
        "issues_by_severity": None,
        "files_fixed": None,
        "files_deleted": None,
        "agent_invoked": None,
        "structural_only": None,
    }
    result = SweepResult.from_dict(d)
    assert result.files_scanned == 0
    assert result.files_skipped == 0
    assert result.issues_found == 0
    assert result.issues_by_severity == {}
    assert result.files_fixed == 0
    assert result.files_deleted == 0
    assert result.agent_invoked is False
    assert result.structural_only is False


def test_from_dict_preserves_real_integer_counters() -> None:
    """Integer counters must round-trip unchanged — the ``None``->0
    coercion is defense in depth, not a replacement for the normal path.
    """
    d = {
        "sweep_id": "deadbeef",
        "timestamp": "2026-04-21T05:52:24Z",
        "files_scanned": 1234,
        "files_skipped": 5,
        "issues_found": 575,
        "issues_by_severity": {"CRITICAL": 10, "WARNING": 200, "INFO": 365},
        "files_fixed": 32,
        "files_deleted": 0,
        "agent_invoked": True,
        "structural_only": False,
    }
    result = SweepResult.from_dict(d)
    assert result.files_scanned == 1234
    assert result.files_skipped == 5
    assert result.issues_found == 575
    assert result.issues_by_severity == {
        "CRITICAL": 10,
        "WARNING": 200,
        "INFO": 365,
    }
    assert result.files_fixed == 32
    assert result.files_deleted == 0
    assert result.agent_invoked is True
    assert result.structural_only is False


def test_from_dict_missing_keys_still_defaults_to_zero() -> None:
    """Original behaviour — totally missing keys must still default to 0."""
    d = {"sweep_id": "x"}
    result = SweepResult.from_dict(d)
    assert result.files_scanned == 0
    assert result.files_fixed == 0
    assert result.files_deleted == 0
    assert result.agent_invoked is False
    assert result.structural_only is False


def test_run_watch_emits_deep_sweep_fix_mode_event_in_both_branches() -> None:
    """``daemon.run_watch`` must emit ``daemon.deep_sweep_fix_mode`` on
    BOTH the skip branch (``fix_mode=False``) and the proceed branch
    (``fix_mode=True``) so operators can grep a single event name to see
    whether fix mode engaged on any given deep-sweep tick.

    This is a source-level assertion rather than a live daemon
    integration test — ``run_watch`` is an infinite loop with heavy
    async I/O and spinning it up inside pytest is out of scope for a
    regression of a single log line. The source check is load-bearing:
    the bug mode we are guarding against is the event being removed or
    renamed without anyone noticing until an operator goes looking for
    it during an incident.
    """
    src = inspect.getsource(daemon.run_watch)
    # Both branches must emit the event name literal.
    assert src.count('"daemon.deep_sweep_fix_mode"') >= 2, (
        "daemon.deep_sweep_fix_mode should be emitted in both the "
        "skipped and proceed branches of the deep-sweep gate"
    )
    # The proceed branch must emit fix_mode=True; the skip branch,
    # fix_mode=False. These literals are matched loosely because the
    # kwargs can appear in any order.
    assert "fix_mode=True" in src
    assert "fix_mode=False" in src


def test_to_dict_from_dict_roundtrip_never_emits_none() -> None:
    """End-to-end: a freshly constructed SweepResult -> to_dict ->
    from_dict must preserve integer-typed counter fields. Guards against
    silent regressions where a new counter field lands in the dataclass
    but not in to_dict/from_dict."""
    original = SweepResult(sweep_id="abc")
    original.files_fixed = 7
    original.files_deleted = 2
    original.issues_found = 42
    round_tripped = SweepResult.from_dict(original.to_dict())
    assert round_tripped.files_fixed == 7
    assert round_tripped.files_deleted == 2
    assert round_tripped.issues_found == 42
    # Counters are int — never None, even after a round trip.
    assert isinstance(round_tripped.files_fixed, int)
    assert isinstance(round_tripped.files_deleted, int)

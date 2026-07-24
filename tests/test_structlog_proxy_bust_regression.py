"""Regression pin for the ``_bust_structlog_lazy_proxy_cache`` fixture's
stale-monkeypatched-method stripping (diagnosed 2026-07-24).

Order-INDEPENDENT coverage for the interaction that broke the brief
crash-guard tests (``test_brief_watches`` / ``_weather`` / ``_process_hub``)
in the full suite:

  A ``structlog`` ``BoundLoggerLazyProxy`` resolves ``log.warning`` (and the
  other log methods) dynamically via ``__getattr__`` — they are NOT real
  instance attributes. When a test spies on one with
  ``monkeypatch.setattr(mod.log, "warning", spy)``, monkeypatch snapshots the
  RESOLVED bound method as the "original" (``hasattr`` is True) and, on
  teardown, *restores* it by SETTING ``log.__dict__["warning"]``. That leaves
  a real instance attribute shadowing ``__getattr__`` for the rest of the
  process, with its ``_processors`` frozen to an orphaned list —
  ``structlog.testing.capture_logs()`` (which swaps the CURRENT config list)
  can no longer reach it, so the event renders through the frozen chain and
  ``capture_logs`` returns ``[]``.

The end-to-end brief crash-guard tests only exercise this when
``test_brief_dispatch`` (the real spy primer) happens to collect first, which
is fragile. These two tests reproduce the primer→victim sequence in a single
file (same-file definition order is deterministic) so the fixture's fix stays
pinned regardless of collection order.
"""

from __future__ import annotations

import structlog

import alfred.brief.daemon as brief_daemon


def test_spy_primer_leaves_stale_method_attr_without_bust(monkeypatch) -> None:
    """The spy pattern installs a real ``warning`` instance attribute; after
    ``monkeypatch.undo`` (test teardown) it would normally persist. This test
    documents the primer half — the shadow IS installed while the spy is
    active."""
    assert "warning" not in brief_daemon.log.__dict__  # clean start (bust ran)
    monkeypatch.setattr(brief_daemon.log, "warning", lambda *a, **k: None)
    # Shadow now installed as a real instance attribute (the trap).
    assert "warning" in brief_daemon.log.__dict__


def test_capture_logs_works_after_spy_primer() -> None:
    """The victim half: after the primer's ``monkeypatch.undo`` restored the
    stale ``warning`` attribute, ``_bust_structlog_lazy_proxy_cache`` (autouse)
    must have stripped it so ``log.warning`` re-resolves against the current
    config and ``capture_logs`` captures the event."""
    # _bust stripped the artifact the primer left behind.
    assert "warning" not in brief_daemon.log.__dict__

    with structlog.testing.capture_logs() as cap:
        brief_daemon.log.warning("regression.stale_spy_probe", marker=42)

    events = [e for e in cap if e.get("event") == "regression.stale_spy_probe"]
    assert len(events) == 1, f"capture_logs missed the event: {cap}"
    assert events[0]["marker"] == 42

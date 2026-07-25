"""#34 curator on-failure hardening — retry-in-place-then-quarantine.

Replaces the silent-loss path: the daemon used to `mark_processed` (move to
`inbox/processed/` + mark done) UNCONDITIONALLY on a `claude -p` failure, so a
raw email was moved out of inbox and marked processed but never structured —
the 39-item loss during the 2026-07 outage.

These pins prove the new behavior via the ACTUAL disposition functions
(`_handle_processing_failure` for a failure, `mark_processed` for a recovery)
and the REAL `watcher.full_scan`:
  * a transient failure leaves the file in inbox UNMARKED → full_scan re-picks
    it → self-heal on the next tick (build-time care: the self-heal pin);
  * a sustained failure quarantines to `failed/` after `max_retries` (terminal:
    out of inbox so no loop, intact so recoverable);
  * success clears the counter;
  * the legacy `processed` escape hatch reproduces the old behavior;
  * the handler is exception-safe (a failing move must not propagate a loss).

Tests run unconditionally.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.curator.config import load_from_unified
from alfred.curator.state import StateManager
from alfred.curator.watcher import InboxWatcher
from alfred.curator.writer import mark_processed


def _config(vault: Path, *, action: str = "retry", max_retries: int = 3):
    return load_from_unified(
        {
            "vault": {"path": str(vault)},
            "curator": {"on_failure": {"action": action, "max_retries": max_retries}},
        }
    )


def _mk_inbox_file(vault: Path, name: str = "email-gmail-20260101-000000-x.md") -> Path:
    inbox = vault / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    f = inbox / name
    f.write_text("---\ntype: note\nname: X\n---\nbody\n", encoding="utf-8")
    return f


def _state(vault: Path) -> StateManager:
    s = StateManager(vault / "curator_state.json")
    s.load()
    return s


def _handle(f, state, config, reason="fail"):
    # Imported inside so the suite conftest / stubs (if any) don't shadow it.
    from alfred.curator.daemon import _handle_processing_failure

    _handle_processing_failure(f, state, config, reason)


# --- retry-in-place (transient self-heal) ---------------------------------


def test_first_failure_leaves_file_in_inbox_unmarked(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    f = _mk_inbox_file(vault)
    config, state = _config(vault), _state(vault)
    _handle(f, state, config, "agent_failed: blip")

    assert f.exists()                                    # still in inbox
    assert not (vault / "inbox/failed").exists()         # not quarantined
    proc = vault / "inbox/processed"
    assert not proc.exists() or not list(proc.glob("*.md"))  # not moved to processed
    assert state.state.failed_count(f.name) == 1
    assert not state.state.is_processed(f.name)
    # Frontmatter untouched → mtime pristine for Monitor A (#31).
    assert "status" not in frontmatter.load(str(f)).metadata


def test_full_scan_repicks_the_unmarked_failed_file(tmp_path: Path) -> None:
    """The self-heal mechanism: an un-marked failed file IS re-picked by
    full_scan (nothing — no state.processed dedup, no status skip — intervenes)."""
    vault = tmp_path / "v"
    f = _mk_inbox_file(vault)
    config, state = _config(vault), _state(vault)
    _handle(f, state, config)
    found = InboxWatcher(inbox_path=vault / "inbox").full_scan()
    assert f in found


def test_success_after_failures_clears_counter_and_moves(tmp_path: Path) -> None:
    """Fail twice (below max), then the recovery tick succeeds → counter cleared,
    file moved to processed/. Models failing-then-recovering backend end-to-end."""
    vault = tmp_path / "v"
    f = _mk_inbox_file(vault)
    config, state = _config(vault), _state(vault)
    _handle(f, state, config)
    _handle(f, state, config)
    assert state.state.failed_count(f.name) == 2
    assert f.exists()

    # Recovery tick: the SUCCESS-path disposition.
    mark_processed(f, config.vault.processed_path)
    state.state.mark_processed(f.name, str(f), [], [], "claude")

    assert state.state.failed_count(f.name) == 0          # cleared on success
    assert state.state.is_processed(f.name)
    assert not f.exists()                                 # moved out of inbox
    assert list((vault / "inbox/processed").glob("*.md"))


# --- quarantine (sustained → terminal) ------------------------------------


def test_quarantine_at_max_retries(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    f = _mk_inbox_file(vault)
    config, state = _config(vault, max_retries=3), _state(vault)
    _handle(f, state, config, "outage")   # 1
    _handle(f, state, config, "outage")   # 2
    assert f.exists()                      # still retrying below the cap
    _handle(f, state, config, "outage")   # 3 → quarantine

    assert not f.exists()                  # moved out of inbox
    quarantined = list((vault / "inbox/failed").glob("email-gmail-*.md"))
    assert len(quarantined) == 1
    assert state.state.failed_count(f.name) == 0          # cleared on quarantine
    assert frontmatter.load(str(quarantined[0])).metadata.get("status") == "failed"


def test_full_scan_ignores_quarantined_file(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    f = _mk_inbox_file(vault)
    config, state = _config(vault, max_retries=1), _state(vault)
    _handle(f, state, config, "outage")   # max_retries=1 → immediate quarantine
    assert list((vault / "inbox/failed").glob("*.md"))
    found = InboxWatcher(inbox_path=vault / "inbox").full_scan()
    assert f not in found                  # the failed/ subdir is not scanned


# --- legacy escape hatch ---------------------------------------------------


def test_legacy_processed_action_reproduces_old_behavior(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    f = _mk_inbox_file(vault)
    config, state = _config(vault, action="processed"), _state(vault)
    _handle(f, state, config)
    assert not f.exists()                                 # moved to processed/
    assert list((vault / "inbox/processed").glob("*.md"))
    assert state.state.is_processed(f.name)
    assert state.state.processed[f.name].backend_used == "failed_legacy_processed"


# --- exception-safety (build-time care) ------------------------------------


def test_handler_is_exception_safe(tmp_path: Path, monkeypatch) -> None:
    """If the quarantine move itself raises, the handler must NOT propagate —
    it runs inside the callers' except blocks, so a throw would re-introduce a
    loss path. Worst case: file stays in inbox."""
    vault = tmp_path / "v"
    f = _mk_inbox_file(vault)
    config, state = _config(vault, max_retries=1), _state(vault)

    import alfred.curator.daemon as d

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(d, "quarantine", _boom)
    # Must not raise:
    _handle(f, state, config, "outage")
    assert f.exists()                                     # safe default: not lost

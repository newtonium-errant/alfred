"""#53 — the mail state writer must not leave debris in whatever cwd it finds.

The named polluter. ``StateManager.save()`` does ``mkdir(parents=True)`` and
writes, and its path came from ``MailConfig.state_path``, whose default was a
cwd-relative ``./data/mail_state.json``. The writer is a DAEMON THREAD —
``orchestrator._maybe_start_mail_fetch_loop`` starts ``mail-fetch`` with
``daemon=True`` and it ticks forever — so under the suite it outlives the test
that started it and keeps writing against the process cwd.

Result: ``data/mail_state.json`` materialising in whatever tree the suite ran
from. Three victims, the last being reviewer-60's near-false-BLOCK on the #57
gate (two phantom failures traced to debris from a concurrent run).

Two independent halves are pinned here, because either alone leaves the hole
open:

  * **Absolute at construction** — the destination is fixed when the manager is
    built, so a later cwd change cannot redirect the write.
  * **Config-derived default** — the ``data/`` location comes from the
    instance's own ``logging.dir`` rather than a hardcoded ``./data`` repeated
    per module (the CLAUDE.md instance-scoped-state rule).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alfred.mail.config import load_from_unified
from alfred.mail.state import StateManager


# --- absolute at construction ----------------------------------------------


def test_a_relative_path_is_resolved_when_the_manager_is_built(tmp_path, monkeypatch):
    """The anchor. Construct under one cwd, write under another — the file must
    land where it was aimed, not where the process happened to wander.

    This is the daemon-thread scenario in miniature: the config was built while
    a test held its tmp cwd, the thread ticks later once that cwd is gone."""
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.chdir(home)
    mgr = StateManager("./data/mail_state.json")

    monkeypatch.chdir(elsewhere)  # the cwd moves out from under the writer
    mgr.save()

    assert (home / "data" / "mail_state.json").exists(), "write did not follow its anchor"
    assert not (elsewhere / "data").exists(), "the write followed the cwd — unanchored"


def test_the_stored_path_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert StateManager("./data/mail_state.json").path.is_absolute()


def test_an_absolute_path_is_preserved(tmp_path):
    """The anchor must not rewrite a path the operator gave explicitly."""
    target = tmp_path / "explicit" / "m.json"
    assert StateManager(target).path == target.resolve()


# --- config-derived default -------------------------------------------------


def test_the_default_follows_the_configured_data_dir(tmp_path):
    """Per-instance state lands in the INSTANCE's data dir. A default that
    ignores ``logging.dir`` is how one instance's writer reaches another's
    store (CLAUDE.md state-path rule)."""
    cfg = load_from_unified({"mail": {}, "logging": {"dir": str(tmp_path / "inst")}})
    assert cfg.state_path == f"{tmp_path / 'inst'}/mail_state.json"
    assert cfg.fetch.shadow_dir == f"{tmp_path / 'inst'}/mail_shadow"


def test_an_explicit_state_path_still_wins(tmp_path):
    cfg = load_from_unified({
        "mail": {"state": {"path": "/explicit/m.json"}},
        "logging": {"dir": str(tmp_path)},
    })
    assert cfg.state_path == "/explicit/m.json"


def test_production_shape_is_unchanged(tmp_path):
    """The deploy-safety pin. Production runs with ``logging.dir: ./data`` and
    cwd = WorkingDirectory; this change must not relocate Salem's live mail
    state. If this ever fails, the fix has become a migration."""
    assert load_from_unified({"mail": {}}).state_path == "./data/mail_state.json"
    assert load_from_unified(
        {"mail": {}, "logging": {"dir": "./data"}}
    ).state_path == "./data/mail_state.json"


# --- the debris pin the dispatch asked for ----------------------------------


def test_the_fetch_path_leaves_no_debris_outside_the_configured_state_dir(
    tmp_path, monkeypatch,
):
    """Run the offending path from a FOREIGN cwd and assert nothing
    materialises there.

    Drives ``fetch_all`` — the function the daemon thread calls each tick —
    rather than ``StateManager`` directly, because the defect lived in the
    path handed ACROSS that boundary, not inside either side of it.

    The assertion is deliberately "did anything appear in this tree at all",
    not "was mail_state.json created": a debris guard that names the one file
    it already knows about cannot catch the next one.
    """
    from alfred.mail.fetcher import fetch_all

    foreign = tmp_path / "foreign_cwd"
    foreign.mkdir()
    state_dir = tmp_path / "configured_state"
    vault = tmp_path / "vault"
    vault.mkdir()

    cfg = load_from_unified({
        "mail": {},                       # no accounts → fetch is a no-op that still saves
        "logging": {"dir": str(state_dir)},
    })

    monkeypatch.chdir(foreign)
    fetch_all(cfg, vault, only_flagged=True)

    leaked = sorted(p for p in foreign.rglob("*") if p.is_file())
    assert leaked == [], f"debris left in the cwd: {[str(p) for p in leaked]}"
    assert (state_dir / "mail_state.json").exists(), (
        "state went somewhere other than the configured dir"
    )

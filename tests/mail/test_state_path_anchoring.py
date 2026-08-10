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

from alfred.mail.config import MailAccount, MailConfig, load_from_unified
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


# ===========================================================================
# #75 — the residue #53 left behind
#
# #53 fixed absolute-at-construction and the derived default, and the debris
# guard still caught ``data/mail_state.json`` on its first full-suite run. Two
# things survived:
#
#   1. ``MailConfig.state_path`` was still the cwd-relative literal, so a
#      hand-built config aimed the writer at the cwd. The live contaminator was
#      ``orchestrator._fetch_tick(MailConfig(), ...)`` in the shadow-parity
#      tests: ``fetch_all`` has no early return for "no accounts", so a
#      zero-account tick still ran ``save()``.
#   2. The fetcher built a NEW manager EVERY TICK. Since the path resolves at
#      construction, a long-lived loop re-aimed itself at whatever cwd each
#      tick happened to see — the defect that made this cross-file and
#      nondeterministic rather than reproducible from one file.
# ===========================================================================


def test_a_bare_config_carries_no_cwd_relative_default():
    """The literal itself. A default that resolves against the cwd is wrong on
    every instance, so there is deliberately no fallback to be right about."""
    assert MailConfig().state_path == ""


def test_an_empty_state_path_fails_loud_rather_than_guessing():
    """Refusal, not a fallback. A guessed path writes somewhere real and nobody
    notices; the error names the two supported ways to set it."""
    from alfred.mail.fetcher import state_manager_for

    with pytest.raises(ValueError) as exc:
        state_manager_for(MailConfig())
    msg = str(exc.value)
    assert "load_from_unified" in msg
    assert "mail.state.path" in msg


def test_the_contaminator_shape_leaves_nothing_in_the_cwd(tmp_path, monkeypatch):
    """THE acceptance pin — the exact call that was seeding the suite.

    A bare-config tick from a foreign cwd. Before #75 this wrote
    ``data/mail_state.json`` into whatever directory was live, which under the
    suite meant the repo root or an unrelated test's tmp dir. The tick must
    still not RAISE (fault isolation is what keeps the webhook alive), so the
    assertion is about the filesystem, not about the return.
    """
    import alfred.orchestrator as orch

    foreign = tmp_path / "foreign_cwd"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    orch._fetch_tick(MailConfig(), Path("/tmp"))

    leaked = sorted(str(p.relative_to(foreign)) for p in foreign.rglob("*"))
    assert leaked == [], f"the tick seeded the cwd: {leaked}"


def _fetch_enabled_config(state_path: str, *, poll_interval: int = 300) -> MailConfig:
    cfg = MailConfig(
        accounts=[MailAccount(
            name="gmail", email="a@gmail.com", imap_host="imap.gmail.com", fetch=True,
        )],
        state_path=state_path,
        poll_interval=poll_interval,
    )
    cfg.fetch.enabled = True
    return cfg


def test_the_daemon_loop_builds_one_manager_for_every_tick(tmp_path, monkeypatch):
    """The per-tick-reconstruction pin.

    Constructing inside the tick is wrong even with a good default, because the
    path is resolved at construction: a daemon's state file must not move when
    something else changes directory underneath it. Pins the loop handing the
    SAME instance to every tick, so a regression that moves construction back
    into the tick fails here rather than as debris three files away.

    Drives the REAL thread and stops it through the real stop event, rather
    than substituting a fake thread and a fake sleep. The earlier version
    faked both, which meant it never exercised the mechanism it depended on.
    """
    import threading

    import alfred.orchestrator as orch

    stop = threading.Event()
    seen: list[object] = []

    def _spy(config, vault_path, *, only_flagged=False, state_mgr=None):
        seen.append(state_mgr)
        if len(seen) >= 3:
            stop.set()
        return 0

    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", _spy)

    thread = orch._maybe_start_mail_fetch_loop(
        _fetch_enabled_config(
            str(tmp_path / "state" / "mail_state.json"), poll_interval=1,
        ),
        tmp_path / "vault",
        stop_event=stop,
    )
    thread.join(timeout=10)

    assert not thread.is_alive(), "the loop ignored its stop event"
    assert len(seen) == 3, f"expected 3 ticks, saw {len(seen)}"
    assert all(m is not None for m in seen), "the loop never passed a manager"
    assert seen[0] is seen[1] is seen[2], "a NEW manager per tick — the #75 defect"


def test_a_stopped_loop_falls_silent(tmp_path, monkeypatch):
    """The teardown contract, asserted from outside: a stopped loop emits NOTHING.

    This is the pin that protects the rest of the suite. A loop nobody can stop
    keeps logging for the remainder of the session, and a ``capture_logs()``
    block anywhere downstream collects those events as if they were its own —
    measured at 8 foreign ``mail.*`` events in one 2.2s window. Any test
    asserting ``len(captured) == N`` is then one scheduling accident from a
    false red, which is this lane's own failure class one layer up.

    Deliberately asserts on the LOG WINDOW rather than only on
    ``is_alive()``: a dead thread is the mechanism, silence is the property
    other tests actually depend on.
    """
    import threading
    import time

    import structlog

    import alfred.orchestrator as orch

    stop = threading.Event()
    ticked = threading.Event()

    def _noisy(config, vault_path, *, only_flagged=False, state_mgr=None):
        # Stands in for the real fetch_all, which logs mail.fetch.starting /
        # mail.state.loaded / mail.fetch_complete every tick.
        structlog.get_logger("alfred.mail.fetcher").info("mail.fetch.starting", accounts=0)
        ticked.set()
        return 0

    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", _noisy)

    thread = orch._maybe_start_mail_fetch_loop(
        _fetch_enabled_config(str(tmp_path / "state" / "mail_state.json"), poll_interval=1),
        tmp_path / "vault",
        stop_event=stop,
    )
    assert ticked.wait(timeout=10), "the loop never ran — the silence below would prove nothing"

    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), "the thread outlived its stop"

    # The window every other test's capture_logs() stands for.
    with structlog.testing.capture_logs() as cap:
        time.sleep(0.3)
    foreign = [e for e in cap if str(e.get("event", "")).startswith("mail.")]
    assert foreign == [], f"a stopped loop is still emitting into other tests: {foreign}"


def test_fetch_all_writes_through_the_manager_it_was_handed(tmp_path, monkeypatch):
    """The other half of the per-tick pin, and the half that actually catches
    the revert.

    The loop-level pin above proves the LOOP builds one manager and passes it
    every tick. It monkeypatches ``fetch_all``, so it stays green if
    ``fetch_all`` ignores the argument and rebuilds from config — which is
    precisely the regression (construction moves back inside, the path
    re-resolves per call, the handed manager becomes decorative). Found by
    mutation: reverting ``fetch_all`` to unconditional construction left the
    loop pin green.

    So drive the REAL ``fetch_all`` and aim the three locations apart: the
    manager points at one dir, the config at another, the cwd at a third. Only
    the handed manager's dir may be written.
    """
    from alfred.mail.fetcher import fetch_all

    handed = tmp_path / "handed"
    from_config = tmp_path / "from_config"
    foreign = tmp_path / "foreign_cwd"
    foreign.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()

    mgr = StateManager(handed / "mail_state.json")
    cfg = MailConfig(state_path=str(from_config / "mail_state.json"))

    monkeypatch.chdir(foreign)
    fetch_all(cfg, vault, only_flagged=True, state_mgr=mgr)

    assert (handed / "mail_state.json").exists(), (
        "the handed manager was ignored"
    )
    assert not from_config.exists(), (
        "rebuilt from config — per-tick construction is back"
    )
    leaked = sorted(str(p.relative_to(foreign)) for p in foreign.rglob("*"))
    assert leaked == [], f"debris in the cwd: {leaked}"


def test_the_loop_refuses_to_start_on_an_unusable_state_path(tmp_path, monkeypatch):
    """Fail at STARTUP, not once per tick.

    Inside the tick the refusal would be swallowed by ``_fetch_tick``'s
    catch-all and logged forever as ``mail.fetch.loop_error`` — a daemon that
    looks alive and persists nothing. Building the manager before the thread
    exists puts the error where the operator sees it, and starts no thread.
    """
    import threading

    import alfred.orchestrator as orch

    started: list[str] = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None, **kw):
            pass

        def start(self):
            started.append("started")

    monkeypatch.setattr(threading, "Thread", _FakeThread)

    with pytest.raises(ValueError):
        orch._maybe_start_mail_fetch_loop(_fetch_enabled_config(""), tmp_path / "vault")
    assert started == [], "a thread was started on an unusable state path"


def test_the_loop_logs_where_it_will_write(tmp_path, monkeypatch):
    """ILB: the debris was invisible partly because nothing said where the loop
    was writing. The resolved destination is reported once, at start."""
    import threading

    import structlog

    import alfred.orchestrator as orch

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    target = tmp_path / "state" / "mail_state.json"

    with structlog.testing.capture_logs() as cap:
        orch._maybe_start_mail_fetch_loop(
            _fetch_enabled_config(str(target)), tmp_path / "vault",
        )

    started = [c for c in cap if c.get("event") == "mail.fetch.loop_started"]
    assert len(started) == 1
    assert started[0]["state_path"] == str(target.resolve())

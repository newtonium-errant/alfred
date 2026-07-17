"""Task #4 (box half) — STAY-C bug-report watcher: relay-spool surfacing + modes + PHI-safety.

The watcher SURFACES reports by writing a Salem brief-relay SPOOL FILE (STAY-C uses NO Telegram
— standing rule 2026-07-16). Its load-bearing property is the FAIL-SAFE default: any
missing/unparseable mode resolves to ``locked`` (count + opaque ids), never ``full`` (bodies).
Tests drive the pure functions + run_once with a fake writer (captures the whole-file text)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred._data import get_systemd_dir
from alfred.scribe import bug as bug_mod
from alfred.scribe.config import ScribeBugConfig, ScribeConfig, ScribeSttConfig
from alfred.scripts import stayc_bug_watcher as w

_PHI = "Jane-Patient-DOB-1970"


def _cfg(tmp_path):
    return ScribeConfig(mode="synthetic", input_dir=str(tmp_path / "inbox"),
                        stt=ScribeSttConfig(provider="fake"),
                        bug=ScribeBugConfig(dir=str(tmp_path / "bugs")), encounter_salt="S")


def _seed(tmp_path, summary, detail):
    cfg = _cfg(tmp_path)
    _, bug_id = bug_mod.write_bug_report(cfg, summary=summary, detail=detail)
    return bug_id, bug_mod.resolve_bug_dir(cfg)


# ---------------------------------------------------------------------------
# fail-safe mode resolution (the load-bearing property)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val", [None, "", "  ", "locked", "LOCKED", "lockd", "nonsense",
                                 "fulll", "1", "true", "off"])
def test_mode_defaults_to_locked_for_anything_but_full(val):
    env = {} if val is None else {w.ENV_MODE: val}
    assert w.resolve_forward_mode(env) == w.FORWARD_MODE_LOCKED   # fail-safe: never escalate


@pytest.mark.parametrize("val", ["full", "FULL", " Full ", "fUlL"])
def test_mode_is_full_only_for_the_exact_full_string(val):
    assert w.resolve_forward_mode({w.ENV_MODE: val}) == w.FORWARD_MODE_FULL


def test_mode_missing_env_is_locked():
    assert w.resolve_forward_mode({}) == w.FORWARD_MODE_LOCKED


# ---------------------------------------------------------------------------
# build_snapshot — PHI boundary per mode
# ---------------------------------------------------------------------------

def test_locked_snapshot_is_count_and_opaque_ids_only(tmp_path):
    # R1 — plant PHI in the SUMMARY (the leak vector the old slug-in-id created). The opaque id
    # carries no summary text, and locked mode NEVER opens the file, so the spool is PHI-safe.
    bid, bug_dir = _seed(tmp_path, _PHI + " cant save", "and PHI in the detail too")
    reports = w.list_unresolved_reports(bug_dir)
    text = w.build_snapshot(w.FORWARD_MODE_LOCKED, reports, generated_at="2026-07-16T00:00:00Z", new_count=1)
    assert bid in text and "unresolved: 1" in text and "generated_at:" in text
    assert _PHI not in text and _PHI.lower() not in text.lower()  # NEVER the summary or its slug
    assert "jane" not in text.lower() and "patient" not in text.lower()   # not even a fragment


def test_full_snapshot_includes_summary_and_body(tmp_path):
    bid, bug_dir = _seed(tmp_path, "a plain summary", _PHI + " in the detail")
    reports = w.list_unresolved_reports(bug_dir)
    text = w.build_snapshot(w.FORWARD_MODE_FULL, reports, generated_at="2026-07-16T00:00:00Z", new_count=1)
    assert bid in text and "a plain summary" in text and _PHI in text   # summary line + full body


def test_empty_snapshot_is_an_explicit_signal(tmp_path):
    # ILB — a zero-unresolved snapshot SAYS so (Salem must distinguish "0 open bugs" from a
    # stale/missing file), and it is written every run.
    text = w.build_snapshot(w.FORWARD_MODE_LOCKED, [], generated_at="2026-07-16T00:00:00Z", new_count=0)
    assert "unresolved: 0" in text and "no unresolved bug reports" in text


def test_run_once_locked_relay_never_contains_summary_or_body(tmp_path):
    # END-TO-END fail-safe: DEFAULT (locked) mode, a report whose SUMMARY + body carry PHI is
    # relayed by opaque id only — the spool text never contains either.
    _seed(tmp_path, _PHI + " summary", _PHI + " detail")
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    sent = []
    summary = w.run_once(bug_dir, mode=w.resolve_forward_mode({}), writer=sent.append,
                         state_path=tmp_path / "state.json")
    assert summary["unresolved"] == 1 and summary["new"] == 1 and summary["mode"] == "locked"
    assert sent and _PHI not in sent[0] and _PHI.lower() not in sent[0].lower()


# ---------------------------------------------------------------------------
# R2 — a relay WRITE failure fails loud and preserves state
# ---------------------------------------------------------------------------

def test_run_once_raises_and_preserves_state_on_write_failure(tmp_path):
    # R2 — a spool write failure (unwritable path / permission) must NOT advance state: run_once
    # propagates and the ids stay unmarked, so the next run re-surfaces them (never the silent
    # sink where an undeliverable relay is treated as success).
    _seed(tmp_path, "one", "d")
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    state = tmp_path / "state.json"

    def _boom(_text):
        raise w.RelayWriteError("cannot write relay spool")

    with pytest.raises(w.RelayWriteError):
        w.run_once(bug_dir, mode="locked", writer=_boom, state_path=state)
    assert w.load_forwarded(state) == set()                      # NOT advanced → re-surfaced next run
    sent = []
    w.run_once(bug_dir, mode="locked", writer=sent.append, state_path=state)
    assert sent and "unresolved: 1" in sent[0]


def test_relay_writer_atomic_and_raises_on_unwritable(tmp_path):
    # the real writer writes atomically (no .tmp residue) and raises RelayWriteError when the
    # target dir cannot be created (a FILE where the parent dir should be).
    good = tmp_path / "relay" / "spool.md"
    w._relay_writer(good)("hello")
    assert good.read_text() == "hello" and not list(good.parent.glob("*.tmp"))
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a dir")
    with pytest.raises(w.RelayWriteError):
        w._relay_writer(blocker / "spool.md")("x")               # parent is a file → mkdir fails


# ---------------------------------------------------------------------------
# whole-file snapshot semantics + state (new-count, outside the bug dir)
# ---------------------------------------------------------------------------

def test_snapshot_is_a_whole_file_rolling_view(tmp_path):
    # The spool is REGENERATED whole each run: it always reflects ALL currently-unresolved
    # reports (both, here), not an append of just the new one. `new` counts only the delta.
    a, bug_dir = _seed(tmp_path, "first", "d")
    state = tmp_path / "state.json"
    sent = []
    first = w.run_once(bug_dir, mode="locked", writer=sent.append, state_path=state)
    assert first["unresolved"] == 1 and first["new"] == 1
    second = w.run_once(bug_dir, mode="locked", writer=sent.append, state_path=state)
    assert second["unresolved"] == 1 and second["new"] == 0      # same report, no longer "new"
    b, _ = _seed(tmp_path, "second", "d")
    third = w.run_once(bug_dir, mode="locked", writer=sent.append, state_path=state)
    assert third["unresolved"] == 2 and third["new"] == 1        # whole view = both; delta = 1
    assert a in sent[-1] and b in sent[-1]                       # the file lists BOTH


def test_run_once_empty_writes_an_explicit_snapshot(tmp_path):
    bug_dir = tmp_path / "bugs"
    bug_dir.mkdir()
    sent = []
    summary = w.run_once(bug_dir, mode="locked", writer=sent.append,   # ILB — always a signal
                         state_path=tmp_path / "state.json")
    assert summary["unresolved"] == 0 and sent and "unresolved: 0" in sent[0]


def test_state_lives_outside_the_bug_dir(tmp_path):
    # R3 — the state is WATCHER-OWNED, NOT in the (group-r-x) bug dir. save/load use an explicit
    # state_path, and the default is under the watcher's XDG state dir, never the bug dir.
    a, bug_dir = _seed(tmp_path, "first", "d")
    state = tmp_path / "watcher-state" / "state.json"
    w.save_forwarded(state, {a})
    assert state.is_file() and not (bug_dir / w._STATE_NAME).exists()   # NOT in the bug dir
    assert w.load_forwarded(state) == {a}
    assert w.default_state_path(bug_dir) != bug_dir / w._STATE_NAME     # default is watcher-owned


def test_load_forwarded_tolerates_corrupt_state(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    assert w.load_forwarded(state) == set()                     # tolerant → re-counted, never lost


# ---------------------------------------------------------------------------
# bundled systemd artifacts
# ---------------------------------------------------------------------------

def test_watcher_templates_are_bundled_for_the_relay_spool():
    sysd = get_systemd_dir()
    path_tmpl = (sysd / "stayc-bug-watcher.path.template").read_text(encoding="utf-8")
    svc_tmpl = (sysd / "stayc-bug-watcher.service.template").read_text(encoding="utf-8")
    # the .path watches the bug dir and triggers the .service.
    assert "PathModified=<STAYC_BUG_DIR>" in path_tmpl
    assert "Unit=stayc-bug-watcher.service" in path_tmpl
    # the .service runs the watcher, writes the Salem relay spool (NO Telegram, NO network).
    assert "-m alfred.scripts.stayc_bug_watcher" in svc_tmpl
    assert "STAYC_BUG_RELAY_PATH=<RELAY_PATH>" in svc_tmpl            # the relay spool env
    # channel swapped out — NO stale Telegram mention in EITHER template (the .path escaped the
    # first sweep; bind both).
    for name, tmpl in (("service", svc_tmpl), ("path", path_tmpl)):
        assert "Telegram" not in tmpl and "TELEGRAM" not in tmpl, name
    assert "AF_INET" not in svc_tmpl                                  # no network at all
    # writable holes: the relay dir + the watcher's own state dir (NOT the bug dir).
    assert "ReadWritePaths=<RELAY_DIR> <STAYC_WATCHER_STATE_DIR>" in svc_tmpl
    assert not any(ln.strip() == "ReadWritePaths=<STAYC_BUG_DIR>" for ln in svc_tmpl.splitlines())
    # the shared-group cross-user provisioning (R3) is documented in the template.
    assert "groupadd" in svc_tmpl and "stayc-bugs" in svc_tmpl and "usermod -aG" in svc_tmpl

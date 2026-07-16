"""Task #4 (box half) — STAY-C bug-report watcher: forwarding modes + PHI-safety + state.

The watcher is the SURFACING component (a systemd .path unit outside the clinical unit).
Its load-bearing property is the FAIL-SAFE default: any missing/unparseable mode resolves to
``locked`` (count + filename only, PHI-safe), never ``full`` (body egress). Tests drive the
pure functions + run_once with a fake sender (no httpx, no network)."""

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
# build_alert — PHI boundary per mode
# ---------------------------------------------------------------------------

def test_locked_alert_is_count_and_filename_only_no_body(tmp_path):
    bid, bug_dir = _seed(tmp_path, "dead button", _PHI + " in the detail")
    reports = w.scan_new_reports(bug_dir, set())
    text = w.build_alert(w.FORWARD_MODE_LOCKED, reports)
    assert bid in text and "1 new bug report" in text            # count + filename(id)
    assert _PHI not in text                                       # ...NEVER the body content
    assert "dead-button" in bid                                   # (the id carries the slug only)


def test_full_alert_includes_the_body(tmp_path):
    bid, bug_dir = _seed(tmp_path, "dead button", _PHI + " in the detail")
    reports = w.scan_new_reports(bug_dir, set())
    text = w.build_alert(w.FORWARD_MODE_FULL, reports)
    assert bid in text and _PHI in text                          # synthetic-era full-body forward


def test_run_once_locked_never_sends_body(tmp_path):
    # END-TO-END fail-safe: with the DEFAULT (locked) mode, a report whose body carries PHI is
    # surfaced by id only — the sent text never contains the body.
    _seed(tmp_path, "problem", _PHI)
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    sent = []
    summary = w.run_once(bug_dir, mode=w.resolve_forward_mode({}), sender=sent.append)
    assert summary["forwarded"] == 1 and summary["mode"] == "locked"
    assert sent and _PHI not in sent[0]


# ---------------------------------------------------------------------------
# scan + state (no double-forward; dotfile ignored)
# ---------------------------------------------------------------------------

def test_scan_ignores_forwarded_and_state_dotfile(tmp_path):
    a, bug_dir = _seed(tmp_path, "first", "d")
    b, _ = _seed(tmp_path, "second", "d")
    assert {p.stem for p in w.scan_new_reports(bug_dir, set())} == {a, b}
    assert {p.stem for p in w.scan_new_reports(bug_dir, {a})} == {b}
    # the state dotfile is never scanned as a report.
    w.save_forwarded(bug_dir, {a, b})
    assert (bug_dir / w._STATE_NAME).is_file()
    assert w.scan_new_reports(bug_dir, w.load_forwarded(bug_dir)) == []


def test_run_once_does_not_double_forward(tmp_path):
    _seed(tmp_path, "one", "d")
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    sent = []
    first = w.run_once(bug_dir, mode="locked", sender=sent.append)
    second = w.run_once(bug_dir, mode="locked", sender=sent.append)   # .path fires again
    assert first["forwarded"] == 1 and second["forwarded"] == 0       # marked, not re-sent
    assert len(sent) == 1
    # a NEW report after that forwards just the new one.
    _seed(tmp_path, "two", "d")
    third = w.run_once(bug_dir, mode="locked", sender=sent.append)
    assert third["forwarded"] == 1 and len(sent) == 2


def test_run_once_no_new_reports_is_a_signal_not_a_crash(tmp_path):
    bug_dir = tmp_path / "bugs"
    bug_dir.mkdir()
    sent = []
    summary = w.run_once(bug_dir, mode="locked", sender=sent.append)  # ILB — ran, nothing new
    assert summary["forwarded"] == 0 and sent == []


def test_load_forwarded_tolerates_corrupt_state(tmp_path):
    bug_dir = tmp_path / "bugs"
    bug_dir.mkdir()
    (bug_dir / w._STATE_NAME).write_text("{not json", encoding="utf-8")
    assert w.load_forwarded(bug_dir) == set()                    # tolerant → re-send, never lost


# ---------------------------------------------------------------------------
# bundled systemd artifacts
# ---------------------------------------------------------------------------

def test_watcher_templates_are_bundled_with_expected_placeholders():
    sysd = get_systemd_dir()
    path_tmpl = (sysd / "stayc-bug-watcher.path.template").read_text(encoding="utf-8")
    svc_tmpl = (sysd / "stayc-bug-watcher.service.template").read_text(encoding="utf-8")
    # the .path watches the bug dir and triggers the .service.
    assert "PathModified=<STAYC_BUG_DIR>" in path_tmpl
    assert "Unit=stayc-bug-watcher.service" in path_tmpl
    # the .service runs the watcher module, as the operator, with a writable bug dir + no
    # IPAddressDeny (it MUST egress to Telegram, unlike the clinical unit).
    assert "-m alfred.scripts.stayc_bug_watcher" in svc_tmpl
    assert "ReadWritePaths=<STAYC_BUG_DIR>" in svc_tmpl
    # no IPAddressDeny DIRECTIVE (it must egress to Telegram); a comment MENTIONING it is fine.
    assert not any(ln.strip().startswith("IPAddressDeny=") for ln in svc_tmpl.splitlines())
    assert "<STAYC_WATCHER_USER>" in svc_tmpl

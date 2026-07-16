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

def test_locked_alert_is_count_and_opaque_id_only_no_free_text(tmp_path):
    # R1 — plant PHI in the SUMMARY (the real leak vector: the old slug embedded the summary in
    # the id, which locked mode sends). With the OPAQUE id, the ping carries no summary text.
    bid, bug_dir = _seed(tmp_path, _PHI + " cant save", "and in the detail too")
    reports = w.scan_new_reports(bug_dir, set())
    text = w.build_alert(w.FORWARD_MODE_LOCKED, reports)
    assert bid in text and "1 new bug report" in text            # count + OPAQUE id
    assert _PHI not in text and _PHI.lower() not in text.lower()  # NEVER the summary or its slug
    assert "jane" not in text.lower() and "patient" not in text.lower()   # not even a fragment


def test_full_alert_includes_the_body(tmp_path):
    bid, bug_dir = _seed(tmp_path, "dead button", _PHI + " in the detail")
    reports = w.scan_new_reports(bug_dir, set())
    text = w.build_alert(w.FORWARD_MODE_FULL, reports)
    assert bid in text and _PHI in text                          # synthetic-era full-body forward


def test_run_once_locked_never_sends_summary_or_body(tmp_path):
    # END-TO-END fail-safe: with the DEFAULT (locked) mode, a report whose SUMMARY and body
    # carry PHI is surfaced by opaque id only — the sent text never contains either.
    _seed(tmp_path, _PHI + " summary", _PHI + " detail")
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    sent = []
    summary = w.run_once(bug_dir, mode=w.resolve_forward_mode({}), sender=sent.append,
                         state_path=tmp_path / "state.json")
    assert summary["forwarded"] == 1 and summary["mode"] == "locked"
    assert sent and _PHI not in sent[0] and _PHI.lower() not in sent[0].lower()


def test_run_once_raises_and_preserves_state_on_send_failure(tmp_path):
    # R2 — a Telegram delivery failure (sender raises, e.g. the httpx sender on HTTP >= 400)
    # must NOT mark the reports forwarded: run_once propagates and state is NOT saved, so the
    # reports are re-scanned + retried next run (never the silent sink where a revoked token is
    # treated as success).
    _seed(tmp_path, "one", "d")
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    state = tmp_path / "state.json"

    def _boom(_text):
        raise w.TelegramSendError("Telegram HTTP 401")

    with pytest.raises(w.TelegramSendError):
        w.run_once(bug_dir, mode="locked", sender=_boom, state_path=state)
    assert w.load_forwarded(state) == set()                      # NOT marked → retried next run
    # a retry with a working sender then forwards it.
    sent = []
    w.run_once(bug_dir, mode="locked", sender=sent.append, state_path=state)
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# scan + state (no double-forward; dotfile ignored)
# ---------------------------------------------------------------------------

def test_scan_ignores_forwarded(tmp_path):
    a, bug_dir = _seed(tmp_path, "first", "d")
    b, _ = _seed(tmp_path, "second", "d")
    assert {p.stem for p in w.scan_new_reports(bug_dir, set())} == {a, b}
    assert {p.stem for p in w.scan_new_reports(bug_dir, {a})} == {b}


def test_state_lives_outside_the_bug_dir(tmp_path):
    # R3 — the forwarded-state is WATCHER-OWNED, NOT in the (group-r-x) bug dir. save/load use an
    # explicit state_path, and the default is under the watcher's XDG state dir, never the bug dir.
    a, bug_dir = _seed(tmp_path, "first", "d")
    state = tmp_path / "watcher-state" / "forwarded.json"
    w.save_forwarded(state, {a})
    assert state.is_file() and not (bug_dir / w._STATE_NAME).exists()   # NOT in the bug dir
    assert w.load_forwarded(state) == {a}
    assert w.default_state_path(bug_dir) != bug_dir / w._STATE_NAME     # default is watcher-owned


def test_run_once_does_not_double_forward(tmp_path):
    _seed(tmp_path, "one", "d")
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    state = tmp_path / "state.json"
    sent = []
    first = w.run_once(bug_dir, mode="locked", sender=sent.append, state_path=state)
    second = w.run_once(bug_dir, mode="locked", sender=sent.append, state_path=state)  # .path fires again
    assert first["forwarded"] == 1 and second["forwarded"] == 0       # marked, not re-sent
    assert len(sent) == 1
    # a NEW report after that forwards just the new one.
    _seed(tmp_path, "two", "d")
    third = w.run_once(bug_dir, mode="locked", sender=sent.append, state_path=state)
    assert third["forwarded"] == 1 and len(sent) == 2


def test_run_once_no_new_reports_is_a_signal_not_a_crash(tmp_path):
    bug_dir = tmp_path / "bugs"
    bug_dir.mkdir()
    sent = []
    summary = w.run_once(bug_dir, mode="locked", sender=sent.append,   # ILB — ran, nothing new
                         state_path=tmp_path / "state.json")
    assert summary["forwarded"] == 0 and sent == []


def test_run_once_caps_reports_per_run(tmp_path):
    # F6 — a backlog must not fan out into hundreds of chunks (post-R2 a rate-limit 429 HARD
    # fails the run). Forward at most MAX_REPORTS_PER_RUN per trigger; the rest drain later.
    for i in range(w.MAX_REPORTS_PER_RUN + 3):
        _seed(tmp_path, f"report {i}", "d")
    bug_dir = bug_mod.resolve_bug_dir(_cfg(tmp_path))
    state = tmp_path / "state.json"
    sent = []
    summary = w.run_once(bug_dir, mode="locked", sender=sent.append, state_path=state)
    assert summary["forwarded"] == w.MAX_REPORTS_PER_RUN and summary["overflow"] == 3
    assert "more this run" in sent[0]                            # overflow is signalled
    # the remaining drain on the next run.
    second = w.run_once(bug_dir, mode="locked", sender=sent.append, state_path=state)
    assert second["forwarded"] == 3


def test_load_forwarded_tolerates_corrupt_state(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    assert w.load_forwarded(state) == set()                     # tolerant → re-send, never lost


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
    # the .service runs the watcher module as the operator, with its OWN state dir writable
    # (NOT the bug dir — the watcher only reads that) + no IPAddressDeny (it MUST egress).
    assert "-m alfred.scripts.stayc_bug_watcher" in svc_tmpl
    assert "STAYC_BUG_STATE=<STAYC_WATCHER_STATE>" in svc_tmpl        # relocated state (R3)
    assert "ReadWritePaths=<STAYC_WATCHER_STATE_DIR>" in svc_tmpl     # writable state dir, not bug dir
    assert not any(ln.strip() == "ReadWritePaths=<STAYC_BUG_DIR>" for ln in svc_tmpl.splitlines())
    # no IPAddressDeny DIRECTIVE (it must egress to Telegram); a comment MENTIONING it is fine.
    assert not any(ln.strip().startswith("IPAddressDeny=") for ln in svc_tmpl.splitlines())
    assert "<STAYC_WATCHER_USER>" in svc_tmpl
    # the shared-group cross-user provisioning (R3) is documented in the template.
    assert "groupadd" in svc_tmpl and "stayc-bugs" in svc_tmpl and "usermod -aG" in svc_tmpl

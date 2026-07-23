"""#14 slice 14b — note_profile artifact + loader + attribution: contract-first tests.

Pins the load-bearing contracts: fail-closed-to-DEFAULT (absent AND corrupt), highest-valid-version
resolution, the atomic 0600 write + canonical sha, the create-time attribution matching 14a's exact
frontmatter reader, the DRAFT_EDIT_FIELDS-un-widened regression, and the loader-tolerance (required
fields present, optionals default). Regression pins UNCONDITIONAL (no importorskip).
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import frontmatter
import pytest
import structlog
import yaml

from alfred.cli import build_parser, cmd_scribe
from alfred.scribe import notegen_profile as np
from alfred.scribe.config import load_from_unified
from alfred.scribe.notegen import _SECTION_HEADINGS
from alfred.scribe.pipeline import VerifiedNote, _create_ai_draft
from alfred.vault.scope import STAYC_CLINICAL_DRAFT_EDIT_FIELDS


def _cfg(tmp_path):
    return load_from_unified({"scribe": {"mode": "clinical", "encounter_salt": "S",
                                         "input_dir": str(tmp_path / "inbox")}})


def _seed_dict(**over):
    d = np.seed_profile_dict(1)
    d.update(over)
    return d


# ===========================================================================
# DEFAULT + schema + validation
# ===========================================================================

def test_default_profile_is_soap_v0():
    p = np.DEFAULT_PROFILE
    assert p.note_type == "soap" and p.profile_version == 0
    assert [s.key for s in p.sections] == ["subjective", "objective", "assessment", "plan"]
    # headings come from the renderer's single source of truth
    assert all(s.heading == _SECTION_HEADINGS[s.key] for s in p.sections)
    assert p.succinctness_target_words_per_claim == 25


def test_profile_from_dict_roundtrip():
    p = np.profile_from_dict(_seed_dict())
    assert np.profile_from_dict(p.to_dict()).to_dict() == p.to_dict()


def test_loader_tolerance_required_fields_only():
    # Only the REQUIRED structural fields present → builds with optionals defaulted (the loader-tolerance
    # / empty-dict trap — optionals must not be required in fixtures).
    minimal = {"profile_version": 2, "note_type": "soap",
               "sections": [{"key": "subjective", "heading": "## Subjective", "required": True, "order": 1}],
               "succinctness_target_words_per_claim": 30}
    p = np.profile_from_dict(minimal)
    assert p.profile_version == 2 and p.terminology_preferences == () and p.style_guidance_ref is None


def test_extra_unknown_key_tolerated():
    p = np.profile_from_dict(_seed_dict(future_field="ignored"))   # forward-compat
    assert p.note_type == "soap"


@pytest.mark.parametrize("bad", [
    {"profile_version": "x"},                                        # non-int version
    {"profile_version": -1},                                         # negative
    {"note_type": ""},                                              # empty id
    {"sections": []},                                              # empty sections
    {"succinctness_target_words_per_claim": 0},                     # non-positive target
])
def test_malformed_raises_profile_error(bad):
    with pytest.raises(np.ProfileError):
        np.profile_from_dict(_seed_dict(**bad))


def test_malformed_section_raises():
    with pytest.raises(np.ProfileError):
        np.profile_from_dict(_seed_dict(sections=[{"key": "s", "heading": "## S", "order": 1}]))  # no required


# ===========================================================================
# Fail-closed loader + resolve_active_profile (load-bearing)
# ===========================================================================

def test_load_profile_absent_is_none(tmp_path):
    assert np.load_profile(tmp_path / "nope.json") is None


def test_load_profile_corrupt_json_is_none(tmp_path):
    p = tmp_path / "note_profile_v1.json"
    p.write_text("{ not json", encoding="utf-8")
    assert np.load_profile(p) is None


def test_load_profile_malformed_schema_is_none(tmp_path):
    p = tmp_path / "note_profile_v1.json"
    p.write_text(json.dumps({"profile_version": "bad"}), encoding="utf-8")
    assert np.load_profile(p) is None


def test_resolve_absent_is_default(tmp_path):
    p = np.resolve_active_profile(_cfg(tmp_path))
    assert p is np.DEFAULT_PROFILE and p.profile_version == 0 and p.note_type == "soap"


def test_resolve_highest_valid_version_wins(tmp_path):
    cfg = _cfg(tmp_path)
    pdir = np.resolve_profiles_dir(cfg)
    pdir.mkdir(parents=True)
    for v in (1, 2, 3):
        np.write_profile(np.profile_path(pdir, v), np.seed_profile_dict(v))
    assert np.resolve_active_profile(cfg).profile_version == 3


def test_resolve_corrupt_highest_falls_back_and_logs(tmp_path):
    cfg = _cfg(tmp_path)
    pdir = np.resolve_profiles_dir(cfg)
    pdir.mkdir(parents=True)
    np.write_profile(np.profile_path(pdir, 1), np.seed_profile_dict(1))
    np.profile_path(pdir, 2).write_text("{ corrupt", encoding="utf-8")   # highest version, corrupt
    with structlog.testing.capture_logs() as cap:
        active = np.resolve_active_profile(cfg)
    assert active.profile_version == 1                                    # fell back to the valid v1
    corrupt = [e for e in cap if e.get("event") == "scribe.notegen_profile.corrupt"]
    assert len(corrupt) == 1 and corrupt[0]["version"] == 2               # observable skip (log-emission pin)


def test_resolve_all_corrupt_is_default(tmp_path):
    cfg = _cfg(tmp_path)
    pdir = np.resolve_profiles_dir(cfg)
    pdir.mkdir(parents=True)
    np.profile_path(pdir, 1).write_text("{ corrupt", encoding="utf-8")
    assert np.resolve_active_profile(cfg).profile_version == 0            # → built-in default (fail-safe)


def test_resolve_is_total_on_unanticipated_raise(tmp_path, monkeypatch):
    # TOTAL contract: an UNANTICIPATED exception (not the OSError/ValueError/ProfileError trio) must NOT
    # escape — the outermost catch-all degrades to DEFAULT + logs a DISTINCT resolve_failed event. Inject
    # a KeyError from profile_from_dict (which load_profile only guards for ProfileError → it escapes).
    cfg = _cfg(tmp_path)
    pdir = np.resolve_profiles_dir(cfg)
    pdir.mkdir(parents=True)
    np.write_profile(np.profile_path(pdir, 1), np.seed_profile_dict(1))   # a VALID file must exist to load
    monkeypatch.setattr(np, "profile_from_dict", lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    with structlog.testing.capture_logs() as cap:
        active = np.resolve_active_profile(cfg)                            # MUST NOT raise
    assert active is np.DEFAULT_PROFILE and active.profile_version == 0    # degraded, never crashed
    failed = [e for e in cap if e.get("event") == "scribe.notegen_profile.resolve_failed"]
    assert len(failed) == 1                                               # observable, distinct from .corrupt


def test_create_ai_draft_survives_resolve_failure(tmp_path, monkeypatch):
    # The clinical draft path must survive a resolver failure — note-gen ALWAYS works (soap/0 attribution).
    cfg = _cfg(tmp_path)
    pdir = np.resolve_profiles_dir(cfg)
    pdir.mkdir(parents=True)
    np.write_profile(np.profile_path(pdir, 1), np.seed_profile_dict(1))
    monkeypatch.setattr(np, "profile_from_dict", lambda *a, **k: (_ for _ in ()).throw(TypeError("boom")))
    path = _create_ai_draft(tmp_path / "vault", "Enc", "enc-abc0123456789d", cfg, _vnote())
    fm = frontmatter.load(str(tmp_path / "vault" / path))
    assert fm["note_profile_id"] == "soap" and fm["note_profile_version"] == 0   # drafted, degraded


# ===========================================================================
# Atomic write + canonical sha
# ===========================================================================

def test_write_profile_0600_and_meta(tmp_path):
    dest = tmp_path / "note_profile_v1.json"
    meta = np.write_profile(dest, np.seed_profile_dict(1))
    assert (os.stat(dest).st_mode & 0o777) == 0o600
    assert meta["profile_version"] == 1 and meta["note_type"] == "soap"
    assert meta["profile_sha256"] == np.profile_sha256(np.seed_profile_dict(1))
    # canonical on-disk bytes → re-read loads back
    assert np.load_profile(dest).profile_version == 1


def test_profile_sha256_stable_and_canonical():
    a = np.profile_sha256(np.seed_profile_dict(1))
    assert a == np.profile_sha256(np.seed_profile_dict(1))              # deterministic
    # key ORDER in the source dict does not change the canonical sha (sorted keys)
    reordered = dict(reversed(list(np.seed_profile_dict(1).items())))
    assert np.profile_sha256(reordered) == a


def test_write_profile_refuses_malformed(tmp_path):
    with pytest.raises(np.ProfileError):
        np.write_profile(tmp_path / "x.json", {"profile_version": "bad"})


# ===========================================================================
# Attribution — the cross-slice contract with 14a's reader
# ===========================================================================

def _vnote():
    return VerifiedNote(body="# E\n\n## Subjective\n- Chest pain [S1]\n",
                        grounding_flags=[], flag_count=0, structured=None)


def test_attribution_absent_profile_matches_14a_default(tmp_path):
    cfg = _cfg(tmp_path)
    path = _create_ai_draft(tmp_path / "vault", "Enc", "enc-abc0123456789d", cfg, _vnote())
    fm = frontmatter.load(str(tmp_path / "vault" / path))
    # EXACT field names 14a reads (note_profile_id / note_profile_version), soap/0 default
    assert fm["note_profile_id"] == "soap" and fm["note_profile_version"] == 0


def test_attribution_reflects_active_version(tmp_path):
    cfg = _cfg(tmp_path)
    pdir = np.resolve_profiles_dir(cfg)
    pdir.mkdir(parents=True)
    np.write_profile(np.profile_path(pdir, 1), np.seed_profile_dict(1))
    path = _create_ai_draft(tmp_path / "vault", "Enc", "enc-abc0123456789d", cfg, _vnote())
    fm = frontmatter.load(str(tmp_path / "vault" / path))
    assert fm["note_profile_id"] == "soap" and fm["note_profile_version"] == 1


def test_draft_edit_fields_stay_un_widened():
    # REGRESSION: attribution is create-ONLY; the update-path allowlist must NOT include it (else the
    # retain-draft_original path could rewrite attribution, breaking the create-time-locked contract).
    assert "note_profile_id" not in STAYC_CLINICAL_DRAFT_EDIT_FIELDS
    assert "note_profile_version" not in STAYC_CLINICAL_DRAFT_EDIT_FIELDS


# ===========================================================================
# CLI — init / show
# ===========================================================================

def _run(tmp_path, *argv):
    cfg = {"vault": {"path": str(tmp_path / "vault")}, "logging": {"dir": str(tmp_path / "data")},
           "scribe": {"mode": "clinical", "encounter_salt": "S", "input_dir": str(tmp_path / "inbox")}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    ns = build_parser().parse_args(["--config", str(tmp_path / "config.yaml"), "scribe", *argv])
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            cmd_scribe(ns)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    out = buf.getvalue()
    js = json.loads(out[out.index("{"):]) if "{" in out else {}
    return code, js


def test_cli_init_creates_v1_0600(tmp_path):
    code, js = _run(tmp_path, "notegen-profile", "init")
    assert code == 0 and js["profile_version"] == 1
    dest = np.resolve_profiles_dir(_cfg(tmp_path)) / "note_profile_v1.json"
    assert dest.is_file() and (os.stat(dest).st_mode & 0o777) == 0o600


def test_cli_init_refuses_clobber_without_force(tmp_path):
    assert _run(tmp_path, "notegen-profile", "init")[0] == 0
    code, js = _run(tmp_path, "notegen-profile", "init")
    assert code == 1 and "refusing" in js["error"]
    assert _run(tmp_path, "notegen-profile", "init", "--force")[0] == 0    # --force overwrites


def test_cli_show_default_then_v1(tmp_path):
    code, js = _run(tmp_path, "notegen-profile", "show")
    assert code == 0 and js["source"] == "built-in default" and js["active"]["profile_version"] == 0
    _run(tmp_path, "notegen-profile", "init")
    code, js = _run(tmp_path, "notegen-profile", "show")
    assert js["active"]["profile_version"] == 1 and js["on_disk_canonical"] is True
    assert js["profile_sha256"] == np.profile_sha256(js["active"])

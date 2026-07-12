"""#58 — attest REQUIRES the encounter be complete (note-completeness marker gate).

The medico-legal core: attest fail-closed-REFUSES unless the note's
``encounter_completeness`` marker reads ``complete: true`` (or an audited
--force-incomplete override). Covers the mutation-bind, positive, override
(success / fail-closed / scope-intact), CAS bracket, the drift guard, and the
completeness-marker read contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frontmatter
import pytest
import structlog

from alfred.scribe.attest import attest
from alfred.scribe.attestation import AttestationError, validate_encounter_complete
from alfred.scribe import completeness_marker
from alfred.vault.ops import vault_read, vault_edit, vault_create
from alfred.vault.scope import STAYC_CLINICAL_DRAFT_EDIT_FIELDS

_CLIN = {"np_jamie"}
_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _draft(tmp_path, *, completeness=None, source_id="enc-abc0123456789d", status="ai_draft"):
    """A born-ai_draft clinical_note; ``completeness`` (a dict) sets the marker."""
    fields = {
        "ai_draft": True, "synthetic": True, "status": status,
        "source_id": source_id, "drafted_by": "stayc_scribe",
    }
    if completeness is not None:
        fields["encounter_completeness"] = completeness
    return vault_create(
        tmp_path, "clinical_note", f"Synthetic encounter {source_id}",
        set_fields=fields, body="## Subjective\nReports chest pain.\n",
        scope="stayc_clinical",
    )["path"]


def _attest(tmp_path, rel, **kw):
    return attest(tmp_path, rel, new_status="attested", attester="np_jamie",
                  clinician_ids=_CLIN, audit_path=tmp_path / "audit.jsonl", now=_NOW, **kw)


# ---------------------------------------------------------------------------
# THE mutation-bind — attest of an incomplete note is REFUSED, no triad written
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", [
    None,                                        # absent
    {"protocol": 1, "complete": False},          # explicitly false
    {"protocol": 1, "complete": "true"},         # string, not bool True → fail-closed
    "not-a-dict",                                # malformed non-dict
    {"protocol": 1},                             # missing 'complete'
])
def test_attest_refuses_incomplete_no_triad_written(tmp_path, marker):
    # #58 CORE mutation-bind: an incomplete/absent/malformed marker → attest RAISES
    # encounter_incomplete AND the note stays ai_draft with no triad. MUTATION-BIND:
    # delete validate_encounter_complete from authorize_attestation → it signs → the
    # raises-assertion turns RED.
    rel = _draft(tmp_path, completeness=marker)
    with pytest.raises(AttestationError) as exc:
        _attest(tmp_path, rel)
    assert exc.value.reason == "encounter_incomplete"
    fm = vault_read(tmp_path, rel)["frontmatter"]
    assert fm["status"] == "ai_draft"                       # never flipped
    assert "attested_by" not in fm and "attested_at" not in fm  # triad NEVER written
    assert not (tmp_path / "audit.jsonl").exists()          # no audit entry


def test_attest_succeeds_with_complete_marker_frozen(tmp_path):
    # POSITIVE: a complete marker → attest succeeds, triad written, marker FROZEN
    # (attest writes ONLY the triad; the marker is a DRAFT_EDIT field sealed at attest).
    rel = _draft(tmp_path, completeness={"protocol": 1, "complete": True, "folded_through": 2})
    with structlog.testing.capture_logs() as caps:
        _attest(tmp_path, rel)
    fm = vault_read(tmp_path, rel)["frontmatter"]
    assert fm["status"] == "attested" and fm["attested_by"] == "np_jamie"
    assert fm["encounter_completeness"]["complete"] is True   # marker unchanged (frozen)
    audit = json.loads((tmp_path / "audit.jsonl").read_text().strip())
    assert audit["forced"] is False and audit["completeness"] == "complete"
    assert not any(c.get("event") == "scribe.attest.incomplete_override" for c in caps)


# ---------------------------------------------------------------------------
# Override — audited, bypasses ONLY completeness
# ---------------------------------------------------------------------------

def test_override_attests_incomplete_with_reason_and_audits(tmp_path):
    rel = _draft(tmp_path, completeness=None)                # markerless → absent
    with structlog.testing.capture_logs() as caps:
        _attest(tmp_path, rel, allow_incomplete=True, override_reason="recorder died before close")
    fm = vault_read(tmp_path, rel)["frontmatter"]
    assert fm["status"] == "attested"
    audit = json.loads((tmp_path / "audit.jsonl").read_text().strip())
    assert audit["forced"] is True and audit["completeness"] == "absent"
    assert "reason" not in audit and "override_reason" not in audit  # #58-D2: reason NOT stored
    ov = [c for c in caps if c.get("event") == "scribe.attest.incomplete_override"]
    assert len(ov) == 1 and ov[0]["completeness"] == "absent"


def test_override_fail_closed_empty_reason(tmp_path):
    rel = _draft(tmp_path, completeness=None)
    for bad_reason in (None, "", "   "):
        with pytest.raises(AttestationError) as exc:
            _attest(tmp_path, rel, allow_incomplete=True, override_reason=bad_reason)
        assert exc.value.reason == "force_without_reason"
    assert vault_read(tmp_path, rel)["frontmatter"]["status"] == "ai_draft"  # never attested


def test_override_does_not_bypass_lifecycle_or_attester(tmp_path):
    # force bypasses ONLY completeness — the lifecycle + distinct-attester stay absolute.
    # self-attest (attester == the drafter) still refuses even with force.
    rel = _draft(tmp_path, completeness=None)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="stayc_scribe",
               clinician_ids={"stayc_scribe"}, audit_path=tmp_path / "a.jsonl", now=_NOW,
               allow_incomplete=True, override_reason="forced")
    assert exc.value.reason == "scribe_self_attest"
    # an ILLEGAL lifecycle skip (ai_draft→amended) still refuses even with force.
    rel2 = _draft(tmp_path, completeness=None, source_id="enc-0000000000000002")
    with pytest.raises(AttestationError) as exc2:
        attest(tmp_path, rel2, new_status="amended", attester="np_jamie",
               clinician_ids=_CLIN, audit_path=tmp_path / "a.jsonl", now=_NOW,
               allow_incomplete=True, override_reason="forced")
    assert exc2.value.reason == "illegal_status_transition"


# ---------------------------------------------------------------------------
# validate_encounter_complete (unit) + CAS bracket + drift guard
# ---------------------------------------------------------------------------

def test_validate_encounter_complete_unit():
    validate_encounter_complete(encounter_complete=True, forced=False, force_reason=None)   # ok
    validate_encounter_complete(encounter_complete=False, forced=True, force_reason="r")    # ok (forced)
    with pytest.raises(AttestationError) as e1:
        validate_encounter_complete(encounter_complete=False, forced=False, force_reason=None)
    assert e1.value.reason == "encounter_incomplete"
    with pytest.raises(AttestationError) as e2:
        validate_encounter_complete(encounter_complete=False, forced=True, force_reason="  ")
    assert e2.value.reason == "force_without_reason"


def test_cas_bracket_refuses_note_changed_under_attest(tmp_path, monkeypatch):
    # The double-read CAS: if the note body changes between attest's first and
    # second vault_read → note_changed_under_attest, no triad written.
    rel = _draft(tmp_path, completeness={"protocol": 1, "complete": True})
    import importlib
    # the package __init__'s `from .attest import attest` shadows the plain
    # `alfred.scribe.attest` attribute with the function — import the SUBMODULE.
    attest_mod = importlib.import_module("alfred.scribe.attest")
    orig_read = attest_mod.vault_read
    calls = {"n": 0}

    def _racing_read(vp, rp):
        calls["n"] += 1
        rec = orig_read(vp, rp)
        if calls["n"] == 1:
            # AFTER the first read, a concurrent regen rewrites the body on disk.
            vault_edit(vp, rp, body_replace="## Subjective\nRACING regen body.\n",
                       scope="stayc_clinical")
        return rec

    monkeypatch.setattr(attest_mod, "vault_read", _racing_read)
    with pytest.raises(AttestationError) as exc:
        _attest(tmp_path, rel)
    assert exc.value.reason == "note_changed_under_attest"
    assert vault_read(tmp_path, rel)["frontmatter"]["status"] == "ai_draft"  # no triad


def test_scope_literal_matches_marker_field_drift_guard():
    # DRIFT GUARD: scope.py cannot import scribe, so it cross-references the field
    # NAME by a string literal — assert it equals the module constant.
    assert completeness_marker.MARKER_FIELD == "encounter_completeness"
    assert completeness_marker.MARKER_FIELD in STAYC_CLINICAL_DRAFT_EDIT_FIELDS


def test_is_complete_read_contract():
    ic = completeness_marker.is_complete
    assert ic({"encounter_completeness": {"complete": True}}) is True
    assert ic({"encounter_completeness": {"complete": False}}) is False
    assert ic({"encounter_completeness": {"complete": "true"}}) is False   # string ≠ True
    assert ic({"encounter_completeness": "not-a-dict"}) is False
    assert ic({}) is False and ic(None) is False and ic("x") is False


def test_stamp_on_sealed_note_denied_by_scope(tmp_path):
    # A stamp attempt on a SEALED (attested) note is denied by the stayc_clinical
    # SEALED branch (the marker is a DRAFT_EDIT field — only writable while ai_draft).
    from alfred.vault.scope import ScopeError
    rel = _draft(tmp_path, completeness={"protocol": 1, "complete": True})
    _attest(tmp_path, rel)                                      # → attested / SEALED
    with pytest.raises(ScopeError):
        completeness_marker.stamp_complete(
            tmp_path, rel, now=_NOW, expected_final_seq=None, folded_through=1)

"""Attest ↔ event-store integration pins (event-store design §5.2 / §8 row 1-3 / §15.5).

These pin the #11 attestation-as-event wiring in ``scribe/attest.py``:

  * ``attest.recorded`` [D] emitted POST-triad, carrying the attested-version ``body_sha`` +
    PHI-free provenance; the attested-digest index updated under the clinical lock;
  * ``attest.refused`` emitted best-effort THEN the refusal re-raised (never masked), no triad;
  * store PREFLIGHT fail-closes the attest (``event_store_unavailable``) BEFORE the first read;
  * DUAL-WRITE — the legacy JSONL trail AND the chained event both written;
  * CAS-window-unwidened — a refusal INSIDE the CAS bracket emits NO ``attest.recorded`` (the
    durable emission sits post-triad, outside the window);
  * #58-D2 — the forced-override free-text reason NEVER lands in the event store;
  * ``events=None`` (tests / non-clinical) → the attest path is byte-identical to pre-#11.
"""

from __future__ import annotations

from datetime import datetime, timezone

import frontmatter
import pytest

from alfred.evstore import EventStoreError
from alfred.scribe import SCRIBE_DRAFTER_IDENTITY
from alfred.scribe.attest import _body_sha, attest
from alfred.scribe.attestation import AttestationError
from alfred.scribe.events import ScribeEvents
from alfred.vault.ops import vault_create, vault_read

_CLINICIANS = {"np_jamie", "dr_synthetic"}
_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
_CLOCK = "2026-07-16T12:00:00+00:00"


def _events(tmp_path):
    """A real, ACTIVE clinical facade rooted at ``tmp_path/ev`` (store) / ``tmp_path/logs``."""
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s",
                      "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: _CLOCK)


def _make_ai_draft(tmp_path, *, source_id="enc-abc0123456789d",
                   drafted_by=SCRIBE_DRAFTER_IDENTITY, complete=True):
    fields = {"ai_draft": True, "synthetic": True, "status": "ai_draft",
              "source_id": source_id, "drafted_by": drafted_by}
    if complete:
        fields["encounter_completeness"] = {"protocol": 1, "complete": True}
    return vault_create(
        tmp_path, "clinical_note", f"Synthetic encounter {source_id}",
        set_fields=fields, body="## Subjective\nSynthetic patient reports chest pain.\n",
        scope="stayc_clinical",
    )["path"]


# --- attest.recorded [D] + attested-digest index ----------------------------

def test_attest_emits_recorded_post_triad(tmp_path):
    ev = _events(tmp_path)
    rel = _make_ai_draft(tmp_path)
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW, events=ev)
    rows = ev.query("clinical", kind="attest.recorded")
    assert len(rows) == 1
    r = rows[0]
    assert r["actor"] == "np_jamie" and r["actor_kind"] == "clinician"
    assert r["subject_id"] == "enc-abc0123456789d"
    p = r["payload"]
    assert p["from_status"] == "ai_draft" and p["to_status"] == "attested"
    assert p["completeness"] == "complete" and p["forced"] is False
    assert p["creator"] == SCRIBE_DRAFTER_IDENTITY
    # body_sha is the ATTESTED-version pin — the sha of the note body as signed.
    assert p["body_sha"] == _body_sha(vault_read(tmp_path, rel)["body"])
    assert p["grounding_flag_count"] == 0 and p["grounding_reasons"] == []


def test_attest_updates_digest_index(tmp_path):
    ev = _events(tmp_path)
    rel = _make_ai_draft(tmp_path, source_id="enc-idx0000000001")
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW, events=ev)
    idx = ev.attested_digest("enc-idx0000000001")
    assert idx is not None
    assert idx["body_sha"] == _body_sha(vault_read(tmp_path, rel)["body"])
    assert idx["rel_path"] == rel  # rel_path lives ONLY in the index (§7.4)
    assert idx["attested_at"] == _NOW.isoformat()  # event ts == index attested_at


def test_grounding_reasons_carried_claim_never(tmp_path):
    ev = _events(tmp_path)
    # a note carrying grounding flags — only the reason ENUM leaves frontmatter, never the claim.
    rel = vault_create(
        tmp_path, "clinical_note", "Synthetic enc grounded",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "source_id": "enc-grounded00001", "drafted_by": SCRIBE_DRAFTER_IDENTITY,
                    "encounter_completeness": {"protocol": 1, "complete": True},
                    "grounding_flags": [{"reason": "hedged_language",
                                         "claim": "patient PHI claim text"}]},
        body="## S\nreports pain\n", scope="stayc_clinical",
    )["path"]
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW, events=ev)
    p = ev.query("clinical", kind="attest.recorded")[0]["payload"]
    assert p["grounding_flag_count"] == 1 and p["grounding_reasons"] == ["hedged_language"]
    # the free-text claim is PHI — it NEVER reaches the chained store.
    assert "patient PHI claim text" not in (tmp_path / "ev" / "clinical.jsonl").read_text()


# --- attest.refused (best-effort, then re-raise) ----------------------------

def test_attest_refused_emits_then_reraises_no_triad(tmp_path):
    ev = _events(tmp_path)
    rel = _make_ai_draft(tmp_path, drafted_by="np_jamie")  # creator self-attest → refusal
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW, events=ev)
    assert exc.value.reason == "self_attest"
    refused = ev.query("clinical", kind="attest.refused")
    assert len(refused) == 1
    assert refused[0]["payload"]["reason"] == "self_attest"
    assert refused[0]["payload"]["from_status"] == "ai_draft"
    # the refusal is never masked: NO success event, NO triad written.
    assert ev.query("clinical", kind="attest.recorded") == []
    assert frontmatter.load(str(tmp_path / rel))["status"] == "ai_draft"


# --- preflight fail-close (before the first vault_read) ---------------------

def test_preflight_failure_fail_closes_before_read(tmp_path, monkeypatch):
    ev = _events(tmp_path)
    rel = _make_ai_draft(tmp_path)

    def _boom(stream=None):
        raise EventStoreError("events dir gone")

    monkeypatch.setattr(ev, "preflight", _boom)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW, events=ev)
    assert exc.value.reason == "event_store_unavailable"
    # preflight sits BEFORE the first vault_read — the note is never touched.
    assert frontmatter.load(str(tmp_path / rel))["status"] == "ai_draft"
    assert ev.query("clinical", kind="attest.recorded") == []


# --- dual-write (legacy trail + chained event both written) -----------------

def test_dual_write_both_trails_present(tmp_path):
    ev = _events(tmp_path)
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW, events=ev)
    assert audit.exists()  # the legacy independent trail (written FIRST)
    assert (tmp_path / "ev" / "clinical.jsonl").exists()  # the chained event trail
    assert len(ev.query("clinical", kind="attest.recorded")) == 1


# --- CAS-window-unwidened (mutation) ----------------------------------------

def test_cas_refusal_inside_window_emits_no_recorded(tmp_path, monkeypatch):
    """A change caught by the CAS bracket refuses BEFORE the triad write. Because the durable
    emission is POST-triad (outside the CAS window), such a refusal must emit NO ``attest.recorded``
    — the mutation pin that fails if the emission is ever moved inside the bracket."""
    ev = _events(tmp_path)
    rel = _make_ai_draft(tmp_path)
    import importlib
    # NB the package __init__'s ``from .attest import attest`` shadows the plain
    # attribute — import the SUBMODULE explicitly (mirrors the sovereign CLI tests).
    attest_mod = importlib.import_module("alfred.scribe.attest")

    real_read = attest_mod.vault_read
    calls = {"n": 0}

    def _changing_read(vp, rp):
        calls["n"] += 1
        rec = dict(real_read(vp, rp))
        if calls["n"] == 2:  # the CAS RE-read sees a body that moved under attest
            rec["body"] = rec["body"] + "\nMUTATED"
        return rec

    monkeypatch.setattr(attest_mod, "vault_read", _changing_read)
    with pytest.raises(AttestationError) as exc:
        attest_mod.attest(tmp_path, rel, new_status="attested", attester="np_jamie",
                          clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl",
                          now=_NOW, events=ev)
    assert exc.value.reason == "note_changed_under_attest"
    assert ev.query("clinical", kind="attest.recorded") == []  # nothing emitted from the window
    assert frontmatter.load(str(tmp_path / rel))["status"] == "ai_draft"


# --- #58-D2 free-text-never-in-store ----------------------------------------

def test_forced_override_reason_never_in_event_store(tmp_path):
    ev = _events(tmp_path)
    rel = _make_ai_draft(tmp_path, source_id="enc-forced0000001", complete=False)
    reason = "patient MRN 4432211 — recorder died mid-visit"
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW,
           allow_incomplete=True, override_reason=reason,
           vault_audit_path=tmp_path / "vault_audit.log", events=ev)
    store_text = (tmp_path / "ev" / "clinical.jsonl").read_text()
    assert reason not in store_text and "MRN" not in store_text  # #58-D2 / §11
    p = ev.query("clinical", kind="attest.recorded")[0]["payload"]
    assert p["forced"] is True and p["completeness"] == "absent"


# --- events=None → byte-identical to pre-#11 --------------------------------

def test_events_none_touches_no_store(tmp_path):
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW)  # no events kwarg
    assert frontmatter.load(str(tmp_path / rel))["status"] == "attested"
    assert audit.exists()
    assert not (tmp_path / "ev").exists()  # the event store is never created

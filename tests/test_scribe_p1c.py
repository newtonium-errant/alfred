"""Tests for the scribe config + mode-gate + attestation integrity (P1-c).

SECURITY/LEGAL-CRITICAL. The mode flag is the legal line in code; the
attestation controls are the medico-legal integrity gate. Every control gets a
positive (allowed) AND negative (fail-closed refuse) pin, plus the
mutation-verified pins the brief names: mode default synthetic, synthetic-mode
refusal, forward-only no-un-attest, distinct-attester no-self-attest, and the
ingest-decision log-emission pin.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.scribe import (
    SCRIBE_DRAFTER_IDENTITY,
    SCRIBE_MODE_CLINICAL,
    SCRIBE_MODE_SYNTHETIC,
    STATUS_AI_DRAFT,
    STATUS_AMENDED,
    STATUS_ATTESTED,
    AttestationError,
    ScribeConfig,
    ScribeIngestRefused,
    authorize_attestation,
    guard_ingest,
    load_from_unified,
    validate_attester,
    validate_status_transition,
)

_CLINICIANS = frozenset({"dr_synthetic", "np_jamie"})


# ---------------------------------------------------------------------------
# Config — the fail-closed mode default (mutation: default→clinical fails)
# ---------------------------------------------------------------------------

def test_mode_defaults_synthetic_when_absent():
    # Mutation pin: if the default were "clinical" this fails.
    assert load_from_unified({}).mode == SCRIBE_MODE_SYNTHETIC
    assert load_from_unified({"scribe": {}}).mode == SCRIBE_MODE_SYNTHETIC
    assert ScribeConfig().mode == SCRIBE_MODE_SYNTHETIC
    assert ScribeConfig().is_clinical is False


def test_mode_clinical_only_on_exact_string():
    assert load_from_unified({"scribe": {"mode": "clinical"}}).mode == SCRIBE_MODE_CLINICAL
    assert load_from_unified({"scribe": {"mode": "clinical"}}).is_clinical is True
    # case/space tolerant on the EXACT token
    assert load_from_unified({"scribe": {"mode": "  CLINICAL "}}).mode == SCRIBE_MODE_CLINICAL


@pytest.mark.parametrize(
    "bad", ["synthetic", "clinicalish", "clinical_note", "clin", "", "CLINIC", None, 42, ["clinical"]],
)
def test_mode_unknown_or_malformed_resolves_synthetic(bad):
    # Fail-closed: anything that is not the exact "clinical" token => synthetic.
    assert load_from_unified({"scribe": {"mode": bad}}).mode == SCRIBE_MODE_SYNTHETIC


def test_config_clinicians_fail_closed_default_and_load():
    # scribe P2-a — the designated-clinician allowlist. FAIL-CLOSED default:
    # absent => empty list => no valid attester.
    assert load_from_unified({"scribe": {}}).clinicians == []
    assert load_from_unified({}).clinicians == []
    cfg = load_from_unified({"scribe": {"clinicians": ["np_jamie", "dr_synthetic"]}})
    assert cfg.clinicians == ["np_jamie", "dr_synthetic"]


def test_config_schema_tolerant_and_no_empty_dict_crash():
    # Unknown sub-field ignored; empty sub-dicts don't crash the _build.
    cfg = load_from_unified({"scribe": {
        "mode": "synthetic",
        "stt": {"provider": "faster-whisper", "bogus": 1},
        "llm": {"base_url": "http://127.0.0.1:11434", "extra": "x"},
        "input_dir": "/data/algernon/stayc-clinical/scribe/inbox",
    }})
    assert cfg.stt.provider == "faster-whisper"
    assert cfg.llm.base_url == "http://127.0.0.1:11434"
    assert cfg.input_dir == "/data/algernon/stayc-clinical/scribe/inbox"
    # empty scribe sub-blocks build all-defaults, no crash
    empty = load_from_unified({"scribe": {"stt": {}, "llm": {}}})
    assert empty.stt.provider == ""
    assert empty.llm.base_url == ""


# ---------------------------------------------------------------------------
# Mode-gate — synthetic REFUSES non-synthetic input (mutation-verified)
# ---------------------------------------------------------------------------

def test_synthetic_mode_accepts_synthetic_input():
    cfg = ScribeConfig(mode=SCRIBE_MODE_SYNTHETIC)
    guard_ingest(cfg, provenance={"synthetic": True}, source_id="s1")  # no raise


@pytest.mark.parametrize(
    "prov",
    [{}, {"synthetic": False}, {"synthetic": "true"}, {"synthetic": 1}, {"other": True}, None, "synthetic"],
)
def test_synthetic_mode_refuses_non_synthetic_input(prov):
    # Mutation pin: strict `is True` — the string "true"/int 1/missing all refuse.
    cfg = ScribeConfig(mode=SCRIBE_MODE_SYNTHETIC)
    with pytest.raises(ScribeIngestRefused) as exc:
        guard_ingest(cfg, provenance=prov, source_id="s2")
    assert exc.value.reason == "missing_synthetic_provenance"


def test_clinical_mode_accepts_without_synthetic_tag():
    # clinical is the last switch; the guard would allow (real audio wiring P2).
    cfg = ScribeConfig(mode=SCRIBE_MODE_CLINICAL)
    guard_ingest(cfg, provenance={}, source_id="c1")  # no raise


def test_unknown_mode_falls_through_to_synthetic_required():
    # Defense-in-depth: a mode that isn't exactly "clinical" cannot open the
    # clinical path even if _normalize_mode were bypassed.
    cfg = ScribeConfig(mode="weird")
    with pytest.raises(ScribeIngestRefused):
        guard_ingest(cfg, provenance={}, source_id="u1")


def test_ingest_decision_log_emission_pin():
    cfg = ScribeConfig(mode=SCRIBE_MODE_SYNTHETIC)
    # accept path
    with structlog.testing.capture_logs() as caps:
        guard_ingest(cfg, provenance={"synthetic": True}, source_id="ok")
    accept = [c for c in caps if c.get("event") == "scribe.ingest_decision"]
    assert len(accept) == 1
    assert accept[0]["mode"] == SCRIBE_MODE_SYNTHETIC
    assert accept[0]["accepted"] is True
    assert accept[0]["reason"] == "synthetic_provenance_present"
    assert accept[0]["source_id"] == "ok"
    # refuse path
    with structlog.testing.capture_logs() as caps:
        with pytest.raises(ScribeIngestRefused):
            guard_ingest(cfg, provenance={}, source_id="no")
    refuse = [c for c in caps if c.get("event") == "scribe.ingest_decision"]
    assert len(refuse) == 1
    assert refuse[0]["accepted"] is False
    assert refuse[0]["reason"] == "missing_synthetic_provenance"


# ---------------------------------------------------------------------------
# Attestation (a) — FORWARD-ONLY lifecycle (mutation: allow revert fails)
# ---------------------------------------------------------------------------

def test_legal_forward_transitions():
    validate_status_transition(STATUS_AI_DRAFT, STATUS_ATTESTED)   # no raise
    validate_status_transition(STATUS_ATTESTED, STATUS_AMENDED)    # no raise


def test_no_un_attesting_attested_to_ai_draft():
    # THE forward-only pin (mutation: permit this revert => fails).
    with pytest.raises(AttestationError) as exc:
        validate_status_transition(STATUS_ATTESTED, STATUS_AI_DRAFT)
    assert exc.value.reason == "illegal_status_transition"


@pytest.mark.parametrize(
    "cur,new",
    [
        (STATUS_AI_DRAFT, STATUS_AMENDED),     # skip
        (STATUS_AMENDED, STATUS_ATTESTED),     # backward
        (STATUS_AMENDED, STATUS_AI_DRAFT),     # backward
        (STATUS_ATTESTED, STATUS_ATTESTED),    # same->same (no re-attest)
        (STATUS_AI_DRAFT, STATUS_AI_DRAFT),    # same->same
        ("bogus", STATUS_ATTESTED),            # unknown source
        (STATUS_ATTESTED, "bogus"),            # unknown target
    ],
)
def test_illegal_transitions_refused(cur, new):
    with pytest.raises(AttestationError):
        validate_status_transition(cur, new)


# ---------------------------------------------------------------------------
# Attestation (b) — DISTINCT human clinician (mutation: allow self-attest fails)
# ---------------------------------------------------------------------------

def test_distinct_clinician_attester_allowed():
    validate_attester(
        attester="dr_synthetic", creator=SCRIBE_DRAFTER_IDENTITY,
        clinician_ids=_CLINICIANS,
    )  # no raise


def test_scribe_drafter_may_not_attest():
    # THE distinct-attester pin (mutation: permit self-attest => fails).
    with pytest.raises(AttestationError) as exc:
        validate_attester(
            attester=SCRIBE_DRAFTER_IDENTITY, creator=SCRIBE_DRAFTER_IDENTITY,
            clinician_ids=_CLINICIANS | {SCRIBE_DRAFTER_IDENTITY},
        )
    assert exc.value.reason == "scribe_self_attest"


def test_creator_may_not_self_attest():
    with pytest.raises(AttestationError) as exc:
        validate_attester(
            attester="dr_synthetic", creator="dr_synthetic",
            clinician_ids=_CLINICIANS,
        )
    assert exc.value.reason == "self_attest"


def test_non_clinician_attester_refused():
    with pytest.raises(AttestationError) as exc:
        validate_attester(
            attester="random_user", creator=SCRIBE_DRAFTER_IDENTITY,
            clinician_ids=_CLINICIANS,
        )
    assert exc.value.reason == "attester_not_clinician"


def test_empty_attester_refused():
    with pytest.raises(AttestationError) as exc:
        validate_attester(attester="  ", creator="", clinician_ids=_CLINICIANS)
    assert exc.value.reason == "attester_missing"


@pytest.mark.parametrize("bad_creator", ["", "   ", None])
def test_empty_creator_fail_closed(bad_creator):
    # NOTE-2 hardening (mutation pin): a medico-legal self-attest guard must
    # not be disable-able by passing an empty creator — the old
    # ``if creator and ...`` short-circuit skipped the self-attest check when
    # creator was blank. Fail closed: empty/None/blank creator => refuse.
    with pytest.raises(AttestationError) as exc:
        validate_attester(
            attester="dr_synthetic", creator=bad_creator, clinician_ids=_CLINICIANS,
        )
    assert exc.value.reason == "creator_missing"


def test_authorize_attestation_refuses_empty_creator():
    with pytest.raises(AttestationError) as exc:
        authorize_attestation(
            current_status=STATUS_AI_DRAFT, new_status=STATUS_ATTESTED,
            attester="dr_synthetic", creator="", clinician_ids=_CLINICIANS,
        )
    assert exc.value.reason == "creator_missing"


def test_empty_clinician_allowlist_refuses_everyone():
    # Fail-closed: no designated clinicians => no attester passes.
    with pytest.raises(AttestationError):
        validate_attester(attester="dr_synthetic", creator="x", clinician_ids=frozenset())


# ---------------------------------------------------------------------------
# authorize_attestation — combined gate + log emission
# ---------------------------------------------------------------------------

def test_authorize_attestation_happy_path_and_log():
    with structlog.testing.capture_logs() as caps:
        authorize_attestation(
            current_status=STATUS_AI_DRAFT, new_status=STATUS_ATTESTED,
            attester="np_jamie", creator=SCRIBE_DRAFTER_IDENTITY,
            clinician_ids=_CLINICIANS,
        )
    ev = [c for c in caps if c.get("event") == "scribe.attestation"]
    assert len(ev) == 1
    assert ev[0]["authorized"] is True
    assert ev[0]["from_status"] == STATUS_AI_DRAFT
    assert ev[0]["to_status"] == STATUS_ATTESTED


def test_authorize_attestation_refuses_self_attest_and_logs():
    with structlog.testing.capture_logs() as caps:
        with pytest.raises(AttestationError):
            authorize_attestation(
                current_status=STATUS_AI_DRAFT, new_status=STATUS_ATTESTED,
                attester=SCRIBE_DRAFTER_IDENTITY, creator=SCRIBE_DRAFTER_IDENTITY,
                clinician_ids=_CLINICIANS | {SCRIBE_DRAFTER_IDENTITY},
            )
    ev = [c for c in caps if c.get("event") == "scribe.attestation"]
    assert len(ev) == 1
    assert ev[0]["authorized"] is False
    assert ev[0]["reason"] == "scribe_self_attest"


def test_authorize_attestation_refuses_un_attest():
    with pytest.raises(AttestationError) as exc:
        authorize_attestation(
            current_status=STATUS_ATTESTED, new_status=STATUS_AI_DRAFT,
            attester="np_jamie", creator=SCRIBE_DRAFTER_IDENTITY,
            clinician_ids=_CLINICIANS,
        )
    assert exc.value.reason == "illegal_status_transition"

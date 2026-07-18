"""Consent facade contract pins (#12 slice 12a — design doc §3, §12).

The consent state machine (§3.1) + the 4 typed emitters (§3.3) + consent_state() resolver, all
in scribe.events. Contract-first: the legality matrix, PHI-free scalar payloads, emitter-authority
(only these constructors, exact stream/kind/actor), durable-vs-capture posture, and the resolver's
"violation_refused is not a state" property. The KINDS widening pin lives in test_scribe_events.py
(already green — #11 contract-registered the consent kinds; 12a adds only emitters, no schema change).
"""
from __future__ import annotations

import pytest

from alfred.evstore import EventStoreError
from alfred.scribe.events import ConsentTransitionError, ScribeEvents

_CLOCK = "2026-07-17T09:00:00+00:00"
_ENC = "enc-1720000000000-0123456789abcdef"      # an opaque encounter id (PHI-free, §10)
_ENC2 = "enc-1720000000001-fedcba9876543210"


def _events(tmp_path, mode="clinical"):
    raw = {"scribe": {"mode": mode, "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: _CLOCK)


# --- state machine legality (§3.1 / §12) ------------------------------------

def test_consent_state_empty_before_any_event(tmp_path):
    assert _events(tmp_path).consent_state(_ENC) == ""      # ∅ — no consent set


def test_legal_transitions_confirm_then_withdraw(tmp_path):
    ev = _events(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")     # ∅ → confirmed
    assert ev.consent_state(_ENC) == "confirmed"
    ev.consent_withdrawn(subject_id=_ENC, at_seq=3, actor="np_jamie")  # confirmed → withdrawn
    assert ev.consent_state(_ENC) == "withdrawn"


def test_legal_transition_decline(tmp_path):
    ev = _events(tmp_path)
    ev.consent_declined(subject_id=_ENC, captured_by="np_jamie")       # ∅ → declined
    assert ev.consent_state(_ENC) == "declined"


def test_withdraw_from_empty_is_refused(tmp_path):
    ev = _events(tmp_path)
    with pytest.raises(ConsentTransitionError):
        ev.consent_withdrawn(subject_id=_ENC, at_seq=0, actor="np_jamie")  # ∅ → withdrawn illegal


def test_double_confirm_is_refused(tmp_path):
    ev = _events(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")
    with pytest.raises(ConsentTransitionError):
        ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")  # confirmed → confirmed illegal


def test_confirmed_cannot_decline(tmp_path):
    ev = _events(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")
    with pytest.raises(ConsentTransitionError):
        ev.consent_declined(subject_id=_ENC, captured_by="np_jamie")


@pytest.mark.parametrize("terminal_setup", ["declined", "withdrawn"])
def test_terminal_states_refuse_all_transitions(tmp_path, terminal_setup):
    ev = _events(tmp_path)
    if terminal_setup == "declined":
        ev.consent_declined(subject_id=_ENC, captured_by="np_jamie")
    else:
        ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")
        ev.consent_withdrawn(subject_id=_ENC, at_seq=1, actor="np_jamie")
    for illegal in (
        lambda: ev.consent_confirmed(subject_id=_ENC, captured_by="x"),
        lambda: ev.consent_declined(subject_id=_ENC, captured_by="x"),
        lambda: ev.consent_withdrawn(subject_id=_ENC, at_seq=9, actor="x"),
    ):
        with pytest.raises(ConsentTransitionError):
            illegal()


def test_transition_error_is_eventstoreerror(tmp_path):
    # ConsentTransitionError subclasses EventStoreError so a route that already fails-closed on a
    # durable-append error (`except EventStoreError`) also fails-closed on an illegal transition.
    ev = _events(tmp_path)
    assert issubclass(ConsentTransitionError, EventStoreError)
    try:
        ev.consent_withdrawn(subject_id=_ENC, at_seq=0, actor="x")
        raise AssertionError("expected refusal")
    except EventStoreError:
        pass                                                # caught by the base handler


# --- the resolver reads STATE kinds only (§3.2) -----------------------------

def test_violation_refused_is_not_a_state(tmp_path):
    # THE resolver bug the design guards: a violation_refused appended AFTER confirmed must not be
    # read as the "latest" state. consent_state ignores it; withdraw stays legal.
    ev = _events(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")
    ev.consent_violation_refused(subject_id=_ENC, seq=1)              # NOT a state
    assert ev.consent_state(_ENC) == "confirmed"                     # still confirmed
    ev.consent_withdrawn(subject_id=_ENC, at_seq=1, actor="np_jamie")  # ...so withdraw is still legal
    assert ev.consent_state(_ENC) == "withdrawn"


def test_consent_state_is_per_encounter(tmp_path):
    ev = _events(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")
    ev.consent_declined(subject_id=_ENC2, captured_by="np_jamie")
    assert ev.consent_state(_ENC) == "confirmed" and ev.consent_state(_ENC2) == "declined"


# --- emitter authority + PHI-free scalar payloads (§3.3 / §10) --------------

def test_confirmed_declined_authority_and_payload(tmp_path):
    ev = _events(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")
    ev.consent_declined(subject_id=_ENC2, captured_by="dr_x")
    conf = ev.query("clinical", kind="consent.confirmed")[0]
    dec = ev.query("clinical", kind="consent.declined")[0]
    for row, who in ((conf, "np_jamie"), (dec, "dr_x")):
        assert row["actor"] == who and row["actor_kind"] == "clinician"
        assert row["payload"] == {"method": "verbal", "captured_by": who}   # exact, PHI-free
        assert set(row["payload"]) == {"method", "captured_by"}             # no undeclared field


def test_withdrawn_authority_and_payload(tmp_path):
    ev = _events(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")
    ev.consent_withdrawn(subject_id=_ENC, at_seq="3", actor="np_jamie")     # str coerced → int
    w = ev.query("clinical", kind="consent.withdrawn")[0]
    assert w["actor"] == "np_jamie" and w["actor_kind"] == "clinician"
    assert w["payload"] == {"at_seq": 3} and isinstance(w["payload"]["at_seq"], int)


def test_violation_refused_authority_and_payload(tmp_path):
    ev = _events(tmp_path)
    ev.consent_violation_refused(subject_id=_ENC, seq="1")                  # str coerced → int
    v = ev.query("clinical", kind="consent.violation_refused")[0]
    assert v["actor"] == "stayc_scribe" and v["actor_kind"] == "system"    # the gate refused it
    assert v["payload"] == {"seq": 1} and isinstance(v["payload"]["seq"], int)


def test_no_generic_consent_emit_verb():
    # The 4 typed emitters are the ONLY consent constructors (§2.2) — no generic verb.
    assert not hasattr(ScribeEvents, "emit")
    assert not hasattr(ScribeEvents, "consent_emit")


# --- durable-vs-capture posture (§3.3) --------------------------------------

def test_durable_emitters_fail_loud_capture_swallows(tmp_path):
    # confirmed/declined/withdrawn are DURABLE — an inactive store RAISES (consent evidence must be
    # recorded before the act it gates is acknowledged). violation_refused is best-effort — swallows.
    ev = _events(tmp_path)
    ev._active = False                                      # simulate a degraded/inactive store
    for durable in (
        lambda: ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie"),
        lambda: ev.consent_declined(subject_id=_ENC2, captured_by="np_jamie"),
    ):
        with pytest.raises(EventStoreError):
            durable()
    assert ev.consent_violation_refused(subject_id=_ENC, seq=1) is None     # best-effort → None

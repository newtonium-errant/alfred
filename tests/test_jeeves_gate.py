"""The Jeeves capture gate (task #81).

The gate is what stands between an always-listening microphone and the
network, so the pins here are about the branch structure itself: only the
exact word opens the live path, an unrecognised mode falls through to the
strict branch, and every decision — accept or refuse — emits exactly one
event carrying its reason.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.jeeves.config import (
    JEEVES_MODE_LIVE,
    JEEVES_MODE_SYNTHETIC,
    JeevesConfig,
)
from alfred.jeeves.gate import (
    ACCEPTED_LIVE_MODE,
    ACCEPTED_SYNTHETIC_PROVENANCE,
    REFUSED_MISSING_SYNTHETIC,
    JeevesCaptureRefused,
    guard_capture,
)


def config(mode: str) -> JeevesConfig:
    cfg = JeevesConfig()
    cfg.mode = mode
    return cfg


def test_live_mode_accepts_untagged_input():
    guard_capture(config(JEEVES_MODE_LIVE), provenance={}, source_id="cue@1s")


def test_synthetic_mode_accepts_tagged_input():
    guard_capture(
        config(JEEVES_MODE_SYNTHETIC),
        provenance={"synthetic": True}, source_id="cue@1s",
    )


def test_synthetic_mode_refuses_untagged_input():
    with pytest.raises(JeevesCaptureRefused) as exc:
        guard_capture(
            config(JEEVES_MODE_SYNTHETIC), provenance={}, source_id="cue@1s")
    assert exc.value.reason == REFUSED_MISSING_SYNTHETIC
    assert exc.value.mode == JEEVES_MODE_SYNTHETIC
    assert exc.value.source_id == "cue@1s"


def test_an_unrecognised_mode_can_never_open_the_live_path():
    """DEFENSE IN DEPTH. The live branch is gated on the EXACT string, so a
    config whose mode did not normalize falls through to the strict branch —
    even if the normalizer were bypassed entirely."""
    for bogus in ("Live", "LIVE ", "clinical", "", "enabled", "true"):
        with pytest.raises(JeevesCaptureRefused):
            guard_capture(config(bogus), provenance={})
    # ...and the same bogus mode with synthetic provenance still works.
    guard_capture(config("clinical"), provenance={"synthetic": True})


@pytest.mark.parametrize("provenance", [
    {"synthetic": "true"}, {"synthetic": 1}, {"synthetic": 1.0},
    {"synthetic": "yes"}, {"synthetic": None}, {"synth": True},
    {}, None, "synthetic", ["synthetic"], 1, True,
])
def test_only_a_literal_boolean_true_passes_the_strict_test(provenance):
    """Accepting truthy values would mean a YAML quirk or a JSON round trip
    could arm the live path."""
    with pytest.raises(JeevesCaptureRefused):
        guard_capture(config(JEEVES_MODE_SYNTHETIC), provenance=provenance)


def test_a_refusal_emits_exactly_one_decision_event_with_its_reason():
    with structlog.testing.capture_logs() as captured:
        with pytest.raises(JeevesCaptureRefused):
            guard_capture(
                config(JEEVES_MODE_SYNTHETIC), provenance={},
                source_id="cue@42.5s")
    events = [c for c in captured if c.get("event") == "jeeves.capture_decision"]
    assert len(events) == 1
    assert events[0] == {
        "event": "jeeves.capture_decision",
        "log_level": "info",
        "mode": JEEVES_MODE_SYNTHETIC,
        "accepted": False,
        "reason": REFUSED_MISSING_SYNTHETIC,
        "source_id": "cue@42.5s",
    }


def test_an_acceptance_also_emits_one_event_with_which_branch_took_it():
    """Intentionally-left-blank: a refused capture and an idle device must
    not look the same in a log, and neither must the two accept paths — one
    of them means real audio is flowing."""
    with structlog.testing.capture_logs() as captured:
        guard_capture(config(JEEVES_MODE_LIVE), provenance={}, source_id="a")
        guard_capture(
            config(JEEVES_MODE_SYNTHETIC),
            provenance={"synthetic": True}, source_id="b")
    events = [c for c in captured if c.get("event") == "jeeves.capture_decision"]
    assert [e["reason"] for e in events] == [
        ACCEPTED_LIVE_MODE, ACCEPTED_SYNTHETIC_PROVENANCE,
    ]
    assert all(e["accepted"] for e in events)


def test_the_refusal_message_carries_no_content():
    """An exception message ends up in tracebacks, logs and error reports at
    once — one of the easiest places for content to leak."""
    with pytest.raises(JeevesCaptureRefused) as exc:
        guard_capture(
            config(JEEVES_MODE_SYNTHETIC),
            provenance={"synthetic": False, "transcript": "the words spoken"},
            source_id="cue@1s",
        )
    assert "the words spoken" not in str(exc.value)
    assert "transcript" not in str(exc.value)


def test_the_refusal_says_how_to_fix_it():
    """A fence that refuses without naming the switch is a fence somebody
    works around."""
    with pytest.raises(JeevesCaptureRefused) as exc:
        guard_capture(config(JEEVES_MODE_SYNTHETIC), provenance={})
    assert "jeeves.mode" in str(exc.value)
    assert "live" in str(exc.value)

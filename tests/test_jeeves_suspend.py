"""The company toggle — one suspended state, two doors (#98, ruling 3).

Ruling 3 in the operator's words: *"Jeeves, company" suspends wake-word and
capture until a release phrase, WITH visible indication it took effect, PLUS
a manual button as an equal second path — both set the same state, both
logged, and the state survives a restart (fail-closed: if the state is
unknown → suspended).*

Every fail-closed pin here carries a POSITIVE CONTROL in the same test. "The
state could not be read, therefore suspended" is worth nothing on its own: a
build that returned ``suspended=True`` unconditionally — or one where the
whole store is broken — passes it. The control is the readable, running
state that must come back NOT suspended, and it is what makes the pin able
to fail in both directions.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
import structlog

from alfred.jeeves import cues, marklog, service, suspend, telemetry
from alfred.jeeves.audio import AudioFormat, MemoryAudioSource, silence, tone
from alfred.jeeves.config import (
    JEEVES_MODE_LIVE,
    JeevesConfig,
    JeevesCueConfig,
    JeevesRingConfig,
    JeevesSttConfig,
    JeevesWindowConfig,
)
from alfred.jeeves.gate import (
    ACCEPTED_LIVE_MODE,
    REFUSED_SUSPENDED,
    JeevesCaptureSuspended,
    guard_capture,
    guard_not_suspended,
)
from alfred.jeeves.wake import ScriptedWakeDetector
from alfred.telegram.stt_backends import SttResult

FMT = AudioFormat(sample_rate=1000, sample_width=2, channels=1)   # 2000 B/s


@pytest.fixture(autouse=True)
def _clear_latches():
    """The unconfigured-store warning is latched once per lifecycle, which
    would make it invisible to every test after the first."""
    suspend.reset_warning_latches()
    yield
    suspend.reset_warning_latches()


def state_path(tmp_path) -> str:
    return str(tmp_path / "jeeves" / "suspended.json")


def write_state(tmp_path, payload) -> str:
    path = tmp_path / "jeeves" / "suspended.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return str(path)


def running_state(tmp_path) -> str:
    """THE POSITIVE CONTROL: a readable state file that says 'running'."""
    return write_state(tmp_path, {
        "suspended": False,
        "source": suspend.SOURCE_MANUAL,
        "reason": suspend.REASON_MANUAL_RELEASE,
        "since": "2026-08-11T12:00:00+00:00",
    })


# ---------------------------------------------------------------------------
# Fail-closed reads — each paired with the control that makes it non-vacuous
# ---------------------------------------------------------------------------


def test_a_readable_running_state_is_not_suspended(tmp_path):
    """The control on its own. If this ever goes red, every fail-closed pin
    below becomes unfalsifiable."""
    status = suspend.read_status(running_state(tmp_path))
    assert status.suspended is False
    assert status.reason == suspend.REASON_MANUAL_RELEASE
    assert status.fail_closed is False


def test_a_missing_state_file_is_suspended_and_a_readable_one_is_not(tmp_path):
    """FAIL-CLOSED + CONTROL. A missing file after a suspension and a missing
    file on a first boot are indistinguishable from here, so the tie goes to
    the microphone: one failure is visible and recoverable in five seconds,
    the other is invisible and is the failure the toggle exists to prevent."""
    missing = suspend.read_status(str(tmp_path / "never" / "written.json"))
    assert missing.suspended is True
    assert missing.reason == suspend.REASON_NO_STATE_FILE
    assert missing.fail_closed is True

    # ...and the SAME function on a readable running state says otherwise,
    # so "always suspended" cannot pass this test.
    assert suspend.read_status(running_state(tmp_path)).suspended is False


def test_an_unreadable_state_file_is_suspended_and_a_readable_one_is_not(tmp_path):
    """FAIL-CLOSED + CONTROL. A directory where the file should be is a
    deterministic OSError that does not depend on the test running as a
    non-root user."""
    blocked = tmp_path / "blocked.json"
    blocked.mkdir()
    status = suspend.read_status(str(blocked))
    assert status.suspended is True
    assert status.reason == suspend.REASON_UNREADABLE

    assert suspend.read_status(running_state(tmp_path)).suspended is False


def test_a_corrupt_state_file_is_suspended_and_a_readable_one_is_not(tmp_path):
    """FAIL-CLOSED + CONTROL. Half a JSON object is the signature of a device
    that lost power mid-write."""
    corrupt = write_state(tmp_path, '{"suspended": tr')
    status = suspend.read_status(corrupt)
    assert status.suspended is True
    assert status.reason == suspend.REASON_CORRUPT

    assert suspend.read_status(running_state(tmp_path)).suspended is False


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}, "yes"])
def test_a_non_boolean_suspended_is_malformed_not_coerced(tmp_path, value):
    """STRICT, the gate's posture. A truthiness coercion here would let a
    JSON round-trip or a YAML quirk decide whether a microphone is live —
    and note that ``"false"`` and ``0`` resolve to SUSPENDED too, which is
    the direction that proves this is not just truthiness by another name."""
    path = write_state(tmp_path, {"suspended": value})
    status = suspend.read_status(path)
    assert status.suspended is True
    assert status.reason == suspend.REASON_MALFORMED

    assert suspend.read_status(running_state(tmp_path)).suspended is False


def test_a_non_dict_state_is_malformed(tmp_path):
    path = write_state(tmp_path, "[1, 2, 3]")
    assert suspend.read_status(path).reason == suspend.REASON_MALFORMED


def test_every_fail_closed_reason_is_flagged_as_assumed(tmp_path):
    """The UI renders "you asked me to stop" and "I cannot tell" differently,
    so the struct has to be able to tell them apart."""
    ruled = write_state(tmp_path, {
        "suspended": True, "source": suspend.SOURCE_SPOKEN,
        "reason": suspend.REASON_SPOKEN_SUSPEND,
    })
    asked_for = suspend.read_status(ruled)
    assert asked_for.suspended is True
    assert asked_for.fail_closed is False, (
        "a suspension the operator asked for must not render as an assumed one"
    )
    assumed = suspend.read_status(str(tmp_path / "gone.json"))
    assert assumed.suspended is True and assumed.fail_closed is True


def test_an_unconfigured_store_is_not_suspended_and_says_so_loudly():
    """THE ONE EXCEPTION, and it is a config state rather than an unknown
    one: fail-closed cannot apply to a feature with nowhere to keep its
    state, because that reading would leave every hand-built config
    permanently suspended with no file able to release it."""
    with structlog.testing.capture_logs() as captured:
        status = suspend.read_status("")
    assert status.suspended is False
    assert status.reason == suspend.REASON_STORE_UNCONFIGURED
    events = [
        c for c in captured
        if c.get("event") == "jeeves.suspend.store_unconfigured"
    ]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"


def test_the_unconfigured_warning_is_latched_once_per_lifecycle():
    """It runs once per audio chunk. Twelve identical warnings a second is
    not observability."""
    with structlog.testing.capture_logs() as captured:
        for _ in range(5):
            suspend.read_status("")
    events = [
        c for c in captured
        if c.get("event") == "jeeves.suspend.store_unconfigured"
    ]
    assert len(events) == 1


def test_a_fail_closed_read_is_logged_with_its_reason(tmp_path):
    with structlog.testing.capture_logs() as captured:
        suspend.read_status(str(tmp_path / "absent.json"))
    events = [c for c in captured if c.get("event") == "jeeves.suspend.fail_closed"]
    assert len(events) == 1
    assert events[0]["reason"] == suspend.REASON_NO_STATE_FILE
    assert events[0]["suspended"] is True
    assert events[0]["source"] == suspend.SOURCE_FAIL_CLOSED


def test_schema_tolerance_drops_a_key_this_build_does_not_know(tmp_path):
    """The CLAUDE.md load() contract: a state file written by a newer build
    must not crash the loader on a rollback."""
    path = write_state(tmp_path, {
        "suspended": True, "source": suspend.SOURCE_SPOKEN,
        "reason": suspend.REASON_SPOKEN_SUSPEND,
        "a_field_from_the_future": {"nested": [1, 2]},
    })
    status = suspend.read_status(path)
    assert status.suspended is True
    assert status.reason == suspend.REASON_SPOKEN_SUSPEND


# ---------------------------------------------------------------------------
# Two doors, one transition function
# ---------------------------------------------------------------------------


def test_both_doors_write_a_state_that_differs_ONLY_in_its_source(tmp_path):
    """"Spoken-or-manual interchangeable" is only true if the two doors
    cannot drift, and two code paths that each wrote the flag themselves
    WOULD drift — one of them would forget the atomic write, the 0600, or
    the log line. So they are the same call with a different word."""
    spoken_path = str(tmp_path / "a" / "suspended.json")
    manual_path = str(tmp_path / "b" / "suspended.json")
    stamp = "2026-08-11T09:00:00+00:00"

    suspend.set_suspended(
        spoken_path, True, source=suspend.SOURCE_SPOKEN, now=stamp)
    suspend.set_suspended(
        manual_path, True, source=suspend.SOURCE_MANUAL, now=stamp)

    spoken = json.loads(open(spoken_path, encoding="utf-8").read())
    manual = json.loads(open(manual_path, encoding="utf-8").read())
    assert spoken["suspended"] == manual["suspended"] is True
    assert spoken["since"] == manual["since"] == stamp
    assert spoken["source"] == suspend.SOURCE_SPOKEN
    assert manual["source"] == suspend.SOURCE_MANUAL
    # ...and NOTHING else differs.
    assert {k: v for k, v in spoken.items() if k not in ("source", "reason")} == \
           {k: v for k, v in manual.items() if k not in ("source", "reason")}


def test_both_doors_leave_the_same_telemetry_row_shape(tmp_path):
    """The manual button is a UI process that never touches the capture loop.
    If only the spoken path emitted telemetry, the morning rollup would show
    a device that suspends itself and never comes back."""
    rows = []
    for source in (suspend.SOURCE_SPOKEN, suspend.SOURCE_MANUAL):
        tele = str(tmp_path / f"{source}.jsonl")
        suspend.set_suspended(
            str(tmp_path / f"{source}.json"), True,
            source=source, telemetry_path=tele, now="2026-08-11T09:00:00+00:00",
        )
        rows.append(telemetry.read_rows(tele)[0])

    assert [r["event"] for r in rows] == [
        telemetry.EVENT_SUSPENDED, telemetry.EVENT_SUSPENDED,
    ]
    assert [r["toggle_source"] for r in rows] == [
        suspend.SOURCE_SPOKEN, suspend.SOURCE_MANUAL,
    ]
    assert set(rows[0]) == set(rows[1])


def test_a_release_gets_a_row_too(tmp_path):
    """ILB, BOTH DIRECTIONS. A rollup that recorded suspensions and not
    releases would show a device that goes deaf and never comes back."""
    tele = str(tmp_path / "t.jsonl")
    path = state_path(tmp_path)
    suspend.set_suspended(
        path, True, source=suspend.SOURCE_SPOKEN, telemetry_path=tele)
    suspend.set_suspended(
        path, False, source=suspend.SOURCE_MANUAL, telemetry_path=tele)
    events = [r["event"] for r in telemetry.read_rows(tele)]
    assert events == [telemetry.EVENT_SUSPENDED, telemetry.EVENT_RESUMED]


def test_the_fail_closed_source_can_never_be_WRITTEN(tmp_path):
    """It is a READ outcome. Storing it would make an assumed suspension
    indistinguishable from one the operator asked for, permanently."""
    with pytest.raises(ValueError) as exc:
        suspend.set_suspended(
            state_path(tmp_path), True, source=suspend.SOURCE_FAIL_CLOSED)
    assert suspend.SOURCE_FAIL_CLOSED in str(exc.value)
    assert not os.path.exists(state_path(tmp_path))


@pytest.mark.parametrize("source", [suspend.SOURCE_SPOKEN, suspend.SOURCE_MANUAL])
def test_the_state_survives_a_restart(tmp_path, source):
    """The ruling's own words. A flag in memory is not a suspension."""
    path = state_path(tmp_path)
    suspend.set_suspended(path, True, source=source)
    # A "restart" is exactly this: a fresh read, no in-process state.
    assert suspend.is_suspended(path) is True
    suspend.set_suspended(path, False, source=source)
    assert suspend.is_suspended(path) is False


def test_the_write_is_atomic_and_owner_only(tmp_path):
    """0600 under a 0700 directory — the mark log's posture. This file says
    whether a microphone in a shared space is listening."""
    path = state_path(tmp_path)
    suspend.set_suspended(path, True, source=suspend.SOURCE_MANUAL)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert not os.path.exists(path + ".tmp"), "a .tmp file survived the rename"


def test_a_write_failure_resolves_fail_closed_rather_than_lying(tmp_path):
    """If the transition did not reach the disk it will not survive a
    restart, and the safe reading of "I could not record that you released
    me" is that the device stays off."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    status = suspend.set_suspended(
        str(blocker / "suspended.json"), False, source=suspend.SOURCE_MANUAL)
    assert status.suspended is True
    assert status.fail_closed is True


def test_a_transition_with_no_store_is_reported_not_silently_dropped(tmp_path):
    with structlog.testing.capture_logs() as captured:
        status = suspend.set_suspended("", True, source=suspend.SOURCE_MANUAL)
    assert status.reason == suspend.REASON_STORE_UNCONFIGURED
    assert [c["event"] for c in captured if c["event"] == "jeeves.suspend.no_store"]


# ---------------------------------------------------------------------------
# Transition logging — ILB in both directions
# ---------------------------------------------------------------------------


def test_a_suspend_and_a_release_are_BOTH_logged_with_their_source(tmp_path):
    path = state_path(tmp_path)
    with structlog.testing.capture_logs() as captured:
        suspend.set_suspended(path, True, source=suspend.SOURCE_SPOKEN)
        suspend.set_suspended(path, False, source=suspend.SOURCE_MANUAL)
    events = [c for c in captured if c.get("event") == "jeeves.suspend.transition"]
    assert len(events) == 2
    assert [e["suspended"] for e in events] == [True, False]
    assert [e["source"] for e in events] == [
        suspend.SOURCE_SPOKEN, suspend.SOURCE_MANUAL,
    ]
    assert [e["reason"] for e in events] == [
        suspend.REASON_SPOKEN_SUSPEND, suspend.REASON_MANUAL_RELEASE,
    ]
    # ``changed`` is measured against the state that was ACTUALLY in force,
    # which on a device with no state file is the fail-closed suspension —
    # so the first line reads as "already suspended, now for a stated
    # reason" and only the release is a real change. That asymmetry is the
    # fail-closed default being visible in the log rather than implied.
    assert [e["changed"] for e in events] == [False, True]
    assert events[0]["previous_reason"] == suspend.REASON_NO_STATE_FILE


def test_a_repeated_suspend_is_logged_as_a_no_op(tmp_path):
    """Saying "Jeeves, company" twice is one suspension, and a log that
    could not say so would read as two visitors."""
    path = state_path(tmp_path)
    suspend.set_suspended(path, True, source=suspend.SOURCE_SPOKEN)
    with structlog.testing.capture_logs() as captured:
        suspend.set_suspended(path, True, source=suspend.SOURCE_SPOKEN)
    event = [c for c in captured if c.get("event") == "jeeves.suspend.transition"][0]
    assert event["changed"] is False
    assert event["previous_suspended"] is True


# ---------------------------------------------------------------------------
# The spoken door's grammar
# ---------------------------------------------------------------------------


def classify(text: str, **kwargs):
    return cues.classify(
        text,
        cue_config=kwargs.pop("cue_config", JeevesCueConfig(route_target="peerbox")),
        **kwargs,
    )


@pytest.mark.parametrize("text", [
    "jeeves company",
    "jeeves, company",
    "jeeves we have company",
    "jeeves we've got company",
    "jeeves stop listening",
])
def test_the_ruled_company_phrase_suspends(text: str):
    assert classify(text).verb == cues.CUE_SUSPEND


@pytest.mark.parametrize("text", ["jeeves all clear", "jeeves as you were"])
def test_the_proposed_release_phrases_resume(text: str):
    """PROPOSED, not ruled — the operator ruled that a release exists and
    left the words open. Pinned so the proposal is a fact in the tree rather
    than a sentence in a report."""
    assert classify(text).verb == cues.CUE_RESUME


def test_accompany_does_not_contain_a_company_cue():
    """Token boundaries. The plain substring matcher the capture verbs use
    would fire on this, and a false suspend costs the operator a deaf
    device."""
    result = classify("jeeves, note that i'll accompany you tomorrow")
    assert result.verb == cues.CUE_MARK_DOWN


def test_the_toggle_is_anchored_to_the_cue_not_to_the_whole_window():
    """THE ANCHORING PIN + ITS CONTROL. A cued window opens up to 45 seconds
    BEFORE the operator spoke, so most of the transcript is ordinary
    conversation that happened to be in the ring."""
    lookback = (
        "so we have company coming over on saturday and i said that was fine "
        "and then i need to remember about the bearing jeeves note that"
    )
    assert classify(lookback).verb == cues.CUE_MARK_DOWN, (
        "a conversation about company in the LOOKBACK must not suspend the mic"
    )
    # THE CONTROL: the same words, said at the cue, DO suspend — so this is
    # an anchoring pin and not a "the toggle never fires" pin.
    assert classify(
        "i need to remember about the bearing jeeves we have company"
    ).verb == cues.CUE_SUSPEND


def test_with_no_wake_token_rendered_the_tail_still_carries_the_toggle():
    """Q6 finding: an STT may render the wake word as something else or drop
    it. The fallback scope is the final few tokens — where a phrase spoken at
    the cue position ends up."""
    assert classify("the bearing is the wrong size company").verb == \
        cues.CUE_SUSPEND
    # ...and the same phrase far enough back is out of the tail (control).
    assert classify(
        "company was here earlier and then we talked about the bearing and "
        "the wrong size and ordering a new one from the supplier tomorrow"
    ).verb == cues.CUE_NONE


def test_the_toggle_outranks_a_capture_verb_in_the_same_window():
    """A window that carries both is a window in which the operator asked for
    privacy, and honouring the capture instead is the one outcome he cannot
    undo."""
    assert classify("jeeves company note that").verb == cues.CUE_SUSPEND


def test_the_toggle_grammars_are_not_minimal_pairs_with_anything(tmp_path):
    """The design's rule, applied to the new grammars: the room is noisy and
    the recogniser runs at distance."""
    grammars = {
        "suspend": cues.SUSPEND_PHRASES,
        "resume": cues.RESUME_PHRASES,
        "mark": cues.MARK_PHRASES,
        "miss": cues.MISS_PHRASES,
    }
    for left in ("suspend", "resume"):
        for right in grammars:
            if left == right:
                continue
            conflicts = cues.minimal_pair_conflicts(
                grammars[left], grammars[right])
            assert conflicts == [], (
                f"{left} and {right} phrases are minimal pairs: {conflicts}"
            )


def test_a_toggle_carries_no_lookback_modifier():
    """Suspending is not a capture, so "how far back" means nothing for it,
    and recording a number would put a lookback in a telemetry row that no
    window was ever cut with."""
    result = classify("jeeves company the last couple of minutes")
    assert result.verb == cues.CUE_SUSPEND
    assert result.lookback_seconds is None
    assert result.modifier == cues.MODIFIER_NONE


# ---------------------------------------------------------------------------
# The gate — and the reason it is a SEPARATE gate
# ---------------------------------------------------------------------------


def build_config(tmp_path, **overrides) -> JeevesConfig:
    base = dict(
        mode=JEEVES_MODE_LIVE,
        ring=JeevesRingConfig(
            seconds=60, sample_rate=1000, sample_width=2, channels=1),
        window=JeevesWindowConfig(
            lookback_seconds=5.0, extended_lookback_seconds=20.0,
            silence_seconds=2.0, max_lookahead_seconds=5.0,
            max_lookback_seconds=30.0, silence_rms_threshold=0.01),
        stt=JeevesSttConfig(api_key="DUMMY_GROQ_TEST_KEY"),
        cues=JeevesCueConfig(route_target="peerbox"),
        telemetry_path=str(tmp_path / "telemetry.jsonl"),
        mark_log_path=str(tmp_path / "marks.jsonl"),
        suspend_state_path=state_path(tmp_path),
    )
    base.update(overrides)
    return JeevesConfig(**base)


def test_the_suspend_gate_refuses_with_its_own_reason_and_status(tmp_path):
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_SPOKEN)
    with pytest.raises(JeevesCaptureSuspended) as exc:
        guard_not_suspended(cfg, source_id="cue@1s")
    assert exc.value.reason == REFUSED_SUSPENDED
    assert exc.value.status.reason == suspend.REASON_SPOKEN_SUSPEND
    assert exc.value.source_id == "cue@1s"
    # ...and the CONTROL: released, the same call returns rather than raises.
    suspend.set_suspended(
        cfg.suspend_state_path, False, source=suspend.SOURCE_SPOKEN)
    assert guard_not_suspended(cfg, source_id="cue@1s").suspended is False


def test_the_suspend_gate_logs_exactly_one_decision_either_way(tmp_path):
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, False, source=suspend.SOURCE_MANUAL)
    with structlog.testing.capture_logs() as captured:
        guard_not_suspended(cfg, source_id="a")
    accepted = [c for c in captured if c.get("event") == "jeeves.suspend_decision"]
    assert len(accepted) == 1 and accepted[0]["accepted"] is True

    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_MANUAL)
    with structlog.testing.capture_logs() as captured:
        with pytest.raises(JeevesCaptureSuspended):
            guard_not_suspended(cfg, source_id="b")
    refused = [c for c in captured if c.get("event") == "jeeves.suspend_decision"]
    assert len(refused) == 1
    assert refused[0]["accepted"] is False
    assert refused[0]["reason"] == REFUSED_SUSPENDED


def test_the_refusal_message_names_both_ways_back(tmp_path):
    """A fence that refuses without naming the switch is one somebody works
    around — and here the switch is the whole point of the second door."""
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_SPOKEN)
    with pytest.raises(JeevesCaptureSuspended) as exc:
        guard_not_suspended(cfg)
    assert "manual" in str(exc.value)
    assert "release" in str(exc.value)


def test_a_SUSPENDED_device_does_not_break_the_RECEIVING_instances_route(tmp_path):
    """THE REASON THE TWO GATES ARE SEPARATE FUNCTIONS.

    ``guard_capture`` has two callers: this package's service, which runs ON
    the garage device, and ``alfred.transport.routes_jeeves``, which runs on
    the RECEIVING instance. Suspension is a property of a microphone; the
    receiving instance has none, and its copy of ``suspend_state_path``
    names a file on the wrong machine — one that has never been written,
    which is the fail-closed case. Folding the toggle into ``guard_capture``
    would therefore have made a peer refuse every capture the device
    successfully sent, with a reason true of neither.
    """
    cfg = build_config(tmp_path, suspend_state_path=str(tmp_path / "absent.json"))
    assert suspend.read_status(cfg.suspend_state_path).suspended is True

    with structlog.testing.capture_logs() as captured:
        guard_capture(cfg, provenance={}, source_id="inbound")   # must NOT raise
    decisions = [c for c in captured if c.get("event") == "jeeves.capture_decision"]
    assert len(decisions) == 1
    assert decisions[0]["accepted"] is True
    assert decisions[0]["reason"] == ACCEPTED_LIVE_MODE


# ---------------------------------------------------------------------------
# The service — suspended means the ring is not fed at all
# ---------------------------------------------------------------------------


class StubStt:
    def __init__(self, *texts: str):
        self.texts = list(texts) or ["Jeeves, note that"]
        self.calls: list[int] = []

    async def transcribe(self, audio: bytes, mime: str, vocab: list[str]):
        self.calls.append(len(audio))
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        return SttResult(
            text=text, backend_id="stub", tier="comparable",
            has_speech_signal=False, confidence_raw=-0.28,
        )


def build_service(tmp_path, *, texts=("Jeeves, note that",), cue_at=10.0,
                  config=None, release_detector=None, stt=None):
    return service.JeevesService(
        config or build_config(tmp_path),
        audio_format=FMT,
        detector=ScriptedWakeDetector([cue_at], audio_format=FMT),
        stt_backend=stt or StubStt(*texts),
        provenance={"synthetic": True},
        release_detector=release_detector,
    )


def speech_then_quiet(speech_s: float = 12.0, quiet_s: float = 4.0):
    blobs = [tone(1.0, FMT) for _ in range(int(speech_s))]
    blobs += [silence(1.0, FMT) for _ in range(int(quiet_s))]
    return MemoryAudioSource(blobs, audio_format=FMT)


async def test_a_suspended_service_holds_NO_audio_and_a_running_one_does(tmp_path):
    """THE PIN + ITS CONTROL. Refusing at the gate alone would leave thirty
    minutes of a visitor's conversation sitting in RAM, retrievable the
    moment the toggle came off — which is not what the operator was promised
    when he said the word."""
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_SPOKEN)
    svc = build_service(tmp_path, config=cfg)
    outcomes = await svc.run(speech_then_quiet())
    assert outcomes == []
    assert svc.ring.held_seconds == 0.0, "audio entered the ring while suspended"

    # THE CONTROL: released, the identical stream fills the ring and captures.
    suspend.set_suspended(
        cfg.suspend_state_path, False, source=suspend.SOURCE_SPOKEN)
    svc2 = build_service(tmp_path, config=cfg)
    outcomes2 = await svc2.run(speech_then_quiet())
    assert svc2.ring.held_seconds > 0.0
    assert [o.verb for o in outcomes2] == [cues.CUE_MARK_DOWN]


async def test_a_suspended_service_makes_no_stt_call(tmp_path):
    """Suspension that still uploaded would be "listening, just quietly"."""
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_MANUAL)
    stub = StubStt("Jeeves, note that")
    svc = build_service(tmp_path, config=cfg, stt=stub)
    await svc.run(speech_then_quiet())
    assert stub.calls == []


async def test_a_suspended_service_says_so_ONCE_not_once_per_chunk(tmp_path):
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_SPOKEN)
    svc = build_service(tmp_path, config=cfg)
    with structlog.testing.capture_logs() as captured:
        await svc.run(speech_then_quiet())
    events = [c for c in captured if c.get("event") == "jeeves.service.suspended"]
    assert len(events) == 1
    assert events[0]["reason"] == suspend.REASON_SPOKEN_SUSPEND
    assert events[0]["release_path"] == "manual_only"


async def test_the_spoken_company_cue_suspends_the_service(tmp_path):
    """THE SPOKEN DOOR, end to end: the operator says it, the flag moves, and
    it is on disk for the next process to read."""
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, False, source=suspend.SOURCE_MANUAL)
    svc = build_service(tmp_path, config=cfg, texts=("Jeeves, company",))
    outcomes = await svc.run(speech_then_quiet())

    assert [o.verb for o in outcomes] == [cues.CUE_SUSPEND]
    assert outcomes[0].disposition == service.DISPOSITION_SUSPENDED
    assert suspend.is_suspended(cfg.suspend_state_path) is True
    assert suspend.read_status(cfg.suspend_state_path).source == \
        suspend.SOURCE_SPOKEN
    # Nothing was captured: no mark, and the ring was emptied on the way down.
    assert marklog.read_entries(cfg.mark_log_path) == []
    assert svc.ring.held_seconds == 0.0


async def test_the_spoken_toggle_writes_a_telemetry_row(tmp_path):
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, False, source=suspend.SOURCE_MANUAL)
    svc = build_service(tmp_path, config=cfg, texts=("Jeeves, company",))
    await svc.run(speech_then_quiet())
    kinds = [r["event"] for r in telemetry.read_rows(cfg.telemetry_path)]
    assert telemetry.EVENT_SUSPENDED in kinds


async def test_a_capture_in_flight_when_the_button_lands_is_REFUSED(tmp_path):
    """THE SECOND ENFORCEMENT POINT, and it is not redundant. The manual door
    is another process: the operator can press the button while a lookahead
    is still collecting, so a capture that began legitimately can be in
    flight when suspension arrives."""
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, False, source=suspend.SOURCE_MANUAL)
    svc = build_service(tmp_path, config=cfg, cue_at=3.0)

    source = speech_then_quiet(speech_s=6.0, quiet_s=0.0)
    chunks = list(source.chunks())
    for chunk in chunks[:5]:
        await svc.feed(chunk)
    # ...the button, mid-lookahead, from "another process".
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_MANUAL)
    outcomes = await svc.flush()

    assert len(outcomes) == 1
    assert outcomes[0].disposition == service.DISPOSITION_REFUSED
    assert outcomes[0].reason == REFUSED_SUSPENDED
    assert marklog.read_entries(cfg.mark_log_path) == []


async def test_the_release_detector_is_the_only_thing_fed_while_suspended(tmp_path):
    """The spoken release cannot come through the capture path — that path is
    the ring plus a cloud STT call, and suspension is exactly the state in
    which neither may happen. So it takes a second LOCAL recogniser."""
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_SPOKEN)
    releaser = ScriptedWakeDetector([4.0], audio_format=FMT, model="release")
    svc = build_service(tmp_path, config=cfg, release_detector=releaser)

    await svc.run(speech_then_quiet())

    assert suspend.is_suspended(cfg.suspend_state_path) is False
    assert suspend.read_status(cfg.suspend_state_path).source == \
        suspend.SOURCE_SPOKEN
    # ...and audio started entering the ring again once it fired.
    assert svc.ring.held_seconds > 0.0


async def test_without_a_release_recogniser_the_service_says_manual_only(tmp_path):
    """An operator saying the release phrase at a device that structurally
    cannot hear him gets silence, and nothing anywhere would say why."""
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_SPOKEN)
    svc = build_service(tmp_path, config=cfg, release_detector=None)
    with structlog.testing.capture_logs() as captured:
        await svc.run(speech_then_quiet())
    events = [
        c for c in captured
        if c.get("event") == "jeeves.service.no_release_detector"
    ]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"


def test_the_service_read_hook_reports_the_reason_not_just_a_boolean(tmp_path):
    """THE SEAM Part C renders. A banner that says only "SUSPENDED" cannot
    tell the operator whether the device heard him or whether it fell back
    because its state file is corrupt."""
    cfg = build_config(tmp_path, suspend_state_path=write_state(
        tmp_path, '{"suspended": tr'))
    svc = build_service(tmp_path, config=cfg)
    status = svc.suspend_status()
    assert status.suspended is True
    assert status.reason == suspend.REASON_CORRUPT
    assert status.fail_closed is True
    assert "not valid JSON" in status.detail

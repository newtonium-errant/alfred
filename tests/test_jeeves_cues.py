"""Cue grammar + classification (task #81, stage 1).

The design's two wording constraints are enforced here rather than trusted:
the cues must not be a minimal pair, and the ROUTE verb must name its target.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.jeeves import cues
from alfred.jeeves.config import JeevesCueConfig

TARGETED = JeevesCueConfig(route_target="peerbox")
INERT = JeevesCueConfig()          # no route target — ROUTE grammar is dead


def classify(text: str, config: JeevesCueConfig = TARGETED, **kw):
    return cues.classify(text, cue_config=config, **kw)


# ---------------------------------------------------------------------------
# The minimal-pair rule, computed
# ---------------------------------------------------------------------------


def test_the_shipped_grammars_are_not_minimal_pairs():
    """DESIGN CONSTRAINT (§3): the two cues must differ by more than one
    short unstressed syllable, because the room is noisy and the recogniser
    runs at distance. 'note that' vs 'send that' is fine; 'mark that' vs
    'park that' is not."""
    routes = cues.route_phrases(TARGETED)
    conflicts = cues.minimal_pair_conflicts(cues.MARK_PHRASES, routes)
    assert conflicts == [], f"MARK and ROUTE are minimal pairs: {conflicts}"

    conflicts = cues.minimal_pair_conflicts(cues.MARK_PHRASES, cues.MISS_PHRASES)
    assert conflicts == [], f"MARK and MISS are minimal pairs: {conflicts}"


def test_the_minimal_pair_detector_catches_the_designs_own_counterexample():
    """MUTATION-STYLE CHECK on the checker itself. A rule that cannot fail is
    not a rule — this proves the detector fires on the exact pair the design
    names as forbidden, so the green result above means something."""
    conflicts = cues.minimal_pair_conflicts(("mark that",), ("park that",))
    assert conflicts == [("mark that", "park that")]

    # ...and does NOT fire on the pair the design names as acceptable.
    assert cues.minimal_pair_conflicts(("note that",), ("send that",)) == []


def test_every_route_phrase_names_the_target_and_no_mark_phrase_does():
    """The ASYMMETRY, pinned. ROUTE is the marked verb precisely because it
    names the destination out loud; a ROUTE phrase that didn't would be as
    easy to trip as a mark."""
    routes = cues.route_phrases(TARGETED)
    assert routes, "a configured target must produce a route grammar"
    for phrase in routes:
        assert "peerbox" in phrase
    for phrase in cues.MARK_PHRASES:
        assert "peerbox" not in phrase


def test_grammars_are_disjoint():
    assert not set(cues.MARK_PHRASES) & set(cues.MISS_PHRASES)
    assert not set(cues.MARK_PHRASES) & set(cues.route_phrases(TARGETED))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Jeeves, note that.",
    "jeeves note this",
    "Jeeves — mark that, would you",
    "Jeeves, make a note: the bearing is the wrong size",
    "Jeeves, write that down",
])
def test_mark_down_trips_easily(text: str):
    """MARK-DOWN is the cheap, high-frequency verb and SHOULD trip easily —
    a false positive costs a junk line in a log nobody has to read."""
    assert classify(text).verb == cues.CUE_MARK_DOWN


@pytest.mark.parametrize("text", [
    "Jeeves, tell peerbox the compressor is leaking",
    "Jeeves, send that to peerbox",
    "Jeeves send this to peerbox please",
    "jeeves pass that to peerbox",
])
def test_route_requires_naming_the_target(text: str):
    result = classify(text)
    assert result.verb == cues.CUE_ROUTE
    assert result.route_target == "peerbox"


def test_route_wins_over_mark_when_both_are_spoken():
    """The more consequential verb, which the operator went out of his way
    to say, wins."""
    result = classify("Jeeves note that and tell peerbox about it")
    assert result.verb == cues.CUE_ROUTE


def test_miss_report_wins_over_everything():
    """The scarce signal. Cue false-negatives are invisible by construction,
    so a miss report must never be swallowed by a mark."""
    result = classify("Jeeves, you missed that — note it next time")
    assert result.verb == cues.CUE_MISS_REPORT


def test_route_is_inert_without_a_configured_target():
    """FAIL-CLOSED. An unconfigured destination must not be guessed: the
    utterance stays local and the reason is reported, so the operator learns
    his CONFIG is the problem and not his phrasing."""
    result = classify("Jeeves, tell peerbox the compressor is leaking", INERT)
    assert result.verb == cues.CUE_NONE
    assert result.reason == cues.NONE_REASON_ROUTE_INERT
    assert result.route_target == ""


def test_route_inert_is_logged_with_an_actionable_reason():
    with structlog.testing.capture_logs() as captured:
        classify("Jeeves, send that to whoever", INERT)
    events = [c for c in captured if c.get("event") == "jeeves.cue.route_inert"]
    assert len(events) == 1
    assert "route_target" in events[0]["detail"]


def test_unrecognised_speech_is_no_verb_not_a_route_problem():
    result = classify("Jeeves, what time is it")
    assert result.verb == cues.CUE_NONE
    assert result.reason == cues.NONE_REASON_NO_VERB


def test_empty_transcript_has_its_own_reason():
    """Intentionally-left-blank: a cue that transcribed to nothing is a real
    event with a real cause, not an absence of processing."""
    result = classify("   ")
    assert result.verb == cues.CUE_NONE
    assert result.reason == cues.NONE_REASON_EMPTY


def test_every_classification_emits_exactly_one_event():
    with structlog.testing.capture_logs() as captured:
        classify("Jeeves, note that")
    events = [c for c in captured if c.get("event") == "jeeves.cue.classified"]
    assert len(events) == 1
    assert events[0]["verb"] == cues.CUE_MARK_DOWN
    assert events[0]["matched_phrase"] == "note that"


def test_the_classification_event_never_carries_the_transcript():
    """The matched phrase is from a closed code-side grammar; the words the
    operator actually said must not reach a log line."""
    secret = "the account number is 4417 double nine"
    with structlog.testing.capture_logs() as captured:
        classify(f"Jeeves note that {secret}")
    for entry in captured:
        rendered = " ".join(str(v) for v in entry.values())
        assert "4417" not in rendered
        assert "account number" not in rendered


# ---------------------------------------------------------------------------
# The lookback modifier — without it the telemetry only measures the default
# ---------------------------------------------------------------------------


def test_the_designs_worked_modifier_maps_to_the_configured_extended_reach():
    result = classify(
        "Jeeves, note the last couple of minutes",
        extended_lookback_seconds=180.0,
    )
    assert result.verb == cues.CUE_MARK_DOWN
    assert result.modifier == cues.MODIFIER_EXTENDED
    assert result.lookback_seconds == pytest.approx(180.0)


@pytest.mark.parametrize("text,expected", [
    ("Jeeves note the last two minutes", 120.0),
    ("Jeeves note the last 5 minutes", 300.0),
    ("Jeeves note the last minute", 60.0),
    ("Jeeves note the last ten minutes", 600.0),
])
def test_explicit_minute_counts_are_parsed(text: str, expected: float):
    result = classify(text)
    assert result.modifier == cues.MODIFIER_EXPLICIT
    assert result.lookback_seconds == pytest.approx(expected)


def test_no_modifier_means_use_the_default():
    """``None`` is distinct from a number: it means "the configured default",
    which the design labels a guess and the telemetry exists to replace."""
    result = classify("Jeeves, note that")
    assert result.modifier == cues.MODIFIER_NONE
    assert result.lookback_seconds is None


def test_a_modifier_rides_a_route_cue_too():
    result = classify("Jeeves, send the last couple of minutes to peerbox")
    assert result.verb == cues.CUE_ROUTE
    assert result.modifier == cues.MODIFIER_EXTENDED
    assert result.route_target == "peerbox"


# ---------------------------------------------------------------------------
# The modifier form — a bare verb stem, licensed by a spoken span
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Jeeves, note the last couple of minutes",
    "Jeeves, mark the last two minutes",
    "Jeeves, keep the last minute",
    "Jeeves, save the last 5 minutes",
])
def test_a_spoken_span_licenses_a_bare_mark_stem(text: str):
    """The design's own worked example fits NEITHER phrase table — the verb
    is followed by a time span, not by "that". Requiring "note THAT the last
    couple of minutes" would be asking the operator to speak badly to
    satisfy a lookup table."""
    assert classify(text).verb == cues.CUE_MARK_DOWN


def test_a_bare_stem_alone_does_NOT_capture():
    """The stems fire only WITH a modifier. Without one the phrase tables
    govern, because a bare "note" in ordinary conversation is just a word."""
    assert classify("Jeeves, I took a note of it").verb == cues.CUE_NONE
    assert classify("Jeeves, save the receipt").verb == cues.CUE_NONE


def test_stems_match_whole_tokens_only():
    """Substring matching would make 'note' fire on 'notebook'."""
    result = classify("Jeeves, my notebook covers the last two minutes")
    assert result.verb == cues.CUE_NONE


def test_a_route_stem_without_a_named_target_is_not_a_route():
    """"send the last couple of minutes" with no destination is an
    unfinished sentence; guessing where to send it is the one thing this
    device must never do. It degrades to MARK — local, reversible."""
    result = classify("Jeeves, send the last couple of minutes")
    assert result.verb != cues.CUE_ROUTE


def test_route_verb_stems_stay_in_step_with_the_phrase_templates():
    """DRIFT PIN. The stems are the modifier-form shorthand for the same
    verbs the templates spell out; a template gaining a verb the stems don't
    know would make the two forms disagree about what a route is."""
    template_stems = {
        prefix.split()[0]
        for prefix in (
            t.split(" {target}")[0].strip() for t in cues.ROUTE_PHRASE_TEMPLATES
        )
        if prefix and "{" not in prefix
    }
    assert template_stems <= set(cues.ROUTE_VERB_STEMS), (
        f"route templates use verbs the stems don't know: "
        f"{template_stems - set(cues.ROUTE_VERB_STEMS)}"
    )


def test_the_verb_stems_are_not_minimal_pairs_either():
    """The minimal-pair rule applies to the modifier form as much as to the
    phrase tables — it is the same room and the same recogniser."""
    conflicts = cues.minimal_pair_conflicts(
        cues.MARK_VERB_STEMS, cues.ROUTE_VERB_STEMS,
    )
    assert conflicts == [], f"mark/route stems are minimal pairs: {conflicts}"


# ---------------------------------------------------------------------------
# The mis-heard wake word (Q6 trial)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["cheese", "leaves", "jesus", "jeeps", "eaves"])
def test_a_mis_transcribed_wake_word_does_not_block_classification(variant: str):
    """Q6 TRIAL FINDING. The tablet's own on-device STT never produced
    'Jeeves' once — it wrote cheese / leaves / jeeps / Jesus every time.
    Jeeves never uses that STT, but the acoustic detector has ALREADY fired
    by the time this runs, so the transcript's rendering of the wake word
    must not gate the capture."""
    result = classify(
        f"{variant}, note that",
        confusable_terms=("cheese", "leaves", "jesus", "jeeps", "eaves"),
    )
    assert result.verb == cues.CUE_MARK_DOWN
    assert result.wake_variant == variant


def test_a_confusable_mid_sentence_is_just_a_word():
    """Someone talking about cheese is not a mis-heard wake word — only the
    opening tokens are considered."""
    result = classify(
        "Jeeves, note that we are out of cheese",
        confusable_terms=("cheese",),
    )
    assert result.wake_variant == "jeeves"


def test_the_correct_wake_word_is_recorded_as_itself():
    result = classify("Jeeves, note that", confusable_terms=("cheese",))
    assert result.wake_variant == "jeeves"


def test_no_leading_wake_token_records_nothing():
    result = classify("please note that", confusable_terms=("cheese",))
    assert result.wake_variant == ""


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_punctuation_never_decides_whether_a_capture_happens():
    """Whisper's punctuation is a guess about prosody; a comma it placed
    between 'note' and 'that' must not cost the operator a capture."""
    assert classify("Jeeves... note, that!").verb == cues.CUE_MARK_DOWN


def test_normalisation_handles_a_smart_apostrophe():
    assert cues.normalize("you didn’t get that") == "you didn't get that"
    assert classify("Jeeves, you didn’t get that").verb == cues.CUE_MISS_REPORT


def test_route_aliases_widen_the_names_not_the_grammar():
    config = JeevesCueConfig(route_target="peerbox", route_aliases=["the box"])
    assert classify("Jeeves, tell the box about it", config).verb == cues.CUE_ROUTE
    assert classify("Jeeves, tell peerbox about it", config).verb == cues.CUE_ROUTE
    # An unrelated name still isn't a route.
    assert classify("Jeeves, tell somebody about it", config).verb != cues.CUE_ROUTE


def test_the_longest_matching_phrase_is_the_one_reported():
    """Reporting the shorter of two overlapping matches would understate
    what was actually heard in every telemetry row."""
    result = classify("Jeeves, you missed that")
    assert result.matched_phrase == "you missed that"


def test_an_alias_routes_to_the_ONE_target_not_a_second_one():
    """ONE destination by design — the pin behind ``JeevesCueConfig``'s
    single-target paragraph (#98 Part B, operator ruling (a)).

    ``route_aliases`` exists for a target STT spells inconsistently, NOT for a
    choice of destinations. Read the other way — "the list of places Jeeves can
    send to" — it would be wrong in the one direction that matters: audio
    leaving the garage to a destination the operator never named.

    So this asserts the alias resolves to the CONFIGURED target, not to itself.
    Without it the docstring is prose that a future target-table refactor could
    silently falsify, which is this codebase's named comment-lies trap.
    """
    config = JeevesCueConfig(route_target="Salem", route_aliases=["Hypatia"])

    spoken_alias = cues.classify(
        "jeeves tell hypatia the compressor is leaking", cue_config=config)

    assert spoken_alias.verb == cues.CUE_ROUTE
    # The matched PHRASE is normalized (lower-cased) because matching happens
    # against normalized transcript text; the carried TARGET keeps the casing
    # the operator configured. Two different values on purpose — a sink that
    # assumed the target was lower-cased would build the wrong peer name.
    assert spoken_alias.matched_phrase == "tell hypatia"   # heard the alias
    assert spoken_alias.route_target == "Salem"            # routes to the TARGET
    assert spoken_alias.route_target.lower() != "hypatia"

    # Positive control: the target's own name routes identically, so the
    # assertion above is about resolution and not about the alias being
    # rejected outright.
    spoken_target = cues.classify(
        "jeeves tell salem the compressor is leaking", cue_config=config)
    assert spoken_target.verb == cues.CUE_ROUTE
    assert spoken_target.route_target == spoken_alias.route_target


def test_an_unconfigured_target_leaves_route_inert():
    """The other half of one-destination: with no target there is no ROUTE
    grammar at all, rather than a guess about where audio should go."""
    config = JeevesCueConfig(route_target="", route_aliases=["Hypatia"])

    assert cues.route_phrases(config) == ()
    result = cues.classify(
        "jeeves tell hypatia the compressor is leaking", cue_config=config)
    assert result.verb != cues.CUE_ROUTE

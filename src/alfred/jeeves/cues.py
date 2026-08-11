"""Cue grammar — classifying the verb from the transcript (task #81, stage 1).

The wake word carries the acoustic burden; the VERB is classified from text,
here. Two verbs, DELIBERATELY ASYMMETRIC (design §3):

===============  ==========================  ==============================
                 MARK-DOWN                   ROUTE
===============  ==========================  ==============================
Cue              "Jeeves, note that"         "Jeeves, tell <target> …"
Effect           appended to a local log     POSTed as a vault record
Leaves the room  no (beyond the STT trip)    YES
Reversible       trivially — it's a file     needs a vault edit
Default          yes                         no
===============  ==========================  ==============================

The asymmetry IS the design. MARK-DOWN is cheap, high-frequency and
low-consequence, so it should trip easily — a false positive costs a junk
line in a log nobody has to read. ROUTE has effects outside the garage and
lands in a system the operator reviews each morning, so its cue is longer,
more marked, and NAMES THE TARGET EXPLICITLY. A false-positive route costs a
junk record and a moment of "what is this?" at review.

Two constraints the design puts on the wording, both enforced here rather
than trusted:

* **The cues must not be a minimal pair.** "note that" vs "send that" is
  fine; "mark that" vs "park that" is not. :func:`minimal_pair_conflicts`
  computes it (one differing token, and those tokens within one edit of each
  other) and ``tests/test_jeeves_cues.py`` pins the shipped grammar against
  it. Prose in a design doc cannot fail; this can.
* **The target is named, and it is CONFIG.** There is no instance name in
  this module. An unconfigured target leaves the ROUTE grammar INERT — the
  utterance stays local and the refusal is logged with its reason — rather
  than guessing a destination for audio that leaves the property.

A FOURTH grammar is not a capture verb at all: the COMPANY TOGGLE (#98,
ruling 3). "Jeeves, company" suspends wake-word and capture until a release
phrase; the release is deliberately narrower than the suspend, because the
two mistakes are not symmetric — a false suspend makes the device visibly
deaf, a false release makes it listen while the operator believes it is not.
The toggle is also the one grammar matched against a SCOPE of the transcript
rather than all of it (:func:`_toggle_scope`): the window opens 45 seconds
before the cue, and a conversation about "the company" in that lookback must
not reach into the microphone.

A third capture cue exists for a reason the design argues at length: cue
FALSE NEGATIVES are invisible by construction ("I said Jeeves and nothing
happened" leaves no trace anywhere), so a system that learns only from its
false positives drifts toward being deaf. The miss-report cue — "Jeeves, you
missed that" — is itself a wake event, and it is the single highest-value
training signal this design can produce.

TOLERATING THE MIS-HEARD WAKE WORD. The acoustic detector has already fired
by the time this runs, so the transcript's rendering of the wake word is not
load-bearing and must not gate classification. The Q6 trial is why: the
tablet's own on-device STT never produced "Jeeves" once, writing cheese /
leaves / jeeps / Jesus every time. Jeeves never uses that STT — but the
lesson stands, so a leading confusable is RECORDED (as a closed-set enum,
for tuning) and otherwise ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from .config import JeevesCueConfig

log = structlog.get_logger(__name__)

# The classification outcomes. Stable strings — telemetry buckets on them and
# the morning-review surfacing will too.
CUE_MARK_DOWN = "mark_down"
CUE_ROUTE = "route"
CUE_MISS_REPORT = "miss_report"
# The company toggle (#98, ruling 3) — the SPOKEN door onto the suspended
# state. Not a capture verb: nothing is transcribed, logged or sent; the
# utterance flips a flag that :mod:`.suspend` owns.
CUE_SUSPEND = "suspend"
CUE_RESUME = "resume"
CUE_NONE = "none"

# Why a transcript classified as CUE_NONE. Distinct reasons because they
# have distinct remedies: an operator whose route target is unset needs to
# edit config; one whose phrasing missed needs to know which words work.
NONE_REASON_EMPTY = "empty_transcript"
NONE_REASON_NO_VERB = "no_cue_verb"
NONE_REASON_ROUTE_INERT = "route_target_unset"

# MARK-DOWN — the cheap verb. Short, several ways to say it, trips easily.
MARK_PHRASES: tuple[str, ...] = (
    "note that",
    "note this",
    "note it",
    "mark that",
    "mark this",
    "mark it down",
    "make a note",
    "write that down",
    "write this down",
)

# ROUTE — the marked verb. Every template NAMES the target; that is the
# asymmetry, not decoration. ``{target}`` is substituted from config.
ROUTE_PHRASE_TEMPLATES: tuple[str, ...] = (
    "tell {target}",
    "send that to {target}",
    "send this to {target}",
    "pass that to {target}",
    "give that to {target}",
    "{target} needs to know",
)

# The MODIFIER FORM. "Jeeves, note the last couple of minutes" is the
# design's own worked example and it fits NEITHER phrase table above — the
# verb is followed by a time span, not by "that". Rather than multiplying the
# tables by every modifier phrasing, a spoken modifier licenses a BARE VERB
# STEM: if the operator named a span, he has already made his intent
# unambiguous, and requiring "note THAT the last couple of minutes" would be
# asking him to speak badly to satisfy a lookup table.
#
# These stems fire ONLY when a modifier was parsed. Without one, the phrase
# tables govern — a bare "note" in ordinary conversation must not capture.
MARK_VERB_STEMS: tuple[str, ...] = ("note", "mark", "keep", "capture", "save")
ROUTE_VERB_STEMS: tuple[str, ...] = ("tell", "send", "pass", "give")

# MISS REPORT — the false-negative capture. Deliberately generous: the cost
# of a false miss-report is one telemetry row and one local capture, and the
# value of a true one is the only labelled negative example this system can
# ever get.
MISS_PHRASES: tuple[str, ...] = (
    "you missed that",
    "you missed it",
    "missed that",
    "you didn't get that",
    "you didn't catch that",
    "you did not get that",
)

# THE COMPANY TOGGLE (#98, ruling 3). Two grammars with DELIBERATELY OPPOSITE
# strictness, because their false-positive costs point opposite ways:
#
# * A false SUSPEND makes the device deaf. Annoying, visible (the banner says
#   so), fixed by one release. So suspend is allowed to be generous.
# * A false RESUME makes the device listen while the operator believes it is
#   not. That is the failure the toggle exists to prevent, and it is silent
#   from where he is standing. So release is the narrow, marked phrase.
#
# "company" is the operator's ruled word. The variants are the same sentence
# said naturally; "stop listening" is the plain-language form somebody
# reaches for when the ruled phrase does not come to mind, and it fails in
# the safe direction.
SUSPEND_PHRASES: tuple[str, ...] = (
    "company",
    "we have company",
    "we've got company",
    "stop listening",
)

# PROPOSED, not ruled — the operator ruled that a release phrase exists and
# left the words open (#98 B.3). "all clear" is the idiomatic counterpart to
# "company" (it means precisely "the visitor has gone"), and "as you were" is
# the butler register this whole device is named for. Both are two- and
# three-token phrases with no neighbour anywhere in this grammar, which is
# what ``test_the_toggle_grammars_are_not_minimal_pairs`` checks rather than
# asserts.
RESUME_PHRASES: tuple[str, ...] = (
    "all clear",
    "as you were",
)

# How far back from the END of a window transcript the toggle phrases may be
# matched when the wake word was not rendered at all. See
# :func:`_toggle_scope` — a cued window opens up to 45 seconds BEFORE the
# operator spoke, so an unanchored match would let a conversation about
# "the company" half a minute earlier suspend the device.
TOGGLE_TAIL_TOKENS = 8

# Lookback modifiers (design §3 + §4.3). Without a way to EXPRESS a long
# lookback the telemetry only ever measures the default, and ruling 5's
# question — is 30 minutes the right ring? — stays unanswerable.
MODIFIER_NONE = ""
MODIFIER_EXTENDED = "extended"
MODIFIER_EXPLICIT = "explicit"

_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a": 1, "couple": 2, "few": 3,
}

# "the last couple of minutes" — the design's worked example, and the one
# form that maps to the configured extended lookback rather than to a count.
_EXTENDED_RE = re.compile(r"\blast (?:couple|few) (?:of )?minutes\b")
# "the last two minutes" / "the last 5 minutes" / "the last minute".
_EXPLICIT_RE = re.compile(
    r"\blast (?:(\d+|" + "|".join(_NUMBER_WORDS) + r") )?minutes?\b")

_PUNCT_RE = re.compile(r"[^\w\s']+")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class CueClassification:
    """What a cued window's transcript turned out to be.

    ``lookback_seconds`` is ``None`` when no modifier was spoken, meaning
    "use the configured default" — distinct from a modifier that resolved to
    a number, which the window extractor honours (and clamps).
    """

    verb: str
    matched_phrase: str = ""
    modifier: str = MODIFIER_NONE
    lookback_seconds: float | None = None
    #: The route destination this cue named (empty unless ``verb`` is ROUTE).
    route_target: str = ""
    #: Which member of the CLOSED confusable set the transcript opened with,
    #: or "" for none. A fixed code-side vocabulary, NOT user content — see
    #: the telemetry module's allowlist for why that distinction is load-bearing.
    wake_variant: str = ""
    #: Populated only when ``verb`` is CUE_NONE.
    reason: str = ""

    @property
    def is_cue(self) -> bool:
        return self.verb != CUE_NONE


def normalize(transcript: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Punctuation goes because Whisper's is a guess about prosody, and a comma
    it happened to place between "note" and "that" must not decide whether a
    capture happens.
    """
    if not transcript:
        return ""
    lowered = transcript.lower().replace("’", "'")
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", lowered)).strip()


def route_phrases(config: JeevesCueConfig) -> tuple[str, ...]:
    """The ROUTE grammar for a configured target (plus any aliases).

    Empty when no target is configured — which is what makes the ROUTE verb
    inert rather than misdirected.
    """
    target = (config.route_target or "").strip().lower()
    if not target:
        return ()
    names = [target]
    for alias in config.route_aliases:
        a = alias.strip().lower()
        if a and a not in names:
            names.append(a)
    return tuple(
        template.format(target=name)
        for name in names
        for template in ROUTE_PHRASE_TEMPLATES
    )


def _find_phrase(text: str, phrases: tuple[str, ...]) -> str:
    """The LONGEST matching phrase, or "".

    Longest-first matters: "you missed that" and "missed that" both match the
    same utterance, and reporting the shorter one would understate what was
    actually heard in every telemetry row.
    """
    best = ""
    for phrase in phrases:
        if phrase and phrase in text and len(phrase) > len(best):
            best = phrase
    return best


def _has_token(text: str, token: str) -> bool:
    """Whole-token containment. Substring matching would make "note" fire on
    "notebook" and "keep" on "keeper"."""
    return token in text.split()


def _modifier_mark(text: str, modifier: str) -> str:
    """A bare MARK stem, licensed by a spoken modifier. "" when neither."""
    if modifier == MODIFIER_NONE:
        return ""
    for stem in MARK_VERB_STEMS:
        if _has_token(text, stem):
            return stem
    return ""


def _modifier_route(text: str, config: JeevesCueConfig, modifier: str) -> str:
    """A bare ROUTE stem PLUS the named target, licensed by a modifier.

    The target requirement does not relax: "send the last couple of minutes"
    with no destination is not a route, it is an unfinished sentence, and
    guessing where to send it is the one thing this device must never do.
    """
    if modifier == MODIFIER_NONE:
        return ""
    names = [n for n in (
        [(config.route_target or "").strip().lower()]
        + [a.strip().lower() for a in config.route_aliases]
    ) if n]
    if not names:
        return ""
    target = next((n for n in names if n in text), "")
    if not target:
        return ""
    for stem in ROUTE_VERB_STEMS:
        if _has_token(text, stem):
            # Greppable, and it records BOTH halves of what made this a
            # route: the verb stem and the target that was named.
            return f"{stem}:{target}"
    return ""


def _detect_wake_variant(
    text: str, wake_word: str, confusables: tuple[str, ...],
) -> str:
    """Which wake-word rendering the transcript opened with.

    Only the first few tokens are considered: the wake word is what STARTED
    the utterance, and a "cheese" appearing mid-sentence is a person talking
    about cheese.
    """
    head = text.split()[:3]
    for token in head:
        if token == wake_word:
            return token
        if token in confusables:
            return token
    return ""


def _find_phrase_bounded(text: str, phrases: tuple[str, ...]) -> str:
    """:func:`_find_phrase`, but on TOKEN boundaries — longest match wins.

    The plain substring form is right for the capture verbs (a stray match
    costs a junk line in a log). It is wrong for the toggle: ``"company"``
    is a substring of ``"accompany"``, and "I'll accompany you" must not
    make the device deaf. Padding both sides turns containment into
    whole-token containment without a regex per phrase.
    """
    padded = f" {text} "
    best = ""
    for phrase in phrases:
        if phrase and f" {phrase} " in padded and len(phrase) > len(best):
            best = phrase
    return best


def _toggle_scope(
    text: str, wake_word: str, confusables: tuple[str, ...],
) -> str:
    """The part of a window transcript a TOGGLE phrase may be matched in.

    A cued window opens up to ``lookback_seconds`` (45 s by default) BEFORE
    the operator spoke, so most of the transcript is ordinary conversation
    that happened to be in the ring. Matching the toggle anywhere in it
    would mean "we have company coming over on Saturday", said half a minute
    before an unrelated "Jeeves, note that", suspends the microphone.

    So the toggle is anchored to the CUE, two ways:

    * From the LAST rendering of the wake word onwards — "…jeeves company".
      This is the normal case and it tolerates the operator carrying on
      talking afterwards, because the scope runs to the end.
    * Failing that (the Q6 finding: an STT may render the wake word as
      something else entirely, or drop it), the final
      :data:`TOGGLE_TAIL_TOKENS` tokens, which is where a phrase spoken at
      the cue position ends up.

    The capture verbs are deliberately NOT scoped this way. Their grammars
    are shipped and their false positives are cheap; the toggle's are not
    symmetric — see the phrase tables.
    """
    tokens = text.split()
    if not tokens:
        return ""
    wake_tokens = {wake_word, *confusables}
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index] in wake_tokens:
            return " ".join(tokens[index:])
    return " ".join(tokens[-TOGGLE_TAIL_TOKENS:])


def _parse_modifier(
    text: str, extended_seconds: float,
) -> tuple[str, float | None]:
    """Extract a lookback modifier, if one was spoken.

    Extended ("a couple of minutes") is checked first because it also
    matches the explicit pattern, and resolving it to the CONFIGURED
    extended lookback rather than a literal two minutes is what lets the
    operator tune the phrase's meaning without changing how he speaks.
    """
    if _EXTENDED_RE.search(text):
        return MODIFIER_EXTENDED, extended_seconds
    match = _EXPLICIT_RE.search(text)
    if match:
        raw = match.group(1)
        if raw is None:
            return MODIFIER_EXPLICIT, 60.0
        if raw.isdigit():
            return MODIFIER_EXPLICIT, float(int(raw)) * 60.0
        count = _NUMBER_WORDS.get(raw)
        if count:
            return MODIFIER_EXPLICIT, float(count) * 60.0
    return MODIFIER_NONE, None


def classify(
    transcript: str,
    *,
    cue_config: JeevesCueConfig,
    wake_word: str = "jeeves",
    confusable_terms: tuple[str, ...] = (),
    extended_lookback_seconds: float = 120.0,
) -> CueClassification:
    """Classify a cued window's transcript into a verb.

    Order is SUSPEND → RESUME → MISS → ROUTE → MARK, and it is not
    arbitrary. The TOGGLE runs first because "Jeeves, company" must suspend
    even when the same window carries a capture verb: a window that contains
    both is a window in which the operator asked for privacy, and honouring
    the capture instead is the one outcome he cannot undo. A miss report is
    next (it must never be swallowed by a mark — it is the scarce signal),
    and ROUTE precedes MARK because a transcript can legitimately contain
    both — "note that and tell <target>" — and the more consequential verb,
    which the operator went out of his way to say, wins.
    """
    text = normalize(transcript)
    if not text:
        # Intentionally-left-blank: an empty transcript on a CUED window is a
        # real, distinct event (the room was quiet, or STT returned nothing),
        # not an absence of processing.
        log.info(
            "jeeves.cue.classified",
            verb=CUE_NONE,
            reason=NONE_REASON_EMPTY,
            detail="a cue fired but the window transcribed to nothing",
        )
        return CueClassification(verb=CUE_NONE, reason=NONE_REASON_EMPTY)

    wake_variant = _detect_wake_variant(
        text, wake_word.lower(), tuple(t.lower() for t in confusable_terms),
    )
    modifier, lookback = _parse_modifier(text, extended_lookback_seconds)

    # THE TOGGLE, first and anchored to the cue. Neither branch carries a
    # lookback modifier: suspending is not a capture, so "how far back"
    # means nothing for it, and recording one would put a number in the
    # telemetry row that no window was ever cut with.
    scope = _toggle_scope(
        text, wake_word.lower(), tuple(t.lower() for t in confusable_terms),
    )
    suspend = _find_phrase_bounded(scope, SUSPEND_PHRASES)
    if suspend:
        return _classified(CueClassification(
            verb=CUE_SUSPEND, matched_phrase=suspend, wake_variant=wake_variant,
        ))
    resume = _find_phrase_bounded(scope, RESUME_PHRASES)
    if resume:
        return _classified(CueClassification(
            verb=CUE_RESUME, matched_phrase=resume, wake_variant=wake_variant,
        ))

    miss = _find_phrase(text, MISS_PHRASES)
    if miss:
        return _classified(CueClassification(
            verb=CUE_MISS_REPORT, matched_phrase=miss, modifier=modifier,
            lookback_seconds=lookback, wake_variant=wake_variant,
        ))

    routes = route_phrases(cue_config)
    route = _find_phrase(text, routes) or _modifier_route(
        text, cue_config, modifier,
    )
    if route:
        return _classified(CueClassification(
            verb=CUE_ROUTE, matched_phrase=route, modifier=modifier,
            lookback_seconds=lookback, wake_variant=wake_variant,
            route_target=cue_config.route_target,
        ))

    mark = _find_phrase(text, MARK_PHRASES) or _modifier_mark(text, modifier)
    if mark:
        return _classified(CueClassification(
            verb=CUE_MARK_DOWN, matched_phrase=mark, modifier=modifier,
            lookback_seconds=lookback, wake_variant=wake_variant,
        ))

    # No verb matched. If the ROUTE grammar is inert for want of a configured
    # target, say SO — an operator who said "tell <someone>" and got nothing
    # needs to know his config is the reason, not his phrasing.
    reason = NONE_REASON_ROUTE_INERT if not routes and _looks_like_route(text) \
        else NONE_REASON_NO_VERB
    if reason == NONE_REASON_ROUTE_INERT:
        log.warning(
            "jeeves.cue.route_inert",
            detail="the utterance looks like a ROUTE cue but "
                   "jeeves.cues.route_target is unset, so the ROUTE grammar "
                   "is inert and nothing left the device. Set route_target to "
                   "the peer this instance should send captures to.",
        )
    return _classified(CueClassification(
        verb=CUE_NONE, modifier=modifier, lookback_seconds=lookback,
        wake_variant=wake_variant, reason=reason,
    ))


def _looks_like_route(text: str) -> bool:
    """Does this transcript carry a ROUTE verb with SOME name after it?

    Used only to pick the right CUE_NONE reason. Matching the bare verb (not
    the target) is the whole point: it is how "you meant to route and your
    config is unset" gets distinguished from "you said something else".
    """
    stems = {t.split(" {target}")[0].strip() for t in ROUTE_PHRASE_TEMPLATES}
    return any(stem and f"{stem} " in f"{text} " for stem in stems if "{" not in stem)


def _classified(result: CueClassification) -> CueClassification:
    """Emit the one classification event and return the result.

    Every classification logs — including the ones that found nothing — so a
    quiet Jeeves is readable as "cues fired, none classified" rather than as
    an absence. The transcript itself is NEVER logged: the matched phrase is
    from a closed code-side grammar, the variant from a closed set.
    """
    log.info(
        "jeeves.cue.classified",
        verb=result.verb,
        matched_phrase=result.matched_phrase,
        modifier=result.modifier,
        lookback_seconds=result.lookback_seconds,
        wake_variant=result.wake_variant,
        reason=result.reason,
    )
    return result


# ---------------------------------------------------------------------------
# The minimal-pair rule, made computable
# ---------------------------------------------------------------------------


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance. Small strings only — this runs in a test, not
    on the capture path."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        row = [i]
        for j, cb in enumerate(b, start=1):
            row.append(min(
                prev[j] + 1,          # deletion
                row[j - 1] + 1,       # insertion
                prev[j - 1] + (ca != cb),  # substitution
            ))
        prev = row
    return prev[-1]


def minimal_pair_conflicts(
    left: tuple[str, ...],
    right: tuple[str, ...],
    *,
    max_token_distance: int = 1,
) -> list[tuple[str, str]]:
    """Phrase pairs across two grammars that differ by ONE close token.

    The design's rule is "the two cues must not be a minimal pair, because
    the room is noisy and the recogniser runs at distance" — with "mark that"
    vs "park that" as the counter-example. That is exactly this: same token
    count, one differing position, and the differing tokens within
    ``max_token_distance`` edits of each other.

    Returns the offending pairs (empty is the healthy state) so a failure
    names the two phrases rather than just asserting False.
    """
    conflicts: list[tuple[str, str]] = []
    for a in left:
        atoks = a.split()
        for b in right:
            btoks = b.split()
            if len(atoks) != len(btoks):
                continue
            diffs = [
                (x, y) for x, y in zip(atoks, btoks) if x != y
            ]
            if len(diffs) != 1:
                continue
            x, y = diffs[0]
            if _edit_distance(x, y) <= max_token_distance:
                conflicts.append((a, b))
    return conflicts

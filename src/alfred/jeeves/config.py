"""Typed view of the ``jeeves:`` config block (task #81, stage 1).

Hand-rolled construction — deliberately NOT the shared ``_build`` /
``_DATACLASS_MAP`` dispatch, for the two reasons CLAUDE.md documents: the
key-name collision footgun (``stt`` would dispatch to the telegram map's OWN
STTConfig) and the empty-dict-into-required-field trap. Every field carries a
default and every sub-block is filtered against ``__dataclass_fields__``, so
an unknown key is dropped and an empty ``{}`` never crashes the constructor.

INSTANCE-AGNOSTIC BY CONSTRUCTION (ruling 7 — promotion-ready). There is no
instance name anywhere in this module, and every path default DERIVES from
the instance's own data dir via :func:`alfred.common.instance_paths`. A
cwd-relative ``./data/...`` literal would be ONE shared file across every
co-located instance (the #53/#74 shape), which for Jeeves would mean one
instance's garage marks landing in another's log.

FAIL-CLOSED VALUES, three of them, each failing in the safe direction:

* ``mode`` — only the exact string ``"live"`` opens the live path; absent /
  None / unknown / malformed all resolve to ``"synthetic"``. Flipping is one
  deliberate config edit. See :mod:`.gate`.
* ``cues.route_target`` — empty (the default) leaves the ROUTE verb INERT.
  Route is the verb with effects OUTSIDE the garage, so an unconfigured
  target must not guess a destination; the utterance stays local.
* ``miss_audio_dir`` — empty (the default) means miss-report audio is NEVER
  written to disk. The DECISION has since been made (#98, ruling 1: an
  explicit miss report is in-the-moment consent, so its window is retained
  sensitive-local, surfaced for the recogniser example, then deleted), but
  the default stays empty: setting this directory is the same deliberate,
  once, at-deploy edit as ``mode: live``, and an instance that has not made
  it keeps fence 2 whole. See :mod:`.miss_store`.
* ``suspend_state_path`` — the COMPANY TOGGLE's store (#98, ruling 3), and
  the one path here that is ALWAYS derived rather than left empty. The other
  two knobs mean "off" when unset; a suspension that could not be recorded
  would mean "on", which is the wrong direction for a microphone. See
  :mod:`.suspend` for why the load itself is fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from alfred._env import substitute_env_in_value
from alfred.common.instance_paths import instance_data_path

log = structlog.get_logger(__name__)

# The two legal modes. ``synthetic`` is the fail-closed default (only
# synthetic-tagged captures may be processed); ``live`` is the deliberate
# flip that lets real garage audio through. Same posture as the STAY-C
# scribe's synthetic/clinical line — copied SHAPE, not code.
JEEVES_MODE_SYNTHETIC = "synthetic"
JEEVES_MODE_LIVE = "live"

# Where the jeeves tree sits under the instance data dir, and the two files
# within it. Named so the derivation and any future placeholder cannot drift.
_JEEVES_DIR_NAME = "jeeves"
_TELEMETRY_FILE_NAME = "telemetry.jsonl"
_MARK_LOG_FILE_NAME = "marks.jsonl"
_SUSPEND_FILE_NAME = "suspended.json"

# How long a retained miss-report window survives WITHOUT being extracted
# (#98, ruling 1). Seven days, and the number is a judgement about two
# opposite failures:
#
# * Too short (1–3 days) silently destroys the scarce signal. Miss reports
#   are the only labelled negative examples this system can produce, and a
#   weekend away or a skipped morning review would delete one before it was
#   ever seen.
# * Too long (30 days) turns "a training sample awaiting review" into an
#   audio archive of the garage, which is the exact artefact fence 2 exists
#   to prevent. Retention that outlives the operator's memory of the
#   conversation is retention he can no longer consent to meaningfully.
#
# A week is the shortest window that survives a normal gap in a daily
# habit: morning review gets ~7 chances at each artefact, and an artefact
# nobody has acted on in seven days is one nobody is going to.
DEFAULT_MISS_AUDIO_RETENTION_DAYS = 7

# Ring default — 30 minutes, ratified (ruling 5) and explicitly NOT to be
# tuned on a handful of captures. ``window.lookback_used_seconds`` telemetry
# is what answers the size question after ~4 weeks of real use.
DEFAULT_RING_SECONDS = 1800

# Canonical capture format: 16 kHz mono 16-bit PCM. 16 kHz because that is
# openWakeWord's native input rate and Whisper's working rate, so the ring
# holds exactly what both consumers want and nothing resamples twice.
# At these values a 30-minute ring is 1800 * 16000 * 2 = ~57.6 MB of RAM —
# comfortable on a tablet or a Pi 5, and worth knowing before the seconds
# knob is raised.
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_SAMPLE_WIDTH = 2
DEFAULT_CHANNELS = 1

# Window defaults (design §3). 45 s is LABELLED A GUESS in the design; the
# telemetry is how it stops being one.
DEFAULT_LOOKBACK_SECONDS = 45.0
DEFAULT_EXTENDED_LOOKBACK_SECONDS = 120.0
DEFAULT_SILENCE_SECONDS = 3.0
DEFAULT_MAX_LOOKAHEAD_SECONDS = 60.0
DEFAULT_MAX_LOOKBACK_SECONDS = 300.0

# Wake-word providers. ``off`` is the fail-closed default: the detector never
# fires, so an un-deployed instance that happens to load this config captures
# nothing. ``fake`` is the deterministic test/CI detector (needs no model
# files). ``openwakeword`` is the real on-device model — the ONLY value that
# requires the ``[jeeves]`` extra.
WAKE_PROVIDER_OFF = "off"
WAKE_PROVIDER_FAKE = "fake"
WAKE_PROVIDER_OPENWAKEWORD = "openwakeword"
WAKE_PROVIDERS = frozenset({
    WAKE_PROVIDER_OFF, WAKE_PROVIDER_FAKE, WAKE_PROVIDER_OPENWAKEWORD,
})


@dataclass
class JeevesRingConfig:
    """The RAM ring buffer's size and audio format.

    ``seconds`` is the ratified 30 minutes. The three format fields exist so
    a different mic adapter (stage 3) can declare what it actually produces
    rather than having 16 kHz mono assumed underneath it — a silent format
    mismatch would corrupt both the wake model's input and the STT upload.
    """

    seconds: int = DEFAULT_RING_SECONDS
    sample_rate: int = DEFAULT_SAMPLE_RATE
    sample_width: int = DEFAULT_SAMPLE_WIDTH
    channels: int = DEFAULT_CHANNELS


@dataclass
class JeevesWakeConfig:
    """Local wake-word detection (:mod:`.wake`).

    ``provider`` dispatches the detector. ``off`` (the default) is INERT —
    it never fires, and the service logs that it is inert once so "Jeeves
    heard nothing" is distinguishable from "Jeeves was never listening".

    ``confusable_terms`` is the CLOSED set the Q6 trial measured as the real
    confusion set for this wake word (the operator's in-recording probe:
    Groq kept them distinct over a bass line, the tablet's own on-device STT
    wrote one of these for every utterance of "Jeeves"). It is a fixed
    code-side vocabulary, not user content, which is why a matched member
    may appear in a telemetry row (see :mod:`.telemetry`).
    """

    provider: str = WAKE_PROVIDER_OFF
    # Path to the openWakeWord model file. Empty with provider=openwakeword
    # is a FAIL-LOUD condition at detector construction — a real detector
    # with no model is not a detector.
    model_path: str = ""
    # Detection threshold. openWakeWord scores 0..1 per frame.
    threshold: float = 0.5
    # Suppression window after an accepted detection. One spoken wake word
    # spans several model frames, so a raw detector emits a BURST — without
    # this, one utterance becomes half a dozen cues, half a dozen STT calls
    # and half a dozen near-identical captures.
    refractory_seconds: float = 2.0
    # The wake word itself, as it is expected to be TRANSCRIBED. Used only
    # by the transcript-side classifier to recognise (and tolerate) the
    # leading token; the acoustic model is what actually detects.
    wake_word: str = "jeeves"
    confusable_terms: list[str] = field(
        default_factory=lambda: ["cheese", "leaves", "jesus", "jeeps", "eaves"],
    )


@dataclass
class JeevesWindowConfig:
    """How much audio a cue takes out of the ring (design §3).

    ``lookback_seconds`` is the retrospective default — the whole reason the
    ring exists is that the primary cue form is past-tense ("note THAT").
    ``extended_lookback_seconds`` is what the "the last couple of minutes"
    modifier reaches for; without a way to EXPRESS a long lookback the
    telemetry would only ever measure the default and ruling 5's question
    would stay unanswered.

    Lookahead runs until ``silence_seconds`` of quiet, capped at
    ``max_lookahead_seconds``, so "Jeeves, note that — the bearing is the
    wrong size, order the 6203" is captured as one utterance.
    """

    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS
    extended_lookback_seconds: float = DEFAULT_EXTENDED_LOOKBACK_SECONDS
    silence_seconds: float = DEFAULT_SILENCE_SECONDS
    max_lookahead_seconds: float = DEFAULT_MAX_LOOKAHEAD_SECONDS
    # Ceiling on a SPOKEN lookback modifier. Not the ring size: an STT
    # mis-hear ("the last thirty minutes") would otherwise upload most of
    # the ring to the cloud in a single call, which is both the cost
    # argument and the privacy fence undone by one bad transcription.
    max_lookback_seconds: float = DEFAULT_MAX_LOOKBACK_SECONDS
    # Normalized RMS below which a frame counts as silence (0..1). A lounge
    # with music playing has a HIGH noise floor, so this is deliberately a
    # config knob rather than a constant — the ratification's environment
    # correction (music, not tools) makes it the field-tuned number.
    silence_rms_threshold: float = 0.01


@dataclass
class JeevesSttConfig:
    """Cloud STT for cued windows (:mod:`.stt`).

    Shaped to be accepted BY THE SEAM: ``vocab_terms`` +
    ``vocab_decided_path`` are the two attributes
    ``stt_vocab_learning.effective_vocab_terms`` reads, so Jeeves consumes
    the learned-vocabulary loop through the single documented entry point
    rather than reading either field itself. Jeeves gets the biasing half of
    the #54 loop for free; the contributing half arrives with the stage-3
    review card.

    ``vocab_decided_path`` empty ⇒ the seam degrades to the static
    ``vocab_terms`` list (logged once by the STT layer, so "no learned terms"
    is distinguishable from "the store is missing").
    """

    api_key: str = ""
    model: str = "whisper-large-v3"
    language: str = "en"
    timeout_seconds: float = 30.0
    vocab_terms: list[str] = field(default_factory=list)
    vocab_decided_path: str = ""


@dataclass
class JeevesCueConfig:
    """Cue grammar knobs (:mod:`.cues`).

    ``route_target`` is the name the ROUTE verb must NAME EXPLICITLY — the
    asymmetry that makes route the marked, deliberate verb. It is
    per-instance by definition and has NO default: an empty value leaves the
    ROUTE grammar inert rather than guessing a destination for audio that
    leaves the garage.

    ``route_aliases`` lets one target answer to more than one spoken form
    (a name that STT renders inconsistently) without widening the grammar.

    **ONE DESTINATION, BY DESIGN — and aliases are not a second one.** Every
    alias resolves to the SAME ``route_target``. With a target of ``"<A>"`` and
    an alias of ``"<B>"``, saying "Jeeves, tell <B> …" routes the capture to
    **<A>**. Aliases exist for one destination that STT spells inconsistently,
    not for a choice of destinations. Anyone reading ``route_aliases`` as "the
    list of places Jeeves can send to" would be wrong in the one direction that
    matters — audio leaving the garage to a destination the operator did not
    name.

    A SECOND destination is therefore a CODE change, not a config change, and
    that is deliberate (#98 Part B, operator ruling): the design rides ONE peer
    for v1, and plural destinations buy nothing until a second instance
    actually wants Jeeves captures. Building the target table now would add a
    config surface the operator maintains for a capability nobody has asked to
    use. The trigger for plurality is real friction from a real consumer — not
    completeness.

    What promotion-readiness already means here, and why it is satisfied: the
    destination is DATA (this field), the sink carries its own peer token
    (:class:`JeevesRouteSinkConfig`), and no module couples to a named instance
    — a fence the suite enforces over this package's whole text, docstrings
    included, which is why the illustration above is ``<A>``/``<B>`` and not
    two real names. Pointing Jeeves at a different instance is a config edit
    today. What is not a config edit is pointing it at *two at once*.

    Pinned by ``test_an_alias_routes_to_the_ONE_target_not_a_second_one`` so
    this paragraph cannot quietly become false.
    """

    route_target: str = ""
    route_aliases: list[str] = field(default_factory=list)


@dataclass
class JeevesRouteSinkConfig:
    """Where a ROUTE capture is sent — the DEVICE side of the peer link.

    Jeeves authenticates as its own peer with its own token, not via an
    asserted header: it is a device, and the stronger model is the right one
    for a credential that lives on a box in a garage. That token grants ONLY
    the narrow write-only capture capability, and it must have no route to a
    clinical instance at all.

    Empty ``base_url`` or ``token`` ⇒ the sink is INERT (not constructed),
    and a ROUTE cue falls back to the local log rather than being lost. Both
    are required because either alone cannot send anything.
    """

    base_url: str = ""
    token: str = ""
    # The ``X-Alfred-Client`` header value. Kept configurable because the
    # receiving instance's ``allowed_clients`` list must agree with it, and
    # that list is the operator's to write.
    client_name: str = "jeeves"
    timeout_seconds: float = 15.0


@dataclass
class JeevesConfig:
    """The typed ``jeeves:`` block.

    ``mode`` is normalized at load to exactly ``synthetic`` or ``live``
    (fail-closed; see :func:`_normalize_mode`). Every path field is
    instance-derived at load when the operator leaves it unset.
    """

    mode: str = JEEVES_MODE_SYNTHETIC
    ring: JeevesRingConfig = field(default_factory=JeevesRingConfig)
    wake: JeevesWakeConfig = field(default_factory=JeevesWakeConfig)
    window: JeevesWindowConfig = field(default_factory=JeevesWindowConfig)
    stt: JeevesSttConfig = field(default_factory=JeevesSttConfig)
    cues: JeevesCueConfig = field(default_factory=JeevesCueConfig)
    # Where a ROUTE capture goes. Unconfigured ⇒ the sink is inert and a
    # ROUTE cue stays LOCAL (never dropped, never misdirected).
    route: JeevesRouteSinkConfig = field(default_factory=JeevesRouteSinkConfig)
    # Content-free telemetry rows (JSONL). Derived per-instance at load.
    telemetry_path: str = ""
    # MARK-DOWN transcripts — the on-device log that syncs nowhere. Derived
    # per-instance at load. This file DOES hold transcript text; that is its
    # entire job (design §5.2), and it is the only local file that does.
    mark_log_path: str = ""
    # Miss-report AUDIO samples. Empty (the default) ⇒ never written. See
    # the module docstring: the fail-closed direction keeps fence 2 intact.
    miss_audio_dir: str = ""
    # How many days a retained miss window survives without being extracted
    # into a recogniser example. Only meaningful when miss_audio_dir is set.
    miss_audio_retention_days: int = DEFAULT_MISS_AUDIO_RETENTION_DAYS
    # The COMPANY TOGGLE's store (#98, ruling 3). Derived per-instance at
    # load, so a loaded config is never in the "no store" state — the
    # toggle's whole point is surviving a restart, and a suspension with
    # nowhere to live would not. See :mod:`.suspend`.
    suspend_state_path: str = ""

    @property
    def is_live(self) -> bool:
        """True iff the config resolved to live mode (the deliberate flip)."""
        return self.mode == JEEVES_MODE_LIVE


def _normalize_mode(raw_mode: Any) -> str:
    """The fail-closed mode line.

    ONLY the exact (case/space-insensitive) string ``"live"`` selects live
    mode. Absent / None / unknown / malformed => ``"synthetic"``. So a typo,
    a truncated value or an omitted key can never accidentally open the path
    that lets real garage audio through.
    """
    if isinstance(raw_mode, str) and raw_mode.strip().lower() == JEEVES_MODE_LIVE:
        return JEEVES_MODE_LIVE
    return JEEVES_MODE_SYNTHETIC


def _known(cls, data: Any) -> dict[str, Any]:
    """Schema-tolerance filter — the incoming dict reduced to fields ``cls``
    actually declares. A non-dict yields ``{}`` (all-defaults)."""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__}


def _coerce_positive_int(value: Any, default: int) -> int:
    """A positive int, or the default. A zero/negative ring or sample rate
    would divide-by-zero or hold nothing; nonsense never crashes the load."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _coerce_positive_float(value: Any, default: float) -> float:
    """A positive float, or the default (same posture as the int coercer)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f > 0 else default


def _coerce_str(value: Any) -> str:
    """A string, with the YAML-null trap closed.

    ``str(None)`` is ``"None"`` — a non-empty string that would defeat every
    empty-means-inert default in this module (a ``route_target:`` with no
    value would arm the ROUTE verb pointed at a peer called "None"). The
    same D4 trap the scribe config guards for its string fields.
    """
    if value is None:
        return ""
    return str(value).strip()


def _coerce_str_list(value: Any) -> list[str]:
    """Non-empty string entries only; a non-list yields ``[]``."""
    if not isinstance(value, list):
        return []
    return [s for s in (_coerce_str(v) for v in value) if s]


def _build_ring(data: Any) -> JeevesRingConfig:
    known = _known(JeevesRingConfig, data)
    cfg = JeevesRingConfig()
    cfg.seconds = _coerce_positive_int(
        known.get("seconds", cfg.seconds), DEFAULT_RING_SECONDS)
    cfg.sample_rate = _coerce_positive_int(
        known.get("sample_rate", cfg.sample_rate), DEFAULT_SAMPLE_RATE)
    cfg.sample_width = _coerce_positive_int(
        known.get("sample_width", cfg.sample_width), DEFAULT_SAMPLE_WIDTH)
    cfg.channels = _coerce_positive_int(
        known.get("channels", cfg.channels), DEFAULT_CHANNELS)
    return cfg


def _normalize_wake_provider(value: Any) -> str:
    """Fail-closed provider line: an unknown provider resolves to ``off``.

    Loud, because an operator who typed ``openwakework`` has a device that
    will never hear anything and no other signal that says so.
    """
    s = _coerce_str(value).lower()
    if not s:
        return WAKE_PROVIDER_OFF
    if s in WAKE_PROVIDERS:
        return s
    log.error(
        "jeeves.config.unknown_wake_provider",
        configured=s,
        known=sorted(WAKE_PROVIDERS),
        applied=WAKE_PROVIDER_OFF,
        detail="jeeves.wake.provider is not a known provider — falling back "
               "to 'off' (the detector will NEVER fire). Fix the value or "
               "the device is deaf with no other symptom.",
    )
    return WAKE_PROVIDER_OFF


def _build_wake(data: Any) -> JeevesWakeConfig:
    known = _known(JeevesWakeConfig, data)
    cfg = JeevesWakeConfig()
    cfg.provider = _normalize_wake_provider(known.get("provider", cfg.provider))
    cfg.model_path = _coerce_str(known.get("model_path", cfg.model_path))
    cfg.threshold = _coerce_positive_float(
        known.get("threshold", cfg.threshold), cfg.threshold)
    cfg.refractory_seconds = _coerce_positive_float(
        known.get("refractory_seconds", cfg.refractory_seconds),
        cfg.refractory_seconds)
    cfg.wake_word = _coerce_str(known.get("wake_word", cfg.wake_word)).lower() \
        or JeevesWakeConfig().wake_word
    if "confusable_terms" in known:
        terms = [t.lower() for t in _coerce_str_list(known["confusable_terms"])]
        if terms:
            cfg.confusable_terms = terms
    return cfg


def _build_window(data: Any) -> JeevesWindowConfig:
    known = _known(JeevesWindowConfig, data)
    cfg = JeevesWindowConfig()
    for name in (
        "lookback_seconds", "extended_lookback_seconds", "silence_seconds",
        "max_lookahead_seconds", "max_lookback_seconds",
        "silence_rms_threshold",
    ):
        if name in known:
            setattr(cfg, name, _coerce_positive_float(
                known[name], getattr(cfg, name)))
    return cfg


def _build_stt(data: Any) -> JeevesSttConfig:
    known = _known(JeevesSttConfig, data)
    cfg = JeevesSttConfig()
    cfg.api_key = _coerce_str(known.get("api_key", cfg.api_key))
    cfg.model = _coerce_str(known.get("model", cfg.model)) or cfg.model
    cfg.language = _coerce_str(known.get("language", cfg.language))
    cfg.timeout_seconds = _coerce_positive_float(
        known.get("timeout_seconds", cfg.timeout_seconds), cfg.timeout_seconds)
    cfg.vocab_terms = _coerce_str_list(known.get("vocab_terms", []))
    cfg.vocab_decided_path = _coerce_str(
        known.get("vocab_decided_path", cfg.vocab_decided_path))
    # An UNRESOLVED ${VAR} in the api key survives substitution as the literal
    # placeholder — truthy, publicly known from the example config, and it
    # would send every cued window at a credential that cannot work. Blank it
    # so the STT layer reports "not configured" instead of "auth failed".
    if "${" in cfg.api_key:
        log.error(
            "jeeves.config.unresolved_api_key_placeholder",
            detail="an UNRESOLVED ${VAR} placeholder survived env "
                   "substitution in jeeves.stt.api_key — the environment "
                   "variable is NOT set. Treating it as EMPTY (fail-closed): "
                   "STT reports unconfigured rather than failing auth on "
                   "every capture.",
        )
        cfg.api_key = ""
    return cfg


def _build_route(data: Any) -> JeevesRouteSinkConfig:
    """Schema-tolerant build of :class:`JeevesRouteSinkConfig`.

    The token gets the same fail-closed treatment as the STT key: an
    unresolved ``${VAR}`` placeholder survives substitution as a truthy,
    publicly-known literal, and arming a peer link with a guessable bearer
    is strictly worse than leaving the sink inert (which keeps captures
    local rather than losing them).
    """
    known = _known(JeevesRouteSinkConfig, data)
    cfg = JeevesRouteSinkConfig()
    cfg.base_url = _coerce_str(known.get("base_url", cfg.base_url)).rstrip("/")
    cfg.token = _coerce_str(known.get("token", cfg.token))
    cfg.client_name = (
        _coerce_str(known.get("client_name", cfg.client_name)) or cfg.client_name
    )
    cfg.timeout_seconds = _coerce_positive_float(
        known.get("timeout_seconds", cfg.timeout_seconds), cfg.timeout_seconds)
    if "${" in cfg.token:
        log.error(
            "jeeves.config.unresolved_route_token_placeholder",
            detail="an UNRESOLVED ${VAR} placeholder survived env "
                   "substitution in jeeves.route.token — the environment "
                   "variable is NOT set, so the value is the literal, "
                   "PUBLICLY-KNOWN placeholder string. Treating it as EMPTY "
                   "(fail-closed): the sink stays INERT and ROUTE captures "
                   "stay on the device, rather than the peer link arming "
                   "with a guessable credential.",
        )
        cfg.token = ""
    return cfg


def _build_cues(data: Any) -> JeevesCueConfig:
    known = _known(JeevesCueConfig, data)
    cfg = JeevesCueConfig()
    cfg.route_target = _coerce_str(known.get("route_target", cfg.route_target))
    cfg.route_aliases = _coerce_str_list(known.get("route_aliases", []))
    return cfg


def default_telemetry_path(raw: dict[str, Any]) -> str:
    """``<instance data dir>/jeeves/telemetry.jsonl``."""
    return instance_data_path(raw, _JEEVES_DIR_NAME, _TELEMETRY_FILE_NAME)


def default_mark_log_path(raw: dict[str, Any]) -> str:
    """``<instance data dir>/jeeves/marks.jsonl``."""
    return instance_data_path(raw, _JEEVES_DIR_NAME, _MARK_LOG_FILE_NAME)


def default_suspend_state_path(raw: dict[str, Any]) -> str:
    """``<instance data dir>/jeeves/suspended.json``."""
    return instance_data_path(raw, _JEEVES_DIR_NAME, _SUSPEND_FILE_NAME)


def load_from_unified(raw: dict[str, Any]) -> JeevesConfig:
    """Build :class:`JeevesConfig` from the unified config dict.

    An absent ``jeeves:`` block yields the all-defaults config: synthetic
    mode, wake provider ``off``, route target unset. That is a fully INERT
    Jeeves — which is the correct state for every instance that has not
    deployed a capture device.
    """
    block = raw.get("jeeves") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        block = {}
    block = substitute_env_in_value(block)

    return JeevesConfig(
        mode=_normalize_mode(block.get("mode")),
        ring=_build_ring(block.get("ring")),
        wake=_build_wake(block.get("wake")),
        window=_build_window(block.get("window")),
        stt=_build_stt(block.get("stt")),
        cues=_build_cues(block.get("cues")),
        route=_build_route(block.get("route")),
        telemetry_path=(
            _coerce_str(block.get("telemetry_path")) or default_telemetry_path(raw)
        ),
        mark_log_path=(
            _coerce_str(block.get("mark_log_path")) or default_mark_log_path(raw)
        ),
        miss_audio_dir=_coerce_str(block.get("miss_audio_dir")),
        miss_audio_retention_days=_coerce_positive_int(
            block.get(
                "miss_audio_retention_days", DEFAULT_MISS_AUDIO_RETENTION_DAYS,
            ),
            DEFAULT_MISS_AUDIO_RETENTION_DAYS,
        ),
        suspend_state_path=(
            _coerce_str(block.get("suspend_state_path"))
            or default_suspend_state_path(raw)
        ),
    )

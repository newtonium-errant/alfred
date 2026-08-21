"""Load config.yaml into typed dataclasses with env-var substitution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_RE = re.compile(r"\$\{(\w+)\}")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ${VAR} placeholders with environment variables."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


# --- Dataclasses ---


@dataclass
class AllowedUser:
    """One entry in the ``allowed_users`` allowlist, with an optional role.

    VERA MVP (2026-06-09) — VERA is the first multi-user instance, so the
    allowlist must carry a per-user role (``owner`` vs ``ops``). Existing
    single-role instances (Salem, KAL-LE, Hypatia) ship a flat list of
    bare ints; the loader normalizes those to ``AllowedUser(id, "owner")``
    so their behaviour is unchanged.

    ``role`` defaults to ``"owner"`` — the back-compat default. A bare-int
    entry (or any entry without an explicit role) is an owner, preserving
    full powers for every existing flat-allowlist instance.

    ``name`` (VERA reporter follow-up, 2026-06-09) is the sender's display
    name (e.g. ``"Andrew"``, ``"Ben"``), surfaced to the talker turn so
    the agent can attribute per-message authorship — e.g. set a ticket's
    ``reporter`` field from the actual sender rather than a hardcoded
    value. Defaults to ``None``: bare-int and role-only entries carry no
    name, and single-user instances never need one (the sender is always
    the owner). When ``None`` the talker falls back to the role label (see
    ``bot._name_for`` / the sender-identity block in ``conversation.py``).
    """

    id: int
    role: str = "owner"
    name: str | None = None


def _normalize_allowed_users(raw: Any) -> list[AllowedUser]:
    """Coerce a mixed ``allowed_users`` list into ``list[AllowedUser]``.

    Accepts BOTH shapes in the same list (back-compat by construction):

      * bare int ``123`` → ``AllowedUser(123, "owner")`` (flat-list
        instances — Salem / KAL-LE / Hypatia — keep owner powers).
      * dict ``{id: 222, role: ops}`` → ``AllowedUser(222, "ops")``
        (VERA's role-bearing entries).

    A dict missing ``role`` defaults to ``"owner"`` (same back-compat
    default as a bare int). Entries with a non-int / missing ``id`` are
    dropped (defensive against malformed YAML) rather than crashing the
    whole config load — a single bad allowlist row shouldn't take the
    daemon down. Bare ints that are actually booleans (YAML ``true`` →
    Python ``True`` is an ``int`` subclass) are dropped too.
    """
    if not raw:
        return []
    out: list[AllowedUser] = []
    for entry in raw:
        if isinstance(entry, bool):
            # YAML ``true`` parses as Python ``True`` (an int subclass) —
            # never a valid user id; drop it.
            continue
        if isinstance(entry, int):
            out.append(AllowedUser(id=entry, role="owner"))
            continue
        if isinstance(entry, dict):
            uid = entry.get("id")
            if isinstance(uid, bool) or not isinstance(uid, int):
                continue
            role = entry.get("role")
            role_str = role if isinstance(role, str) and role else "owner"
            # ``name`` (VERA reporter follow-up) — optional sender display
            # name. Absent / empty / non-str → None (the back-compat
            # default for bare-int + role-only entries).
            name = entry.get("name")
            name_str = name if isinstance(name, str) and name else None
            out.append(AllowedUser(id=uid, role=role_str, name=name_str))
            continue
        # Unknown entry shape (str, list, None) — drop defensively.
    return out


@dataclass
class VaultConfig:
    path: str = ""
    # See ``alfred.vault.config_helpers`` for the dont_scan/dont_index split.
    # Talker only uses ``ignore_dirs`` for build_vault_context (a scanning
    # concern). ``dont_index_dirs`` is carried for config-shape consistency.
    ignore_dirs: list[str] = field(default_factory=lambda: [".obsidian"])
    # New (2026-05-01) — see vault/config_helpers.py for the rationale.
    dont_scan_dirs: list[str] | None = None
    dont_index_dirs: list[str] = field(default_factory=list)

    @property
    def vault_path(self) -> Path:
        return Path(self.path)


@dataclass
class AnthropicConfig:
    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    temperature: float = 0.7


# Default domain-vocab biasing terms (STT fallback spec §9, must-fix 2).
# Migrated from the previously-hardcoded ``transcribe._STT_VOCABULARY_PROMPT``
# so EVERY backend in the chain biases the same terms (Whisper prompt=,
# Deepgram keywords) — without this a fallback transcribes domain terms
# worse than primary, breaking the "comparable = seamless" claim (§7).
# Per-instance configs may override ``stt.vocab_terms``.
DEFAULT_STT_VOCAB_TERMS: list[str] = [
    "Algernon", "Salem", "S.A.L.E.M.", "KAL-LE", "K.A.L.-L.E.", "Hypatia",
    "V.E.R.A.", "STAY-C", "Zettelkasten", "aftermath-lab",
    "library-alexandria", "distiller", "surveyor", "curator", "janitor",
    "talker", "gcal", "Obsidian", "Andrew Newton", "RRTS", "Fergus",
    "Marcus Aurelius", "Heraclitus", "Stoicism", "Epicureanism",
    "Meditations", "Hayes", "Ryan Holiday",
]


# Learned-vocabulary store paths (#54). Named constants rather than inline
# literals because the Daily Sync review surface DERIVES its own defaults from
# these (``daily_sync.config.SttVocabConfig``): the web chat route writes the
# corpus, the CLI writes the decided store, and the review card reads both — a
# read side and a write side that disagree about the path is exactly the drift
# the routine_match single-source contract already had to close once.
# Instance-neutral (every instance has its own ``data/``), so no per-instance
# literal is baked in.
DEFAULT_STT_VOCAB_CORPUS_PATH = "./data/stt_corrections.jsonl"
DEFAULT_STT_VOCAB_DECIDED_PATH = "./data/stt_vocab_decided.jsonl"


# R4 calibration-loop stores. Same single-source contract as the two above and
# for the same reason: the web session-close CAPTURES into ``pending``, the CLI
# approve/reject WRITES ``decided``, and the Daily Sync review section READS
# both. ``daily_sync.config.CalibrationReviewConfig`` derives these rather than
# holding independently-defaulted duplicates.
# Instance-neutral literals (every instance has its own ``data/``).
DEFAULT_CALIBRATION_PENDING_PATH = "./data/calibration_pending.jsonl"
DEFAULT_CALIBRATION_DECIDED_PATH = "./data/calibration_decided.jsonl"


#: Backward-compatible private alias. Existing tests import the underscored
#: name; the public one exists because ``daily_sync.config`` now derives the
#: review surface's term list from it (one source, two readers).
_DEFAULT_STT_VOCAB_TERMS = DEFAULT_STT_VOCAB_TERMS


@dataclass
class SttBackendConfig:
    """One backend in the STT fallback chain (spec §9).

    A 2-backend chain (Groq → Deepgram) in M1; M4 adds the local-whisper
    ``never_skip`` backstop. ``api_key`` is env-substituted upstream
    (``${GROQ_API_KEY}`` / ``${DEEPGRAM_API_KEY}``) before the dataclass
    is built. ``tier`` drives the §7 seamless-vs-flagged UX (M1 backends
    are both ``comparable`` → seamless). ``never_skip`` is carried now for
    the M2 circuit-breaker / M4 backstop (no M1 backend sets it).
    """

    backend: str = "groq-whisper"          # "groq-whisper" | "deepgram"
    api_key: str = ""
    model: str = ""                        # backend-specific default if ""
    tier: str = "comparable"
    timeout_s: float = 10.0
    language: str = "en"
    never_skip: bool = False
    # Groq-only: verbose_json is required for no_speech_prob / avg_logprob.
    response_format: str = "verbose_json"
    # Deepgram-only output-shape parity (§7) — NOT optional in practice.
    punctuate: bool = True
    smart_format: bool = True


@dataclass
class SttShadowCaptureConfig:
    """RETIRED capability's config block — KEPT FOR YAML BACK-COMPAT ONLY.

    This block once configured the BATCH STT shadow-capture (R1-baseline
    corpus builder, ``algernon-stt-test-series-2026-06-27``): when enabled,
    every Telegram voice note ran BOTH engines and appended the audio + both
    transcripts + their divergence to a replayable corpus. Its only door was
    ``bot.py``'s ``on_voice``, deleted in T4 C3 (50460499); the operator
    ruled "agreed retire" and ``telegram/stt_shadow.py`` was DELETED in R2
    (2026-08-20). **Nothing reads these fields.** Setting ``enabled: true``
    does nothing at all.

    The STREAMING twin is alive and unaffected: ``web/voice_stt_shadow.py``
    on the WebRTC path, configured by the SEPARATE
    ``web.voice.stt.shadow_capture`` block (``WebSttShadowCaptureConfig`` in
    ``web/config.py``). Same concept, different referent — EVERY
    ``shadow_capture:`` block in ``config.yaml.example`` sits under a
    ``web:`` section and configures that web one (two of them at the time
    of writing, :911 and :1006 — grep rather than trust the coordinates);
    this Telegram block was never in the example file at all.

    WHY THE DATACLASS SURVIVES ITS MODULE (the T5 pattern —
    ``TodayCommandConfig`` feca775f, ``InstanceConfig.aliases`` 656d1a87):
    ``_build`` has NO unknown-key filter — it assigns every key straight
    through and calls ``cls(**kwargs)``. A deployed YAML still carrying
    ``telegram: stt: shadow_capture:`` therefore raises ``TypeError`` at
    talker config load the instant this field stops existing, and the box
    carries the block on all four instances.

    The blast radius, DRIVEN against the field-removed mutant rather than
    reasoned (control vs mutant, both legs run):
      * LOUD and dominant — ``load_from_unified`` raises, so the talker
        DAEMON fails to start (``telegram/daemon.py`` imports it and calls
        it in ``run``) along with the talker CLI paths.
      * LOUD on the health surface too, as of 2026-08-20 — and this arm
        USED TO BE the quiet one. ``skill_audit``'s ``except TypeError``
        around ``load_talker_config`` is documented for the missing
        ``instance.name`` case but caught THIS TypeError as well, returning
        ``instance_missing`` with reason "telegram.instance config
        incomplete (...)" even when ``instance.name`` was present and fine.
        ``_check_skill_capability_audit`` reported SKIP, a QUIET health
        status, so the surface raised no attention card AND blamed the
        wrong thing. ``audit_skill`` now discriminates on the CONFIG SHAPE
        (is the ``name`` key absent?) rather than on the exception class,
        so this shape returns ``config_error`` and the probe WARNs.
        Re-measured after the fix, same field-removal mutant: control OK ->
        mutant WARN.
      * NOT the health-aggregator swallow. An earlier draft of this note
        blamed ``_load_tool_checks`` dropping the talker probe; that is
        FALSE and is corrected here where the next reader meets it —
        under the mutation ``alfred.telegram.health`` still imports and
        ``talker`` still registers, because removing a dataclass field is
        not an ImportError and that except-branch is never entered.

    Verified empirically, both directions, by
    ``tests/telegram/test_stt_shadow_config_compat.py`` — whose
    ``test_unknown_stt_key_still_raises`` is the positive control proving
    the tolerance is THIS FIELD and not loader permissiveness.

    Removing this block is therefore a config-migration task (drop the key
    from every deployed YAML first), not a code cleanup.
    """

    enabled: bool = False
    dir: str = "./data/stt_corpus"


@dataclass
class STTConfig:
    # --- legacy single-backend fields (back-compat) ---
    # Existing per-instance configs (Salem/KAL-LE/Hypatia) carry only these
    # three. When ``chain`` is empty they synthesize a 1-backend Groq chain
    # (``effective_chain``), preserving today's exact behaviour.
    provider: str = "groq"
    api_key: str = ""
    model: str = "whisper-large-v3"
    # --- STT fallback chain (spec §9) ---
    vocab_terms: list[str] = field(
        default_factory=lambda: list(DEFAULT_STT_VOCAB_TERMS)
    )
    # Per-instance EXTRA caption-artifact denylist (clinic-capture Piece 2b).
    # UNIONS onto the universal ``common.stt_noise`` default (never replaces it).
    # Empty = defaults only. Dropped at the STT seams BEFORE a hallucinated line
    # can drive a live turn or land in structuring (clinical-safety control).
    hallucination_denylist: list[str] = field(default_factory=list)
    total_budget_s: float = 30.0           # global per-message chain deadline
    min_transcript_chars: int = 3          # "empty" threshold (post-trim)
    chain: list[SttBackendConfig] = field(default_factory=list)
    # --- learned vocabulary (#54) ---
    # The operator corrects the same mis-heard domain terms by hand every time;
    # this is the loop that lets those corrections teach the biasing list.
    #
    # ``vocab_learning_enabled`` gates CAPTURE ONLY and defaults OFF, per the
    # convention every other judgment surface follows (routine_match,
    # tier_recurrence; the batch shadow_capture was a fourth until R2 retired
    # it 2026-08-20). Default-OFF is not timidity here: capture writes the
    # operator's own message text to a new file. That alone is the reason —
    # an instance opts in deliberately; it is never decided for them by a
    # default.
    #
    # INSTANCE ATTRIBUTION (corrected 2026-08-21). This comment previously
    # read "Hypatia's voice traffic is CLINICAL"; that is wrong, and the
    # ``CalibrationConfig`` docstring was copied from it. The clinical
    # instance is STAY-C (``config.stayc-clinical.yaml``, the
    # ``stayc_clinical`` scope family); Hypatia is the scholar/scribe
    # instance and is NOT clinical. STAY-C cannot reach this switch at all:
    # ``telegram`` is catalogued in ``EGRESS_CONFIG_SECTIONS`` and is absent
    # from the fail-closed ``SOVEREIGN_ALLOWED_SECTIONS``
    # (``sovereign/boundary.py``), so a sovereign config carrying a
    # ``telegram`` block refuses at load.
    #
    # POSTURE — stated precisely, because it is easy to over-read in both
    # directions. Reducing PHI exposure is an ACTIVE, ONGOING policy, not a
    # finished migration; and it is NOT a no-cloud rule — cloud LLM use
    # continues and has been improved. So do NOT relax the conservative
    # PHI-egress guards that name Hypatia elsewhere (``config.yaml.example``
    # ``shadow_capture``) on the strength of this correction: "Hypatia is not
    # the clinical instance" does not mean "Hypatia may egress freely".
    #
    # The two paths are instance-neutral (each instance has its own ``data/``),
    # so no per-instance literal is baked in — see the hardcoding rule.
    vocab_learning_enabled: bool = False
    #: Full (transcript, sent) pairs — the audit trail corrections are mined from.
    vocab_corpus_path: str = DEFAULT_STT_VOCAB_CORPUS_PATH
    #: The operator's approve/reject verdicts. READ on every transcription via
    #: ``stt_vocab_learning.effective_vocab_terms`` — unlike the corpus, this one
    #: is live even when capture is off, so a term approved before an instance
    #: disabled capture keeps biasing.
    vocab_decided_path: str = DEFAULT_STT_VOCAB_DECIDED_PATH
    # --- shadow-capture: RETIRED (R2, 2026-08-20) ---
    # Kept ONLY so a deployed YAML carrying the block still loads; no code
    # reads it. See SttShadowCaptureConfig's docstring for why removing it
    # is a config migration, not a cleanup.
    shadow_capture: SttShadowCaptureConfig = field(
        default_factory=SttShadowCaptureConfig
    )

    def effective_chain(self) -> list[SttBackendConfig]:
        """The chain to run — the explicit ``chain`` if configured, else a
        single Groq backend synthesized from the legacy fields.

        Back-compat: an instance with only the old ``provider``/``api_key``/
        ``model`` (no ``chain:``) runs exactly as before — one Groq backend,
        no fallback. An instance that adds ``chain:`` gets the fallback.
        """
        if self.chain:
            return self.chain
        return [SttBackendConfig(
            backend="groq-whisper",
            api_key=self.api_key,
            model=self.model or "whisper-large-v3",
            tier="comparable",
        )]


@dataclass
class SessionConfig:
    gap_timeout_seconds: int = 1800
    state_path: str = "./data/talker_state.json"
    # Phase 2 deferred-enhancement #1 (per ``project_hypatia_phase2_followups.md``):
    # when true, after a substantive session closes, the talker invokes a
    # short LLM call to derive a 3-5 word topic slug from the transcript
    # and renames the session record so the filename reflects what the
    # session was *about* (not the opening greeting). Defaults OFF for
    # safety — only opt-in instances (Hypatia in Phase 2) flip it. Failure
    # is isolated: if derivation errors, the original opening-text slug
    # is preserved and a warning is logged.
    derive_slug_from_substance: bool = False
    # Clinic-capture arc (2026-07): when true, a capture CANDIDATE
    # (``session.is_capture_candidate`` — explicitly /capture-typed OR a
    # substantive session) that closes on a NON-/end path (web ``/chat/open``
    # reopen, the daemon timeout sweeper) auto-runs the capture structuring pass
    # so a dictated action-item dump is never lost. Defaults OFF to preserve
    # Salem's current behaviour (Salem captures structure via Telegram /end);
    # flipped ON for Hypatia, whose captures land on the PWA (always
    # session_type="conversation", no web /end). The fail-safe
    # ``capture_structured: pending`` marker is written UNCONDITIONALLY at close
    # regardless of this flag — the flag only governs the auto-LLM pass.
    auto_structure_on_close: bool = False


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/talker.log"


@dataclass
class TtsConfig:
    """ElevenLabs TTS config for the wk2b ``/brief`` command.

    ``voice_id`` accepts either an ElevenLabs canonical voice id (e.g.
    ``21m00Tcm4TlvDq8ikWAM`` for Rachel) or a friendly name (``"Rachel"``)
    which the synthesiser resolves via a lookup at call time. The
    friendly-name path is offered because ElevenLabs voice ids are
    opaque and unmemorable; config-by-name lets users read their
    config at a glance.
    """

    provider: str = "elevenlabs"
    api_key: str = ""
    model: str = "eleven_turbo_v2_5"
    voice_id: str = "Rachel"
    summary_word_target: int = 300


@dataclass
class InstanceConfig:
    """Per-instance persona identity for the talker.

    ``name`` is the casual, greeting-friendly form ("Salem", "KAL-LE",
    "Hypatia"). ``canonical`` is the formal form used once in the SKILL's
    identity paragraph ("S.A.L.E.M.", "K.A.L.L.E.", "H.Y.P.A.T.I.A.").
    ``aliases`` was the multi-instance opening-cue router's
    case-insensitive accept list (phone-autocorrect / voice-transcription
    variants: ``"Salem"`` → S.A.L.E.M., ``"Pat"`` → Hypatia). The router
    died with the Telegram retirement (T5, 2026-08-19); the field stays
    for config compatibility (instance YAMLs carry it) and for any
    future name-matching consumer.

    ``name`` is **required** (no default). "Alfred" is the project /
    architecture name, never an instance name — defaulting to it
    silently mis-attributes prose ("Alfred's earlier message" on a
    Salem-installed bot) and silently misconfigures peer-protocol
    identification. A config YAML without ``instance.name`` raises
    ``TypeError`` at load time. See
    ``feedback_hardcoding_and_alfred_naming.md``.

    ``skill_bundle`` picks which SKILL bundle the talker loads at
    startup (``"vault-talker"`` for Salem, ``"vault-kalle"`` for KAL-LE,
    ``"vault-hypatia"`` for Hypatia). The bundle name resolves to
    ``src/alfred/_bundled/skills/<skill_bundle>/SKILL.md``.

    ``tool_set`` selects which vault-bridge tool schema the talker
    exposes to the model AND which scope the dispatcher routes to in
    ``conversation._execute_tool``. ``"talker"`` (default) uses
    ``TALKER_VAULT_TOOLS``; ``"kalle"`` adds ``bash_exec``;
    ``"hypatia"`` reuses the talker tool list with the hypatia scope.
    Callers read ``conversation.VAULT_TOOLS_BY_SET``.
    """

    name: str
    canonical: str = ""
    aliases: list[str] = field(default_factory=list)
    skill_bundle: str = "vault-talker"
    tool_set: str = "talker"


@dataclass
class IdleTickConfig:
    """Talker idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``talker.idle_tick`` log event so observers can distinguish
    *idle / healthy* from *broken*. Without it, a quiet stretch (no inbound
    traffic) is indistinguishable from a hung daemon. See
    ``src/alfred/telegram/heartbeat.py`` for the rationale and the cadence-
    rationale comment block.

    Defaults are deliberately on — the cost is negligible (~290 KB/day at
    60s) and the diagnostic value compounds.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class VisionConfig:
    """Per-instance vision (image-message) gate for the Telegram bot.

    When ``enabled=True`` (the default for Salem / Hypatia / KAL-LE — all on
    Claude 4.x with native vision), Telegram ``photo`` messages download,
    save to ``<vault.path>/inbox/screenshot-<...>.jpg``, and pass into the
    Anthropic Messages API as a multimodal user content block.

    When ``enabled=False`` the photo handler short-circuits with a
    user-facing reply explaining vision is off. Gating exists so future
    PHI-sensitive instances (V.E.R.A. / STAY-C) can default to ``false``
    until a PHI-firewall design lands. Per
    ``feedback_intentionally_left_blank.md``: never silently drop — always
    tell the user what happened.

    See ``project_image_vision_support.md`` for the deferred-Phase-2 plan
    this implements.
    """

    enabled: bool = True
    # User-facing reply when ``enabled=False``. Operator-tunable so the
    # PHI-firewall instances can phrase the gate in their own voice.
    disabled_reply: str = (
        "Sorry — image messages aren't enabled for this instance. "
        "Please describe the screenshot in text and I'll help."
    )


@dataclass
class FictionConfig:
    """Per-instance gate for the ``/fiction <title>`` slash command.

    Default ``False`` so Salem (and any other operational-vault
    instance) never accidentally registers the command. Hypatia opts
    in via ``telegram.fiction.command_enabled: true`` in
    ``config.hypatia.yaml`` because her vault layout (~/library-
    alexandria/) has the ``draft/fiction/`` directory pattern the
    command writes into.

    Conditional registration: when ``enabled=False`` (or the
    ``fiction`` block is absent entirely), ``/fiction`` is NOT
    registered as a CommandHandler at all — Telegram's "unknown
    command" behaviour fires for instances that legitimately don't
    support fiction posture.

    See ``project_hypatia_phase2_followups.md`` for the deferred
    Phase 2.5 plan this implements.
    """

    command_enabled: bool = False


@dataclass
class TodayCommandConfig:
    """Config block for the RETIRED ``/today`` slash command (Tier Phase
    2A, 2026-05-28; command died with the Telegram retirement, T5
    2026-08-19 — ``today_command.py`` is deleted).

    The dataclass is KEPT because ``timezone`` outlived the command: it
    is the instance timezone the tier surface still resolves through
    ``conversation._resolve_tier_timezone`` (prefers
    ``today_command.timezone``, falls back to Salem's tz), so Salem's
    configured block keeps steering the tier done-state date boundary.
    ``enabled`` is inert — nothing registers a command any more.

    ``timezone`` defaults to ``America/Halifax`` (Salem's tz) since the
    talker config doesn't carry a system timezone today.
    """

    enabled: bool = False
    #: IANA timezone for the tier compute's 'today' boundary — consumed
    #: by ``conversation._resolve_tier_timezone``. Defaults to Salem's
    #: tz since Phase 2A was Salem-only.
    timezone: str = "America/Halifax"


@dataclass
class VoiceTrainConfig:
    """Per-instance gate for the ``/train`` + ``/method-source`` slash commands.

    Default ``False`` so Salem (and any other operational-vault
    instance) never accidentally registers the commands. Hypatia opts
    in via ``telegram.voice_train.command_enabled: true`` in
    ``config.hypatia.yaml`` because her vault layout has the
    ``document/essay/`` + ``voice/`` + ``method/`` directory patterns
    the worker writes into.

    Conditional registration: when ``command_enabled=False`` (or the
    ``voice_train`` block is absent entirely), neither slash command
    is registered as a CommandHandler — Telegram's "unknown command"
    behaviour fires for instances that legitimately don't support
    voice/method training.

    The async extraction worker only starts when
    ``command_enabled=True``. With it disabled, no queue file is
    polled, no Opus calls are made.

    See ``project_image_vision_support.md`` and
    ``project_hypatia_phase2_followups.md`` for adjacent posture gates
    this follows the shape of.
    """

    command_enabled: bool = False
    # JSONL queue file the slash-command handlers append to + the
    # worker drains. ``None`` defaults to
    # ``<vault.path>/../data/<instance>/extraction_queue.jsonl`` at
    # daemon startup so each instance gets an isolated queue without
    # the operator setting it explicitly.
    queue_path: str | None = None
    # Worker poll interval (seconds). 8s ticks pick up jobs within
    # ack-perception time without burning CPU on idle ticks. Operator-
    # tunable for low-volume instances.
    worker_poll_seconds: int = 8
    # Model used for the structured-extraction call. Opus 4.x is the
    # default — extraction is deeper than per-turn conversation.
    extraction_model: str = "claude-opus-4-5"
    # Minimum char count for "most-recent paste" classification when
    # the slash command is invoked with no body. Below this, the
    # handler refuses with a "no recent paste" reply rather than
    # extracting from a one-line "ok cool" prior message.
    min_paste_chars: int = 200
    # Multi-message paste debounce window (seconds). Telegram caps
    # individual messages at ~4096 chars; long Substack pastes get
    # split across 2-4 messages by the client. After ``/train`` (or
    # ``/method-source``) fires, the bot buffers the paste for
    # ``debounce_seconds`` of operator silence before flushing — any
    # plain-text messages in the same chat during that window are
    # appended to the buffer instead of going through the natural-
    # language conversation path. See Bug #58 (2026-05-08) and the
    # ``PendingPaste`` block in ``voice_train.py``.
    #
    # Ticket #70 (2026-05-07): bumped from 5s → 10s because Telegram
    # client auto-split inter-chunk delays were observed at 7-12s in
    # real use, causing the 5s default to flush prematurely and drop
    # late chunks to the natural-language conversation handler. 10s
    # captures the long-tail of the chunking-gap distribution at the
    # cost of slower ack on single-message /train (acceptable — the
    # ack is a courtesy, not gating user action). End-marker detection
    # (see :func:`_buffer_has_end_marker`) flushes complete-essay
    # pastes early, recovering most of the latency cost.
    debounce_seconds: int = 10
    # Hard ceiling on how long a buffer may stay open. Even if the
    # operator keeps typing, the buffer flushes at this point so it
    # can't grow unbounded. 60s is generous for a multi-paragraph
    # paste-in-pieces workflow but keeps a wandered-off buffer from
    # holding the slot indefinitely.
    max_buffer_seconds: int = 60
    # Ticket #70 (2026-05-07) — rapid-arrival continuation window.
    # When a second chunk arrives within ``rapid_arrival_seconds`` of
    # the prior chunk in the same buffer, the chunk is treated as
    # continuation regardless of debounce expiry. This catches the
    # "Telegram bursts the auto-splits sub-second" case where a flush
    # could otherwise race ahead of an in-flight chunk delivery. 3s
    # is a generous window; bursts are observed sub-1s in practice.
    rapid_arrival_seconds: float = 3.0


@dataclass
class BashExecConfig:
    """KAL-LE's ``bash_exec`` tool config.

    ``audit_path`` is the JSONL log path every bash_exec invocation
    appends to. Separate from the main talker/transport audit logs
    because security review treats this one as high-sensitivity (code-
    mutation history).
    """

    audit_path: str = "./data/bash_exec.jsonl"
    # Timeout + output caps are enforced as hard-coded constants in
    # ``bash_exec.py`` — they're invariants, not config. Putting them
    # here would invite "let's just raise the timeout" which breaks
    # the safety contract. Config only carries the audit path.


@dataclass
class CalibrationConfig:
    """R4 — the voice-calibration learning loop (capture → propose → approve → inject).

    TWO SWITCHES, BOTH DEFAULT-OFF, and they are genuinely different decisions —
    the same split ``SttConfig.vocab_learning_enabled`` / ``SttVocabConfig.enabled``
    already draws for the vocabulary loop:

      * ``capture_enabled`` gates the ANALYZER at web session close. It costs an
        LLM call per closed session and it reads the operator's transcript, so an
        instance opts in deliberately — reading the operator's own words is
        reason enough on its own, and this is never decided for an instance by
        a default. (This bullet previously asserted "Hypatia's voice traffic is
        CLINICAL". It is not: STAY-C is the clinical instance, Hypatia is the
        scholar/scribe one. See the INSTANCE ATTRIBUTION note on
        ``SttConfig.vocab_learning_enabled``, the split this one mirrors.)
      * ``inject_enabled`` gates the READ side: whether the approved calibration
        block is injected into the model's system prompt
        (``run_turn(calibration_str=...)``). Separate because "may it learn about
        me" and "may it act on what it learned" are different permissions, and an
        instance can legitimately accumulate approved calibration before turning
        the behaviour change on.

    Neither switch can apply anything on its own: the APPLY door is
    ``calibration_store.approve_proposal`` and it requires a named operator. There
    is deliberately no ``auto_confirm_hours`` field here — see that module's
    docstring for why routing this through the attribution-audit timeout was
    refused.

    The two store paths are instance-neutral and single-sourced; the Daily Sync
    review section derives them rather than re-defaulting them.
    """

    capture_enabled: bool = False
    inject_enabled: bool = False
    #: Analyzer drafts awaiting a thumb. Written by capture, read by review.
    pending_path: str = DEFAULT_CALIBRATION_PENDING_PATH
    #: Operator verdicts. Written by approve/reject, read by review + capture
    #: (the re-proposal exclusion).
    decided_path: str = DEFAULT_CALIBRATION_DECIDED_PATH
    #: The model the analyzer runs on. Matches ``calibration.propose_updates``'
    #: own default rather than restating a different one.
    model: str = "claude-sonnet-4-6"
    #: How much of the closed transcript the analyzer reads.
    transcript_tail_turns: int = 20
    #: Hard cap on drafts accepted from ONE session. A prompt-injected or
    #: malfunctioning analyzer returning fifty proposals must not be able to
    #: flood the operator's review queue; the cap bites in the log, never
    #: silently.
    max_proposals_per_session: int = 5


@dataclass
class TalkerConfig:
    bot_token: str = ""
    # VERA MVP (2026-06-09): role-bearing allowlist. Each entry is an
    # ``AllowedUser(id, role)``; the loader normalizes bare-int YAML
    # entries (Salem / KAL-LE / Hypatia flat lists) to role ``"owner"``.
    # See ``_normalize_allowed_users`` + ``AllowedUser``.
    allowed_users: list[AllowedUser] = field(default_factory=list)
    primary_users: list[str] = field(default_factory=list)
    # Peer-message precedence label style (2026-06-09) — how the inbound
    # peer-relay prefix renders the Z/O/P/R precedence in Telegram:
    # ``letters`` (``[KAL-LE · O]``), ``words`` (``[KAL-LE · Immediate]``),
    # or ``both`` (``[KAL-LE · O Immediate]``). Default ``words`` — the
    # safe choice for any new / non-technical user (Ben on VERA). Andrew's
    # instances (Salem/KAL-LE/Hypatia) set ``letters`` (ex-USAF reads
    # Z/O/P/R instantly). An absent key keeps ``words``. Consumed by
    # ``peers.render_precedence_prefix`` in the daemon relay path.
    precedence_label_style: str = "words"
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    # R4 calibration loop — see :class:`CalibrationConfig`. Both switches
    # default OFF, so an absent block leaves the loop dormant exactly as it was
    # before the doors were built.
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    instance: InstanceConfig = field(default_factory=InstanceConfig)
    # wk2b c5: ``tts`` is optional — absent means /brief falls back to
    # "not configured" reply, not a hard failure. ``None`` sentinel kept
    # as a default so health.py can distinguish "section missing" from
    # "section present with empty fields".
    tts: TtsConfig | None = None
    # Stage 3.5: bash_exec — only relevant when
    # ``instance.tool_set == "kalle"``. Absent on Salem, present on
    # KAL-LE with a KAL-LE-specific audit path.
    bash_exec: BashExecConfig | None = None
    # Idle-tick heartbeat — see :class:`IdleTickConfig`. Defaulted-on
    # via the dataclass default_factory; absent block in YAML keeps
    # ``enabled=True`` / ``interval_seconds=60``.
    idle_tick: IdleTickConfig = field(default_factory=IdleTickConfig)
    # Vision (image-message) gate — see :class:`VisionConfig`. Default-on
    # for current 3 live instances (all Claude 4.x); absent block keeps
    # ``enabled=True``. Future PHI-sensitive instances flip to false in
    # config until a PHI-firewall design lands.
    vision: VisionConfig = field(default_factory=VisionConfig)
    # Fiction posture gate — see :class:`FictionConfig`. Default-OFF;
    # only Hypatia opts in. None sentinel matches the existing
    # optional-block convention (tts / bash_exec) so health probes can
    # tell "block absent" from "block present, command disabled".
    fiction: FictionConfig | None = None
    # Voice/method training gate — see :class:`VoiceTrainConfig`.
    # Default-OFF / None sentinel like fiction. Hypatia is Phase 1's
    # only opt-in; Salem/KAL-LE adoption is a config flip when their
    # workflows need it.
    voice_train: VoiceTrainConfig | None = None
    # Today-command block — see :class:`TodayCommandConfig`. The /today
    # command is retired (T5 2026-08-19); the block survives because
    # ``conversation._resolve_tier_timezone`` reads its ``timezone``.
    # Salem populates it; KAL-LE / Hypatia leave it absent (None).
    today_command: TodayCommandConfig | None = None
    # Path to the config file this TalkerConfig was loaded from. Carried
    # so lazy/late loaders (notably the inter-instance peer-tool dispatcher
    # in ``conversation._dispatch_peer_inter_instance_tool``) can re-read
    # the SAME config file at call time rather than defaulting to
    # ``config.yaml``. Without this, a Hypatia daemon started with
    # ``--config config.hypatia.yaml`` would see its peer-tool dispatcher
    # silently fall back to Salem's config and report ``unknown peer
    # 'salem'`` for any propose_*/query_canonical call. ``None`` is the
    # backward-compat default — populated by :func:`load_config` (path
    # arg known directly) and by :func:`load_from_unified` when the raw
    # dict carries the synthetic ``_config_path`` key (set by ``alfred
    # cli`` before handing ``raw`` to the orchestrator).
    config_path: str | None = None


# --- Recursive builder ---

_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "anthropic": AnthropicConfig,
    "stt": STTConfig,
    # R4. Unique within this map (no other block carries a ``calibration``
    # sub-field), so ``_build`` recurses it without the collision footgun; every
    # field defaults, so the empty-dict-into-required-field trap is moot.
    "calibration": CalibrationConfig,
    # ``shadow_capture`` is a sub-block of ``stt`` — its name is unique
    # WITHIN THIS MAP (web's same-named block is hand-rolled by
    # ``web/config.py:_build_shadow_capture`` and never reaches here), so
    # registering it lets ``_build`` recurse stt.shadow_capture →
    # SttShadowCaptureConfig without the collision footgun. All its fields
    # default, so the empty-dict trap is moot (omitted block → enabled=False).
    # RETIRED capability (R2, 2026-08-20) — this entry is load-bearing for
    # BACK-COMPAT ONLY: without it a YAML carrying the block builds a plain
    # dict into the field instead of the dataclass. Kept with the field.
    "shadow_capture": SttShadowCaptureConfig,
    "session": SessionConfig,
    "logging": LoggingConfig,
    "instance": InstanceConfig,
    "tts": TtsConfig,
    "bash_exec": BashExecConfig,
    "idle_tick": IdleTickConfig,
    "vision": VisionConfig,
    "fiction": FictionConfig,
    "voice_train": VoiceTrainConfig,
    "today_command": TodayCommandConfig,
}


# List-valued config keys whose items are dicts to build into a dataclass.
# (The scalar ``_DATACLASS_MAP`` recurses into dict values; this handles
# list-of-dict values like ``stt.chain`` → list[SttBackendConfig].)
_LIST_DATACLASS_MAP: dict[str, type] = {
    "chain": SttBackendConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict."""
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in _DATACLASS_MAP and isinstance(value, dict):
            kwargs[key] = _build(_DATACLASS_MAP[key], value)
        elif key in _LIST_DATACLASS_MAP and isinstance(value, list):
            item_cls = _LIST_DATACLASS_MAP[key]
            kwargs[key] = [
                _build(item_cls, item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path = "config.yaml") -> TalkerConfig:
    """Load and parse config.yaml into TalkerConfig."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    cfg = load_from_unified(raw)
    # Stamp the resolved path onto the config so lazy loaders (the
    # inter-instance peer-tool dispatcher) re-read the SAME file we
    # just loaded — see ``TalkerConfig.config_path`` for the rationale.
    cfg.config_path = str(config_path.resolve())
    return cfg


def load_from_unified(raw: dict[str, Any]) -> TalkerConfig:
    """Build TalkerConfig from a pre-loaded unified config dict."""
    from alfred.vault.config_helpers import normalize_vault_block

    raw = _substitute_env(raw)
    tool = dict(raw.get("telegram", {}) or {})
    vault_raw = normalize_vault_block(raw.get("vault", {}) or {})
    # Strip vault keys that don't exist on our trimmed VaultConfig.
    vault_raw.pop("inbox_dir", None)
    vault_raw.pop("processed_dir", None)
    vault_raw.pop("ignore_files", None)
    # Map unified logging.dir -> logging.file
    log_raw = dict(raw.get("logging", {}) or {})
    log_dir = log_raw.pop("dir", "./data")
    if "file" not in log_raw:
        log_raw["file"] = f"{log_dir}/talker.log"

    # wk2b c5: the ``tts`` section is optional. If omitted we leave the
    # field as ``None`` so health probes + /brief handler can distinguish
    # "not configured" from "configured with empty values".
    tts_raw = tool.get("tts")
    built = _build(TalkerConfig, {
        "bot_token": tool.get("bot_token", ""),
        # VERA MVP: normalize the mixed (bare-int OR role-dict) allowlist
        # into list[AllowedUser] BEFORE _build. _build assigns it directly
        # (allowed_users isn't a _DATACLASS_MAP key and the value is a
        # list, not a dict, so no recursion fires). Bare-int entries
        # become role "owner" — back-compat for flat-list instances.
        "allowed_users": _normalize_allowed_users(
            tool.get("allowed_users", []) or []
        ),
        "primary_users": tool.get("primary_users", []) or [],
        # Peer-precedence label style — default "words" when absent.
        "precedence_label_style": str(
            tool.get("precedence_label_style", "words") or "words"
        ),
        "anthropic": tool.get("anthropic", {}) or {},
        "stt": tool.get("stt", {}) or {},
        # R4 — an absent block builds the all-defaults dataclass (both switches
        # off), so the loop stays dormant for every instance that hasn't opted in.
        "calibration": tool.get("calibration", {}) or {},
        "session": tool.get("session", {}) or {},
        "vault": vault_raw,
        "logging": log_raw,
        "instance": tool.get("instance", {}) or {},
    })
    if isinstance(tts_raw, dict) and tts_raw:
        built.tts = _build(TtsConfig, tts_raw)
    bash_raw = tool.get("bash_exec")
    if isinstance(bash_raw, dict) and bash_raw:
        built.bash_exec = _build(BashExecConfig, bash_raw)
    # Idle-tick — defaulted-on; if the user provides a partial dict
    # (just ``enabled: false``), merge over the dataclass default.
    idle_raw = tool.get("idle_tick")
    if isinstance(idle_raw, dict):
        built.idle_tick = _build(IdleTickConfig, idle_raw)
    # Vision — defaulted-on; partial-dict merge mirrors idle_tick. A
    # YAML block of ``vision: {enabled: false}`` preserves the default
    # ``disabled_reply`` text without forcing the operator to copy it.
    vision_raw = tool.get("vision")
    if isinstance(vision_raw, dict):
        built.vision = _build(VisionConfig, vision_raw)
    # Fiction — defaulted-OFF / None sentinel. Only constructs the
    # FictionConfig when the block is explicitly present in YAML, so
    # ``health.py`` can tell "Hypatia opted in" from "Salem omitted".
    fiction_raw = tool.get("fiction")
    if isinstance(fiction_raw, dict) and fiction_raw:
        built.fiction = _build(FictionConfig, fiction_raw)
    # Voice/method training — defaulted-OFF / None sentinel. Same shape
    # as fiction. Block-absent means commands NOT registered; block
    # present with explicit ``command_enabled: true`` registers /train
    # + /method-source AND starts the extraction worker.
    voice_train_raw = tool.get("voice_train")
    if isinstance(voice_train_raw, dict) and voice_train_raw:
        built.voice_train = _build(VoiceTrainConfig, voice_train_raw)
    # Today command — defaulted-OFF / None sentinel. Kept for its
    # ``timezone`` (see :class:`TodayCommandConfig`). A
    # ``telegram.moc_suggestions`` or ``telegram.inventory_views`` block
    # in an old YAML is silently ignored (their gates died with the
    # Telegram retirement, T5 2026-08-19); this loader only reads the
    # keys it names.
    today_command_raw = tool.get("today_command")
    if isinstance(today_command_raw, dict) and today_command_raw:
        built.today_command = _build(
            TodayCommandConfig, today_command_raw,
        )
    # Synthetic ``_config_path`` key — set by the CLI in ``cmd_up`` /
    # other entry points before handing ``raw`` to the orchestrator,
    # carried through ``multiprocessing`` pickling to subprocess
    # daemons. See ``TalkerConfig.config_path`` for the rationale.
    raw_path = raw.get("_config_path")
    if isinstance(raw_path, str) and raw_path:
        built.config_path = raw_path
    return built

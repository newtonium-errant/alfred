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


@dataclass
class STTConfig:
    provider: str = "groq"
    api_key: str = ""
    model: str = "whisper-large-v3"


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
    ``aliases`` is the multi-instance router's case-insensitive accept
    list so phone-autocorrect / voice-transcription variants still route
    correctly (``"Salem"`` → S.A.L.E.M., ``"Pat"`` → Hypatia) without a
    code change.

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
class InventoryViewsConfig:
    """Per-instance gate for the ``/questions`` + ``/research-pointers``
    slash commands (Phase 4 Sub-arc C, 2026-05-18).

    Default ``False`` so Salem (and any other operational-vault instance)
    never accidentally registers the commands. Hypatia opts in via
    ``telegram.inventory_views.command_enabled: true`` in
    ``config.hypatia.yaml`` because her vault layout has the
    ``question/`` + ``research-pointer/`` directories these commands
    surface.

    Conditional registration: when ``command_enabled=False`` (or the
    ``inventory_views`` block is absent entirely), neither slash
    command is registered — Telegram's "unknown command" behaviour
    fires for instances that legitimately don't surface inventory MOCs.

    Mirror of :class:`FictionConfig` + :class:`VoiceTrainConfig` shape —
    same Hypatia-only-by-default convention. See
    ``project_hypatia_zettelkasten_redesign.md`` Sub-arc C for the
    arc spec.
    """

    command_enabled: bool = False
    # Per-MOC-group cap on rendered bullets. Default 20 — anything
    # higher and the Telegram-side rendering starts to scroll past the
    # operator's attention. Operator-tunable for low-density vaults
    # that want everything in one view; high-density vaults can keep
    # the default and rely on the MOC/_*.md inventory MOC for full
    # state (the slash command is a glance-view, not exhaustive).
    per_group_cap: int = 20


@dataclass
class MocSuggestionsConfig:
    """Per-instance gate for the ``/moc-suggestions`` + ``/accept-moc`` +
    ``/reject-moc`` slash commands (Phase 5 Sub-arc D2, 2026-05-19).

    Default ``False`` so Salem + KAL-LE never accidentally register the
    commands. Hypatia opts in via
    ``telegram.moc_suggestions.command_enabled: true`` in
    ``config.hypatia.yaml`` because her vault is the only one running
    the surveyor MOC-suggestion stage (D1 ship; ``surveyor.moc_suggestion.enabled=true``
    in her config).

    Conditional registration: when ``command_enabled=False`` (or the
    ``moc_suggestions`` block is absent entirely), none of the three
    slash commands are registered — Telegram's "unknown command"
    behaviour fires for instances that legitimately don't have a queue.

    Mirror of :class:`InventoryViewsConfig` + :class:`FictionConfig` +
    :class:`VoiceTrainConfig` shape — same Hypatia-only-by-default
    convention. See ``project_hypatia_zettelkasten_redesign.md`` Phase 5
    Sub-arc D for the arc spec.
    """

    command_enabled: bool = False
    #: Path to the surveyor's MOC suggestion JSONL queue. None means
    #: derive from a sensible default at handler-call time — but the
    #: handler needs an explicit hint because the bot doesn't have a
    #: direct reference to the surveyor's state path. Operator sets
    #: this in config.hypatia.yaml to match her surveyor.state.path
    #: parent. Same pattern as :class:`VoiceTrainConfig.queue_path` —
    #: explicit JSONL path operator-set per instance.
    queue_path: str | None = None


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
class TalkerConfig:
    bot_token: str = ""
    allowed_users: list[int] = field(default_factory=list)
    primary_users: list[str] = field(default_factory=list)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    stt: STTConfig = field(default_factory=STTConfig)
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
    # Inventory views gate — see :class:`InventoryViewsConfig`.
    # Default-OFF / None sentinel matching fiction + voice_train shape.
    # Hypatia is Phase 4 Sub-arc C's only opt-in; the /questions +
    # /research-pointers commands surface her question/ and
    # research-pointer/ records (types Salem + KAL-LE don't have).
    inventory_views: InventoryViewsConfig | None = None
    # MOC suggestions gate — see :class:`MocSuggestionsConfig`.
    # Default-OFF / None sentinel matching the inventory_views shape.
    # Hypatia is Phase 5 Sub-arc D2's only opt-in; the
    # /moc-suggestions + /accept-moc + /reject-moc commands consume
    # the JSONL queue written by surveyor's Stage 8 (D1 ship). Salem +
    # KAL-LE leave this block absent.
    moc_suggestions: MocSuggestionsConfig | None = None
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
    "session": SessionConfig,
    "logging": LoggingConfig,
    "instance": InstanceConfig,
    "tts": TtsConfig,
    "bash_exec": BashExecConfig,
    "idle_tick": IdleTickConfig,
    "vision": VisionConfig,
    "fiction": FictionConfig,
    "voice_train": VoiceTrainConfig,
    "inventory_views": InventoryViewsConfig,
    "moc_suggestions": MocSuggestionsConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict."""
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in _DATACLASS_MAP and isinstance(value, dict):
            kwargs[key] = _build(_DATACLASS_MAP[key], value)
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
        "allowed_users": tool.get("allowed_users", []) or [],
        "primary_users": tool.get("primary_users", []) or [],
        "anthropic": tool.get("anthropic", {}) or {},
        "stt": tool.get("stt", {}) or {},
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
    # Inventory views — defaulted-OFF / None sentinel. Same shape as
    # fiction + voice_train. Block-absent means the /questions +
    # /research-pointers commands are NOT registered.
    inventory_views_raw = tool.get("inventory_views")
    if isinstance(inventory_views_raw, dict) and inventory_views_raw:
        built.inventory_views = _build(
            InventoryViewsConfig, inventory_views_raw,
        )
    # MOC suggestions — defaulted-OFF / None sentinel. Same shape as
    # inventory_views. Block-absent means the /moc-suggestions +
    # /accept-moc + /reject-moc commands are NOT registered.
    moc_suggestions_raw = tool.get("moc_suggestions")
    if isinstance(moc_suggestions_raw, dict) and moc_suggestions_raw:
        built.moc_suggestions = _build(
            MocSuggestionsConfig, moc_suggestions_raw,
        )
    # Synthetic ``_config_path`` key — set by the CLI in ``cmd_up`` /
    # other entry points before handing ``raw`` to the orchestrator,
    # carried through ``multiprocessing`` pickling to subprocess
    # daemons. See ``TalkerConfig.config_path`` for the rationale.
    raw_path = raw.get("_config_path")
    if isinstance(raw_path, str) and raw_path:
        built.config_path = raw_path
    return built

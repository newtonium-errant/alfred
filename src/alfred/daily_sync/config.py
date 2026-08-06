"""Daily Sync config — typed dataclasses + ``load_from_unified``.

Per-instance config block at the top level of the unified config:

```yaml
daily_sync:
  enabled: true
  schedule:
    time: "09:00"
    timezone: "America/Halifax"
  batch_size: 5
  corpus:
    path: "./data/email_calibration.salem.jsonl"
  confidence:
    high: false
    medium: false
    low: false
    spam: false
    filing: false          # #7 7c-i — topical-filing-axis gate (consumer: 7c-ii Gmail label write)
  state:
    path: "./data/daily_sync_state.json"
```

When the block is absent (or ``enabled: false``) the orchestrator does
not start the Daily Sync daemon, the slash commands report "not
configured", and the email classifier's few-shot rotation is silently
disabled (no corpus to read from).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from alfred.common.schedule import ScheduleConfig
from alfred.routine.match_calibration import (
    DEFAULT_CORPUS_PATH as _ROUTINE_MATCH_CORPUS_DEFAULT,
    DEFAULT_PENDING_MAX_AGE_DAYS as _ROUTINE_MATCH_MAX_AGE_DEFAULT,
    DEFAULT_PENDING_MAX_ITEMS as _ROUTINE_MATCH_MAX_ITEMS_DEFAULT,
    DEFAULT_PENDING_PATH as _ROUTINE_MATCH_PENDING_DEFAULT,
)
from alfred.telegram.config import (
    DEFAULT_STT_VOCAB_CORPUS_PATH as _STT_VOCAB_CORPUS_DEFAULT,
    DEFAULT_STT_VOCAB_DECIDED_PATH as _STT_VOCAB_DECIDED_DEFAULT,
    DEFAULT_STT_VOCAB_TERMS as _STT_VOCAB_TERMS_DEFAULT,
)

log = structlog.get_logger(__name__)

ENV_RE = re.compile(r"\$\{(\w+)\}")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders with environment variables."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


@dataclass
class CorpusConfig:
    """Path to the per-instance calibration corpus.

    Append-only JSONL. One row per Andrew-confirmed (or Andrew-corrected)
    classifier item. The Daily Sync writer appends; the classifier
    reader rotates the tail into its few-shot example slots.
    """

    path: str = "./data/email_calibration.salem.jsonl"


@dataclass
class ConfidenceConfig:
    """Confidence flags — priority tiers + the filing-axis gate.

    Flipped via the ``/calibration_ok <flag>`` Telegram command and
    persisted to a small state file (NOT this dataclass — the dataclass
    only holds the seed values from config). The flags are read by
    surfacing consumers (c3/c4/c5) to gate per-tier surfacing on
    Andrew's explicit approval.

    ``filing`` (#7 7c-i) is NOT a priority tier — it's the topical-filing
    axis gate. Additive + defaults False; built-before-consumer (the
    consumer is the 7c-ii Gmail-side label write, hard-gated on this
    flag). Nothing auto-flips it; only ``/calibration_ok filing`` does.
    """

    high: bool = False
    medium: bool = False
    low: bool = False
    spam: bool = False
    filing: bool = False


@dataclass
class StateConfig:
    """Path to the Daily Sync state file.

    Holds: last-fired date, last batch (item index → record path), the
    Telegram message_id sequence of the most recent push (so the reply
    parser can match), and the persisted per-tier confidence flags.
    """

    path: str = "./data/daily_sync_state.json"


@dataclass
class AttributionConfig:
    """Attribution-audit section provider config (Phase 2 of audit arc).

    The Daily Sync's attribution-audit section reads
    ``attribution_audit`` frontmatter from across the vault and surfaces
    unconfirmed items for Andrew's per-item ``confirm`` / ``reject``.
    See ``src/alfred/daily_sync/attribution_section.py`` for the
    section provider and ``src/alfred/vault/attribution.py`` for the
    underlying primitives shipped in c1.

    ``scan_paths`` is empty by default → the section walks the whole
    vault. Restrict for performance once vault grows past ~10k records;
    typical entries are vault-relative subpaths like ``["note", "person"]``.
    """

    enabled: bool = True
    batch_size: int = 5
    scan_paths: list[str] = field(default_factory=list)
    # Audit corpus path — separate from the email calibration corpus
    # so the two streams stay independently auditable. Append-only
    # JSONL; one row per Andrew confirm or reject. The path is a
    # default; the production config may override it.
    corpus_path: str = "./data/attribution_audit_corpus.jsonl"


@dataclass
class FrictionThresholdsConfig:
    """Detection thresholds for the friction analyzer (K3 c1).

    Each threshold gates one friction-event category. Defaults match
    the K3 spec (3 failures / 5 successes / 24h window). Bumping any
    of these post-deploy raises the bar (fewer events surface);
    lowering it floods the queue. Tune based on operator-feedback
    signal-to-noise.
    """

    failed_pattern_count: int = 3
    repeated_pattern_count: int = 5
    window_hours: int = 24


@dataclass
class FrictionAnalyzerConfig:
    """Friction analyzer (K3 c1) config block.

    Reads KAL-LE's bash_exec.jsonl audit log, scores friction events
    along three categories (failed_pattern / repeated_pattern /
    missing_tool), and appends to the friction log file the section
    provider (K3 c2) reads from.

    ``audit_log_path`` empty string means "fall back to
    ``telegram.bash_exec.audit_path`` from the unified config" — this
    is the production case for KAL-LE. Operators can override per-
    instance for testing or split-log scenarios.

    ``log_path`` is the friction-event JSONL the section provider
    will read. Append-only; one row per friction event.

    ``enabled: false`` (default) is the per-instance opt-in switch.
    KAL-LE is the first instance to flip it on; Salem and Hypatia
    leave it absent (no bash_exec audit log → no friction surface).
    """

    enabled: bool = False
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="07:30", timezone="America/Halifax",
        )
    )
    audit_log_path: str = ""
    log_path: str = "./data/kalle_friction_log.jsonl"
    thresholds: FrictionThresholdsConfig = field(
        default_factory=FrictionThresholdsConfig,
    )


@dataclass
class RoutineMatchConfig:
    """Self-correcting routine matcher — Daily Sync surface (Phase 1).

    ``enabled`` defaults OFF — instances opt in via
    ``daily_sync.routine_match.enabled: true`` (Salem is the first, routine
    being Salem-only).

    **pending_path single-source contract (reviewer NOTE #2).** The routine
    CLI WRITES this capture sink (``routine.cli.cmd_done``); this Daily Sync
    section READS it — they MUST be the SAME file. Rather than hold an
    independently-defaulted duplicate (which silently drifts if an operator
    overrides ``routine.match_calibration.pending_path`` but forgets this one),
    :func:`load_from_unified` DERIVES this field from the routine tool's
    resolved config at LOAD time: absent an explicit
    ``daily_sync.routine_match.pending_path``, it tracks
    ``routine.match_calibration.pending_path`` (including operator overrides).
    The dataclass default below stays the shared
    ``routine.match_calibration.DEFAULT_PENDING_PATH`` constant as the
    final fallback. An explicit ``daily_sync.routine_match.pending_path`` is
    still honoured (for the operator who genuinely wants them split — an
    intentional, non-silent choice).
    """

    enabled: bool = False
    pending_path: str = _ROUTINE_MATCH_PENDING_DEFAULT
    # Same single-source contract as pending_path, for the three values the
    # review filter needs (``match_calibration.filter_pending_for_review``).
    # The corpus is what reply_dispatch WRITES operator verdicts to and what
    # this section must READ to stop re-surfacing already-ruled-on rows — a
    # drift between the two is exactly the bug the filter closes, so it is
    # derived from the routine config rather than independently defaulted.
    corpus_path: str = _ROUTINE_MATCH_CORPUS_DEFAULT
    pending_max_age_days: int = _ROUTINE_MATCH_MAX_AGE_DEFAULT
    pending_max_items: int = _ROUTINE_MATCH_MAX_ITEMS_DEFAULT


@dataclass
class SttVocabConfig:
    """#54 — learned-speech-vocabulary review surface (Daily Sync).

    ``enabled`` defaults OFF, like every other judgment surface. It is a
    SEPARATE switch from ``telegram.stt.vocab_learning_enabled``: that one gates
    CAPTURE (writing the operator's message text to a corpus), this one gates
    the morning REVIEW card. An instance that captures but has not turned this
    on simply accumulates corrections until it does — nothing is lost, and the
    two decisions ("may it record me" / "show me the proposals") are genuinely
    different ones.

    FIELD NAMES DELIBERATELY MIRROR ``telegram.config.SttConfig``. That is not
    sloppiness: it makes this object duck-type as an stt-config, so the section
    can pass it straight to ``stt_vocab_learning.effective_vocab_terms`` — the
    ONE seam every vocabulary consumer goes through. Re-deriving the
    static ∪ learned union here would be a second implementation of the exact
    thing that seam exists to prevent.

    SINGLE-SOURCED FROM THE TALKER CONFIG, the same contract ``RoutineMatchConfig``
    holds for ``pending_path``. The web chat route WRITES the corpus and the CLI
    WRITES the decided store; this section READS both — they MUST be the same
    files. Rather than hold independently-defaulted duplicates that drift the
    moment an operator overrides the talker side, :func:`load_from_unified`
    DERIVES all three fields from ``telegram.stt`` at load time. An explicit
    ``daily_sync.stt_vocab.<field>`` still wins (an intentional, non-silent split).
    """

    enabled: bool = False
    #: Where capture appends (transcript, sent) pairs. Read-only here.
    vocab_corpus_path: str = _STT_VOCAB_CORPUS_DEFAULT
    #: The operator's approve/reject verdicts — read for BOTH the "don't re-ask"
    #: filter and the learned half of the currently-biasing list.
    vocab_decided_path: str = _STT_VOCAB_DECIDED_DEFAULT
    #: The SHIPPED static terms. Carried so the card can show the full list the
    #: operator is growing, not just the learned tail.
    vocab_terms: list[str] = field(default_factory=list)


@dataclass
class TierRecurrenceConfig:
    """#20 P5 — ad-hoc-T3 recurrence→promote proposals, Daily Sync surface (B1).

    ``enabled`` defaults OFF — a new judgment-making surface, opt-in per instance via
    ``daily_sync.tier_recurrence.enabled: true`` (Salem-personal, like routine_match). Detection scans
    the ``vault/daily/*.md`` ``done_at`` history (the #20 producer) and PROPOSES promoting a recurring
    ad-hoc chore to a routine record; the operator approves/rejects (B2). The pending + decided stores
    are SELF-OWNED (written AND read by this subsystem — no cross-tool single-source contract, unlike
    ``routine_match``'s pending_path). A chore checked off on ≥ ``threshold_done_days`` DISTINCT days
    within ``window_days`` triggers a proposal.
    """

    enabled: bool = False
    pending_path: str = "./data/tier_recurrence_pending.salem.jsonl"
    decided_path: str = "./data/tier_recurrence_decided.salem.jsonl"
    threshold_done_days: int = 3
    window_days: int = 30
    # B2 — the routine record a REPLY-approve places a promoted chore into. NO junk default: when unset
    # (""), reply-approve is DISABLED and the Daily Sync surface directs to the CLI
    # (``alfred tier-recurrence approve <id> --routine <name>``) — never a blind placement into an
    # auto-created generic record. The operator either configures a deliberate named home here (then
    # reply-approve is informed — the surface shows the target + cadence) or names the record per-approve
    # via the CLI ``--routine``. (D1/D6 no-junk-default guard.)
    promote_routine: str = ""


@dataclass
class TicketNotifyConfig:
    """#22b — KAL-LE ticket → PWA-notify observability surface (Daily Sync).

    ``enabled`` defaults OFF — a per-instance opt-in (Salem is the first and,
    today, the only instance that HOSTS the web notify store the KAL-LE
    ticket→PWA-notify pipeline fans into). When ``true`` the Daily Sync grows
    a READ-ONLY "Ticket notifications" section that surfaces the ticket
    notices Salem received since the last sync, FAIL-LOUD: it renders an
    explicit ⚠️ when the pipeline can't be trusted (web surface off,
    notifications disabled, no operator to key to, or the store read failed)
    rather than a silent empty section — so the operator can confirm the
    #22 pipeline is live in production.

    Operator directive (2026-07): *"to start i want it included in the daily
    sync so I can see if it's happening. fail loud."* Read-only by design —
    the PWA notification tray still OWNS read/ack; this section never mutates
    the store. Instances that don't opt in (KAL-LE / Hypatia) leave this
    absent → the provider returns None and the section is omitted, so their
    Daily Sync is byte-unchanged (no false ⚠️).

    The store path is DERIVED (not configured): ``<state.path>.parent /
    web_notify_state.json`` — the same ``data_dir`` convention the #30 web
    outbound spool uses (``write_latest(Path(config.state.path).parent, …)``),
    which is the cross-process contract with the talker daemon that WRITES
    the store. Add an explicit ``store_path`` override here only if a deploy
    ever splits the daily_sync state dir from the talker data_dir.
    """

    enabled: bool = False
    # Optional explicit override of the derived store path. Empty string =
    # derive from ``<state.path>.parent / web_notify_state.json`` (the
    # production case). Present only for split-dir deploys / tests.
    store_path: str = ""


@dataclass
class DailySyncConfig:
    """Top-level Daily Sync config.

    ``enabled`` is the master switch — when False, the orchestrator skips
    starting the daemon and slash commands reply "not configured".
    """

    enabled: bool = False
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(time="09:00", timezone="America/Halifax"),
    )
    batch_size: int = 5
    corpus: CorpusConfig = field(default_factory=CorpusConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    state: StateConfig = field(default_factory=StateConfig)
    attribution: AttributionConfig = field(default_factory=AttributionConfig)
    # Friction analyzer / queue (K3) — defaulted-OFF; instances opt
    # in via ``daily_sync.friction_analyzer.enabled: true``. KAL-LE is
    # the first such instance.
    friction_analyzer: FrictionAnalyzerConfig = field(
        default_factory=FrictionAnalyzerConfig,
    )
    # Self-correcting routine matcher surface (Phase 1) — defaulted-OFF;
    # Salem opts in via ``daily_sync.routine_match.enabled: true``.
    routine_match: RoutineMatchConfig = field(
        default_factory=RoutineMatchConfig,
    )
    # #54 — learned-speech-vocabulary proposals. Defaulted-OFF; an instance opts
    # in via ``daily_sync.stt_vocab.enabled: true``. Paths + static terms are
    # derived from ``telegram.stt`` at load time (see SttVocabConfig).
    stt_vocab: SttVocabConfig = field(
        default_factory=SttVocabConfig,
    )
    # #20 P5 — ad-hoc-T3 recurrence→promote proposal surface (B1). Defaulted-OFF;
    # Salem opts in via ``daily_sync.tier_recurrence.enabled: true``.
    tier_recurrence: TierRecurrenceConfig = field(
        default_factory=TierRecurrenceConfig,
    )
    # #22b — KAL-LE ticket → PWA-notify observability surface. Defaulted-OFF;
    # Salem opts in via ``daily_sync.ticket_notify.enabled: true`` (it hosts
    # the web notify store the pipeline fans into). Read-only fail-loud.
    ticket_notify: TicketNotifyConfig = field(
        default_factory=TicketNotifyConfig,
    )
    # Path to the config file this DailySyncConfig was loaded from.
    # Carried so lazy/late loaders (the canonical-proposals queue-path
    # helpers in ``canonical_proposals_section`` and ``reply_dispatch``)
    # can re-read the SAME config file at call time rather than
    # defaulting to ``config.yaml``. Without this, a Hypatia daily_sync
    # daemon (started with ``--config config.hypatia.yaml``) would have
    # its queue-path helpers silently fall back to Salem's transport
    # config and look up the wrong proposals JSONL. ``None`` is the
    # backward-compat default — populated by :func:`load_config` (path
    # arg known directly) and by :func:`load_from_unified` when the raw
    # dict carries the synthetic ``_config_path`` key (set by the CLI in
    # ``_load_unified_config`` before handing ``raw`` to the
    # orchestrator). Mirrors ``TalkerConfig.config_path`` shipped in
    # commit 420364b — same bug class, two more sites.
    config_path: str | None = None


_DATACLASS_MAP: dict[str, type] = {
    "schedule": ScheduleConfig,
    "corpus": CorpusConfig,
    "confidence": ConfidenceConfig,
    "state": StateConfig,
    "attribution": AttributionConfig,
    "friction_analyzer": FrictionAnalyzerConfig,
    "thresholds": FrictionThresholdsConfig,
    "routine_match": RoutineMatchConfig,
    "stt_vocab": SttVocabConfig,
    "tier_recurrence": TierRecurrenceConfig,
    "ticket_notify": TicketNotifyConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    Unknown top-level keys are ignored so a future schema bump on
    ``config.yaml.example`` doesn't break parsing on installs pinned to
    an older copy of this code.
    """
    field_names = {f.name for f in cls.__dataclass_fields__.values()}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in field_names:
            continue
        if key in _DATACLASS_MAP and isinstance(value, dict):
            kwargs[key] = _build(_DATACLASS_MAP[key], value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_from_unified(raw: dict[str, Any]) -> DailySyncConfig:
    """Build a DailySyncConfig from the unified config dict.

    Returns a default-constructed (``enabled=False``) config when the
    ``daily_sync`` block is absent. Callers can rely on ``.enabled`` to
    decide whether to wire downstream work.
    """
    raw = _substitute_env(raw)
    section = raw.get("daily_sync", {}) or {}
    if not section:
        cfg = DailySyncConfig(enabled=False)
    else:
        cfg = _build(DailySyncConfig, section)
    # Single-source the routine_match pending_path (reviewer NOTE #2). The
    # routine CLI WRITES this capture sink; this section READS it — same file.
    # Derive the default from the routine tool's resolved config so an operator
    # override of ``routine.match_calibration.pending_path`` propagates here
    # instead of silently drifting. An explicit
    # ``daily_sync.routine_match.pending_path`` is honoured (intentional split).
    # See ``RoutineMatchConfig`` for the full contract; mirrors the Phase-2b
    # corpus_path single-source (reply_dispatch reads the routine config).
    rm_section = section.get("routine_match") if isinstance(section, dict) else None

    def _rm_explicit(field_name: str) -> bool:
        return isinstance(rm_section, dict) and field_name in rm_section

    # Every field below follows the same single-source contract; an explicit
    # ``daily_sync.routine_match.<field>`` still wins (intentional split).
    _derived = ("pending_path", "corpus_path", "pending_max_age_days", "pending_max_items")
    if not all(_rm_explicit(f) for f in _derived):
        try:
            from alfred.routine.config import load_from_unified as _load_routine

            _mc = _load_routine(raw).match_calibration
            for _field in _derived:
                if not _rm_explicit(_field):
                    setattr(cfg.routine_match, _field, getattr(_mc, _field))
        except Exception as exc:  # noqa: BLE001
            # Never let routine-config resolution break daily_sync load — keep
            # the dataclass default (the shared constant, which is also what the
            # routine tool defaults to, so they still match in the no-override
            # case). Emit a debug line (reviewer ILB note on 9b89cb7): a real
            # routine-config breakage would silently re-introduce the read/write
            # drift via the constant fallback — this makes that diagnosable.
            log.debug(
                "daily_sync.routine_match.config_derive_failed",
                fields=list(_derived),
                error=str(exc),
            )
    # #54 — single-source the STT vocabulary fields from the talker config, the
    # same contract routine_match holds above. The web chat route WRITES the
    # corpus and the CLI WRITES the decided store off ``telegram.stt``; this
    # section READS both, and the static term list is what the review card shows
    # the operator he is growing. Three values that must agree across two config
    # blocks, so they are DERIVED rather than independently defaulted. An
    # explicit ``daily_sync.stt_vocab.<field>`` still wins (intentional split).
    sv_section = section.get("stt_vocab") if isinstance(section, dict) else None

    def _sv_explicit(field_name: str) -> bool:
        return isinstance(sv_section, dict) and field_name in sv_section

    # Read the ``telegram.stt`` BLOCK directly rather than building a whole
    # TalkerConfig. Measured, not assumed: ``telegram.config.load_from_unified``
    # requires an ``instance.name``, so on a config that has an stt block but no
    # instance block it raises — and the fallback would silently point this
    # section at the DEFAULT paths while capture wrote the overridden ones.
    # Reading three keys cannot fail that way, and ``raw`` is already
    # env-substituted above, so ``${VAR}`` paths still resolve.
    #
    # THE KEY IS ``telegram``, NOT ``talker``. The tool is *called* the talker,
    # its config block is not: ``telegram.config.load_from_unified`` reads
    # ``raw.get("telegram")`` and config.yaml.example ships ``telegram:``. The
    # first cut read ``raw.get("talker")``, which never exists — so this derive
    # silently returned defaults on every real config, and capture wrote one
    # path while the review card read another. Exactly the drift this
    # single-source contract exists to prevent, introduced by the contract
    # itself. The both-loaders-agree pin in test_stt_vocab_section.py is what
    # holds it now: it drives BOTH real loaders over ONE raw dict, so no fixture
    # of mine can define the schema into agreement with my own mistake.
    _telegram = raw.get("telegram")
    _talker_stt = (_telegram.get("stt") if isinstance(_telegram, dict) else None) or {}
    if not isinstance(_talker_stt, dict):
        _talker_stt = {}
    # The talker's OWN defaults, so an instance that sets no stt block still
    # gets the same answer the talker itself would compute.
    _sv_fallback = {
        "vocab_corpus_path": _STT_VOCAB_CORPUS_DEFAULT,
        "vocab_decided_path": _STT_VOCAB_DECIDED_DEFAULT,
        "vocab_terms": list(_STT_VOCAB_TERMS_DEFAULT),
    }
    for _field, _default in _sv_fallback.items():
        if _sv_explicit(_field):
            continue  # an explicit daily_sync override wins
        setattr(cfg.stt_vocab, _field, _talker_stt.get(_field, _default))
    # Synthetic ``_config_path`` key — set by the CLI in
    # ``_load_unified_config`` before handing ``raw`` to the
    # orchestrator, carried through ``multiprocessing`` pickling to
    # subprocess daemons. See ``DailySyncConfig.config_path`` for the
    # rationale (mirrors TalkerConfig.config_path shipped in 420364b).
    raw_path = raw.get("_config_path")
    if isinstance(raw_path, str) and raw_path:
        cfg.config_path = raw_path
    return cfg


def load_config(path: str | Path = "config.yaml") -> DailySyncConfig:
    """Load and parse a config file (test helper).

    Stamps the resolved absolute path onto ``cfg.config_path`` so lazy
    loaders (the canonical-proposals queue-path helpers) re-read the
    SAME file we just loaded — see ``DailySyncConfig.config_path`` for
    the rationale.
    """
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = load_from_unified(raw or {})
    cfg.config_path = str(config_path.resolve())
    return cfg

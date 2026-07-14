"""Sovereign ambient-scribe config (scribe P1-c).

Typed view of the ``scribe:`` config block for the STAY-C sovereign scribe.
P1-c ships the CONFIG + the mode-gate + the attestation-integrity scaffold; it
does NOT wire the audio→note pipeline (that is P2) and it does NOT register a
daemon (that is P1-d).

Hand-rolled construction — deliberately NOT the shared ``_build`` /
``_DATACLASS_MAP`` dispatch. Per the CLAUDE.md ``_build`` traps: (1) the
collision footgun — keys like ``state`` / ``agent`` / ``stt`` dispatch to
whatever a global map holds (``telegram``'s map maps ``stt`` → its OWN
STTConfig); and (2) the empty-dict-into-required-field trap. Hand-rolling with
a per-dataclass schema-tolerance filter (``__dataclass_fields__``) sidesteps
BOTH: no key-name dispatch, and every field carries a default so an empty
``{}`` never crashes the constructor.

Fail-closed legal line: ``mode`` normalizes to ``"synthetic"`` unless the
config carries the EXACT string ``"clinical"``. Absent / None / unknown /
malformed all resolve to ``"synthetic"`` — the safe default. Flipping
synthetic→clinical is a single deliberate config edit; nothing else in the
pipeline hard-codes ``"clinical"`` (see ``ingest.guard_ingest``).

SUB-FIELD LOOPBACK DISCIPLINE (P1-a r2 carry-forward). The sovereign boundary
(``alfred.sovereign.boundary``) validates ``scribe.stt`` (barrier a — provider
on the local allowlist), ``scribe.llm`` (barrier b — base_url loopback), and
``scribe.diarize`` (barrier-a sibling — provider on the local diarize allowlist
{off, fake, pyannote}). If this config ever gains a NEW network-capable
sub-field (a future ``scribe.tts`` / ``scribe.summarizer`` / ``scribe.embed``),
it MUST get a matching barrier-a/b-style loopback/local check in boundary.py IN
THE SAME CHANGE — do not add a network sub-field without it. The current fields
below are local-only by construction (no api_key, no cloud endpoint).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alfred._env import substitute_env_in_value

# The two legal modes. ``synthetic`` is the fail-closed default (only
# synthetic-tagged input flows); ``clinical`` is the LAST switch, gated on a
# legal de-id/attestation standard.
SCRIBE_MODE_SYNTHETIC = "synthetic"
SCRIBE_MODE_CLINICAL = "clinical"

_DEFAULT_INPUT_DIR = "./data/scribe/inbox"


@dataclass
class ScribeSttConfig:
    """Local STT config. Barrier (a) validates ``provider`` is on the local
    allowlist {faster-whisper, local-whisper, fake}. LOCAL-ONLY by design —
    NO api_key / endpoint field here. Adding a network sub-field requires a
    boundary-layer loopback check (see the module docstring)."""

    provider: str = ""
    model: str = ""


@dataclass
class ScribeLlmConfig:
    """Local LLM config (Ollama, loopback). Barrier (b) validates ``base_url``
    resolves to loopback. LOCAL-ONLY by design — the base_url must be
    127.0.0.1/localhost/::1 or the sovereign boundary refuses at load."""

    base_url: str = ""
    model: str = ""


@dataclass
class ScribeDiarizeConfig:
    """Local multi-speaker diarization config (scribe P4).

    ``provider`` dispatches the diarizer (``off`` = no diarization, the
    fail-closed default; ``fake`` = the deterministic CI backend;
    ``pyannote`` = the real on-box engine, P4-4). The sovereign boundary's
    barrier-a sibling (``_check_diarize_local``) independently validates
    ``provider`` is on :data:`~alfred.sovereign.SOVEREIGN_DIARIZE_ALLOWLIST`.

    LOCAL-BY-CONSTRUCTION — NO ``api_key`` / ``base_url`` / cloud-endpoint field
    (per the module docstring's SUB-FIELD LOOPBACK DISCIPLINE). pyannote loads
    its embedding model from the local HF cache (``local_files_only``, P4-4); the
    only network-shaped fields are the HF model id + revision, which name a
    CACHE entry, not an endpoint. Adding a real network sub-field here would
    require a matching boundary-layer loopback check IN THE SAME CHANGE.

    Thresholds are CONSERVATIVE FAIL-CLOSED-HIGH placeholders — a turn resolves
    to a KNOWN role only on a strong, unambiguous, high-purity match; anything
    softer degrades to ``unknown`` (un-attributed ≫ mis-attributed). They are
    PLACEHOLDERS pending on-box ``--calibrate`` (real DER/purity on the Ryzen +
    the room mic); do not treat these literals as tuned.
    """

    # Dispatch: off (default, no diarization) / fake (CI) / pyannote (P4-4).
    provider: str = "off"
    # Convenience flag (the provider is the real gate; ``off`` is already inert).
    enabled: bool = False
    # Cosine similarity a cluster centroid must reach vs the enrolled clinician
    # centroid to claim ``clinician`` — HIGH so a weak match fails to ``unknown``.
    match_threshold: float = 0.75
    # Required gap between the best and 2nd-best role candidate — HIGH so an
    # ambiguous (near-tie) assignment fails-closed to ``unknown``.
    separation_margin: float = 0.15
    # Minimum diarization cluster purity for a turn to carry a KNOWN role — HIGH
    # so a mixed/overlap turn degrades to ``unknown``.
    purity_threshold: float = 0.80
    # Turns shorter than this (seconds) are too short to embed reliably → unknown.
    min_turn_s: float = 1.0
    # Filesystem path to the clinician enrollment embedding (P4-5). Empty = no
    # enrollment yet → every cluster fails the match → all ``unknown`` (fail-safe).
    enrollment_path: str = ""
    # Local HF cache entry for the speaker-embedding model (loaded offline in
    # P4-4). Names a CACHE entry, NOT a network endpoint.
    embedding_model: str = "pyannote/wespeaker-voxceleb-resnet34-LM"
    # Pinned revision of the embedding model (cache lookup is revision-exact).
    embedding_revision: str = ""
    # Absolute filesystem path to the MATERIALIZED, repo-id-free pyannote pipeline
    # config YAML (P4-4). ``scripts.stage_diarize_models`` writes it at staging
    # time with the sub-models' ABSOLUTE LOCAL snapshot paths substituted for their
    # HF repo ids, because pyannote's ``from_pretrained`` does NOT reliably honor
    # ``local_files_only`` — a repo-id-bearing config still triggers a hub revision
    # GET. Loading FROM this materialized path is the PRIMARY offline control; the
    # engine ADDITIONALLY validates every model ref here is an existing local path
    # pre-import, and the SovereignHttpGuard + the systemd unit's PRE-IMPORT
    # ``HF_HUB_OFFLINE=1`` are the backstops (a RUNTIME env set is inert — see
    # ``diarize._validate_materialized_config_local``). LOCAL-BY-CONSTRUCTION:
    # a filesystem path, NOT a network endpoint (same class as ``enrollment_path`` /
    # ``embedding_model``) — no boundary barrier applies (SUB-FIELD LOOPBACK
    # DISCIPLINE, module docstring). Empty ⇒ the pyannote provider fails LOUD at
    # load-time (a real engine with no materialized offline config must not boot).
    pipeline_config: str = ""


# The complete, closed set of keys the ``scribe.ingest_web`` sub-tree may carry.
# Barrier (e) (boundary.py) refuses ANY other key when the server is enabled —
# a ``base_url`` / ``webhook`` / ``forward_to`` / cloud-endpoint field in this
# sub-tree would be an egress surface, so the sub-tree is allowlist-closed the
# same way barrier (d) closes the top-level sections. Keep this in lockstep with
# :class:`ScribeIngestWebConfig`'s fields.
INGEST_WEB_ALLOWED_KEYS: frozenset[str] = frozenset({
    "enabled", "host", "port", "token",
    "max_chunk_bytes", "max_chunks_per_encounter", "max_encounter_bytes",
})


def coerce_ingest_web_enabled(value: Any) -> bool:
    """Truthiness for the ``ingest_web.enabled`` flag — the SHARED contract used
    by BOTH the typed loader (``_build_ingest_web``) AND barrier (e) in the
    sovereign boundary. Sharing it guarantees "does the barrier validate the
    bind" == "does the server actually bind": a quoted ``enabled: "false"`` is
    inert in BOTH (not a false-positive barrier-e breach), and no value can bind
    the server without also arming the barrier."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


@dataclass
class ScribeIngestWebConfig:
    """The loopback PWA ingest server (#49 STAY-C, Slice A).

    INERT by default (``enabled: false``) — the daemon starts NO server unless
    the operator flips it on. LOOPBACK-ONLY by construction: barrier (e) in the
    sovereign boundary POSITIVELY asserts ``host`` is provably loopback at
    config-load (a ``0.0.0.0`` bind is a LAN-reachable PHI-ingest hole that must
    fail at the BARRIER, not merely at socket-bind → exit 79, non-restartable).
    NO cloud/egress field belongs here — the sub-tree is allowlist-closed
    (:data:`INGEST_WEB_ALLOWED_KEYS`); an unexpected key breaches barrier (e).

    The server WRITES chunk audio + ``.meta.json`` sidecars into the encounter
    inbox; the EXISTING sweep→accumulate→guard_ingest→local-STT→checkpoint
    pipeline consumes them. The server writes NOTHING to the ledger or
    ScribeState (the pipeline owns both) — it is a thin, write-only ingest face.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8760
    # Bearer token (``secrets.compare_digest``). Barrier (e) requires it PRESENT
    # when enabled — a tokenless loopback ingest face is refused at load. Set via
    # ``${SCRIBE_INGEST_TOKEN}`` (env substitution runs on this block), never
    # inline in a committed config.
    token: str = ""
    # N3 caps — an explicit, signalled ceiling (never a silent drop). aiohttp's
    # ``client_max_size`` is pinned to ``max_chunk_bytes``; the per-encounter
    # chunk-count + total-byte caps are enforced in the route with a "cap hit"
    # observability signal.
    max_chunk_bytes: int = 25 * 1024 * 1024          # 25 MiB per POST
    max_chunks_per_encounter: int = 4096
    max_encounter_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB per encounter


@dataclass
class ScribeConfig:
    """Typed ``scribe:`` block.

    ``mode`` is normalized at load to exactly ``synthetic`` or ``clinical``
    (fail-closed — see ``_normalize_mode``). ``stt`` / ``llm`` are the local
    engine configs the sovereign boundary independently validates.
    """

    mode: str = SCRIBE_MODE_SYNTHETIC
    input_dir: str = _DEFAULT_INPUT_DIR
    stt: ScribeSttConfig = field(default_factory=ScribeSttConfig)
    llm: ScribeLlmConfig = field(default_factory=ScribeLlmConfig)
    # P4 multi-speaker diarization (OFF by default). ``diarize.provider`` is
    # independently validated local by the sovereign boundary's barrier-a sibling.
    diarize: ScribeDiarizeConfig = field(default_factory=ScribeDiarizeConfig)
    # The loopback PWA ingest server (#49). INERT by default; barrier (e)
    # validates its host is loopback + token present when enabled.
    ingest_web: ScribeIngestWebConfig = field(default_factory=ScribeIngestWebConfig)
    # Designated human-clinician identities allowed to ATTEST a clinical_note
    # (scribe P2-a, #41). A plain identity list — NOT a network sub-field, so
    # no boundary barrier applies (barriers a/b gate only stt/llm). FAIL-CLOSED:
    # an empty list (the default) means NO valid attester — every attest is
    # refused (attester_not_clinician) until the operator populates it. The
    # scribe.attest orchestrator passes this to authorize_attestation.
    clinicians: list[str] = field(default_factory=list)
    # Per-instance SECRET that salts the opaque encounter_id (scribe P3-b1). The
    # id is ``"enc-" + hmac_sha256(encounter_salt, raw_label)[:16]`` — an UNsalted
    # sha256 of a low-entropy patient label is trivially reversible, so the salt
    # is what makes the opaque id non-reversible (defeats the P2 synthetic-label
    # leak). SECRET by construction: it is NEVER written to the vault, a log, or
    # the attest audit. FAIL-LOUD (not fail-closed-silent) the moment an
    # encounter_id is computed with an empty salt — a sovereign clinical scribe
    # cannot safely opaque PHI labels without it (see
    # ``identity.compute_encounter_id`` + ``feedback_hardcoding_and_alfred_naming``
    # fail-loud-on-missing-name). Set via ``${SCRIBE_ENCOUNTER_SALT}`` (env
    # substitution runs on this block), never inline in a committed config.
    encounter_salt: str = ""
    # #57 close-manifest — STRICT "ready ⇒ complete" enforcement opt-in. When True
    # (ALSO forced-on by clinical mode, see close_manifest.resolve_require_close_manifest),
    # the /close route REQUIRES a final_seq (400 otherwise, nothing written) and the
    # checkpoint gate treats a missing/ambiguous manifest as fail-closed (never
    # READY). DEFAULT False (inert): the shipped synthetic PWA's legacy empty-close
    # still finalizes to READY — the structural enforcement engages exactly at the
    # clinical (medico-legal) boundary #57 gates.
    require_close_manifest: bool = False
    # #57 STATE_INCOMPLETE terminal grace (seconds). DEFAULT 0 = the operator-visible
    # INCOMPLETE terminal is DISABLED; the ALWAYS-ON primary safety (a promised-but-
    # unarrived tail stays DRAFTED forever + emits close_awaiting_promised_seq every
    # sweep) fully satisfies the invariant. A POSITIVE value opts into marking such an
    # encounter STATE_INCOMPLETE once the _CLOSED sentinel is older than the grace
    # (surfaced status "incomplete — awaiting seq N"; RE-OPENABLE if the tail arrives).
    incomplete_grace_s: int = 0

    @property
    def is_clinical(self) -> bool:
        """True iff the config resolved to clinical mode (the legal line)."""
        return self.mode == SCRIBE_MODE_CLINICAL


def _normalize_mode(raw_mode: Any) -> str:
    """THE fail-closed legal line.

    ONLY the exact (case/space-insensitive) string ``"clinical"`` selects
    clinical mode. Absent / None / unknown / malformed / any other value =>
    ``"synthetic"``. An operator flips to clinical by writing exactly
    ``mode: clinical`` — a single deliberate edit. Everything else stays
    synthetic, so a typo, a truncated value, or an omitted key can never
    accidentally open the clinical path.
    """
    if isinstance(raw_mode, str) and raw_mode.strip().lower() == SCRIBE_MODE_CLINICAL:
        return SCRIBE_MODE_CLINICAL
    return SCRIBE_MODE_SYNTHETIC


def _build(cls, data: Any):
    """Schema-tolerant nested-dataclass build. Filters ``data`` against the
    target dataclass's known fields BEFORE construction (the load-time
    schema-tolerance contract) so an unknown/extra key never crashes the
    constructor, and a non-dict / None yields the all-defaults instance.
    Deliberately NOT key-name dispatch — no ``_DATACLASS_MAP`` collision."""
    if not isinstance(data, dict):
        return cls()
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    return cls(**known)


def load_from_unified(raw: dict[str, Any]) -> ScribeConfig:
    """Build :class:`ScribeConfig` from the unified config dict.

    Reads the ``scribe:`` block, substitutes ``${VAR}`` placeholders, and
    hand-rolls the nested dataclasses. An absent block yields the all-defaults
    config (mode=synthetic). ``${VAR}`` substitution runs here (typed-config
    layer), AFTER the sovereign boundary has already scanned the RAW config for
    cloud-key placeholders at load — so a sovereign config never has a cloud
    ${VAR} to substitute in the first place.
    """
    scribe = raw.get("scribe") or {}
    if not isinstance(scribe, dict):
        scribe = {}
    scribe = substitute_env_in_value(scribe)

    input_dir = scribe.get("input_dir") or _DEFAULT_INPUT_DIR
    clinicians_raw = scribe.get("clinicians") or []
    clinicians = [str(c) for c in clinicians_raw] if isinstance(clinicians_raw, list) else []
    return ScribeConfig(
        mode=_normalize_mode(scribe.get("mode")),
        input_dir=str(input_dir),
        stt=_build(ScribeSttConfig, scribe.get("stt")),
        llm=_build(ScribeLlmConfig, scribe.get("llm")),
        diarize=_build_diarize(scribe.get("diarize")),
        ingest_web=_build_ingest_web(scribe.get("ingest_web")),
        clinicians=clinicians,
        encounter_salt=str(scribe.get("encounter_salt") or ""),
        # #57 — scalar fields on ScribeConfig (no _build nesting): string-safe coerce
        # so a YAML ``"false"`` / ``"5"`` never slips a wrong-typed value through.
        require_close_manifest=coerce_ingest_web_enabled(
            scribe.get("require_close_manifest", False)),
        incomplete_grace_s=_coerce_nonneg_int(scribe.get("incomplete_grace_s"), 0),
    )


def _coerce_nonneg_int(value: Any, default: int) -> int:
    """Coerce ``value`` to a non-negative int (a nonsense value keeps the default —
    never crash the load)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def _build_diarize(data: Any) -> ScribeDiarizeConfig:
    """Schema-tolerant build of :class:`ScribeDiarizeConfig` with explicit type
    coercion (``enabled`` truthy→bool, thresholds→float) so a YAML string value
    (``enabled: "true"``, ``purity_threshold: "0.8"``) can't slip a wrong-typed
    field through. Unknown keys are dropped by the ``__dataclass_fields__`` filter
    (the load-time schema-tolerance contract); the sovereign boundary SEPARATELY
    validates ``provider`` is local (that is the security gate — this coercion is
    convenience, not the boundary). A nonsense threshold keeps the fail-closed-HIGH
    default (never crashes the load)."""
    if not isinstance(data, dict):
        return ScribeDiarizeConfig()
    known = {k: v for k, v in data.items() if k in ScribeDiarizeConfig.__dataclass_fields__}
    cfg = ScribeDiarizeConfig()
    if "enabled" in known:
        cfg.enabled = coerce_ingest_web_enabled(known["enabled"])
    for str_field in (
        "provider", "enrollment_path", "embedding_model", "embedding_revision",
        "pipeline_config",
    ):
        if str_field in known:
            # D4: a YAML null (``pipeline_config:`` with no value) is None, and
            # ``str(None)`` == "None" — a non-empty string that would SLIP PAST the
            # actionable empty-config fail-loud. Coerce None → "" so the boot gate +
            # the runtime check see it as unset and emit the staging instruction.
            v = known[str_field]
            setattr(cfg, str_field, "" if v is None else str(v))
    for float_field in ("match_threshold", "separation_margin", "purity_threshold", "min_turn_s"):
        if float_field in known:
            try:
                setattr(cfg, float_field, float(known[float_field]))
            except (TypeError, ValueError):
                pass  # keep the fail-closed-HIGH default; a nonsense value never crashes the load
    return cfg


def _build_ingest_web(data: Any) -> ScribeIngestWebConfig:
    """Schema-tolerant build of :class:`ScribeIngestWebConfig` with explicit type
    coercion (``enabled`` truthy→bool, ports/caps→int) so a YAML string value
    (``enabled: "true"``, ``port: "8760"``) can't slip a wrong-typed field into
    the server. Unknown keys are dropped by the ``__dataclass_fields__`` filter
    (the load-time schema-tolerance contract); barrier (e) SEPARATELY refuses an
    unknown key at load when the server is enabled (that is the security gate —
    this coercion is convenience, not the boundary)."""
    if not isinstance(data, dict):
        return ScribeIngestWebConfig()
    known = {k: v for k, v in data.items() if k in ScribeIngestWebConfig.__dataclass_fields__}
    cfg = ScribeIngestWebConfig()
    if "enabled" in known:
        cfg.enabled = coerce_ingest_web_enabled(known["enabled"])
    if "host" in known:
        cfg.host = str(known["host"])
    if "token" in known:
        cfg.token = str(known["token"])
    for int_field in ("port", "max_chunk_bytes", "max_chunks_per_encounter", "max_encounter_bytes"):
        if int_field in known:
            try:
                setattr(cfg, int_field, int(known[int_field]))
            except (TypeError, ValueError):
                pass  # keep the default; a nonsense value never crashes the load
    return cfg

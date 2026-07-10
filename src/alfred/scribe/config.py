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
on the local allowlist) and ``scribe.llm`` (barrier b — base_url loopback). If
this config ever gains a NEW network-capable sub-field (a future
``scribe.tts`` / ``scribe.summarizer`` / ``scribe.embed``), it MUST get a
matching barrier-a/b-style loopback/local check in boundary.py IN THE SAME
CHANGE — do not add a network sub-field without it. The current fields below
are local-only by construction (no api_key, no cloud endpoint).
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
        clinicians=clinicians,
        encounter_salt=str(scribe.get("encounter_salt") or ""),
    )

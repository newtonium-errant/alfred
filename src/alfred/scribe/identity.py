"""Salted, opaque encounter identity for the sovereign scribe (scribe P3-b1).

THE single identity primitive. An ``encounter_id`` is computed ONCE at ingest
from the operator's raw label (a per-encounter subdir name, or a legacy flat
file's basename) and flows — opaque — into the pipeline state, the logs, the
note title/filename, and the transcript-ledger filename.

    encounter_id = "enc-" + hmac_sha256(encounter_salt, raw_label)[:16]

Why HMAC-with-a-salt and not a bare ``sha256(raw_label)``: a low-entropy label
(a patient's name, a DOB, an MRN) has a tiny preimage space, so a bare hash is
trivially reversible by a dictionary/rainbow attack — the "opaque" id would leak
the very PHI it exists to hide. The per-instance secret salt (``scribe.encounter_salt``)
makes the id non-reversible without the salt. This closes the P2 leak where the
synthetic-mode ``source_id`` was the operator label VERBATIM (and the clinical
path's ``"sha256:"`` prefix put a colon into note filenames).

FAIL-LOUD on a missing OR UNRESOLVED salt (``feedback_hardcoding_and_alfred_naming``
fail-loud-on-missing-name): a sovereign clinical scribe that cannot safely opaque
a PHI label must STOP loudly, not silently fall back to an unsalted (reversible)
id or a per-instance literal. "Unresolved" covers the ``${SCRIBE_ENCOUNTER_SALT}``
env-placeholder that stays literal when the env var is unset (the ingest-time
compensating control for #67 F2's optional ``EnvironmentFile``). The guard fires
at the point of use (id computation), so config load stays tolerant for
non-scribe instances.

The salt is SECRET: it is an input to this function only and is NEVER returned,
logged, vaulted, or audited.

IDENTITY SHIFT, P2 → P3 (LOAD-BEARING CONVENTION). P2 clinical mode identified a
source by an AUDIO-CONTENT hash (``sha256(bytes)``); P3 identifies by a LABEL
hash — the encounter's subdir name, or a legacy flat file's basename. Content-
hashing cannot work for the checkpoint accumulator (each chunk has different
bytes, but all chunks of one encounter must share ONE id), so the id is
label-based and stable across an encounter's chunks. Consequence: two DIFFERENT-
content flat files with the SAME name now collide to one encounter_id. The
operator's UNIQUE-LABEL convention is therefore load-bearing for flat inputs (a
per-encounter subdir name is naturally unique; a reused flat filename is not).
"""

from __future__ import annotations

import hashlib
import hmac

# The opaque-id prefix. Colon-free (unlike the P2 ``"sha256:"``) so the id is
# safe in a note filename / title on every filesystem + in Obsidian.
ENCOUNTER_ID_PREFIX = "enc-"
_DIGEST_HEX_CHARS = 16  # 64 bits of the HMAC-SHA256 digest — ample collision margin


class EncounterIdentityError(Exception):
    """The encounter identity could not be computed — an EMPTY/missing salt, an
    UNRESOLVED ``${VAR}`` env-placeholder salt, or an empty raw label. Fail-loud:
    never fall back to an unsalted, reversible, or literal id."""


def compute_encounter_id(raw_label: str, *, salt: str) -> str:
    """Return the salted, opaque ``encounter_id`` for ``raw_label``.

    ``raw_label`` is the operator's label for the encounter (a subdir name or a
    flat file's basename) — it MAY be PHI and is consumed only as HMAC input; it
    is never part of the returned id. ``salt`` is the per-instance secret
    (``ScribeConfig.encounter_salt``).

    Raises :class:`EncounterIdentityError` (FAIL-LOUD) if the salt is empty /
    whitespace-only, is an UNRESOLVED env placeholder (still ``${VARNAME}`` —
    ``SCRIBE_ENCOUNTER_SALT`` not set in the environment), or the raw label is
    empty — a sovereign scribe must not mint a reversible or empty identity.

    The unresolved-placeholder guard is the ingest-time compensating control for
    the OPTIONAL ``EnvironmentFile`` (#67 F2): the systemd unit tolerates a
    missing secrets env-file at STARTUP, but if that leaves the salt unresolved,
    ``resolve_env_placeholders`` (``_env.py``) leaves the LITERAL ``${SCRIBE_
    ENCOUNTER_SALT}`` — a PUBLIC, low-entropy string that would salt every id to
    a dictionary-reversible value. This guard makes "fail-loud until the salt is
    set" true regardless of config shape or whether the env-file is present.
    """
    # Check the STRIPPED salt; HMAC still consumes the ORIGINAL ``salt`` below,
    # so this adds a guard without shifting any existing id.
    stripped_salt = salt.strip() if isinstance(salt, str) else ""
    if not stripped_salt:
        raise EncounterIdentityError(
            "scribe.encounter_salt is empty — cannot compute a non-reversible "
            "encounter_id. Set a per-instance secret salt "
            "(scribe.encounter_salt, e.g. ${SCRIBE_ENCOUNTER_SALT}) before "
            "ingesting any encounter. A sovereign clinical scribe refuses to "
            "fall back to an unsalted (reversible) id."
        )
    if stripped_salt.startswith("${"):
        raise EncounterIdentityError(
            "scribe.encounter_salt is an UNRESOLVED env placeholder "
            f"({stripped_salt!r}) — SCRIBE_ENCOUNTER_SALT is not set in the "
            "environment, so the salt is the literal placeholder string "
            "(PUBLIC + low-entropy) and the encounter_id would be reversible by "
            "dictionary attack. Set SCRIBE_ENCOUNTER_SALT (e.g. via the STAY-C "
            "systemd unit's EnvironmentFile, or the launch env) before ingesting "
            "any encounter. A sovereign clinical scribe refuses to mint a "
            "reversible identity."
        )
    if not (isinstance(raw_label, str) and raw_label.strip()):
        raise EncounterIdentityError(
            "encounter raw_label is empty — cannot compute an encounter_id."
        )
    digest = hmac.new(
        salt.encode("utf-8"), raw_label.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{ENCOUNTER_ID_PREFIX}{digest[:_DIGEST_HEX_CHARS]}"

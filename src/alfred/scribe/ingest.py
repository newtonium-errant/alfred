"""The scribe mode-gate — the legal line enforced IN CODE (scribe P1-c).

In ``synthetic`` mode (the fail-closed default) the pipeline INGEST must refuse
any input record/audio that does NOT carry a ``synthetic: true`` provenance
tag. ``clinical`` mode is the LAST switch — gated on a legal de-id/attestation
standard; for P1-c it is merely the distinct enum value the guard would allow,
and nothing wires real audio yet (that is P2).

The mode flag IS the legal line: flipping synthetic→clinical is a single
deliberate config edit (see ``config._normalize_mode``); nothing else here
hard-codes ``clinical``. This guard is the ONE place the mode decides whether
input may be processed.

Observability (intentionally-left-blank): every ingest decision emits exactly
one ``scribe.ingest_decision`` event carrying ``mode`` + ``accepted`` +
``reason`` (+ ``source_id``), so a synthetic-refused input is distinguishable
from an idle pipeline. Refusal RAISES :class:`ScribeIngestRefused` (fail-closed
no-process) AFTER emitting the event — the pipeline cannot silently proceed.
"""

from __future__ import annotations

from typing import Any

import structlog

from .config import SCRIBE_MODE_CLINICAL, SCRIBE_MODE_SYNTHETIC, ScribeConfig

log = structlog.get_logger(__name__)


class ScribeIngestRefused(Exception):
    """Raised when the mode-gate refuses to ingest an input (fail-closed).

    ``reason`` is a greppable id (``missing_synthetic_provenance``); ``mode``
    is the resolved mode; ``source_id`` names the refused input for triage.
    """

    def __init__(self, reason: str, detail: str, mode: str, source_id: str = "") -> None:
        self.reason = reason
        self.detail = detail
        self.mode = mode
        self.source_id = source_id
        super().__init__(f"scribe ingest refused [{reason}]: {detail}")


def _provenance_is_synthetic(provenance: Any) -> bool:
    """STRICT synthetic-provenance test — fail-closed. Only a dict carrying the
    literal boolean ``synthetic: True`` counts. A missing tag, the STRING
    ``"true"``, ``1``, ``None``, a non-dict, all return False (=> refused in
    synthetic mode)."""
    return isinstance(provenance, dict) and provenance.get("synthetic") is True


def _log_ingest(*, mode: str, accepted: bool, reason: str, source_id: str) -> None:
    log.info(
        "scribe.ingest_decision",
        mode=mode,
        accepted=accepted,
        reason=reason,
        source_id=source_id,
    )


def guard_ingest(
    config: ScribeConfig,
    *,
    provenance: Any,
    source_id: str = "",
) -> None:
    """Fail-closed mode-gate. Emits ``scribe.ingest_decision``; raises
    :class:`ScribeIngestRefused` if the input may not be processed.

    * ``clinical`` mode → accept (the last switch; real audio wiring is P2).
    * ANY other mode (synthetic, or — defensively — an unrecognised value) →
      accept ONLY if ``provenance`` carries ``synthetic: true``, else refuse.

    The clinical branch is gated on the EXACT ``config.mode == "clinical"``; a
    config whose mode did not normalize to clinical falls through to the
    synthetic-required branch. So an unknown mode can never open the clinical
    path even if ``_normalize_mode`` were bypassed — defense in depth.

    Args:
        config: the loaded :class:`ScribeConfig` (``mode`` already normalized).
        provenance: the input record/audio metadata dict; checked for
            ``synthetic: true`` (strict boolean).
        source_id: an identifier for the input (filename / hash) for the log.
    """
    mode = config.mode

    if mode == SCRIBE_MODE_CLINICAL:
        _log_ingest(mode=mode, accepted=True, reason="clinical_mode", source_id=source_id)
        return

    if _provenance_is_synthetic(provenance):
        _log_ingest(
            mode=SCRIBE_MODE_SYNTHETIC,
            accepted=True,
            reason="synthetic_provenance_present",
            source_id=source_id,
        )
        return

    _log_ingest(
        mode=SCRIBE_MODE_SYNTHETIC,
        accepted=False,
        reason="missing_synthetic_provenance",
        source_id=source_id,
    )
    raise ScribeIngestRefused(
        "missing_synthetic_provenance",
        "input lacks a synthetic:true provenance tag and the scribe is in "
        "synthetic mode (the fail-closed default). No real-PHI processing is "
        "wired; only synthetic-tagged input may be ingested until scribe.mode "
        "is deliberately flipped to clinical (gated on the legal standard).",
        SCRIBE_MODE_SYNTHETIC,
        source_id,
    )

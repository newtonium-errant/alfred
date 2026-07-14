"""Self-correcting capture sink + enroll audit — scribe P4-5a (torch-free).

The CAPTURE half (part 1) of the P4-5 self-correcting loop, and the enroll audit
trail. Two append-only JSONL sinks under ``<enrollment_dir>/``:

  * ``learning/attest_capture.jsonl`` — BOTH capture event kinds
    (``diarize_stats`` per chunk/rollup, ``attest_outcome`` at attest). The #48
    self-correcting twin: FAIL-SILENT (a capture bug never touches the pipeline /
    the medico-legal attest path) and PHI-FREE BY CONSTRUCTION — ids, enums,
    booleans, scalars, counts ONLY (never a name/label/transcript/note text).
  * ``audit.log`` — enroll lifecycle events, ``preset_id``-ONLY (never a name; the
    ``presets audit`` CLI joins names at display time).

Feed-back (health/proposals) + the P4-6 audit rows land in 5b/6; this ships only
the writers so the sink accumulates from day one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

LEARNING_DIRNAME = "learning"
CAPTURE_NAME = "attest_capture.jsonl"
AUDIT_NAME = "audit.log"

KIND_DIARIZE_STATS = "diarize_stats"
KIND_ATTEST_OUTCOME = "attest_outcome"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Append one JSON line, creating the dir/file 0600. Caller wraps fail-silent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Newly-created file → 0600; an existing file keeps its mode.
    existed = path.exists()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    if not existed:
        try:
            import os
            os.chmod(path, 0o600)
        except OSError:
            pass


def _capture_path(enrollment_dir: str | Path) -> Path:
    return Path(enrollment_dir) / LEARNING_DIRNAME / CAPTURE_NAME


def _audit_path(enrollment_dir: str | Path) -> Path:
    return Path(enrollment_dir) / AUDIT_NAME


def record_diarize_stats(
    enrollment_dir: str | Path, *,
    source_id: str,
    chunk_seq: int | None,
    user: str | None,
    preset_id: str | None,
    centroid_version: int | None,
    engine_fingerprint: dict[str, Any] | None,
    n_segments: int,
    role_counts: dict[str, int],
    best_cosine: float | None,
    separation: float | None,
    min_purity: float | None,
    fail_closed_demotions: int,
) -> None:
    """Append a ``diarize_stats`` row (per chunk, or an encounter rollup). A no-preset
    encounter STILL lands a row (``preset_id`` / ``user`` null — intentionally-left-
    blank, so 'ran without a preset' is distinguishable from 'no capture'). FAIL-SILENT
    + PHI-FREE: on ANY error the row is dropped with a warning, never propagated."""
    try:
        _append_jsonl(_capture_path(enrollment_dir), {
            "kind": KIND_DIARIZE_STATS, "ts": _now(),
            "source_id": source_id, "chunk_seq": chunk_seq,
            "user": user, "preset_id": preset_id, "centroid_version": centroid_version,
            "engine_fingerprint": engine_fingerprint,
            "n_segments": int(n_segments), "role_counts": dict(role_counts),
            "best_cosine": best_cosine, "separation": separation,
            "min_purity": min_purity, "fail_closed_demotions": int(fail_closed_demotions),
        })
    except Exception:  # noqa: BLE001 — capture must NEVER affect the pipeline
        log.warning("scribe.enroll_learning.capture_error", kind=KIND_DIARIZE_STATS,
                    source_id=source_id, detail="diarize_stats capture failed — SWALLOWED")


def record_attest_outcome(
    enrollment_dir: str | Path, *,
    source_id: str,
    user: str | None,
    preset_id: str | None,
    centroid_version: int | None,
    reason: str,
    kept: bool,
    is_banner: bool = False,
) -> None:
    """Append an ``attest_outcome`` row — per speaker-flag ``reason`` (+ a banner row),
    ``kept`` = a normalized-substring heuristic (the P4-5 correction vehicle; no
    attest UX change). FAIL-SILENT + PHI-FREE (reasons are enum literals)."""
    try:
        _append_jsonl(_capture_path(enrollment_dir), {
            "kind": KIND_ATTEST_OUTCOME, "ts": _now(),
            "source_id": source_id, "user": user, "preset_id": preset_id,
            "centroid_version": centroid_version, "reason": reason,
            "kept": bool(kept), "is_banner": bool(is_banner),
        })
    except Exception:  # noqa: BLE001 — capture must NEVER fail a valid attest
        log.warning("scribe.enroll_learning.capture_error", kind=KIND_ATTEST_OUTCOME,
                    source_id=source_id, detail="attest_outcome capture failed — SWALLOWED")


def audit(enrollment_dir: str | Path, event: str, *,
          preset_id: str | None = None, user: str | None = None,
          **fields: Any) -> None:
    """Append one enroll audit event — ``preset_id``-ONLY, NEVER a name/label. The
    ``presets audit`` CLI joins names from the preset files at display time. FAIL-
    SILENT (audit is best-effort observability, never a blocker)."""
    try:
        row = {"ts": _now(), "event": event, "preset_id": preset_id, "user": user}
        row.update(fields)
        _append_jsonl(_audit_path(enrollment_dir), row)
    except Exception:  # noqa: BLE001
        log.warning("scribe.enroll_learning.audit_error", event=event,
                    detail="audit append failed — SWALLOWED")

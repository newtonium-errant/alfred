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

═══════════════════════════════════════════════════════════════════════════════
diarize_stats ROW SHAPE — the 5b health CONTRACT (frozen here, before rows accumulate)
═══════════════════════════════════════════════════════════════════════════════
The sink is APPEND-ONLY JSONL, so a late schema migration is painful — the fields the
5b consumer needs are written from day one:

  * ``eligible_turns`` + ``min_turn_s`` — the LATCH DENOMINATOR. The ONE ratified metric
    is ``match_rate = 1 − unknown/eligible`` where eligible = turns >= ``min_turn_s``.
    ``role_counts`` alone counts ALL segments, so without ``eligible_turns`` the metric
    is UNCOMPUTABLE from the rows.
  * ``engine_fingerprint`` — the 5b FILTER KEY. Rows are POISONED for health purposes
    when they come from:
      (a) the FAKE embed seam (``embedding_model == "fake-embed-v1"``), or
      (b) the PLACEHOLDER era — the real per-cluster embedding extractor is not wired,
          so every cluster resolves ``unknown`` and the row books role_counts 100%
          unknown against a real ``(preset_id, centroid_version)``, or
      (c) a KILL-SWITCHED engine (``diarized: false`` — nothing was diarized at all).
    **5b MUST filter on ``diarized == true`` AND a REAL (non-fake) engine_fingerprint
    before deriving health**, else the health machine reads instant degraded/stale and
    spams re-record proposals for a preset the matcher never actually saw.
  * ``diarized`` — whether diarization actually RAN for this chunk (see (c) above).
  * ``best_cosine`` / ``separation`` — the match telemetry. ``None`` until the on-box
    per-cluster embedding extraction lands (see ``diarize._cluster_embeddings_for``).
"""

from __future__ import annotations

import json
import os
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


def _append_jsonl(path: Path, row: dict[str, Any], *, enrollment_dir: str | Path) -> None:
    """Append one JSON line, creating the dir tree 0700 and the file 0600.

    ``mkdir(parents=True)`` creates ancestors at the UMASK default (typically 0755), so
    the enrollment ROOT and ``learning/`` would be world-listable whenever the first write
    under the store is an audit/capture row (a rejected /enroll/start, or a no-preset
    encounter's diarize_stats) rather than a preset. The frozen DATA MODEL fixes the whole
    store at 0700 — chmod the root AND the target dir. Caller wraps fail-silent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for d in (Path(enrollment_dir), path.parent):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    # Newly-created file → 0600; an existing file keeps its mode.
    existed = path.exists()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    if not existed:
        try:
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
    eligible_turns: int = 0,
    min_turn_s: float | None = None,
    diarized: bool = False,
) -> None:
    """Append a ``diarize_stats`` row (per chunk, or an encounter rollup). A no-preset
    encounter STILL lands a row (``preset_id`` / ``user`` null — intentionally-left-
    blank, so 'ran without a preset' is distinguishable from 'no capture'). FAIL-SILENT
    + PHI-FREE: on ANY error the row is dropped with a warning, never propagated."""
    if not str(enrollment_dir or ""):
        return                      # store DORMANT — never write relative to the daemon CWD
    if not str(enrollment_dir or ""):
        return                      # store DORMANT — never write relative to the daemon CWD
    try:
        _append_jsonl(_capture_path(enrollment_dir), {
            "kind": KIND_DIARIZE_STATS, "ts": _now(),
            "source_id": source_id, "chunk_seq": chunk_seq,
            "user": user, "preset_id": preset_id, "centroid_version": centroid_version,
            # 5b FILTER KEY — see the module docstring: rows produced by the FAKE seam or
            # the placeholder (all-unknown) extractor must be EXCLUDED from health.
            "engine_fingerprint": engine_fingerprint,
            "n_segments": int(n_segments), "role_counts": dict(role_counts),
            # 5b LATCH DENOMINATOR — match_rate = 1 − unknown/ELIGIBLE, eligible = turns
            # >= min_turn_s. Without these the metric is uncomputable from the sink.
            "eligible_turns": int(eligible_turns),
            "min_turn_s": min_turn_s,
            "diarized": bool(diarized),
            "best_cosine": best_cosine, "separation": separation,
            "min_purity": min_purity, "fail_closed_demotions": int(fail_closed_demotions),
        }, enrollment_dir=enrollment_dir)
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
    if not str(enrollment_dir or ""):
        return                      # store DORMANT — never write relative to the daemon CWD
    try:
        _append_jsonl(_capture_path(enrollment_dir), {
            "kind": KIND_ATTEST_OUTCOME, "ts": _now(),
            "source_id": source_id, "user": user, "preset_id": preset_id,
            "centroid_version": centroid_version, "reason": reason,
            "kept": bool(kept), "is_banner": bool(is_banner),
        }, enrollment_dir=enrollment_dir)
    except Exception:  # noqa: BLE001 — capture must NEVER fail a valid attest
        log.warning("scribe.enroll_learning.capture_error", kind=KIND_ATTEST_OUTCOME,
                    source_id=source_id, detail="attest_outcome capture failed — SWALLOWED")


def has_diarize_stats_for(
    enrollment_dir: str | Path, preset_id: str | None, centroid_version: int | None,
) -> bool:
    """True iff the capture sink already holds a ``diarize_stats`` row for this
    ``(preset_id, centroid_version)`` — the 'is this preset+version already in use?'
    check behind the ``new_preset_first_use`` informational flag (5a) and the 5b
    per-preset health windowing (``new`` is distinguishable from ``ok``).

    A cheap linear scan (5b replaces it with a derived health index). FAIL-SILENT: a
    read/parse error → ``False`` (treat as NEW — over-signal 'first use' rather than
    hide it). A null ``preset_id`` is never a preset use → ``False``."""
    if not preset_id:
        return False
    path = _capture_path(enrollment_dir)
    if not path.is_file():
        return False
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("kind") == KIND_DIARIZE_STATS
                        and row.get("preset_id") == preset_id
                        and row.get("centroid_version") == centroid_version):
                    return True
    except OSError:
        return False
    return False


def audit(enrollment_dir: str | Path, event: str, *,
          preset_id: str | None = None, user: str | None = None,
          **fields: Any) -> None:
    """Append one enroll audit event — ``preset_id``-ONLY, NEVER a name/label. The
    ``presets audit`` CLI joins names from the preset files at display time. FAIL-
    SILENT (audit is best-effort observability, never a blocker).

    The DURABLE biometric-custody trail. Frozen event set: enroll_started /
    enroll_rejected / enroll_aborted, preset_created / preset_rerecorded / preset_renamed /
    preset_deleted, preset_selected, wrong_token_class. Callers MUST pass ids/enums only —
    never a free-text (regex-failed) user string or a preset name."""
    if not str(enrollment_dir or ""):
        return                      # store DORMANT — never write relative to the daemon CWD
    try:
        row = {"ts": _now(), "event": event, "preset_id": preset_id, "user": user}
        row.update(fields)
        _append_jsonl(_audit_path(enrollment_dir), row, enrollment_dir=enrollment_dir)
    except Exception:  # noqa: BLE001
        log.warning("scribe.enroll_learning.audit_error", event=event,
                    detail="audit append failed — SWALLOWED")

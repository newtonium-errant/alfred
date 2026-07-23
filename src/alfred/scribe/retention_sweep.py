"""Daemon-side retention sweep — the driver that seals READY / abandoned encounters (task #13 §3,
slice 13b). Sibling of :class:`~alfred.scribe.events_maintenance.ScribeEventMaintenance`, constructed
once per daemon lifetime and invoked each tick beside it (``daemon.py``).

The 13a :func:`~alfred.scribe.retention.seal_encounter` is the unit-level fail-closed seal path (tar →
seal → self-verify → durable ``retention.sealed`` [D] → ONLY THEN wipe). This sweep is the TRIGGER
GATE that drives it over the encounters on disk — it never re-implements the seal ordering, it only
decides WHICH encounters are eligible and delegates the act to 13a. Four cooperating jobs, each
best-effort, none silently deleting:

  1. **Seal READY-and-unsealed** (§3.2): a subdir whose pipeline state is ``STATE_READY`` — the note
     is drafted + ``_CLOSED`` + all promised seqs folded — is sealed. Idempotency + crash-recovery
     are 13a's (the CHAIN is the source of truth); the sweep just calls ``seal_encounter``.
  2. **Defensive-seal ABANDONED** (§3.6): a subdir with NO ``_CLOSED`` whose most-recent activity is
     older than ``retention.abandon_grace_days`` (default 7) is defensively sealed-and-KEPT (never
     auto-deleted). In retained mode this rides the SAME ``seal_encounter`` — structurally incapable
     of deleting without a durable seal first. Never fires inside grace or on fresh activity.
  3. **Over-window surfacing SEAM** (§4): the s.50 schedule artifact ships in 13c. This slice builds
     the seam — with no schedule present it emits a LATCHED intentionally-left-blank observation and
     skips surfacing WITHOUT failing. It NEVER auto-destroys anything (surface-only, §4).
  4. **diarize_stats rolling prune** (§0.1 / §4 — absorbs the queued P4-5b unowned 180-day prune):
     an age-based row drop on the PHI-FREE telemetry sink, atomic temp→replace rewrite, corrupt/torn
     rows preserved-not-fatal. Emits NO ``retention.*`` event (it is log rotation, not a PHI
     destruction) — counted in the sweep summary.

INTENTIONALLY-LEFT-BLANK: every tick emits a ``scribe.retention.sweep`` summary so "ran, nothing to
do" is distinguishable from "the sweep is broken." Per-encounter failures are ISOLATED (one bad
encounter never halts the sweep); an exception never wedges the daemon loop; the tar/crypto/IO runs
off the event loop via ``asyncio.to_thread`` (mirrors ``pipeline.accumulate_encounter``).

CLINICAL-STORE GATE: encounter seal/wipe requires an ACTIVE clinical event store — the retention
lifecycle is a clinical feature and a retained seal needs the durable ``retention.sealed`` record.
Without an active store the sweep leaves audio UNTOUCHED (fail-safe: never wipe without the
medico-legal store live). The telemetry prune (PHI-free) runs regardless.

DESIGN BOUNDARY — closed-but-stuck INCOMPLETE encounters (A4, DEFERRED to a later slice): an
encounter that IS ``_CLOSED`` but never reached ``STATE_READY`` because a promised tail chunk never
arrived (``STATE_INCOMPLETE`` / a stuck ``STATE_DRAFTED`` WITH chunks) is neither the READY gate
(§3.2) nor the abandoned gate (§3.6 keys on NO ``_CLOSED``). Such an encounter is SKIPPED here, so
its plaintext audio is retained INDEFINITELY under LUKS — surfaced only by the pipeline's own
``close_awaiting_promised_seq`` signal, not sealed by retention. This is a deliberate, flagged
boundary: sealing a promised-but-incomplete encounter would seal a partial archive. The fail-loud
manifest-mismatch recovery (§3.3, findings 2/3/7) makes a FUTURE grace-based defensive seal of these
safe to add without risking the re-opened-encounter destruction path — deferred to the delta review.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

from alfred.scribe import retention as ret
from alfred.scribe import schedule as sched_mod
from alfred.scribe.close_manifest import CLOSE_SENTINEL_NAME
from alfred.scribe.config import (
    RETENTION_MODE_RETAINED, RETENTION_MODE_TRANSIENT, ScribeConfig,
)
from alfred.scribe.enroll_learning import CAPTURE_NAME, LEARNING_DIRNAME, capture_sink_lock
from alfred.scribe.identity import EncounterIdentityError, compute_encounter_id
from alfred.scribe.negation_suppression import (
    NEGATION_CANDIDATE_AGE_CAP_DAYS,
    NEGATION_REVIEW_SPOOL_NAME,
    count_pending,
    prune_candidates_by_age,
    resolve_candidates_dir,
)
from alfred.scribe.state import STATE_READY, ScribeState

log = structlog.get_logger("scribe.retention.sweep")

# The diarize_stats telemetry-sink rolling window (§4 schedule class ``diarize_stats``: 180 days).
# A fixed constant here (NOT a config knob — the §3.7 config surface deliberately omits it): the s.50
# schedule (slice 13c) is the eventual declared home for per-class windows, but the PHI-FREE telemetry
# prune is log-rotation, not a scheduled PHI destruction, so it carries the design's fixed 180 now.
_DIARIZE_STATS_PRUNE_DAYS = 180

# The recipient public key the current (age / pyrage) seal backend expects — a canonical age
# recipient string, ``age1…`` (bech32). This reader is the counterpart to the 13d ``retention keygen``
# ceremony (which writes ``str(recipient)``); both are the age backend's contract, like
# ``retention.SEAL_CIPHER`` / ``SEAL_BLOB_SUFFIX``. The sweep does a LIGHTWEIGHT prefix check for the
# once-latched malformed signal; the real (canonical bech32) parse happens in ``AgeSealer.seal`` (which
# raises the typed SealError on a bad recipient — per-encounter isolated), keeping the sweep dep-agnostic.
_AGE_RECIPIENT_PREFIX = "age1"


def _parse_iso(ts: Any) -> datetime | None:
    """Parse an ISO-8601 ``ts`` (the enroll-sink row + event timestamp shape) to a TIMEZONE-AWARE
    datetime. ``None`` on anything unparseable OR on a tz-NAIVE timestamp — the caller compares against
    an aware cutoff, so a naive value would raise ``TypeError`` and crash the whole prune every tick
    (finding 16). A naive ts cannot be positively dated in a definite zone, so it is treated as
    undateable → the caller PRESERVES it (never drop a row we cannot positively date; the enroll sink's
    own writer always emits aware UTC, so a naive row is foreign / hand-edited / older-version)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None  # tz-naive → undateable → PRESERVE (never crash the aware-cutoff comparison)
    return dt


@dataclass
class RetentionSweepSummary:
    """One sweep's outcome — the intentionally-left-blank signal (idle ≠ broken) + the 13b sweep
    telemetry the daemon logs each tick. ``sealed_ready`` / ``sealed_abandoned`` distinguish the two
    trigger gates (§3.2 vs §3.6); the seal itself is identical (13a)."""

    mode: str = RETENTION_MODE_RETAINED
    encounters_scanned: int = 0
    sealed_ready: int = 0            # §3.2 — a STATE_READY encounter sealed this sweep
    sealed_abandoned: int = 0        # §3.6 — a stale, un-closed encounter defensively sealed-and-kept
    already_sealed: int = 0          # crash-between-event-and-wipe recovery completed (13a idempotency)
    transient_wiped: int = 0         # §3.5 — wiped WITHOUT sealing (config-visible posture)
    no_chunks: int = 0               # an eligible dir with no audio on disk (declined/empty)
    verify_failed: int = 0           # a seal self-verify failed — plaintext left intact, retry next sweep
    empty_disposed: int = 0          # §E — a CLOSED zero-chunk encounter disposed (PHI-named dir removed)
    wipe_incomplete: int = 0         # a COMMITTED seal whose plaintext wipe left residue — OPERATOR ESCALATION
    recovery_mismatch: int = 0       # already-sealed recovery FAILED CLOSED (blob/manifest) — OPERATOR ESCALATION
    skipped: int = 0                 # not eligible (still accumulating / closed-but-not-ready / in grace)
    encounter_errors: int = 0        # per-encounter failures, ISOLATED (the sweep continued)
    pruned_telemetry_rows: int = 0   # diarize_stats rows dropped (age-based, PHI-free log rotation)
    pruned_negation_candidate_rows: int = 0  # #26 derived-PHI candidate/attest rows dropped (age-cap)
    pending_negation_candidates: int = 0     # #26 review-ready count for the PHI-free relay spool
    review_due: int = 0              # §4 over-window PHI classes surfaced (0 until 13c's schedule)
    review_surfaced: bool = False    # §4/E3 — did surfacing actually EVALUATE this tick? (False when
    #                                  skipped/aborted — distinguishes 'ran, 0 due' from 'did not run')
    oldest_review_encounter_id: str = ""  # §4/C3 — the oldest over-window encounter (opaque id, for the spool)
    sealing_available: bool = True   # was the seal path usable (active store + sealer + pubkey)
    schedule_present: bool = False   # was an s.50 schedule found (False until 13c)

    def did_work(self) -> int:
        return (self.sealed_ready + self.sealed_abandoned + self.already_sealed
                + self.transient_wiped + self.verify_failed + self.empty_disposed
                + self.wipe_incomplete + self.recovery_mismatch + self.pruned_telemetry_rows
                + self.encounter_errors + self.review_due)

    def needs_operator_attention(self) -> bool:
        """wipe_incomplete / recovery_mismatch are FAIL-CLOSED states that leave PHI on disk or a
        chain/blob divergence — the sweep must not bury them in a routine summary."""
        return bool(self.wipe_incomplete or self.recovery_mismatch)


class RetentionSweep:
    """Owns the retention sweep's per-lifetime latches (no-schedule / no-pubkey / inactive-store
    observations, latched once so a steady-state condition never spams). One instance per daemon.

    ``sealer_factory`` is injectable so the trigger/contract pins run UNCONDITIONALLY with a fake
    sealer (no crypto dep); production uses :func:`retention.make_default_sealer` (the age / ``pyrage``
    backend)."""

    def __init__(
        self,
        config: ScribeConfig,
        events,
        *,
        sealer_factory: Callable[[], ret.Sealer] = ret.make_default_sealer,
    ) -> None:
        self._config = config
        self._ev = events
        self._sealer_factory = sealer_factory
        self._latched: set[str] = set()

    # --- the async entry point (off-loop crypto/IO, best-effort) ----------

    async def run(self, state: ScribeState, *, now: datetime | None = None) -> RetentionSweepSummary:
        """Run one retention sweep OFF the event loop (tar/crypto/IO via ``asyncio.to_thread``,
        mirroring ``pipeline.run_sweep``'s ``accumulate_encounter`` offload). The daemon wraps this
        best-effort; the sweep additionally isolates per-encounter failures internally, so an
        exception never wedges the loop."""
        now_dt = now or datetime.now(timezone.utc)
        return await asyncio.to_thread(self._run_sync, state, now_dt)

    # --- the synchronous sweep body ---------------------------------------

    def _run_sync(self, state: ScribeState, now_dt: datetime) -> RetentionSweepSummary:
        cfg = self._config
        mode = cfg.retention.mode
        summary = RetentionSweepSummary(mode=mode)
        now_iso = now_dt.isoformat()

        # CLINICAL-STORE GATE. ALL encounter work (seal/wipe AND the §E empty-closed disposal, which
        # reads the chain for idempotency) needs an active store; the retained SEAL additionally needs
        # the sealer + recipient pubkey. Disposal needs NO crypto, so it rides can_process, not can_seal.
        sealer: ret.Sealer | None = None
        pubkey = b""
        can_process = True
        if not self._ev.active:
            self._latch_log(
                "store_inactive",
                "scribe.retention.sweep.store_inactive",
                detail="the clinical event store is inactive — retention seal/wipe is SKIPPED (a "
                       "retained seal needs the durable retention.sealed record; never wipe audio "
                       "without the medico-legal store live). The PHI-free telemetry prune still runs.")
            can_process = False
        # can_seal: the READY/abandoned SEAL path is usable. Transient wipe needs no key; retained
        # needs the sealer + pubkey. Disposal of empty-closed dirs proceeds even without a key.
        can_seal = mode == RETENTION_MODE_TRANSIENT
        if can_process and mode == RETENTION_MODE_RETAINED:
            sealer = self._resolve_sealer()
            pubkey = self._resolve_pubkey()
            can_seal = sealer is not None and pubkey != b""
        summary.sealing_available = can_process and can_seal

        # Each leg below is a SIBLING, wrapped so a stat-EACCES (pathlib re-raises EACCES on
        # is_file/is_dir when a parent dir is unsearchable) folds into a latched escalation instead of
        # escaping _run_sync and killing this tick's ILB summary + the needs_operator_attention
        # emission (D5). The specific stat sites are also individually guarded; this is the backstop.

        # (1)+(2) seal READY / defensively-seal abandoned + (§E) dispose empty-closed — enumerate subdirs.
        if can_process:
            try:
                self._process_encounters(state, now_dt, now_iso, sealer, pubkey, can_seal, summary)
            except OSError:
                summary.encounter_errors += 1
                self._latch_log("process_eacces", "scribe.retention.sweep.process_leg_eacces",
                                detail="the encounter-processing leg hit an unsearchable dir (EACCES) — "
                                       "isolated so the sweep summary + escalation still emit. Latched.")

        # Load the s.50 schedule ONCE (fail-closed None) and thread it into both surfacing + the prune
        # (avoids a double read; both are PHI-free observability, independent of the store gate).
        schedule = self._load_schedule()

        # (3) over-window surfacing (§4) — no schedule ⇒ latched ILB; a schedule ⇒ count over-window
        # sealed encounters (surface-only, NEVER auto-destroy).
        try:
            self._surface_over_window(now_dt, summary, schedule)
        except OSError:
            self._latch_log("surface_eacces", "scribe.retention.sweep.surface_leg_eacces",
                            detail="the over-window surfacing leg hit an unsearchable dir (EACCES) — "
                                   "isolated so the sweep summary + escalation still emit. Latched.")

        # (4) diarize_stats rolling prune (§0.1/§4) — PHI-free log rotation, no retention.* event; the
        # window now comes from the schedule's diarize_stats class (fallback 180d when unpublished).
        try:
            summary.pruned_telemetry_rows = self._prune_diarize_stats(now_dt, schedule)
        except OSError:
            self._latch_log("prune_eacces", "scribe.retention.sweep.prune_leg_eacces",
                            detail="the telemetry-prune leg hit an unsearchable dir (EACCES) — isolated "
                                   "so the sweep summary + escalation still emit. Latched.")

        # (4b) #26 negation-candidate AGE-CAP prune — drop un-reviewed DERIVED-PHI candidate/attest
        # rows older than the cap so an un-actioned PHI row can't linger. Unlike the PHI-FREE diarize
        # sink (which PRESERVES undateable rows), this PHI-bearing sink DROPS an undateable row
        # (fail-safe toward not-retaining-PHI). Its own lock (shared with the destroy-prune) — cannot
        # race the destroy path. Best-effort: a prune error is isolated, never wedges the sweep.
        try:
            summary.pruned_negation_candidate_rows = self._prune_negation_candidates(now_dt)
        except OSError:
            self._latch_log("negation_prune_eacces", "scribe.retention.sweep.negation_prune_leg_eacces",
                            detail="the #26 negation-candidate age-cap prune hit an unsearchable dir "
                                   "(EACCES) — isolated so the sweep summary still emits. Latched.")

        # (5) §4 morning-review relay spool (C3) — PHI-free whole-file snapshot Salem's brief reads.
        self._write_review_spool(now_dt, summary)

        # (5b) #26 negation-paraphrase pending-COUNT relay spool — PHI-FREE (a bare count + generated_at,
        # NEVER the concept-sets), a sibling of the review spool, gated on the same review_spool_path.
        self._write_negation_review_spool(now_dt, summary)

        # Sweep summary — ALWAYS emitted (intentionally-left-blank: idle is distinguishable from broken).
        did = summary.did_work()
        log.info(
            "scribe.retention.sweep",
            mode=summary.mode,
            encounters_scanned=summary.encounters_scanned,
            sealed_ready=summary.sealed_ready,
            sealed_abandoned=summary.sealed_abandoned,
            already_sealed=summary.already_sealed,
            transient_wiped=summary.transient_wiped,
            no_chunks=summary.no_chunks,
            verify_failed=summary.verify_failed,
            empty_disposed=summary.empty_disposed,
            wipe_incomplete=summary.wipe_incomplete,
            recovery_mismatch=summary.recovery_mismatch,
            skipped=summary.skipped,
            encounter_errors=summary.encounter_errors,
            pruned_telemetry_rows=summary.pruned_telemetry_rows,
            review_due=summary.review_due,
            sealing_available=summary.sealing_available,
            schedule_present=summary.schedule_present,
            needs_operator_attention=summary.needs_operator_attention(),
            detail=("ran, nothing to do — no retention work this sweep" if did == 0
                    else "retention sweep completed with work"),
        )
        if summary.needs_operator_attention():
            # Fail-closed states (PHI residue / chain-blob divergence) must be loud, not buried in the
            # routine summary — a distinct error the operator's grep/alert workflow keys on.
            log.error(
                "scribe.retention.sweep.needs_operator_attention",
                wipe_incomplete=summary.wipe_incomplete,
                recovery_mismatch=summary.recovery_mismatch,
                detail="retention sweep left encounters in a FAIL-CLOSED state — either plaintext PHI "
                       "could not be wiped after a committed seal (wipe_incomplete), or an already-sealed "
                       "recovery refused to wipe against a missing/mismatched blob (recovery_mismatch). "
                       "Operator reconciliation required; the next sweep retries the safe cases.")
        return summary

    # --- (1)+(2) encounter enumeration + gate ----------------------------

    def _process_encounters(
        self, state: ScribeState, now_dt: datetime, now_iso: str,
        sealer: "ret.Sealer | None", pubkey: bytes, can_seal: bool, summary: RetentionSweepSummary,
    ) -> None:
        cfg = self._config
        input_dir = Path(cfg.input_dir)
        try:
            input_present = input_dir.is_dir()
        except OSError:
            return  # D5: a stat-EACCES on the inbox parent must not escape — skip this leg (the sweep
            #         summary + operator-attention emission survive; the pipeline's own error covers it)
        if not input_present:
            return  # nothing to seal — the pipeline's own idle signal covers the missing-inbox case
        retained_dir = self._resolved_retained_dir()
        grace_days = cfg.retention.abandon_grace_days
        try:
            entries = sorted(input_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        subdirs = [p for p in entries if p.is_dir() and not p.name.startswith(".")]
        for enc_dir in subdirs:
            summary.encounters_scanned += 1
            try:
                self._process_one(
                    enc_dir, state, now_dt, now_iso, sealer, pubkey, retained_dir,
                    grace_days, can_seal, summary)
            except EncounterIdentityError:
                # A missing/unresolved encounter salt makes EVERY id uncomputable — latch once (not
                # per-encounter-per-tick spam) and skip; a sovereign scribe never seals under an
                # un-openable identity. The pipeline itself already fail-louds on the same condition.
                self._latch_log(
                    "encounter_id_error",
                    "scribe.retention.sweep.encounter_id_error",
                    detail="cannot compute an encounter_id (empty/unresolved scribe.encounter_salt) "
                           "— retention sealing SKIPPED for this encounter (latched).")
                summary.skipped += 1
            except Exception as exc:  # noqa: BLE001 — per-encounter fail-isolated, never fail-whole
                summary.encounter_errors += 1
                log.warning(
                    "scribe.retention.sweep.encounter_error",
                    error_class=type(exc).__name__,   # class only — NO PHI, NO dir name (may be label)
                    detail="an encounter failed this retention sweep — ISOLATED; the sweep continues "
                           "to the remaining encounters (one bad encounter never halts the sweep).")

    def _process_one(
        self, enc_dir: Path, state: ScribeState, now_dt: datetime, now_iso: str,
        sealer: "ret.Sealer | None", pubkey: bytes, retained_dir: Path,
        grace_days: int, can_seal: bool, summary: RetentionSweepSummary,
    ) -> None:
        cfg = self._config
        encounter_id = compute_encounter_id(enc_dir.name, salt=cfg.encounter_salt)
        closed = (enc_dir / CLOSE_SENTINEL_NAME).exists()
        st = state.get(encounter_id)
        is_ready = st is not None and st.state == STATE_READY
        has_chunks = ret.encounter_has_chunks(enc_dir)
        dispose_empty = False

        if closed and not has_chunks:
            # §E — a CLOSED zero-chunk encounter (clinician opened, no audio, /close) is DISPOSED, not
            # sealed: the PHI-named dir must not persist with 'nothing to do' logged forever. Needs no
            # crypto (nothing to seal), so it proceeds even when can_seal is False (pre-13d keygen).
            gate = "empty_closed"
        elif not closed and not has_chunks and self._is_abandoned(enc_dir, now_dt, grace_days):
            # E-EXTENSION — a stale-ABANDONED zero-chunk dir (no _CLOSED, no audio, past the abandon
            # grace) is likewise DISPOSED (else it leaks a patient-named dir forever while logging
            # no_chunks). Needs no crypto; the seal path disposes it via dispose_empty.
            gate = "empty_abandoned"
            dispose_empty = True
        elif is_ready and can_seal:
            gate = "ready"
        elif not closed and can_seal and self._is_abandoned(enc_dir, now_dt, grace_days):
            gate = "abandoned"
        elif has_chunks and self._ev.retention_sealed_row(encounter_id) is not None:
            # recovery reachability keys on the CHAIN, not deletable/mtime state (finding 30 + D11/D13).
            # ANY dir with chunks on disk AND a durable retention.sealed row is a crash-between-event-
            # and-wipe (or a spared late-chunk / abandoned-sealed) encounter — the row is authoritative.
            # NO ``closed`` requirement (D13: an abandoned-gate seal has no _CLOSED, and its recovery
            # needs only the sealer, not the pubkey) and NO mtime/grace requirement (D11: a late chunk
            # spared by the race fix has a FRESH mtime that de-qualifies the abandoned gate — it must
            # still re-enter recovery and persistently escalate, not go signal-quiet for the grace
            # window). Route to seal_encounter's fail-closed recovery (completes the wipe, or escalates
            # recovery_mismatch). A dir with chunks but NO row (fresh/DRAFTED) falls through to skip.
            gate = "recover"
        else:
            # Still accumulating (fresh, un-closed), closed-but-not-yet-READY WITH chunks and no seal
            # row (DRAFTED/INCOMPLETE — the pipeline's own signals cover it), inside the abandon grace,
            # or a READY/abandoned encounter we cannot seal yet (no key) → not eligible this sweep.
            summary.skipped += 1
            return

        outcome = ret.seal_encounter(
            enc_dir, encounter_id, events=self._ev, sealer=sealer,
            recipient_public_key=pubkey, retained_dir=retained_dir,
            mode=cfg.retention.mode, now=now_iso, dispose_empty=dispose_empty)
        self._tally(outcome.status, gate, summary)

    @staticmethod
    def _tally(status: str, gate: str, summary: RetentionSweepSummary) -> None:
        if status == ret.SEAL_STATUS_SEALED:
            if gate == "abandoned":
                summary.sealed_abandoned += 1
            else:
                summary.sealed_ready += 1
        elif status == ret.SEAL_STATUS_ALREADY_SEALED:
            summary.already_sealed += 1
        elif status == ret.SEAL_STATUS_TRANSIENT_WIPED:
            summary.transient_wiped += 1
        elif status == ret.SEAL_STATUS_EMPTY_DISPOSED:
            summary.empty_disposed += 1
        elif status == ret.SEAL_STATUS_WIPE_INCOMPLETE:
            summary.wipe_incomplete += 1
        elif status == ret.SEAL_STATUS_RECOVERY_MISMATCH:
            summary.recovery_mismatch += 1
        elif status == ret.SEAL_STATUS_NO_CHUNKS:
            summary.no_chunks += 1
        elif status == ret.SEAL_STATUS_VERIFY_FAILED:
            summary.verify_failed += 1

    def _is_abandoned(self, enc_dir: Path, now_dt: datetime, grace_days: int) -> bool:
        """§3.6 — True iff the encounter shows NO activity for ``grace_days``. "Activity" is the most
        recent mtime among the dir AND its immediate children (a new chunk lands as a new dir entry,
        moving the dir mtime; taking the max also covers a bare file touch), so a still-recording
        encounter (fresh mtime) is NEVER caught, and the check never fires inside the grace window.
        ``grace_days`` is the operator's config value (default 7, a day-scale window)."""
        threshold = now_dt.timestamp() - grace_days * 86400
        try:
            latest = enc_dir.stat().st_mtime
            for child in enc_dir.iterdir():
                try:
                    latest = max(latest, child.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            return False  # can't stat → never defensively-seal on a read error (fail-safe)
        return latest < threshold

    # --- (3) over-window surfacing seam (§4) -----------------------------

    def _surface_over_window(
        self, now_dt: datetime, summary: RetentionSweepSummary, schedule: dict | None,
    ) -> None:
        """§4 over-window SURFACING. With no schedule published, emit a latched intentionally-left-blank
        observation and skip WITHOUT failing. With a schedule, compare each sealed ``.age`` blob's age
        (now − mtime) against the ``encounter_audio_sealed`` class window → set ``summary.review_due``
        + latch a ``retention_review_due`` signal (once). SURFACE-ONLY (§4): this path NEVER touches a
        blob — destruction stays the explicit operator playbook (§5). A never-pruned window (null) or
        an IO error surfaces nothing (fail-open, non-destructive)."""
        summary.schedule_present = schedule is not None
        if schedule is None:
            # C7: distinguish a PUBLISHED-then-corrupted schedule (a file exists at the path but load
            # returned None — malformed/unreadable) from NEVER-published — a distinct latched signal,
            # not the misleading 'no schedule published yet' text.
            if self._schedule_file_present():
                self._latch_log(
                    "schedule_load_failed", "scribe.retention.sweep.schedule_load_failed",
                    detail="a schedule file EXISTS at retention.schedule_path but could NOT be loaded "
                           "(malformed / corrupt / unreadable) — over-window surfacing is SKIPPED and "
                           "the diarize prune reverted to the 180d fallback. Fix or re-publish the "
                           "schedule (`alfred scribe retention schedule publish`). Latched.")
            else:
                self._latch_log(
                    "no_schedule", "scribe.retention.sweep.no_schedule_published",
                    detail="no s.50 retention schedule is published yet — over-window surfacing is "
                           "SKIPPED; the sweep NEVER auto-destroys. Publish one via `alfred scribe "
                           "retention schedule publish`. Latched once.")
            return
        # a valid schedule clears any prior absent/corrupt latch so a later regression re-warns.
        self._latched.discard("no_schedule")
        self._latched.discard("schedule_load_failed")
        window_days = sched_mod.class_window_days(schedule, sched_mod.SURFACED_PHI_CLASS)
        if window_days is None:
            summary.review_due = 0
            summary.review_surfaced = True  # EVALUATED: the class is never-pruned → definitively 0 due
            self._latched.discard("review_due")  # never-pruned now → clear any prior due latch
            return
        retained_dir = self._resolved_retained_dir()
        cutoff = now_dt.timestamp() - window_days * 86400
        due = 0
        oldest_id = ""
        oldest_ts = None
        try:
            blobs = list(retained_dir.glob(f"*{ret.SEAL_BLOB_SUFFIX}"))
        except OSError:
            # E3: the enumeration ABORTED (unenumerable blob store) — surfacing did NOT run, so
            # review_surfaced stays False (the spool must NOT publish a false all-clear). Emit a
            # latched signal (was silently swallowed) so the operator sees the enumeration failure.
            self._latch_log(
                "review_glob_failed", "scribe.retention.sweep.review_enumeration_failed",
                detail="the sealed-blob store could not be enumerated for over-window surfacing — "
                       "review_due is UNKNOWN this sweep (NOT an all-clear). Latched.")
            return
        # E2: build the {encounter_id: sealed ts} map in ONE chain query, then dict-lookup per blob —
        # C5's per-blob retention_sealed_row was a full-chain scan PER blob (O(blobs × rows), wedges as
        # the deployment ages).
        try:
            ts_by_id = self._ev.retention_sealed_ts_by_id()
        except Exception:  # noqa: BLE001 — the whole-chain query failed (an existing-but-unreadable
            # chain: EACCES/EIO/EISDIR, or a corrupt parse). R5: this is the AGE BASIS for EVERY blob —
            # swallowing it to an empty map would fall every blob to the mtime fallback, and on a
            # restore day (fresh mtimes + a briefly-unreadable chain) an over-window encounter reports
            # UNDER-window → a FALSE all-clear on the s.49 destruction-review obligation (the E3 class,
            # reintroduced via E2's own path). Mirror the glob-failure sibling: latch a distinct signal,
            # leave review_surfaced=False (the brief renders 'not evaluated / UNKNOWN'), write NO
            # all-clear this tick. A SINGLE blob's legitimately-absent row is NOT this path — it is a
            # dict miss handled inside _over_window_basis_ts (the documented mtime fallback stays).
            self._latch_log(
                "review_basis_unavailable", "scribe.retention.sweep.review_basis_unavailable",
                detail="the clinical chain could not be read to date the sealed blobs (the over-window "
                       "AGE BASIS) — review_due is UNKNOWN this sweep (NOT an all-clear). Latched.")
            return
        for blob in blobs:
            basis = self._over_window_basis_ts(blob, ts_by_id)  # C5: durable row ts, fallback mtime
            if basis is not None and basis.timestamp() < cutoff:
                due += 1
                if oldest_ts is None or basis < oldest_ts:  # track the OLDEST over-window encounter (C3)
                    oldest_ts = basis
                    oldest_id = blob.stem                    # <encounter_id>.age → opaque id, PHI-free
        summary.review_due = due
        summary.oldest_review_encounter_id = oldest_id
        summary.review_surfaced = True  # EVALUATED: the blob store was enumerated against a real window
        if due > 0:
            self._latch_log(
                "review_due",
                "scribe.retention.sweep.retention_review_due",
                detail=f"{due} sealed encounter(s) are OVER the s.50 retention window for "
                       f"'{sched_mod.SURFACED_PHI_CLASS}' (schedule "
                       f"{schedule.get('schedule_version')!r}) — SURFACED for the operator's "
                       f"morning-review playbook (the review_due count rides the per-tick sweep "
                       f"summary). The sweep NEVER auto-destroys; run the explicit destroy playbook "
                       f"(§5). Latched until the due set resolves to zero.")
        else:
            # C4: re-arm — the latch is for a RESOLVABLE, actionable alert (the operator destroys the
            # due blobs via the §5 playbook). Discard it when due==0 so a LATER cohort re-emits the
            # signal (mirrors the pubkey-latch discipline), not once-per-daemon-lifetime.
            self._latched.discard("review_due")

    def _schedule_file_present(self) -> bool:
        """True iff a file EXISTS at ``retention.schedule_path`` (used to distinguish a corrupt
        published schedule from a never-published one — C7). An unsearchable parent (is_file EACCES)
        counts as present-but-unloadable."""
        path = self._config.retention.schedule_path
        if not path:
            return False
        try:
            return Path(path).is_file()
        except OSError:
            return True

    def _over_window_basis_ts(self, blob: Path, ts_by_id: dict[str, str]) -> datetime | None:
        """The age basis for the over-window check (C5): the DURABLE ``retention.sealed`` row's ts for
        this blob's encounter_id (``<encounter_id>.age`` → stem), looked up in the ONE-query-per-tick
        ``ts_by_id`` map (E2) — backup-restore-proof + chain-answerable, the design's '10 yr from last
        encounter activity' basis. Falls back to the blob mtime (a DEGRADED basis — a restore that
        drops mtimes resets it; documented for the 13e restore runbook) only when no row is found;
        ``None`` when neither is dateable."""
        ts = _parse_iso(ts_by_id.get(blob.stem))
        if ts is not None:
            return ts
        try:
            return datetime.fromtimestamp(blob.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    # The §4 morning-review relay spool header ts format (UTC, Z-suffixed) — MUST match
    # ``brief.stayc_relay._TS_FORMAT`` (the cross-component contract the brief reader parses).
    _REVIEW_SPOOL_TS = "%Y-%m-%dT%H:%M:%SZ"

    def _write_review_spool(self, now_dt: datetime, summary: RetentionSweepSummary) -> None:
        """Write the PHI-FREE §4 morning-review relay spool (C3) — a whole-file snapshot Salem's
        Morning Brief reads (``brief.stayc_relay``) so the s.50 review obligation reaches the
        morning-review cadence, not just the daemon log. Written EVERY sweep when
        ``review_spool_path`` is configured (ILB: a fresh snapshot proves the sweep is alive; a stale
        one surfaces a dead sweep in the brief). Carries ONLY generated_at + the ``review_due`` count
        + the oldest over-window ``encounter_id`` (an opaque salted-HMAC id) — no PHI, no bodies.
        Atomic write; best-effort (a write failure never crashes the sweep)."""
        path = (self._config.retention.review_spool_path or "").strip()
        if not path:
            return
        lines = [
            "# STAY-C retention review — relay snapshot",
            f"generated_at: {now_dt.strftime(self._REVIEW_SPOOL_TS)}",
            # E3: 'surfaced' distinguishes an EVALUATED all-clear (surfaced true, review_due 0) from a
            # SKIPPED/ABORTED run (surfaced false — no schedule / corrupt / unenumerable store). The
            # brief must NOT render a false 'nothing due' when surfacing did not actually run.
            f"surfaced: {'true' if summary.review_surfaced else 'false'}",
            f"review_due: {summary.review_due}",
            f"oldest_encounter_id: {summary.oldest_review_encounter_id}",
        ]
        data = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            ret._atomic_write_bytes(Path(path), data)
        except OSError:
            # E5: a persistently-unwritable spool path is a STEADY-STATE condition — latch (once), not
            # a bare warning every 30s tick (the module's latch discipline). A successful write clears
            # the latch so a later regression re-warns.
            self._latch_log(
                "review_spool_write_failed", "scribe.retention.sweep.review_spool_write_failed",
                detail="could not write the §4 review relay spool — the morning-review line will read "
                       "stale/no-data until the next successful write. Check the path perms. Latched.")
            return
        self._latched.discard("review_spool_write_failed")  # a good write re-arms the warning

    def _prune_negation_candidates(self, now_dt: datetime) -> int:
        """#26 AGE-CAP prune of the DERIVED-PHI negation candidate + attest-outcome spools — drop rows
        older than :data:`NEGATION_CANDIDATE_AGE_CAP_DAYS` so an un-reviewed candidate can't linger as
        PHI. Row-prune under the negation sink lock (SHARED with the s.49 destroy-prune, so they never
        race). PHI-BEARING posture: an undateable row is DROPPED (fail-safe toward not-retaining-PHI —
        the OPPOSITE of the PHI-free diarize sink, which preserves undateable rows). Returns rows
        dropped (folded into the sweep summary — ILB). No ``retention.*`` event (a rolling prune, not a
        keyed s.49 destruction)."""
        cand_dir = resolve_candidates_dir(self._config)
        cutoff_iso = (now_dt - timedelta(days=NEGATION_CANDIDATE_AGE_CAP_DAYS)).isoformat()
        return prune_candidates_by_age(cand_dir, cutoff_iso)

    def _write_negation_review_spool(self, now_dt: datetime, summary: RetentionSweepSummary) -> None:
        """Write the PHI-FREE #26 pending-COUNT relay snapshot — a sibling of the §4 review spool,
        gated on the SAME ``review_spool_path`` (so the operator opts into all morning-review relays at
        once, no extra config field). Carries ONLY ``generated_at`` + the review-ready pending COUNT —
        NEVER a concept-set (count-only crosses the relay boundary; the PHI pairs stay on-box in
        ``negation-candidates``). The join's 'decided' exclusion comes from the durable event chain (an
        inactive store ⇒ decided = ∅, still a valid count). Atomic write, best-effort + latched-on-fail
        (mirrors the review spool). Every sweep when configured (ILB: a fresh snapshot = sweep alive)."""
        base = (self._config.retention.review_spool_path or "").strip()
        if not base:
            return
        decided = self._ev.negation_decided_ids() if getattr(self._ev, "active", False) else set()
        cand_dir = resolve_candidates_dir(self._config)
        pending = count_pending(cand_dir, decided)
        summary.pending_negation_candidates = pending
        path = Path(base).with_name(NEGATION_REVIEW_SPOOL_NAME)
        lines = [
            "# STAY-C negation-paraphrase review — relay snapshot (PHI-free: count only)",
            f"generated_at: {now_dt.strftime(self._REVIEW_SPOOL_TS)}",
            f"pending: {pending}",
        ]
        data = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            ret._atomic_write_bytes(path, data)
        except OSError:
            self._latch_log(
                "negation_spool_write_failed", "scribe.retention.sweep.negation_spool_write_failed",
                detail="could not write the #26 negation-review relay spool — the morning-review line "
                       "will read stale/no-data until the next successful write. Check path perms. Latched.")
            return
        self._latched.discard("negation_spool_write_failed")

    def _load_schedule(self) -> dict | None:
        """The published s.50 schedule dict, or ``None`` when the path is unset / absent / malformed —
        FAIL-CLOSED (a malformed schedule is treated as no-usable-schedule → skip surfacing, keep the
        180d telemetry fallback; never crash the sweep). Delegates to :func:`schedule.load_schedule`
        (validation-gated) so the CLI publish + the sweep read share one parser."""
        path = self._config.retention.schedule_path
        if not path:
            return None
        return sched_mod.load_schedule(path)

    # --- (4) diarize_stats rolling prune (§0.1/§4) -----------------------

    def _prune_diarize_stats(self, now_dt: datetime, schedule: dict | None = None) -> int:
        """Age-based rolling prune of the PHI-FREE ``diarize_stats``/``attest_outcome`` telemetry sink
        (``<enrollment_dir>/learning/attest_capture.jsonl``) — absorbs the queued P4-5b unowned
        180-day prune. Drops rows whose ``ts`` is older than the window; PRESERVES every row it cannot
        positively date (unparseable/torn or missing ts) — the prune is AGE-BASED, so a row whose age
        is unknown is never positively over-window and is carried through (fail-open, non-destructive,
        mirrors the enroll-sink skip-not-fatal discipline). Atomic temp→replace rewrite (never a torn
        sink); emits NO ``retention.*`` event (log rotation, not a PHI destruction). Returns the row
        count dropped (folded into the sweep summary — intentionally-left-blank).

        Window sourcing (§4, 13c): a published ``schedule``'s ``diarize_stats`` window governs; with
        NO schedule the fixed 180d default applies (the P4-5b absorption). A schedule that declares
        ``diarize_stats`` NEVER-pruned (``window_days: null``) SKIPS the prune (a latched observation —
        deliberate no-prune is distinguishable from a broken prune).

        Concurrency (finding 19): the sink has a CROSS-PROCESS appender the original note missed — the
        ``alfred scribe attest`` CLI's ``record_attest_outcome`` appends from a separate process,
        uncorrelated with daemon ticks. So the read-then-rewrite IS raced. Both the appender and this
        prune hold the STABLE capture-sink lock (:func:`capture_sink_lock`) across their critical
        section, so an append can't land between this read and the ``os.replace`` and be clobbered."""
        enrollment_dir = self._config.diarize.enrollment_dir
        if not enrollment_dir:
            return 0  # the voice-enrollment feature is dormant — no sink to prune (summary shows 0)
        sink = Path(enrollment_dir) / LEARNING_DIRNAME / CAPTURE_NAME
        try:
            sink_present = sink.is_file()
        except OSError:
            return 0  # D5: a stat-EACCES on the learning dir must not escape after encounters were
            #           processed — swallow it so the tick's summary + operator-attention still emit
        if not sink_present:
            return 0
        prune_days = _DIARIZE_STATS_PRUNE_DAYS
        if schedule is not None:
            window = sched_mod.class_window_days(schedule, sched_mod.TELEMETRY_CLASS)
            if window is None:
                # the published schedule declares diarize_stats NEVER-pruned → skip the rolling prune.
                self._latch_log(
                    "diarize_never_pruned",
                    "scribe.retention.sweep.diarize_stats_never_pruned",
                    detail="the published s.50 schedule declares diarize_stats never-pruned "
                           "(window_days null) — the telemetry rolling prune is SKIPPED (deliberate, "
                           "not broken). Latched once.")
                return 0
            prune_days = window
        cutoff = now_dt - timedelta(days=prune_days)
        # Hold the sink lock across READ + REWRITE (finding 19) so a concurrent attest-CLI append is
        # serialized, not clobbered by the read-then-replace snapshot.
        with capture_sink_lock(enrollment_dir):
            return self._prune_sink_locked(sink, cutoff)

    def _prune_sink_locked(self, sink: Path, cutoff: datetime) -> int:
        """The read → age-filter → atomic-rewrite body, run UNDER the capture-sink lock."""
        kept: list[str] = []
        dropped = 0
        try:
            with open(sink, encoding="utf-8", errors="replace") as f:
                for raw_line in f:
                    line = raw_line.rstrip("\n")
                    if not line.strip():
                        continue  # a blank line carries no row — drop it (uncounted; not telemetry)
                    row = None
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        kept.append(line)   # torn/corrupt → PRESERVE (can't date it → never drop)
                        continue
                    dt = _parse_iso(row.get("ts")) if isinstance(row, dict) else None
                    if dt is None:
                        kept.append(line)   # undateable / tz-naive row → PRESERVE (finding 16)
                        continue
                    if dt < cutoff:
                        dropped += 1        # positively over-window → DROP (counted)
                        continue
                    kept.append(line)       # within the window → keep
        except OSError:
            return 0  # a read error must never crash the sweep — the prune is best-effort
        if dropped == 0:
            return 0  # nothing aged out → no rewrite (never churn the sink every tick)
        try:
            self._atomic_rewrite_lines(sink, kept)
        except OSError:
            log.warning(
                "scribe.retention.sweep.prune_rewrite_failed",
                detail="the diarize_stats prune rewrite failed (disk/perms) — rows NOT dropped this "
                       "tick; the stale .prune.tmp is cleaned up, retry next tick. Best-effort.")
            return 0
        return dropped

    @staticmethod
    def _atomic_rewrite_lines(path: Path, lines: list[str]) -> None:
        """Atomic, fsync-durable text rewrite (temp → ``os.replace``, 0600) so the telemetry sink is
        NEVER observed torn. Each kept line is newline-terminated; an empty result yields an empty
        (still-valid) JSONL file."""
        data = ("\n".join(lines) + "\n") if lines else ""
        tmp = path.with_name(path.name + ".prune.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                tmp.unlink()  # never leave a stale .prune.tmp on a write/fsync failure (finding 16 sibling)
            raise
        else:
            os.close(fd)
        try:
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()  # replace failed — clean the tmp so it does not accrete each tick
            raise

    # --- resource resolution ---------------------------------------------

    def _resolved_retained_dir(self) -> Path:
        """The sealed-blob store + relocated transcripts dir. Delegates to the SHARED
        :func:`retention.resolved_retained_dir` (the single source of truth) so the sweep, the backup,
        and the destroy-purge can NEVER silently target a different tree than the seal writes to.
        Empty config ⇒ ``<input_dir parent>/retained`` (STAY-C's ``input_dir`` is ``<STAYC_DATA>/inbox``,
        so ``<STAYC_DATA>/retained`` — under ReadWritePaths, per §3.7)."""
        return ret.resolved_retained_dir(self._config)

    def _resolve_sealer(self) -> "ret.Sealer | None":
        """The production sealer (13a's ``retention.make_default_sealer`` — the age / ``pyrage`` backend
        behind the ``Sealer`` seam), or ``None`` — latched — if ``pyrage`` is not installed. A missing
        backend never crashes the daemon loop; sealing is simply skipped until the (declared) dep is
        present. The sweep stays dep-agnostic (it uses the injected sealer; tests pass a fake)."""
        try:
            return self._sealer_factory()
        except ret.SealerUnavailable:
            self._latch_log(
                "no_sealer",
                "scribe.retention.sweep.sealer_unavailable",
                detail="the age backend (pyrage) is not installed — retention sealing is SKIPPED until "
                       "the declared dep is present. Latched once.")
            return None

    def _resolve_pubkey(self) -> bytes:
        """The recipient PUBLIC key the sweep seals to, read from ``retention.seal_public_key_path`` —
        a canonical age recipient (``age1…``), returned as UTF-8 bytes for the ``Sealer`` protocol.
        Returns ``b""`` — latched — when the path is unset (no keygen yet — the 13d ceremony), the file
        is absent, or its content is not age-recipient-shaped. Re-checked every sweep, so sealing begins
        automatically once the operator runs keygen (no restart). The lightweight ``age1`` prefix check
        is only for the once-latched malformed signal; ``AgeSealer.seal`` does the canonical parse."""
        path = self._config.retention.seal_public_key_path
        if not path:
            self._latch_log(
                "no_pubkey",
                "scribe.retention.sweep.no_seal_public_key",
                detail="retention.seal_public_key_path is unset — no recipient key to seal to (the "
                       "keygen ceremony is slice 13d). Retention sealing is SKIPPED. Latched once.")
            return b""
        p = Path(path)
        try:
            key_present = p.is_file()
        except OSError:
            # D5: a stat-EACCES (an unsearchable PARENT dir — pathlib re-raises EACCES on is_file, only
            # ENOENT/ENOTDIR/EBADF/ELOOP are swallowed) is the same operator-facing condition as an
            # unreadable key. Latch the unreadable signal + return b"" — NEVER let it escape _run_sync
            # and kill the tick's ILB summary + the needs_operator_attention emission.
            self._latch_log(
                "pubkey_unreadable",
                "scribe.retention.sweep.seal_public_key_unreadable",
                detail="the retention.seal_public_key_path could not be stat'd (unsearchable parent "
                       "dir / perms) — retention sealing is SKIPPED until it is reachable. Latched once.")
            return b""
        if not key_present:
            self._latch_log(
                "no_pubkey",
                "scribe.retention.sweep.no_seal_public_key",
                detail="the configured retention.seal_public_key_path does not exist yet — retention "
                       "sealing is SKIPPED until the keygen ceremony (13d) writes it. Latched once.")
            return b""
        try:
            recipient = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            # finding 37: an unreadable key file (perms 0000 / EIO) previously returned b"" SILENTLY —
            # the ONLY unlatched branch. Latch an observation so the operator greps a key signal, not
            # just a bare sealing_available=False in the summary.
            self._latch_log(
                "pubkey_unreadable",
                "scribe.retention.sweep.seal_public_key_unreadable",
                detail="the configured retention.seal_public_key_path exists but could NOT be read "
                       "(perms / IO error) — retention sealing is SKIPPED until it is readable. "
                       "Latched once.")
            return b""
        # finding 17: a full bech32 canonical check (not a bare 'age1' prefix) — a truncated/typo'd
        # recipient that still starts with 'age1' would otherwise clear the check, leave
        # sealing_available=True, and fail EVERY seal with an anonymous per-encounter SealError loop.
        if not ret.is_valid_age_recipient(recipient):
            self._latch_log(
                "pubkey_malformed",
                "scribe.retention.sweep.seal_public_key_malformed",
                detail=f"the seal public key is not a canonical age recipient (an '{_AGE_RECIPIENT_PREFIX}"
                       f"…' bech32 string with a valid checksum + a non-degenerate point) — retention "
                       f"sealing is SKIPPED (a malformed key would fail every seal). Latched once.")
            return b""
        # A good key clears any prior absent/malformed/unreadable latch so a later regression re-warns.
        self._latched.discard("no_pubkey")
        self._latched.discard("pubkey_malformed")
        self._latched.discard("pubkey_unreadable")
        return recipient.encode("utf-8")

    # --- latch helper -----------------------------------------------------

    def _latch_log(self, key: str, event: str, *, detail: str) -> None:
        """Emit ``event`` ONCE per daemon lifetime (a steady-state condition — no schedule, no key,
        inactive store — must not spam every 30 s). Latched once-per-lifecycle, per the surveyor
        VAULT-STATE observability convention (CLAUDE.md)."""
        if key in self._latched:
            return
        self._latched.add(key)
        log.info(event, detail=detail)

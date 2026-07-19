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
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

from alfred.scribe import retention as ret
from alfred.scribe.close_manifest import CLOSE_SENTINEL_NAME
from alfred.scribe.config import (
    RETENTION_MODE_RETAINED, RETENTION_MODE_TRANSIENT, ScribeConfig,
)
from alfred.scribe.enroll_learning import CAPTURE_NAME, LEARNING_DIRNAME
from alfred.scribe.identity import EncounterIdentityError, compute_encounter_id
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
    """Parse an ISO-8601 ``ts`` (the enroll-sink row + event timestamp shape). ``None`` on anything
    unparseable — the caller treats an undateable row as NOT-positively-old (preserve, never drop)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


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
    review_due: int = 0              # §4 over-window PHI classes surfaced (0 until 13c's schedule)
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

        # (1)+(2) seal READY / defensively-seal abandoned + (§E) dispose empty-closed — enumerate subdirs.
        if can_process:
            self._process_encounters(state, now_dt, now_iso, sealer, pubkey, can_seal, summary)

        # (3) over-window surfacing SEAM (§4) — no schedule ⇒ latched ILB, never auto-destroy.
        self._surface_over_window(now_dt, summary)

        # (4) diarize_stats rolling prune (§0.1/§4) — PHI-free log rotation, no retention.* event.
        summary.pruned_telemetry_rows = self._prune_diarize_stats(now_dt)

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
        if not input_dir.is_dir():
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

        if closed and not ret.encounter_has_chunks(enc_dir):
            # §E — a CLOSED zero-chunk encounter (clinician opened, no audio, /close) is DISPOSED, not
            # sealed: the PHI-named dir must not persist with 'nothing to do' logged forever. Needs no
            # crypto (nothing to seal), so it proceeds even when can_seal is False (pre-13d keygen).
            gate = "empty_closed"
        elif is_ready and can_seal:
            gate = "ready"
        elif not closed and can_seal and self._is_abandoned(enc_dir, now_dt, grace_days):
            gate = "abandoned"
        else:
            # Still accumulating (fresh, un-closed), closed-but-not-yet-READY WITH chunks
            # (DRAFTED/INCOMPLETE — the pipeline's own signals cover it), inside the abandon grace, or
            # a READY/abandoned encounter we cannot seal yet (no key) → not eligible this sweep.
            summary.skipped += 1
            return

        outcome = ret.seal_encounter(
            enc_dir, encounter_id, events=self._ev, sealer=sealer,
            recipient_public_key=pubkey, retained_dir=retained_dir,
            mode=cfg.retention.mode, now=now_iso)
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

    def _surface_over_window(self, now_dt: datetime, summary: RetentionSweepSummary) -> None:
        """The s.50 schedule artifact ships in 13c. This is the SEAM: resolve the schedule; with none
        present, emit a latched intentionally-left-blank observation and skip surfacing WITHOUT
        failing. 13c plugs the per-class window comparison in below (enumerate sealed encounters →
        compare age vs class window → set ``summary.review_due``). This path NEVER auto-destroys —
        surfacing is surface-only (§4); destruction stays the explicit operator playbook (§5)."""
        schedule = self._load_schedule()
        summary.schedule_present = schedule is not None
        if schedule is None:
            self._latch_log(
                "no_schedule",
                "scribe.retention.sweep.no_schedule_published",
                detail="no s.50 retention schedule is published yet (the schedule artifact ships in "
                       "slice 13c) — over-window surfacing is SKIPPED; the sweep NEVER auto-destroys. "
                       "Latched once.")
            return
        # 13c SEAM — compare each sealed encounter's age against its class window and set
        # summary.review_due (a due count surfaced to the morning review; NEVER an auto-destroy).
        # Unreachable in 13b (the schedule loader always returns None until 13c ships the reader).

    def _load_schedule(self) -> dict | None:
        """13b stub: there is no schedule reader/writer until 13c, so a schedule is present only if
        the operator's ``schedule_path`` file already exists (it does not in 13b). Returns the parsed
        schedule dict, or ``None`` when absent/unset — the seam 13c fills in with real parsing +
        fail-closed-on-malformed."""
        path = self._config.retention.schedule_path
        if not path:
            return None
        p = Path(path)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 13c owns fail-closed-on-malformed surfacing; in 13b a malformed file is treated as
            # "no usable schedule" (skip surfacing, never crash the sweep).
            return None
        return data if isinstance(data, dict) else None

    # --- (4) diarize_stats rolling prune (§0.1/§4) -----------------------

    def _prune_diarize_stats(self, now_dt: datetime) -> int:
        """Age-based rolling prune of the PHI-FREE ``diarize_stats``/``attest_outcome`` telemetry sink
        (``<enrollment_dir>/learning/attest_capture.jsonl``) — absorbs the queued P4-5b unowned
        180-day prune. Drops rows whose ``ts`` is older than the window; PRESERVES every row it cannot
        positively date (unparseable/torn or missing ts) — the prune is AGE-BASED, so a row whose age
        is unknown is never positively over-window and is carried through (fail-open, non-destructive,
        mirrors the enroll-sink skip-not-fatal discipline). Atomic temp→replace rewrite (never a torn
        sink); emits NO ``retention.*`` event (log rotation, not a PHI destruction). Returns the row
        count dropped (folded into the sweep summary — intentionally-left-blank).

        Concurrency note: the sink's only writer is the pipeline (``record_diarize_stats`` during
        ``run_sweep``), which runs strictly BEFORE this sweep within the daemon tick; the ingest
        server (the only other loop task) never writes the telemetry sink — so this read-then-rewrite
        has no concurrent appender."""
        enrollment_dir = self._config.diarize.enrollment_dir
        if not enrollment_dir:
            return 0  # the voice-enrollment feature is dormant — no sink to prune (summary shows 0)
        sink = Path(enrollment_dir) / LEARNING_DIRNAME / CAPTURE_NAME
        if not sink.is_file():
            return 0
        cutoff = now_dt - timedelta(days=_DIARIZE_STATS_PRUNE_DAYS)
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
                        kept.append(line)   # undateable row → PRESERVE
                        continue
                    if dt < cutoff:
                        dropped += 1        # positively over-window → DROP (counted)
                        continue
                    kept.append(line)       # within the window → keep
        except OSError:
            return 0  # a read error must never crash the sweep — the prune is best-effort
        if dropped == 0:
            return 0  # nothing aged out → no rewrite (never churn the sink every tick)
        self._atomic_rewrite_lines(sink, kept)
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
        finally:
            os.close(fd)
        os.replace(tmp, path)

    # --- resource resolution ---------------------------------------------

    def _resolved_retained_dir(self) -> Path:
        """The sealed-blob store + relocated transcripts dir. Empty config ⇒ derive
        ``<input_dir parent>/retained`` (STAY-C's ``input_dir`` is ``<STAYC_DATA>/inbox``, so this is
        ``<STAYC_DATA>/retained`` — under ReadWritePaths, per §3.7) — a per-instance-correct default,
        never a single-instance literal (mirrors ``ScribeBugConfig.dir``)."""
        configured = self._config.retention.retained_dir
        if configured:
            return Path(configured)
        return Path(self._config.input_dir).parent / "retained"

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
        if not p.is_file():
            self._latch_log(
                "no_pubkey",
                "scribe.retention.sweep.no_seal_public_key",
                detail="the configured retention.seal_public_key_path does not exist yet — retention "
                       "sealing is SKIPPED until the keygen ceremony (13d) writes it. Latched once.")
            return b""
        try:
            recipient = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return b""
        if not recipient.startswith(_AGE_RECIPIENT_PREFIX):
            self._latch_log(
                "pubkey_malformed",
                "scribe.retention.sweep.seal_public_key_malformed",
                detail=f"the seal public key is not an age recipient (expected an '{_AGE_RECIPIENT_PREFIX}"
                       f"…' string) — retention sealing is SKIPPED (a wrong key would seal to an "
                       f"un-openable recipient). Latched once.")
            return b""
        # A good key clears any prior absent/malformed latch so a later disappearance re-warns.
        self._latched.discard("no_pubkey")
        self._latched.discard("pubkey_malformed")
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

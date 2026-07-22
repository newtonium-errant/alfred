"""STAY-C dedicated backup — the CODE side (task #13 §5.4 / §11 Q3, slice 13d-4).

INERT in-repo. This module ships three building blocks; it NEVER inits a restic repo,
NEVER installs a timer, and NEVER runs a scheduled backup on import. The systemd
timer/service install is OPERATOR-GATED (``scripts/install_stayc_backup.py``, mirroring
``install_stayc_unit.py``) — the operator runs ``restic init`` + starts the timer as the
real-data-gate step, never a plain checkout/deploy.

  1. :func:`build_backup_set` — the EXACT restic include paths + exclude patterns. NEVER
     backs up ``data/`` wholesale (that would sweep in the biometric ``enrollment`` store,
     recon §2) and NEVER backs up the plaintext transcript ledger (which stays LUKS on-box).
  2. :func:`seal_file_for_backup` + :func:`sealed_backup_paths` — the SEAL-BEFORE-BACKUP
     primitive (operator ruling A, 2026-07-21): the plaintext transcript ledger + the vault
     clinical_note are age-SEALED to the SAME offline recipient BEFORE they leave the box, so
     the ENTIRE off-box archive is uniformly crypto-shredded (undecryptable without the offline
     private key). The working transcript+note stay LUKS-plaintext on-box (Jamie uses them);
     only the BACKUP copy is a sealed ``.age`` blob.
  3. :func:`purge_encounter` — the destroy step-3f helper: ``restic rewrite --exclude <the
     enc's paths> --forget`` + ``prune`` on the DEDICATED repo, then ASSERT ``restic find
     <enc>`` is EMPTY. A non-empty find (or an unavailable repo/binary) is an INCOMPLETE
     destruction — the destroy CLI (13d-3) fails loud + does NOT emit ``retention.destroyed``.

DEDICATED repo (recon §4): STAY-C is backed up by its OWN restic repo + 10-yr keep policy,
SEPARATE from the general algernon nightly (which caps at 2 yearly snapshots — it would prune
the 10-yr archive out from under the s.50 schedule). Repo creds come from the STAY-C unit
EnvironmentFile as ``STAYC_RESTIC_REPO`` + ``STAYC_RESTIC_PASSWORD_FILE`` (preferred) /
``STAYC_RESTIC_PASSWORD`` — NEVER hardcoded, NEVER the nightly algernon repo (per OQ6).

THE ENC-ID-NAMING INVARIANT (load-bearing — why purge is uniform): EVERY off-box artifact of
an encounter is enc-id-named — ``<enc>.age`` (sealed audio), ``<enc>.manifest.json`` (PHI-free
sidecar), ``<enc>.transcript.age`` + ``<enc>.note.age`` (the seal-before-backup copies). So the
destroy-purge excludes exactly ``<enc>.*`` and the ``restic find <enc>`` assert-empty is a single
uniform check across ALL of the encounter's off-box PHI. This naming is FORCED by the ratified
purge contract (assert ``restic find <id>`` empty), not a free choice.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from alfred.scribe import ledger
from alfred.scribe import retention as ret
from alfred.scribe.config import ScribeConfig

log = structlog.get_logger("scribe.backup")

# --- dedicated-repo credential env (OQ6 — sourced from the STAY-C unit EnvironmentFile) --------
# NEVER hardcoded, NEVER the nightly algernon repo. PASSWORD_FILE is preferred (a path, not a
# secret in the process env — mirrors the proven algernon-backup ~/.config/restic/password);
# PASSWORD is the fallback the ruling named.
ENV_RESTIC_REPO = "STAYC_RESTIC_REPO"
ENV_RESTIC_PASSWORD_FILE = "STAYC_RESTIC_PASSWORD_FILE"
ENV_RESTIC_PASSWORD = "STAYC_RESTIC_PASSWORD"

# The dedicated snapshot tag on the dedicated repo (recon §4 — its own tag + keep policy).
RESTIC_TAG = "stayc"

# The enc-id-named sealed BACKUP copies (ruling A — the off-box transcript + note are age blobs,
# NOT plaintext). Distinct suffixes from the on-box ``.age`` audio blob so a backup set that
# co-locates all three stays self-identifying.
SEAL_TRANSCRIPT_SUFFIX = ".transcript.age"
SEAL_NOTE_SUFFIX = ".note.age"

# A prune on a 10-yr archive repacks the repo — it can take minutes-to-longer against a network
# (SFTP) repo. Generous so a legitimate long prune is never killed mid-repack (which could leave
# the repo needing an ``unlock``). rewrite/find are lighter but share the ceiling for simplicity.
_RESTIC_TIMEOUT = 3600


@dataclass(frozen=True)
class BackupSet:
    """The restic include paths + exclude patterns for the dedicated STAY-C job. ``includes`` are
    absolute tree roots restic backs up; ``excludes`` are patterns it must NEVER capture (the
    plaintext transcript ledger + the biometric enrollment store)."""

    includes: list[Path] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PurgeResult:
    """The outcome of a per-encounter backup purge (destroy step 3f). ``complete`` is True ONLY
    when the post-purge ``restic find <enc>`` returned EMPTY — the destroy CLI gates
    ``retention.destroyed`` on it (an incomplete purge is an incomplete destruction). ``reason``
    names the failure for the fail-loud path; ``excluded_paths`` is the enc-id-named artifact set
    the purge targeted (surfaced by ``--dry-run`` so the operator eyeballs it first)."""

    complete: bool
    encounter_id: str
    excluded_paths: list[str] = field(default_factory=list)
    dry_run: bool = False
    reason: str = ""


# --- retained-dir + sealed-staging-dir resolution (mirror the sweep's derive) -------------------


def resolved_sealed_backup_dir(config: ScribeConfig) -> Path:
    """The staging dir for the enc-id-named SEALED backup copies of the transcript + note (ruling A).
    Derived ``<retained_dir>/backup_sealed`` — UNDER the retained tree (already ReadWritePaths) so
    the backup job can write the sealed copies, and so backing up the retained root captures them with
    NO separate include. Kept OUT of the transcripts/ subdir (the plaintext ledger, structurally
    excluded). Delegates to the shared :func:`retention.resolved_retained_dir` (single source of truth
    — purge/backup can never target a different tree than the seal writes to)."""
    return ret.resolved_retained_dir(config) / "backup_sealed"


# --- (1) the backup set (include/exclude) -------------------------------------------------------


def build_backup_set(config: ScribeConfig) -> BackupSet:
    """The EXACT restic include paths + exclude patterns for the dedicated STAY-C job.

    Includes (under ruling A — off-box archive is uniformly sealed):
      * ``<retained_dir>/*.age`` (sealed audio) + ``<retained_dir>/*.manifest.json`` (PHI-free
        sidecars) — restic backs up the ``retained_dir`` TREE, minus the plaintext transcripts.
      * ``<sealed_backup_dir>`` — the enc-id-named sealed transcript + note copies (seal-before-backup).

    Excludes (STRUCTURAL — never off-box):
      * ``<retained_dir>/transcripts`` — the PLAINTEXT transcript ledger stays LUKS on-box; only its
        SEALED copy leaves the box (ruling A). Backing up the plaintext would put offshore plaintext
        PHI on the Hetzner repo (the exact posture ruling A eliminates).
      * ``**/enrollment`` — the biometric voice-preset store (recon §2 / P4-5 ⚑): NEVER backed up.

    Deliberately does NOT back up ``data/`` wholesale nor the raw vault tree — the note's durable
    off-box copy is its SEALED staging blob, so no plaintext clinical record ever leaves the box."""
    retained = ret.resolved_retained_dir(config)
    excludes = [
        # the plaintext transcript ledger — LUKS on-box only, sealed copy goes off-box instead.
        str(retained / "transcripts"),
        # the biometric enrollment store — structurally unreachable (recon §2). Belt: also a glob
        # so a relocated enrollment dir under any include root is still refused.
        "**/enrollment",
    ]
    enrollment_dir = config.diarize.enrollment_dir
    if enrollment_dir:
        excludes.append(str(enrollment_dir))
    # ONE include root: the retained tree. The sealed-staging dir (<retained>/backup_sealed) rides
    # UNDER it, so a separate include would be REDUNDANT; the ``.age`` audio + PHI-free sidecars are
    # direct children. Only the plaintext transcripts/ subdir is carved back out via the exclude.
    return BackupSet(includes=[retained], excludes=excludes)


# --- (2) seal-before-backup (ruling A) ----------------------------------------------------------


def sealed_backup_paths(config: ScribeConfig, encounter_id: str) -> tuple[Path, Path]:
    """The enc-id-named dest paths for an encounter's SEALED backup copies:
    ``(<sealed_backup_dir>/<enc>.transcript.age, <sealed_backup_dir>/<enc>.note.age)``."""
    d = resolved_sealed_backup_dir(config)
    return (d / f"{encounter_id}{SEAL_TRANSCRIPT_SUFFIX}",
            d / f"{encounter_id}{SEAL_NOTE_SUFFIX}")


def seal_file_for_backup(
    src_path: str | Path, dest_path: str | Path, *,
    sealer: ret.Sealer, recipient_public_key: bytes,
) -> bool:
    """SEAL-BEFORE-BACKUP primitive (ruling A): age-seal ONE plaintext file (transcript ledger or
    vault note) to the offline recipient, writing an atomic ``.age`` blob at ``dest_path``. Returns
    True on a written+self-verified blob, False if the source is absent (nothing to seal) — the
    caller counts it. Raises :class:`~alfred.scribe.retention.SealError` on a bad recipient (the
    typed error the caller isolates), never an untyped crypto exception.

    Self-verifies the blob is a well-formed age envelope before returning (the seal-time discipline,
    mirrors ``retention.seal_encounter`` step 3) so a torn/garbage backup copy is caught here, not on
    a restore a decade later. Uses the R7-hardened ``retention._atomic_write_bytes`` (fsync-durable)."""
    src = Path(src_path)
    try:
        plaintext = src.read_bytes()
    except FileNotFoundError:
        return False  # no source (e.g. a note-less encounter) — nothing to seal, caller counts it
    blob = sealer.seal(plaintext, recipient_public_key)
    if not sealer.verify_wellformed(blob):
        raise ret.SealError(
            f"seal-before-backup produced a malformed age blob for {src.name!r} — refusing to write "
            f"an unverifiable backup copy")
    ret._atomic_write_bytes(Path(dest_path), blob)
    return True


def transcript_source_path(config: ScribeConfig, encounter_id: str) -> Path:
    """The on-box relocated transcript ledger for an encounter — the plaintext source
    seal-before-backup seals. Lives at ``<retained_dir>/transcripts/<enc>.transcript.json`` (where
    ``retention._relocate_ledger`` puts it)."""
    return ledger.ledger_path(ret.resolved_retained_dir(config) / "transcripts", encounter_id)


# --- (3) per-encounter destroy purge (destroy step 3f) ------------------------------------------


def encounter_backup_globs(config: ScribeConfig, encounter_id: str) -> list[str]:
    """The exact ``--exclude`` path patterns for an encounter's off-box artifacts — ALL enc-id-named
    (the invariant): the sealed audio blob, the PHI-free sidecar, and the sealed transcript + note
    backup copies. restic ``--exclude`` matches these absolute paths in every snapshot. Explicit
    per-artifact (NOT a ``<enc>.*`` glob) so a purge can NEVER over-match a differently-named file."""
    retained = ret.resolved_retained_dir(config)
    sealed_backup = resolved_sealed_backup_dir(config)
    return [
        str(retained / f"{encounter_id}{ret.SEAL_BLOB_SUFFIX}"),                 # sealed audio
        str(retained / f"{encounter_id}{ret.SEAL_MANIFEST_SIDECAR_SUFFIX}"),     # PHI-free sidecar
        str(sealed_backup / f"{encounter_id}{SEAL_TRANSCRIPT_SUFFIX}"),          # sealed transcript
        str(sealed_backup / f"{encounter_id}{SEAL_NOTE_SUFFIX}"),                # sealed note
    ]


def _restic_env() -> dict[str, str] | None:
    """The environment for a restic call against the DEDICATED STAY-C repo, or ``None`` when the
    dedicated-repo creds are not configured (``STAYC_RESTIC_REPO`` unset) — the caller fails closed
    (an unconfigured purge is an INCOMPLETE destruction, never a silent success). NEVER falls back to
    the general algernon repo (OQ6). PASSWORD_FILE is preferred over PASSWORD (a path, not a secret
    in the env)."""
    repo = os.environ.get(ENV_RESTIC_REPO)
    if not repo:
        return None
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = repo
    # Defense-in-depth for a DESTRUCTION command: a stale inherited RESTIC_REPOSITORY_FILE conflicts
    # with the RESTIC_REPOSITORY we set (restic errors on both), and a RESTIC_PASSWORD_COMMAND would
    # SHADOW our intended password source — either could silently redirect the purge at a different
    # repo / open with a different key. Drop both so the STAY-C dedicated creds are the ONLY ones in play.
    env.pop("RESTIC_REPOSITORY_FILE", None)
    env.pop("RESTIC_PASSWORD_COMMAND", None)
    pw_file = os.environ.get(ENV_RESTIC_PASSWORD_FILE)
    pw = os.environ.get(ENV_RESTIC_PASSWORD)
    if pw_file:
        env["RESTIC_PASSWORD_FILE"] = pw_file
        env.pop("RESTIC_PASSWORD", None)  # a stale inherited RESTIC_PASSWORD must not shadow the file
    elif pw:
        env["RESTIC_PASSWORD"] = pw
        env.pop("RESTIC_PASSWORD_FILE", None)
    else:
        return None  # a repo with no password source can't be opened — fail closed
    return env


def _run_restic(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run one ``restic`` subcommand against the dedicated repo. Non-zero exits are logged with BOTH
    stderr AND a stdout tail (builder.md subprocess discipline — rate-limit / lock messages can land
    on stdout; the ``stdout_tail=''`` sentinel is grep-able for 'no diagnostic output at all')."""
    proc = subprocess.run(
        ["restic", *args], capture_output=True, text=True, env=env, timeout=_RESTIC_TIMEOUT)
    if proc.returncode != 0:
        log.warning(
            "scribe.backup.restic_nonzero_exit", subcommand=args[0] if args else "",
            code=proc.returncode, stderr=(proc.stderr or "")[:500],
            stdout_tail=(proc.stdout[-2000:] if proc.stdout else ""))
    return proc


def _find_is_empty(proc: subprocess.CompletedProcess, encounter_id: str) -> bool:
    """True iff ``restic find <encounter_id>`` found NOTHING — the encounter_id appears in no matched
    path. restic exits 0 for a no-match find (printing 'No matching files found'), so success is
    decided by the encounter_id's ABSENCE from stdout, not the exit code. A non-zero exit (repo
    unreachable) is NOT empty (fail-closed — an unverifiable purge is incomplete)."""
    if proc.returncode != 0:
        return False
    return encounter_id not in (proc.stdout or "")


def purge_encounter(
    config: ScribeConfig, encounter_id: str, *, dry_run: bool = False,
) -> PurgeResult:
    """Purge ALL of an encounter's off-box artifacts from the DEDICATED STAY-C restic repo, then
    ASSERT they are gone (destroy step 3f). The strict order:

      1. resolve the enc-id-named exclude set (:func:`encounter_backup_globs`).
      2. ``restic rewrite --exclude <each path> --forget`` — rewrites every affected snapshot
         WITHOUT the artifacts, dropping the originals.
      3. ``restic prune`` — frees the excised ciphertext blocks from the repo.
      4. ``restic find <enc>`` — MUST return EMPTY, else the destruction is INCOMPLETE.

    ``dry_run`` runs rewrite with ``--dry-run`` (restic makes NO change), SKIPS prune, and returns
    ``complete=False, dry_run=True`` with the exclude set — the destroy CLI surfaces it for the
    operator to eyeball BEFORE the real run; a dry-run NEVER asserts complete.

    Fail-closed: an unconfigured dedicated repo (``STAYC_RESTIC_REPO`` unset), a missing ``restic``
    binary, a non-zero rewrite/prune, or a non-empty post-purge find ALL yield ``complete=False`` with
    a ``reason`` — the destroy CLI (13d-3) blocks ``retention.destroyed`` on ``complete``, so a purge
    that cannot be CONFIRMED never lets a destruction claim success (a destruction leaving a backup
    copy is incomplete, per Q3)."""
    excluded = encounter_backup_globs(config, encounter_id)
    if shutil.which("restic") is None:
        return PurgeResult(
            complete=False, encounter_id=encounter_id, excluded_paths=excluded, dry_run=dry_run,
            reason="restic binary not found on PATH — cannot purge the dedicated backup repo; the "
                   "destruction is INCOMPLETE until the backup copies are confirmed gone")
    env = _restic_env()
    if env is None:
        return PurgeResult(
            complete=False, encounter_id=encounter_id, excluded_paths=excluded, dry_run=dry_run,
            reason=f"the dedicated STAY-C restic repo is not configured ({ENV_RESTIC_REPO} / a "
                   f"password source unset) — refusing to fall back to the general repo; the "
                   f"destruction is INCOMPLETE until the backup copies are confirmed gone")

    rewrite_args = ["rewrite", "--forget"]
    for path in excluded:
        rewrite_args += ["--exclude", path]
    if dry_run:
        rewrite_args.append("--dry-run")
    rewrite = _run_restic(rewrite_args, env)
    if rewrite.returncode != 0:
        return PurgeResult(
            complete=False, encounter_id=encounter_id, excluded_paths=excluded, dry_run=dry_run,
            reason=f"restic rewrite exited {rewrite.returncode} — the backup purge did NOT complete")

    if dry_run:
        # A dry-run makes NO repo change (no prune, no assert) — it exists to show the operator the
        # exact exclude set before the irreversible real run. NEVER complete.
        return PurgeResult(
            complete=False, encounter_id=encounter_id, excluded_paths=excluded, dry_run=True,
            reason="dry-run — no snapshot rewritten, no prune, no assert (preview only)")

    prune = _run_restic(["prune"], env)
    if prune.returncode != 0:
        return PurgeResult(
            complete=False, encounter_id=encounter_id, excluded_paths=excluded,
            reason=f"restic prune exited {prune.returncode} — the excised blocks may NOT be freed; "
                   f"the backup purge did NOT complete")

    # ASSERT the encounter is gone from every snapshot (the load-bearing check). DELIBERATELY
    # UN-tagged: `rewrite --forget --exclude` above runs REPO-WIDE, so the verify MUST be at least
    # as broad — a `--tag stayc` find would MISS a non-stayc-tagged snapshot the rewrite failed to
    # strip and falsely report complete=True (a backup surviving a "complete" destruction). A
    # destruction backstop verifies at least as broadly as it mutates (over-report → fail closed).
    found = _run_restic(["find", encounter_id], env)
    if _find_is_empty(found, encounter_id):
        log.info(
            "scribe.backup.purge_complete", encounter_id=encounter_id, excluded=len(excluded),
            detail="the encounter's off-box artifacts were rewritten out + pruned, and restic find "
                   "confirms the encounter is gone from every snapshot (backup purge complete).")
        return PurgeResult(complete=True, encounter_id=encounter_id, excluded_paths=excluded)
    return PurgeResult(
        complete=False, encounter_id=encounter_id, excluded_paths=excluded,
        reason="restic find STILL returns the encounter after rewrite+prune — the backup purge did "
               "NOT complete (a destruction that leaves a backup copy is INCOMPLETE, Q3); fail loud")


# --- (4) backup_run — seal-before-backup orchestration (13d-4b, INERT) ---------------------------


@dataclass(frozen=True)
class BackupRunResult:
    """One dedicated-backup run's outcome (13d-4b). ``restic_ran`` is True only when the restic backup
    exited 0; ``malformed_notes`` counts clinical_notes that could NOT be parsed (skip-loud-and-count —
    a backup is NON-destructive, so an un-backed-up malformed note is a loud signal, never fatal, unlike
    the destroy WARN-1 refuse); ``multi_note_encounters`` counts amended encounters whose extra notes
    are NOT captured under the single ``<enc>.note.age`` name (a documented limitation)."""

    encounters: int = 0
    transcripts_sealed: int = 0
    notes_sealed: int = 0
    malformed_notes: int = 0
    multi_note_encounters: int = 0
    restic_ran: bool = False
    dry_run: bool = False
    reason: str = ""


def backup_run(
    config: ScribeConfig, vault_path, *, sealer: ret.Sealer, recipient_public_key: bytes,
    dry_run: bool = False,
) -> BackupRunResult:
    """The dedicated STAY-C backup orchestration (13d-4b) — SEAL-BEFORE-BACKUP then restic. INERT: only
    ever invoked by the operator-gated ``retention backup-run`` CLI (the timer's ExecStart), never on
    import. For each sealed encounter (a ``<enc>.age`` blob in the retained dir): age-seal its transcript
    ledger + its vault clinical_note into the enc-id-named sealed-staging copies (ruling A — the off-box
    archive is uniformly crypto-shredded), seal-IF-ABSENT (age is non-deterministic, so re-sealing would
    churn restic dedup; an AMENDED note needs its staging blob cleared to re-seal — documented). THEN
    ``restic backup`` the :func:`build_backup_set` (retained tree + sealed staging; plaintext transcripts
    + ``**/enrollment`` structurally excluded) tagged :data:`RESTIC_TAG` on the DEDICATED repo.

    MALFORMED NOTE posture (skip-loud-and-count, distinct from the destroy's fail-loud refuse): a backup
    is NON-destructive, so a clinical_note that can't be parsed is simply not sealed — counted +
    surfaced (never silently dropped, never fatal). Returns a :class:`BackupRunResult`; ``dry_run`` seals
    NOTHING + runs NO restic (a plan preview). NEVER raises on a per-encounter seal error (isolated)."""
    retained = ret.resolved_retained_dir(config)
    try:
        blobs = sorted(retained.glob(f"*{ret.SEAL_BLOB_SUFFIX}"))
    except OSError:
        blobs = []

    transcripts_sealed = notes_sealed = multi = 0
    malformed_set: set = set()
    for blob in blobs:
        enc = blob.name[:-len(ret.SEAL_BLOB_SUFFIX)]
        t_dest, n_dest = sealed_backup_paths(config, enc)
        matches, malformed = ret.resolve_note_paths(vault_path, enc)
        malformed_set.update(str(p) for p in malformed)
        if len(matches) > 1:
            multi += 1
            log.warning(
                "scribe.backup.multi_note_encounter", encounter_id=enc, count=len(matches),
                detail="an encounter resolved to MULTIPLE clinical_notes (amended) — only the first is "
                       "sealed under <enc>.note.age; the others are NOT in the off-box backup (documented "
                       "13d-4b limitation; the on-box notes remain the source of truth).")
        if dry_run:
            continue
        try:
            if not t_dest.exists() and seal_file_for_backup(
                    transcript_source_path(config, enc), t_dest,
                    sealer=sealer, recipient_public_key=recipient_public_key):
                transcripts_sealed += 1
            if matches and not n_dest.exists() and seal_file_for_backup(
                    matches[0], n_dest, sealer=sealer, recipient_public_key=recipient_public_key):
                notes_sealed += 1
        except ret.SealError:
            log.warning(
                "scribe.backup.seal_before_backup_failed", encounter_id=enc,
                detail="seal-before-backup failed for an encounter (bad recipient / malformed blob) — "
                       "ISOLATED; the run continues. This encounter's off-box copy is missing until fixed.")

    result_kwargs = dict(
        encounters=len(blobs), transcripts_sealed=transcripts_sealed, notes_sealed=notes_sealed,
        malformed_notes=len(malformed_set), multi_note_encounters=multi)

    if dry_run:
        return BackupRunResult(**result_kwargs, restic_ran=False, dry_run=True,
                               reason="dry-run — sealed nothing, ran no restic (plan preview)")

    if shutil.which("restic") is None:
        return BackupRunResult(**result_kwargs, restic_ran=False,
                               reason="restic binary not found on PATH — sealed the staging copies but "
                                      "did NOT run the backup")
    env = _restic_env()
    if env is None:
        return BackupRunResult(**result_kwargs, restic_ran=False,
                               reason=f"the dedicated STAY-C restic repo is not configured "
                                      f"({ENV_RESTIC_REPO} / a password source unset) — sealed the "
                                      f"staging copies but did NOT run the backup (never the general repo)")
    bs = build_backup_set(config)
    args = ["backup", *[str(p) for p in bs.includes], "--tag", RESTIC_TAG]
    for ex in bs.excludes:
        args += ["--exclude", ex]
    proc = _run_restic(args, env)
    ran = proc.returncode == 0
    log.info(
        "scribe.backup.run", encounters=len(blobs), transcripts_sealed=transcripts_sealed,
        notes_sealed=notes_sealed, malformed_notes=len(malformed_set), restic_ran=ran,
        detail=("dedicated backup complete" if ran else "seal done, restic backup FAILED")
        + (" — nothing to seal (no sealed encounters yet)" if not blobs else ""))
    return BackupRunResult(
        **result_kwargs, restic_ran=ran,
        reason="" if ran else f"restic backup exited {proc.returncode}")

"""The sovereign scribe pipeline state machine (scribe P2-d).

Wires the audio→transcript→note→ai_draft pipeline, ALL LOCAL-PYTHON (NOTE-3 —
NO ``claude -p`` / agent backend: claude -p egresses via cached OAuth even with
keys stripped, so the note path MUST stay local-loopback). The flow, per source:

  watch input_dir → guard_ingest (synthetic gate, fail-closed) → STT (local) →
  delta transcript → generate_verified_note (loopback Ollama) → grounding →
  render → vault_create clinical_note status=ai_draft via the stayc_clinical
  scope.

VERIFY-BEFORE-RENDER (HARD, the P2-c deferred commitment): the combined
``generate → verify → render`` is ONE choke function :func:`generate_verified_note`
— render_soap is called with the GroundingResult produced by verifying THE SAME
structured object. A note can NEVER be rendered without a grounding pass on its
own claims; nothing else in the pipeline calls ``render_soap``. So no code path
reaches ``vault_create`` with an unverified note.

NOTE-2 (type-change guard): the pipeline creates clinical_note status=ai_draft
ONLY — never attested_by / status=attested (that is scribe/attest.py's exclusive
path; the create-bypass scope guard refuses a born-attested note anyway).

NOTE-4 (PHI): source ids are SALTED, opaque encounter ids in EVERY mode
(``identity.compute_encounter_id`` — HMAC-SHA256 of the raw label under the
per-instance ``encounter_salt``); logs / state / audit carry ids + counts +
state-name ONLY — never title / transcript / note text. (P3-b1 closed the P2
leak where the synthetic-mode source_id was the operator label verbatim.)

FAIL-CLOSED (PHI): any exception leaves the source in a retriable state (never
advanced past its real phase), logs the failure with source_id + state +
error-class (no PHI), and emits NO partial/unverified note to the vault.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from alfred.scribe import diarize as diarize_mod
from alfred.scribe import ledger as ledger_mod
from alfred.scribe import stt as stt_mod
from alfred.scribe.attestation import SCRIBE_DRAFTER_IDENTITY
from alfred.scribe.close_manifest import (
    CLOSE_SENTINEL_NAME,
    read_close_manifest,
    resolve_require_close_manifest,
)
from alfred.scribe.completeness_marker import maybe_restamp, regressed, stamp_complete
from alfred.scribe.config import ScribeConfig
from alfred.scribe.grounding import verify as verify_grounding
from alfred.scribe.identity import compute_encounter_id
from alfred.scribe.inferred_dx import check_inferred_diagnoses
from alfred.scribe.ingest import ScribeIngestRefused, guard_ingest
from alfred.scribe.notegen import (
    ContextBudgetExceeded,
    StructuredNote,
    generate_structured,
    render_soap,
)
from alfred.scribe.state import (
    STATE_BUDGET_CAPPED,
    STATE_DRAFTED,
    STATE_FAILED,
    STATE_HUMAN_EDITED,
    STATE_INCOMPLETE,
    STATE_POST_ATTEST_AUDIO,
    STATE_READY,
    STATE_REFUSED,
    STATE_STRUCTURING,
    STATE_TRANSCRIBING,
    ScribeState,
)
from alfred.scribe.transcript import Transcript
from alfred.vault.ops import VaultError, vault_create, vault_edit, vault_read
from alfred.vault.scope import ScopeError

log = structlog.get_logger(__name__)

# Audio files the sweep treats as pipeline inputs. Sidecars (``.meta.json``
# provenance, ``.txt`` fake-STT transcript) are NOT primary inputs.
_AUDIO_EXTENSIONS = frozenset({".wav", ".ogg", ".mp3", ".m4a", ".flac", ".webm"})

# --- P3-b1 checkpoint-accumulator input convention --------------------------
# A per-encounter subdir holds ``chunk_NNN.<audio-ext>`` chunk files, each with
# a ``<chunk>.meta.json`` sidecar ``{synthetic: true, seq: N}``. The integer
# ``NNN`` in the filename is the ordering key (parsed as an int — NOT
# lexicographic, so ``chunk_10`` sorts AFTER ``chunk_2``). An explicit
# ``_CLOSED`` sentinel file finalizes the encounter (no more chunks coming).
_CHUNK_NAME_RE = re.compile(r"^chunk_(\d+)$")
_META_SUFFIX = ".meta.json"
# The `_CLOSED` sentinel NAME + its close-manifest contract are owned by the shared
# ``close_manifest`` module (imported above) — the pipeline is the READER, the
# ingest server the WRITER (no private-constant drift).
# Settle signal: the ``.meta.json`` commit marker (written LAST by the recorder)
# is the REQUIRED, deterministic settle signal — see ``is_chunk_settled`` (the
# dead markerless size+mtime fallback was removed in the Gap-B audit fix).


@dataclass
class VerifiedNote:
    """The output of the verify-before-render choke — a note that has PROVABLY
    been grounding-verified (the grounding ran on THIS structured object)."""

    body: str
    grounding_flags: list[dict[str, Any]] = field(default_factory=list)
    flag_count: int = 0
    structured: StructuredNote | None = None


async def generate_verified_note(
    transcript: Transcript, *, config: ScribeConfig, title: str,
) -> VerifiedNote:
    """THE choke — generate → verify → render on the SAME object.

    Closes the P2-c residual hole (an empty GroundingResult renders clean): here
    the GroundingResult is produced by verifying the exact structured object
    that is then rendered. This is the ONLY producer of a rendered clinical-note
    body in the pipeline.
    """
    structured = await generate_structured(transcript, config=config)
    grounding = verify_grounding(structured, transcript)      # verify THE SAME object
    # #48 — deterministic inferred-diagnosis post-check, BETWEEN verify and render.
    # FLAGS (never removes) a claim naming a lexicon diagnosis absent from its
    # CITED segments; the flags EXTEND grounding.flags so they ride the existing
    # render (flag_for → inline ⚠ INFERRED DIAGNOSIS) + grounding_flags frontmatter
    # path. Grounding is BLIND to this (a label invented from a real segment has no
    # number/negation token) and rule-6 can't stop it (the model disobeys) — code
    # is the lever.
    grounding_flag_count = len(grounding.flags)   # mechanical grounding flags only
    inferred_flags = check_inferred_diagnoses(structured, transcript)
    grounding.flags.extend(inferred_flags)
    # #4 — RECONCILE the flag-count observability seam. verify_grounding already
    # emitted ``scribe.grounding.verified flagged=<grounding-only>`` BEFORE this
    # extend, so that line UNDER-reports the total (a note whose only flag is an
    # inferred_diagnosis logs grounding flagged=0 while the note carries the inline
    # ⚠ + a frontmatter entry). Emit the FINAL, authoritative breakdown here so a
    # downstream flag-counting monitor sees the true total (the note body +
    # frontmatter + flag_count below already use the extended list — this closes
    # only the intermediate-log discrepancy).
    log.info(
        "scribe.grounding.flags_finalized",
        source_id=transcript.source_id,
        total_flags=len(grounding.flags),
        grounding_flags=grounding_flag_count,
        inferred_diagnosis_flags=len(inferred_flags),
    )
    body = render_soap(structured, title=title, grounding=grounding)  # render with THAT grounding
    return VerifiedNote(
        body=body,
        grounding_flags=grounding.metadata,
        flag_count=len(grounding.flags),
        structured=structured,
    )


def _read_provenance(audio_path: Path) -> dict[str, Any]:
    """Read the input's provenance sidecar ``<stem>.meta.json``. Missing /
    malformed → ``{}`` (fail-closed: guard_ingest refuses it in synthetic mode)."""
    meta = audio_path.with_suffix(".meta.json")
    if not meta.is_file():
        return {}
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _source_id_for_input(audio_path: Path, config: ScribeConfig) -> str:
    """Salted, opaque encounter id for a flat (legacy single-chunk) input.

    NOTE-4: the id is ``compute_encounter_id(filename, salt)`` in EVERY mode — a
    salted HMAC of the operator label, non-reversible without the per-instance
    secret. This closed the P2 leak (synthetic mode returned the label verbatim)
    + the ``"sha256:"`` colon-in-filename bug. FAIL-LOUD if the salt is missing
    (``EncounterIdentityError`` propagates)."""
    return compute_encounter_id(audio_path.name, salt=config.encounter_salt)


async def process_source(
    audio_path: Path, *, config: ScribeConfig, state: ScribeState, vault_path: Path,
) -> str:
    """Process one input to a clinical_note ai_draft. Idempotent + fail-closed.

    Returns an outcome tag: ``skipped`` (already done) / ``refused`` /
    ``drafted`` / ``failed``.
    """
    source_id = _source_id_for_input(audio_path, config)

    # Idempotency gate — never reprocess a done source (or a failed one that
    # exhausted its retry budget). Replaying a drafted source is a no-op.
    if state.is_done(source_id):
        return "skipped"

    # Mode gate (fail-closed) — refuse non-synthetic input in synthetic mode.
    provenance = _read_provenance(audio_path)
    try:
        guard_ingest(config, provenance=provenance, source_id=source_id)
    except ScribeIngestRefused:
        state.set(source_id, state=STATE_REFUSED)
        log.info("scribe.pipeline.refused", source_id=source_id, state=STATE_REFUSED)
        return "refused"

    try:
        # recorded → transcribing (STT, local). W1 — the sync, CPU-bound whisper
        # decode runs OFF the event loop (asyncio.to_thread) so a multi-second
        # transcribe never stalls the shared loop (the ingest server rides the
        # same loop; a blocked loop would freeze in-flight ingest POSTs).
        state.set(source_id, state=STATE_TRANSCRIBING)
        transcript = await asyncio.to_thread(
            stt_mod.transcribe, config, audio_path, source_id=source_id,
        )

        # P4 DIARIZE (fail-open-for-availability). Resolve per-segment speaker
        # roles. A diarize failure degrades to speaker=None + a loud log and the
        # note STILL generates from the un-attributed transcript — it must NOT
        # fail the source (un-attributed ≫ mis-attributed), so it is caught HERE
        # rather than bubbling to the outer fail-closed handler.
        try:
            transcript = diarize_mod.assign_speakers(config, audio_path, transcript)
        except Exception as e:  # noqa: BLE001 — fail-open: draft un-attributed, do NOT fail
            log.warning(
                "scribe.diarize.failed",
                source_id=source_id,           # opaque id only — NO PHI (NOTE-4)
                error_class=type(e).__name__,  # class only — never the message
                detail=(
                    "diarization failed — drafting UN-ATTRIBUTED (speaker=None); "
                    "un-attributed ≫ mis-attributed. Does NOT fail the source."
                ),
            )

        # transcribing → structuring (delta → note-gen → verify → render).
        state.set(source_id, state=STATE_STRUCTURING)
        title = f"Encounter {source_id}"  # source_id-based → PHI-free (NOTE-4)
        vnote = await generate_verified_note(
            transcript.delta(), config=config, title=title,
        )

        # structuring → drafted (vault_create clinical_note ai_draft ONLY).
        note_path = _create_ai_draft(
            vault_path, title, source_id, config, vnote,
        )
        state.set(source_id, state=STATE_DRAFTED, note_path=note_path)
        log.info(
            "scribe.pipeline.drafted",
            source_id=source_id,
            state=STATE_DRAFTED,
            grounding_flags=vnote.flag_count,
        )
        return "drafted"
    except Exception as e:  # noqa: BLE001 — fail-closed: retriable, no partial note
        prior = state.get(source_id)
        attempts = (prior.attempts if prior else 0) + 1
        state.set(
            source_id, state=STATE_FAILED, attempts=attempts,
            last_error_class=type(e).__name__,
        )
        log.warning(
            "scribe.pipeline.failed",
            source_id=source_id,
            state=STATE_FAILED,
            error_class=type(e).__name__,   # class only — NO PHI
            attempts=attempts,
        )
        return "failed"


def _create_ai_draft(
    vault_path: Path, title: str, source_id: str, config: ScribeConfig,
    vnote: VerifiedNote,
) -> str:
    """Create-OR-update the clinical_note ai_draft (scribe P3-a).

    Frozen-on-ATTEST (the P3 relaxation of P2's frozen-on-create): while the
    note is a LIVE, unattested ``ai_draft`` the checkpoint co-pilot refreshes it
    in place each pass; once ``status in {attested, amended}`` the body is SEALED
    and this REFUSES (fail-closed — never clobber a sealed medico-legal record).

    Three cases, dispatched on the note's LIVE on-disk status (attestation does
    NOT update ScribeState, so the pipeline reads the note, not the state):

      * New source (no existing note) → ``vault_create`` (born ai_draft, NOTE-2).
      * Existing note, status == ``ai_draft`` → UPDATE in place
        (``body_replace`` + refresh ``grounding_flags``); returns the SAME path
        (no duplicate).
      * Existing note, status ∈ {attested, amended} or missing/unknown → REFUSE
        (raise ``VaultError``; fail-closed).

    Also covers the P2-d crash-window (a prior run created the file but crashed
    before persisting DRAFTED): the resume hits ``already exists`` → this reads
    the live status (ai_draft) → UPDATE in place → resume completes. NOTE-4:
    logs carry the opaque ``source_id`` + status only — never PHI.
    """
    draft_fields = {
        "ai_draft": True,
        "synthetic": config.mode != "clinical",
        "status": "ai_draft",
        "source_id": source_id,
        "drafted_by": SCRIBE_DRAFTER_IDENTITY,
        "grounding_flags": vnote.grounding_flags,
        # P3-b3 retain-the-diff: the AI's generated body, written INTO the note
        # (frontmatter — NOT the body, so it doesn't affect the clobber-detect
        # body-sha). At create, body == draft_original; they diverge only when a
        # clinician later edits the body (clobber-detect then freezes so this
        # stays = the pipeline's last body). Sealed with the note at attest.
        "draft_original": vnote.body,
    }
    try:
        result = vault_create(
            vault_path,
            "clinical_note",
            title,
            set_fields=draft_fields,
            body=vnote.body,
            scope="stayc_clinical",
        )
        return result["path"]
    except VaultError as e:
        if "already exists" not in str(e):
            raise
        # The note already exists → create-OR-update. Recover its rel_path from
        # the message (STRING-PARSE-COUPLED — same as P2-d) and read the LIVE
        # status to decide UPDATE-in-place vs REFUSE.
        tail = str(e).split("already exists:")
        rel_path = tail[-1].strip() if len(tail) > 1 else ""
        return _update_or_refuse_ai_draft(vault_path, rel_path, source_id, vnote)


def _update_or_refuse_ai_draft(
    vault_path: Path, rel_path: str, source_id: str, vnote: VerifiedNote,
) -> str:
    """Refresh an existing LIVE ai_draft in place, or REFUSE if sealed (P3-a).

    Fail-closed: if the note cannot be recovered/read, or its status is not
    ``ai_draft`` (attested / amended / missing / unknown), the draft is SEALED
    and this raises rather than clobber it. NOTE-4: opaque ids only in logs.
    """
    if not rel_path:
        raise VaultError(
            "scribe: could not recover the existing clinical_note path from the "
            "already-exists error — refusing to update (fail-closed)."
        )
    record = vault_read(vault_path, rel_path)
    status = (record.get("frontmatter") or {}).get("status")
    if status != "ai_draft":
        log.warning(
            "scribe.pipeline.update_refused_sealed",
            source_id=source_id,          # opaque id only — NO PHI (NOTE-4)
            status=status or "(missing)",
        )
        raise VaultError(
            f"clinical_note is SEALED (status={status or '(missing)'}) — the "
            f"body is frozen once attested/amended (anti-spoliation). Refusing "
            f"to update the draft; create a status:amended supersede instead."
        )
    # Live ai_draft → mutable. Rewrite body + refresh grounding_flags AND
    # draft_original (P3-b3 retain-the-diff: keep draft_original = the pipeline's
    # LATEST generated body each checkpoint). #58: CLEAR the completeness marker
    # ATOMICALLY with the body rewrite (the body changed → the encounter is no
    # longer complete until it re-finalizes), so a just-regenerated draft can NEVER
    # be attested as complete. This rides the SINGLE body_replace choke and only
    # runs on a LIVE ai_draft (a sealed note raised earlier in this function), so
    # it NEVER hits the SEALED-deny branch. Gated by the stayc_clinical DRAFT_EDIT
    # carve-outs (all three fields are DRAFT_EDIT_FIELDS).
    vault_edit(
        vault_path,
        rel_path,
        body_replace=vnote.body,
        set_fields={
            "grounding_flags": vnote.grounding_flags,
            "draft_original": vnote.body,
            "encounter_completeness": regressed(
                datetime.now(timezone.utc), reason="body regenerated — awaiting re-finalize",
            ),
        },
        scope="stayc_clinical",
    )
    log.info(
        "scribe.pipeline.draft_updated",
        source_id=source_id,              # opaque id only — NO PHI (NOTE-4)
        grounding_flags=vnote.flag_count,
    )
    return rel_path


# --- P3-b1 checkpoint accumulator (identity + input walk + ledger + fold) ---


@dataclass
class AccumResult:
    """Per-encounter outcome of one accumulate pass (NO note-gen in P3-b1)."""

    encounter_id: str
    folded: int = 0          # chunks freshly folded into the ledger this pass
    held: int = 0            # unsettled chunks left for a later sweep
    refused: int = 0         # non-synthetic chunk refused by the mode gate (fail-closed)
    frozen: bool = False     # a seq gap → encounter frozen (never fold over a hole)
    closed: bool = False     # the _CLOSED sentinel finalized the encounter
    segments: int = 0        # accumulated segment count after this pass
    decode_failed: bool = False  # W2: a chunk failed to STT-decode → THIS encounter
                                 # held this pass, isolated (the sweep + other
                                 # encounters are unaffected)
    # #57 close-manifest — the PROMISED completeness bar + the LEDGER-TRUTH folded set.
    expected_final_seq: int | None = None  # the client's asserted final seq (from the _CLOSED manifest); None = legacy/no promise
    close_ambiguous: bool = False          # strict mode + missing/malformed manifest → fail-closed, never READY
    folded_seqs: frozenset[int] = frozenset()  # LEDGER-TRUTH (from chunk_provenance) — the seqs actually folded, NOT the pass-delta

    @property
    def promised_seq_pending(self) -> bool:
        """#57: True iff the close PROMISE is not yet satisfied — the manifest was
        ambiguous (fail-closed), OR seqs ``1..expected_final_seq`` are not ALL folded
        (the LITERAL set-subset predicate, not a ``max>=N`` shortcut). Blocks the
        READY finalize so a client that wrote ``_CLOSED`` before the final chunk
        landed can never reach a premature READY."""
        if self.close_ambiguous:
            return True
        if self.expected_final_seq is None:
            return False
        return not (self.folded_seqs >= frozenset(range(1, self.expected_final_seq + 1)))

    @property
    def pending_tail(self) -> bool:
        """True iff the fold stopped SHORT of the discovered tail this pass — a
        held/unsettled chunk (``held``), a seq gap (``frozen``), a decode failure
        (``decode_failed``), or a mode-gate refusal (``refused``) left an UNFOLDED
        chunk on disk. Gap-A (medico-legal): a CLOSED encounter with
        ``pending_tail`` must NOT finalize to ``ready`` — ``ready`` (the
        attest-invite) must mean the FULL transcript is folded, so a signed note is
        never silently missing its tail. Cleared once the tail settles + folds on a
        later sweep (then ``ready`` finalizes WITH the tail)."""
        return bool(self.held or self.refused or self.frozen or self.decode_failed)


def is_chunk_settled(chunk_path: Path, *, meta_path: Path) -> bool:
    """True iff the chunk is COMMITTED — the ``.meta.json`` marker is present.

    The marker is the REQUIRED settle signal (single source of truth): the
    recorder writes the chunk audio FULLY, THEN drops the ``.meta.json`` sidecar
    LAST (the ingest_web server does exactly this — audio atomic, sidecar
    atomic-LAST), so the sidecar's presence ⇒ the audio is fully written. Never
    hash/STT an unsettled file: a partial-write race would fold a truncated
    transcript IMMUTABLY into the ledger.

    A MARKERLESS chunk is NEVER settled — it is held indefinitely (surfaced by the
    accumulate ``held`` count). A manually-dropped or alternate-recorder chunk
    MUST include a ``.meta.json`` sidecar.

    (Gap-B fix — the former size+mtime-across-2-sweeps markerless FALLBACK was
    REMOVED: ``accumulate_encounter`` never threaded ``prev_stat`` across sweeps,
    so ``prev_stat`` was structurally always None → the fallback returned False for
    every markerless chunk anyway. It was dead code documenting a behavior that
    never ran; marker-only is the honest contract. ``chunk_path`` is retained as
    the settle SUBJECT for API clarity.)"""
    return Path(meta_path).is_file()


def _discover_chunks(encounter_dir: Path) -> list[tuple[Path, int]]:
    """``(chunk_path, seq)`` for every ``chunk_NNN.<audio-ext>``, ordered by
    INTEGER seq (so ``chunk_10`` follows ``chunk_2`` — NOT the lexicographic
    ``sorted()`` bug). ``seq`` is parsed from the filename, so ordering +
    gap-detection work even before a chunk's ``.meta.json`` marker lands."""
    found: list[tuple[Path, int]] = []
    for p in encounter_dir.iterdir():
        if not p.is_file() or p.suffix.lower() not in _AUDIO_EXTENSIONS:
            continue
        m = _CHUNK_NAME_RE.match(p.stem)
        if m:
            found.append((p, int(m.group(1))))
    found.sort(key=lambda c: c[1])   # integer seq order (the fix)
    return found


def _chunk_content_hash(chunk_path: Path) -> str:
    """The chunk's content-hash — the ``chunk_key`` idempotency key. A replay of
    identical bytes folds as a no-op."""
    return hashlib.sha256(chunk_path.read_bytes()).hexdigest()


def accumulate_encounter(
    encounter_dir: Path, *, config: ScribeConfig,
) -> AccumResult:
    """Fold an encounter's settled chunks into its transcript ledger, in seq
    order (scribe P3-b1 FOUNDATION — NO note-gen trigger; that is P3-b2).

    Idempotent + fail-closed:
      * ordered by integer seq; a seq GAP FREEZES the encounter (logs loudly,
        folds nothing past the hole — never build over a hole);
      * each chunk must be SETTLED (commit marker) before hash/STT;
      * the MODE GATE (``guard_ingest``) runs BEFORE STT — a non-synthetic chunk
        in synthetic mode is REFUSED (not STT'd/folded); no real-PHI processing
        until clinical is deliberately enabled;
      * folding is idempotent on the chunk content-hash (replay = no-op);
      * the ledger is persisted ATOMICALLY after the pass.

    Two contracts this depends on:
      * SETTLE-GATE / commit-marker — the recorder MUST write the chunk audio
        FULLY, THEN drop the ``.meta.json`` sidecar LAST. Its presence is the
        "audio complete" signal; the deferred size+mtime-across-2-sweeps path is
        the defense-in-depth fallback for a recorder that omits the marker.
      * UNIQUE-LABEL — the encounter_id is the salted hash of the LABEL
        (subdir/filename), NOT the audio CONTENT (P2 clinical mode hashed the
        bytes). So two DIFFERENT-content flat files with the SAME name now
        collide to one id — the operator's unique-label convention is LOAD-
        BEARING for flat files (a per-encounter subdir name is naturally unique).
    """
    encounter_id = compute_encounter_id(
        encounter_dir.name, salt=config.encounter_salt,
    )
    lpath = ledger_mod.ledger_path(encounter_dir, encounter_id)
    transcript = ledger_mod.load_ledger(lpath) or Transcript(
        source_id=encounter_id, mode=config.mode,
    )

    folded_seqs = {p.get("seq") for p in transcript.chunk_provenance}
    expected = (max(folded_seqs) + 1) if folded_seqs else 1

    result = AccumResult(encounter_id=encounter_id)
    for chunk_path, seq in _discover_chunks(encounter_dir):
        if seq < expected:
            continue                      # already folded (content-hash is the real guard)
        if seq > expected:
            # the expected seq is absent while a higher one is present → an
            # INTERIOR hole (a lost/skipped chunk). Freeze — never fold over it.
            result.frozen = True
            log.warning(
                "scribe.accumulator.seq_gap",
                encounter_id=encounter_id,
                expected_seq=expected,
                found_seq=seq,
                detail="seq gap — encounter FROZEN, refusing to fold over a hole",
            )
            break
        meta_path = chunk_path.with_suffix(_META_SUFFIX)
        if not is_chunk_settled(chunk_path, meta_path=meta_path):
            result.held += 1              # not committed yet — hold for a later sweep
            break                         # cannot skip the expected chunk → stop
        chunk_key = _chunk_content_hash(chunk_path)
        if transcript.has_folded(chunk_key):
            expected += 1                 # idempotent: this content already folded
            continue
        # MODE GATE (fail-closed) BEFORE any STT — the settle-gate checks marker
        # PRESENCE, not synthetic CONTENT, so a non-synthetic chunk would
        # otherwise be STT-processed. Refuse it (not STT'd/folded); the mode
        # gate's purpose is no real-PHI PROCESSING until clinical is enabled.
        try:
            guard_ingest(
                config,
                provenance=_read_provenance(chunk_path),
                source_id=encounter_id,   # opaque id in the ingest_decision log
            )
        except ScribeIngestRefused:
            result.refused += 1
            log.warning(
                "scribe.accumulator.refused_nonsynthetic",
                encounter_id=encounter_id,
                seq=seq,
                detail="chunk not synthetic in synthetic mode — REFUSED, not folded",
            )
            break                         # fail-closed: never fold past a refusal
        # W2 — per-chunk STT ISOLATION. One undecodable chunk (a corrupt/headerless
        # blob — the #1 client trap, B2) must NOT propagate out and kill the whole
        # sweep (which would re-fail every 30s forever, STARVING every other
        # encounter). Isolate it: this encounter is HELD at this seq this pass
        # (never fold over a hole → fail-closed, same posture as a seq gap), an
        # explicit signal is emitted (intentionally-left-blank — a decode failure
        # is distinguishable from idle), and we STOP folding THIS encounter but
        # return normally so run_sweep continues to the next one.
        try:
            chunk_tx = stt_mod.transcribe(config, chunk_path, source_id=encounter_id)
        except Exception as e:  # noqa: BLE001 — fail-isolated, not fail-whole
            result.decode_failed = True
            log.warning(
                "scribe.accumulator.chunk_decode_failed",
                encounter_id=encounter_id,     # opaque id only — NO PHI (NOTE-4)
                seq=seq,
                error_class=type(e).__name__,  # class only — never the message
                detail=(
                    "chunk failed to STT-decode — encounter HELD at this seq "
                    "(isolated); the sweep + other encounters are unaffected"
                ),
            )
            break                              # hold this encounter; do not fold over the hole
        # P4 DIARIZE (fail-open-for-availability) — resolve per-segment speaker
        # roles BEFORE folding. A diarize failure degrades to speaker=None + a
        # loud log and STILL folds the un-attributed text (un-attributed ≫
        # mis-attributed); unlike an STT decode failure it must NOT hold the
        # encounter — NO break, so the fold proceeds this pass.
        try:
            chunk_tx = diarize_mod.assign_speakers(config, chunk_path, chunk_tx)
        except Exception as e:  # noqa: BLE001 — fail-open: fold un-attributed, do NOT hold
            log.warning(
                "scribe.diarize.failed",
                encounter_id=encounter_id,     # opaque id only — NO PHI (NOTE-4)
                seq=seq,
                error_class=type(e).__name__,  # class only — never the message
                detail=(
                    "diarization failed — folding UN-ATTRIBUTED (speaker=None); "
                    "un-attributed ≫ mis-attributed. Does NOT hold the encounter."
                ),
            )
        offset = transcript.segments[-1].end_s if transcript.segments else 0.0
        if transcript.append_chunk(
            chunk_tx, audio_offset_s=offset, chunk_key=chunk_key, seq=seq,
        ):
            result.folded += 1
        expected += 1

    sentinel = encounter_dir / CLOSE_SENTINEL_NAME
    if sentinel.exists():
        transcript.closed = True
        # #57 — read the close manifest's PROMISED final seq (+ strict ambiguity).
        efs, amb = read_close_manifest(
            sentinel, require=resolve_require_close_manifest(config),
        )
        result.expected_final_seq = efs
        result.close_ambiguous = amb
    result.closed = transcript.closed
    result.segments = len(transcript.segments)
    # #57 LEDGER-TRUTH folded set — the seqs ACTUALLY folded (from the persisted
    # ledger), NOT the pass-delta. A release sweep that folds nothing new must still
    # report the full folded set so a complete-but-just-closed encounter finalizes
    # (fixes the pass-delta wedge).
    result.folded_seqs = frozenset(
        p.get("seq") for p in transcript.chunk_provenance if p.get("seq") is not None
    )

    # Persist the authoritative ledger (atomic) BEFORE any downstream draft
    # (none in P3-b1). Intentionally-left-blank: always log the pass so an idle
    # encounter (nothing new settled) is distinguishable from a broken one.
    ledger_mod.save_ledger(lpath, transcript)
    log.info(
        "scribe.accumulator.folded",
        encounter_id=encounter_id,
        folded=result.folded,
        held=result.held,
        refused=result.refused,
        frozen=result.frozen,
        closed=result.closed,
        segments=result.segments,
        decode_failed=result.decode_failed,
    )
    return result


# --- P3-b2 checkpoint note-gen trigger --------------------------------------

# checkpoint outcome → run_sweep counts key (outcomes not present = no count).
_CHECKPOINT_COUNT_KEY = {
    "drafted": "checkpoint_drafted",
    "budget_capped": "budget_capped",
    "human_edited": "human_edited",
    "ready": "ready",
    "post_attest_audio": "post_attest_audio",
    "incomplete": "incomplete",   # #57 — closed but the promised tail hasn't folded
}


def _body_sha(body: str) -> str:
    """sha256 of a note body — the clobber-detect fingerprint. Irreversible, so
    PHI-FREE: safe to persist in the PHI-free ScribeState."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


async def _regen_checkpoint(
    encounter_dir: Path, *, encounter_id: str, config: ScribeConfig,
    state: ScribeState, vault_path: Path,
) -> str:
    """FULL-REGEN the ai_draft from the ACCUMULATED transcript, guarded by
    clobber-detect (before) + the context-budget cap (inside generate). Returns
    an outcome tag: ``drafted`` / ``budget_capped`` / ``human_edited`` /
    ``post_attest_audio`` / ``noop``."""
    transcript = ledger_mod.load_ledger(
        ledger_mod.ledger_path(encounter_dir, encounter_id)
    )
    if transcript is None or not transcript.segments:
        return "noop"                    # nothing to draft yet

    title = f"Encounter {encounter_id}"  # STABLE across checkpoints (opaque id)
    # The seq of the most-recently-folded chunk — the NEW audio (P3-b3 surfaces
    # it when it arrives post-attest). Opaque + PHI-free.
    last_seq = (
        transcript.chunk_provenance[-1].get("seq")
        if transcript.chunk_provenance else None
    )
    prior = state.get(encounter_id)
    note_path = prior.note_path if prior else ""

    # CLOBBER-DETECT — before ANY write, compare the on-disk body sha against the
    # sha the pipeline recorded on its LAST write. Differ ⇒ a human edited the
    # draft ⇒ FREEZE (never clobber a clinician correction — the load-bearing
    # guard). Also short-circuit an already-attested note (P3-b3 post-attest audio).
    if note_path:
        try:
            rec = vault_read(vault_path, note_path)
        except VaultError:
            note_path = ""               # note vanished → treat as first-draft
        else:
            status = (rec.get("frontmatter") or {}).get("status")
            if status in ("attested", "amended"):
                # POST-ATTEST AUDIO — new audio arrived AFTER the draft was
                # signed. REFUSE + SURFACE (a distinct, human-visible terminal
                # outcome for this chunk — NOT a transient FAILED that would
                # retry-churn and bury the signal). The signed note is untouched;
                # the clinician may need to AMEND.
                state.set(encounter_id, state=STATE_POST_ATTEST_AUDIO)
                log.warning(
                    "scribe.pipeline.post_attest_audio",
                    encounter_id=encounter_id,   # opaque id (NOTE-4)
                    seq=last_seq,
                    status=status,
                    detail="new audio arrived AFTER attestation — refused + "
                           "surfaced; the signed note is untouched. The clinician "
                           "may need to amend.",
                )
                return "post_attest_audio"
            on_disk = _body_sha(rec.get("body", ""))
            if prior and prior.pipeline_body_sha and on_disk != prior.pipeline_body_sha:
                state.set(encounter_id, state=STATE_HUMAN_EDITED)
                log.warning(
                    "scribe.pipeline.human_edit_detected",
                    encounter_id=encounter_id,
                    detail="on-disk body differs from the last pipeline write — "
                           "auto-evolution FROZEN, operator opt-in required to resume",
                )
                return "human_edited"

    # FULL-REGEN — the budget guard fires INSIDE generate_structured BEFORE the
    # LLM call, so an over-budget checkpoint never reaches body_replace and the
    # last-good draft stays intact.
    try:
        vnote = await generate_verified_note(transcript, config=config, title=title)
    except ContextBudgetExceeded:
        state.set(encounter_id, state=STATE_BUDGET_CAPPED)
        log.warning(
            "scribe.pipeline.budget_capped",
            encounter_id=encounter_id,
            segment_count=len(transcript.segments),
            detail="regen over context budget — last-good draft intact; encounter "
                   "CAPPED (complete through the prior checkpoint), still folding",
        )
        return "budget_capped"

    # UPDATE-IN-PLACE (P3-a create-or-update: create on the first checkpoint,
    # body_replace after; refuse if the draft was sealed mid-flight).
    try:
        new_path = _create_ai_draft(vault_path, title, encounter_id, config, vnote)
    except (VaultError, ScopeError) as e:
        # #3 — the seal can surface as EITHER a VaultError (detected at
        # _update_or_refuse_ai_draft's vault_read, pipeline path) OR a ScopeError
        # (the stayc_clinical body_replace gate re-reads frontmatter INSIDE
        # vault_edit and finds status flipped to attested — a SIBLING exception,
        # not a VaultError subclass, ops.py never re-wraps it). Both mean the same
        # thing: the note was sealed mid-regen. Catch BOTH so the race is
        # classified post_attest_audio, not misclassified FAILED.
        if "SEALED" in str(e):
            # The note was attested BETWEEN the clobber-detect read and this write
            # (mid-flight attest race) → same post-attest-audio outcome; the
            # regenerated note is discarded, the signed note untouched.
            state.set(encounter_id, state=STATE_POST_ATTEST_AUDIO)
            log.warning(
                "scribe.pipeline.post_attest_audio",
                encounter_id=encounter_id,   # opaque id (NOTE-4)
                seq=last_seq,
                status="attested",
                detail="draft attested mid-regen (race) — refused + surfaced; "
                       "the signed note is untouched.",
            )
            return "post_attest_audio"
        raise
    # Record the sha of the ACTUAL on-disk body (post-write) so the next
    # checkpoint's clobber-detect compares like-for-like. note_path + sha are set
    # in ONE state.set — no window where a path is stored without its sha.
    written = vault_read(vault_path, new_path)
    state.set(
        encounter_id, state=STATE_DRAFTED, note_path=new_path,
        pipeline_body_sha=_body_sha(written.get("body", "")),
    )
    log.info(
        "scribe.pipeline.checkpoint_drafted",
        encounter_id=encounter_id,
        segment_count=len(transcript.segments),
        grounding_flags=vnote.flag_count,
    )
    return "drafted"


def _maybe_mark_incomplete(
    state: ScribeState, encounter_id: str, encounter_dir: Path,
    config: ScribeConfig, cur, *, expected_final_seq: int | None,
    folded_seqs: frozenset[int],
) -> bool:
    """#57 LAYERED terminal (grace-gated, DEFAULT-OFF). Once the ``_CLOSED`` sentinel
    is older than ``config.incomplete_grace_s``, mark the encounter STATE_INCOMPLETE
    (operator-visible "incomplete — awaiting seq N"). DEFAULT grace 0 → DISABLED: the
    ALWAYS-ON primary safety (stays DRAFTED + ``close_awaiting_promised_seq`` every
    sweep) fully satisfies the invariant. Uses the sentinel mtime as the close-clock
    (no new ScribeState field). Runs OUTSIDE the ``cur.state==STATE_DRAFTED`` guard so
    it ALSO fires for an encounter closed before ANY chunk folded (``cur`` is None).
    Idempotent; RE-OPENABLE (if the tail later folds → DRAFTED → re-evaluates).
    Returns True iff it transitioned to STATE_INCOMPLETE."""
    grace = getattr(config, "incomplete_grace_s", 0) or 0
    if grace <= 0:
        return False                          # terminal disabled (default)
    if cur is not None and cur.state == STATE_INCOMPLETE:
        return False                          # already marked (idempotent)
    try:
        closed_at = (encounter_dir / CLOSE_SENTINEL_NAME).stat().st_mtime
    except OSError:
        return False
    if (time.time() - closed_at) <= grace:
        return False                          # still within grace
    state.set(encounter_id, state=STATE_INCOMPLETE)
    log.warning(
        "scribe.pipeline.close_incomplete",
        encounter_id=encounter_id,
        expected_final_seq=expected_final_seq,
        folded_through=max(folded_seqs, default=0),
        detail="_CLOSED promised a final seq that did not arrive within "
               "incomplete_grace_s — marked INCOMPLETE (RE-OPENABLE if the "
               "missing chunk folds later).",
    )
    return True


async def checkpoint_encounter(
    encounter_dir: Path, *, encounter_id: str, config: ScribeConfig,
    state: ScribeState, vault_path: Path, did_fold: bool, closed: bool,
    pending_tail: bool = False,
    expected_final_seq: int | None = None,
    folded_seqs: frozenset[int] = frozenset(),
    close_ambiguous: bool = False,
) -> str:
    """The checkpoint trigger (scribe P3-b2). After P3-b1 folds a chunk, evolve
    the ai_draft in place; ``_CLOSED`` finalizes to ``ready`` (close does NOT
    attest — attest stays orchestrator-only). A ``human_edited`` encounter is
    SKIPPED.

    "ready ⇒ complete" (Gap-A + #57) — the finalize is BLOCKED while the encounter
    is INCOMPLETE, i.e. ANY of:
      * ``pending_tail`` (Gap-A) — this pass folded SHORT of the DISCOVERED tail;
      * ``close_ambiguous`` (#57 strict) — clinical/require + a missing/malformed
        close manifest (fail-closed);
      * PROMISED-pending (#57 structural) — the ``_CLOSED`` manifest promised
        ``final_seq=N`` but seqs ``1..N`` are not ALL folded (the LITERAL set-subset
        predicate — a client that wrote ``_CLOSED`` before the final chunk landed
        can't reach a premature READY).
    Incomplete → STAY DRAFTED/INCOMPLETE (never READY) with a reason-specific ILB;
    the test lives OUTSIDE the DRAFTED guard so it fires even for a never-drafted
    encounter. RE-OPENABLE: the tail folds later → DRAFTED → finalizes READY-with-tail."""
    prior = state.get(encounter_id)
    if prior and prior.state == STATE_HUMAN_EDITED:
        log.info(
            "scribe.pipeline.checkpoint_frozen",
            encounter_id=encounter_id, reason="human_edited",
        )
        return "human_edited_frozen"

    outcome = "noop"
    if did_fold:
        outcome = await _regen_checkpoint(
            encounter_dir, encounter_id=encounter_id, config=config,
            state=state, vault_path=vault_path,
        )

    if closed:
        cur = state.get(encounter_id)
        # PROMISED-pending — the LITERAL "all seqs 1..N folded" set-subset (NOT a
        # max>=N shortcut, so the guarantee never silently rides fold-contiguity).
        promised_pending = expected_final_seq is not None and not (
            folded_seqs >= frozenset(range(1, expected_final_seq + 1))
        )
        incomplete = pending_tail or close_ambiguous or promised_pending
        if incomplete:
            # reason-specific ILB (priority: pending_tail keeps the existing
            # single-log test green; ambiguous is a fail-closed WARNING).
            if pending_tail:
                log.info(
                    "scribe.pipeline.close_pending_tail",
                    encounter_id=encounter_id,
                    detail="_CLOSED seen but a tail chunk is still "
                           "held/unsettled/gapped — STAYING DRAFTED until the tail "
                           "folds (ready must mean the full transcript is signed)",
                )
            elif close_ambiguous:
                log.warning(
                    "scribe.pipeline.close_manifest_ambiguous",
                    encounter_id=encounter_id,
                    detail="strict mode + a missing/malformed close manifest — "
                           "FAIL-CLOSED, never READY until a valid final_seq is "
                           "asserted and all seqs 1..final_seq have folded",
                )
            else:  # promised_pending
                log.info(
                    "scribe.pipeline.close_awaiting_promised_seq",
                    encounter_id=encounter_id,
                    expected_final_seq=expected_final_seq,
                    folded_through=max(folded_seqs, default=0),
                    detail="_CLOSED promised final_seq but not all seqs 1..final_seq "
                           "have folded — STAYING DRAFTED (ready must mean complete)",
                )
            if _maybe_mark_incomplete(
                state, encounter_id, encounter_dir, config, cur,
                expected_final_seq=expected_final_seq, folded_seqs=folded_seqs,
            ):
                return "incomplete"
            return outcome                     # STAY DRAFTED — never READY
        # COMPLETE — promote ONLY a clean current draft to `ready`.
        elif cur and cur.state == STATE_DRAFTED:
            # #58 NOTE-FIRST ordering — stamp the completeness marker on the note
            # (the artifact attest reads) FIRST, and set STATE_READY ONLY on a
            # successful stamp. If the stamp raises, STAY DRAFTED (do NOT set READY)
            # → the DRAFTED-guard re-fires next sweep (while _CLOSED persists) and
            # re-stamps. This guarantees there is NEVER a state=READY / note-marker-
            # less window (the reverse — state-DRAFTED / note-marked — self-heals).
            folded_through = max(folded_seqs, default=0)
            try:
                stamp_complete(
                    vault_path, cur.note_path, now=datetime.now(timezone.utc),
                    expected_final_seq=expected_final_seq, folded_through=folded_through,
                )
            except Exception as e:  # noqa: BLE001 — stamp failed → stay DRAFTED, re-stamp next sweep
                log.warning(
                    "scribe.pipeline.completeness_stamp_failed",
                    encounter_id=encounter_id,
                    error_class=type(e).__name__,   # class only — NO PHI
                    detail="completeness stamp failed — STAYING DRAFTED, will "
                           "re-stamp next sweep (note-first ordering)",
                )
                return outcome
            state.set(encounter_id, state=STATE_READY)
            log.info(
                "scribe.pipeline.encounter_ready",
                encounter_id=encounter_id,
                detail="_CLOSED — draft complete (all promised seqs folded), marker "
                       "stamped, ready for attestation (attest stays orchestrator-only)",
            )
            outcome = "ready"
        # #58 SELF-HEAL — a note already at STATE_READY but MARKERLESS (pre-#58
        # migration, or the crash window between the stamp and state.set) gets
        # idempotently re-stamped on this closed sweep. maybe_restamp is a no-op if
        # the marker is already present or the note is sealed.
        elif cur and cur.state == STATE_READY:
            if maybe_restamp(
                vault_path, cur.note_path, now=datetime.now(timezone.utc),
                expected_final_seq=expected_final_seq, folded_through=max(folded_seqs, default=0),
            ):
                log.info(
                    "scribe.pipeline.completeness_self_healed",
                    encounter_id=encounter_id,
                    detail="markerless READY note re-stamped complete (migration / "
                           "crash-window self-heal)",
                )
    return outcome


async def run_sweep(
    config: ScribeConfig, state: ScribeState, vault_path: Path,
) -> dict[str, int]:
    """Scan input_dir once. Walks BOTH legacy flat files (P2 one-shot back-comp)
    AND one level of per-encounter subdirs (P3-b1 accumulator + P3-b2 checkpoint
    note-gen). The P2 ``iterdir()+is_file()`` SILENTLY SKIPPED subdirs.

    Intentionally-left-blank: emits ``scribe.pipeline.idle`` (ran, nothing to do)
    when the sweep produced no new work — so idle is distinguishable from broken
    — and ``scribe.pipeline.swept`` with counts when it did.

    P3-b2: each subdir encounter ACCUMULATES settled chunks into its ledger, then
    a checkpoint EVOLVES the ai_draft in place from the full accumulated
    transcript (guarded by clobber-detect + the context-budget cap).
    """
    input_dir = Path(config.input_dir)
    counts = {
        "scanned": 0, "drafted": 0, "refused": 0, "failed": 0, "skipped": 0,
        "encounters": 0, "chunks_folded": 0, "held": 0, "frozen": 0,
        "chunks_refused": 0,
        # P3-b2/b3 checkpoint outcomes
        "checkpoint_drafted": 0, "budget_capped": 0, "human_edited": 0,
        "ready": 0, "post_attest_audio": 0,
        "incomplete": 0,   # #57 — closed but the promised tail hasn't folded
    }

    if not input_dir.is_dir():
        log.info(
            "scribe.pipeline.idle",
            input_dir=str(input_dir),
            scanned=0,
            detail="ran, nothing to do — input_dir does not exist yet",
        )
        return counts

    entries = sorted(input_dir.iterdir(), key=lambda p: p.name)
    flat_files = [
        p for p in entries
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTENSIONS
    ]
    subdirs = [p for p in entries if p.is_dir() and not p.name.startswith(".")]
    counts["scanned"] = len(flat_files)

    # (1) legacy flat one-shot (P2 back-comp) — a bare audio file directly under
    # input_dir is a single-chunk encounter; its path is unchanged (only its
    # source_id is now salted-opaque).
    for audio in flat_files:
        outcome = await process_source(
            audio, config=config, state=state, vault_path=vault_path,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    # (2) per-encounter accumulate (P3-b1 fold) → checkpoint (P3-b2 note-gen).
    # W1 — accumulate_encounter is sync + CPU-bound (it runs the whisper decode);
    # run it OFF the event loop so the shared loop (which the ingest server rides)
    # stays free to service ingest POSTs during a multi-second decode.
    # W2 — per-SUBDIR isolation: one broken encounter (an undecodable chunk it
    # couldn't self-isolate, a corrupt dir, a ledger/OS error) must NOT kill the
    # sweep and starve every OTHER encounter every 30s. Wrap each subdir; a
    # failure logs an explicit signal and the sweep CONTINUES to the next one.
    for enc_dir in subdirs:
        counts["encounters"] += 1
        try:
            r = await asyncio.to_thread(accumulate_encounter, enc_dir, config=config)
            counts["chunks_folded"] += r.folded
            counts["held"] += r.held
            counts["chunks_refused"] += r.refused
            counts["frozen"] += 1 if r.frozen else 0
            # Checkpoint trigger — evolve the draft when a chunk folded, and
            # finalize on _CLOSED. (No fold + not closed ⇒ nothing to do.)
            if r.folded > 0 or r.closed:
                outcome = await checkpoint_encounter(
                    enc_dir, encounter_id=r.encounter_id, config=config,
                    state=state, vault_path=vault_path,
                    did_fold=r.folded > 0, closed=r.closed,
                    pending_tail=r.pending_tail,   # Gap-A: block ready finalize on an unfolded DISCOVERED tail
                    expected_final_seq=r.expected_final_seq,   # #57 the promised bar
                    folded_seqs=r.folded_seqs,                 # #57 ledger-truth folded set
                    close_ambiguous=r.close_ambiguous,         # #57 strict fail-closed
                )
                key = _CHECKPOINT_COUNT_KEY.get(outcome)
                if key:
                    counts[key] += 1
        except Exception as e:  # noqa: BLE001 — per-subdir fail-isolated, not fail-whole
            counts["failed"] += 1
            log.warning(
                "scribe.pipeline.encounter_error",
                error_class=type(e).__name__,   # class only — NO PHI, NO dir name (may be label)
                detail=(
                    "an encounter subdir failed this sweep — ISOLATED; the sweep "
                    "continues to the remaining encounters (fail-isolated)"
                ),
            )
            continue

    flat_work = counts["drafted"] + counts["refused"] + counts["failed"]
    acc_work = (
        counts["chunks_folded"] + counts["frozen"] + counts["chunks_refused"]
        + counts["checkpoint_drafted"] + counts["budget_capped"]
        + counts["human_edited"] + counts["ready"] + counts["post_attest_audio"]
    )
    if flat_work == 0 and acc_work == 0:
        log.info(
            "scribe.pipeline.idle",
            input_dir=str(input_dir),
            scanned=counts["scanned"],
            encounters=counts["encounters"],
            held=counts["held"],
            detail="ran, nothing to do — no new settled work",
        )
    else:
        log.info("scribe.pipeline.swept", **counts)
    return counts

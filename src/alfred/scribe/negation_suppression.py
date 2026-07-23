"""#26 — negation-paraphrase self-correcting loop: CAPTURE (Phase 1, writers only).

The THIRD self-correcting capture instance in the STAY-C scribe, after the #48
inferred-dx capture and its P4-5 speaker-attribution twin. It targets the residual
false-positive class #24 v1 left FLAGGED by design: a lexically-DISJOINT negation
PARAPHRASE, where the claim and the cite realize the SAME pertinent-negative with
almost no shared content words ("blood sugars NOT adequately controlled on metformin"
≈ "your sugars HAVEN'T come down as I'd hoped on the metformin" — only "metformin"
overlaps). grounding's (B) subset check cannot ground that without a source of truth
outside the text, and loosening the overlap would drop genuine invented negations —
the false-NEGATIVE that matters most on a medico-legal detector. So the only entity
that can adjudicate "is this disjoint paraphrase faithful?" is the clinician who signs
the note: this module CAPTURES their (implicit) judgment for later operator approval
(``feedback_self_correcting_design_standard.md`` — capture → feed back → human-approve).

Phase 1 ships ONLY the writers (mirrors ``enroll_learning`` — "ships only the writers
so the sink accumulates from day one"). grounding.py is UNCHANGED; the empagliflozin
fixture STILL flags. FEED-BACK (Phase 2) + PROPOSE/APPROVE (Phase 3) are later increments.

CAPTURE IS SPLIT across render and attest, because the two halves live in different
scopes and one of them (the cite concepts) needs the transcript, which ``attest.py``
never loads (it may be retention-sealed or destroyed by attest time):

  * RENDER-time (1a) — :func:`capture_render_candidates`, called from
    ``pipeline.render_verified_note`` where the transcript IS in scope. For each
    lexically-disjoint paraphrase candidate it spools the (claim-negated-concept-set,
    cite-negated-concept-set) PAIR. This is the PHI-BEARING half (concept-sets tied to
    an opaque ``source_id``). It RE-DERIVES grounding's (B) path from grounding's OWN
    helpers (``_negated_concepts`` / ``_CITE_NEGATION_RE``) so grounding.py stays
    byte-identical — no field is added to ``verify``'s output (a test pins the
    re-derivation against ``verify``).
  * ATTEST-time (1b) — :func:`record_negation_attest_outcome`, the THIRD twin beside
    the inferred-dx / speaker attest captures. For each ``negation_mismatch`` flag it
    records the PHI-FREE ``kept`` boolean: did the flagged claim SURVIVE unchanged into
    the attested body? Sign-with-flag-kept = an implicit "this flag is a false positive"
    verdict (byte-for-byte the inferred-dx / speaker kept-heuristic; NO attest UX change).

The JOIN (candidate ⋈ attest_outcome on ``source_id`` + ``section`` + ``claim_index``)
is Phase 3 (morning review), NOT this pass — but the two sinks share those keys so the
join is possible. ``kept=True`` → review-ready; ``kept=False`` → discarded.

TWO SINKS, one logical Tier-1 spool, both under ``<STAYC_DATA>/scribe/`` (dir 0700, file
0600), local-only + NEVER relayed, sited by :func:`resolve_candidates_dir`:

  * ``negation_candidates.jsonl`` — the render pairs (PHI-BEARING: concept-sets tied to
    ``source_id``). NEVER raw claim / cite sentences — concept-SETS only (design §5.1 /
    QA §10).
  * ``negation_attest_outcomes.jsonl`` — the attest ``kept`` rows (PHI-FREE: ids + enums
    + a boolean; 0600 is belt-and-suspenders here).

NEVER the #11 event chain: the concept-pair is DERIVED PHI (barred from the PHI-free,
destruction-surviving chain), and the ``kept`` row is coarse observability, not a
medico-legal fact — so no new ``events.py`` kind, exactly as the two prior twins keep
their attest-side captures to a separate JSONL sink / structlog.

RETENTION SHAPE (wired for Phase 3, not swept here): every row carries ``source_id``
(for destroy-with-encounter under s.49) and ``ts`` (for the unreviewed age-cap). Phase 3
adds the actual prune; Phase 1 only guarantees the rows carry the keys a prune needs.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import structlog

# grounding's OWN (B)-path helpers — imported so the render capture re-derives the
# EXACT concept-sets ``verify`` computes (no re-implementation drift; grounding.py
# stays byte-identical). Same established pattern as ``inferred_dx`` importing
# ``_NEGATION_RE`` / ``_cited_text`` from grounding — grounding never imports back, so
# no cycle.
from alfred.scribe.grounding import _CITE_NEGATION_RE, _negated_concepts

if TYPE_CHECKING:  # type-only — avoid import cost / any cycle at module load
    from alfred.scribe.config import ScribeConfig
    from alfred.scribe.notegen import StructuredNote
    from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)

# The reason string grounding mints for a negation flag. grounding builds it inline as
# ``"negation_mismatch: ..."`` and stores ``reasons[0].split(":", 1)[0]`` — there is no
# shared constant there to import (grounding cannot import this module — it would cycle),
# so the literal lives here and a test pins the two sides in lockstep. The attest twin
# filters ``grounding_flags`` by this exact reason.
NEGATION_MISMATCH_REASON = "negation_mismatch"

# The Tier-1 spool lives under a ``scribe/`` subdir of the STAY-C data root (the parent
# of ``input_dir`` — the same per-instance-correct derivation ``bug.resolve_bug_dir`` and
# the retention sweep's ``_resolved_retained_dir`` use).
_SCRIBE_SUBDIR = "scribe"
NEGATION_CANDIDATES_NAME = "negation_candidates.jsonl"          # render pairs (PHI-bearing)
NEGATION_ATTEST_OUTCOMES_NAME = "negation_attest_outcomes.jsonl"  # attest kept (PHI-free)
NEGATION_GLOSSARY_NAME = "negation_glossary.json"              # Tier-2 approved store (generic vocab)
NEGATION_REVIEW_SPOOL_NAME = "negation_review.spool"          # PHI-FREE pending-COUNT relay snapshot
GLOSSARY_VERSION = 1                                          # #26 v1 = exact-match concept-set PAIRS

KIND_CANDIDATE = "candidate"
KIND_ATTEST_OUTCOME = "attest_outcome"

DISPOSITION_PENDING = "pending"

# The unreviewed-candidate age cap (Phase-3 retention). A derived-PHI candidate that no operator
# reviews within this window is pruned by the retention sweep — an un-actioned PHI row must not
# linger indefinitely. 90d ≈ 3 morning-review cycles (team-lead ruling). A fixed named constant:
# schedule-tunability (like the diarize telemetry window) would require widening the FROZEN
# SCHEDULE_CLASSES contract (which every published schedule must match) — deferred as a separate
# cross-cutting change; changing this literal is the interim tuning knob.
NEGATION_CANDIDATE_AGE_CAP_DAYS = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_candidates_dir(config: "ScribeConfig") -> Path:
    """The Tier-1 spool DIRECTORY — ``<input_dir parent>/scribe``.

    Derived purely from ``config.input_dir`` (mirrors ``bug.resolve_bug_dir`` and the
    retention sweep's ``_resolved_retained_dir``), so an operator who points
    ``input_dir`` at ``<STAYC_DATA>/inbox`` gets ``<STAYC_DATA>/scribe/`` for free —
    per-instance-correct, never a single-instance literal. No config override field yet
    (Phase 1 needs the writers to have a home without touching config.py; a Phase-2
    override lands with the Tier-2 approved-store path)."""
    return Path(config.input_dir).expanduser().parent / _SCRIBE_SUBDIR


def resolve_glossary_path(config: "ScribeConfig") -> Path:
    """The Tier-2 APPROVED store path — ``<STAYC_DATA>/scribe/negation_glossary.json``,
    a sibling of the candidate spool. Data-layer (operator-grown, mutable), NOT bundled
    code — de-identified generic clinical vocab that survives s.49 destruction (the whole
    point of the generalize-at-approval step), so it is NOT retention-swept."""
    return resolve_candidates_dir(config) / NEGATION_GLOSSARY_NAME


def _candidates_file(candidates_dir: str | Path) -> Path:
    return Path(candidates_dir) / NEGATION_CANDIDATES_NAME


def _attest_outcomes_file(candidates_dir: str | Path) -> Path:
    return Path(candidates_dir) / NEGATION_ATTEST_OUTCOMES_NAME


@contextlib.contextmanager
def _sink_lock(sink_path: Path):
    """Serialize the sink's writers — the render (pipeline daemon) + attest (CLI, a
    SEPARATE process) + the future retention prune — via an exclusive ``flock`` on a
    STABLE per-sink lock file. The sink itself will be rotated by the Phase-3 prune
    (``os.replace``), so flocking the sink fd is unreliable (the pre-replace inode gets
    orphaned — the finding-19 lesson in ``enroll_learning``); the lock file's inode never
    moves. BEST-EFFORT: on any lock failure, proceed WITHOUT it — the guarded loss is a
    single row, never worth failing a valid attest / render or wedging the prune."""
    lock_path = sink_path.parent / f".{sink_path.name}.lock"
    fd = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
            fd = None
        # Proceed unlocked, but LOUDLY (intentionally-left-blank: 'proceeded unlocked' must
        # be distinguishable from 'locked') — e.g. a root-owned lock file → EACCES for the
        # unprivileged daemon. Stays best-effort (never fail a valid attest over a lock).
        log.warning(
            "scribe.negation_suppression.sink_lock_skipped",
            error_class=type(exc).__name__, sink=sink_path.name,
            detail="the negation spool lock could NOT be acquired — proceeding WITHOUT "
                   "serialization; a concurrent prune/append race could drop one row. "
                   "Check the lock-file ownership/perms.")
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _append_row(sink_path: Path, row: dict[str, Any]) -> None:
    """Append one JSON line — dir tree 0700, file 0600, flock-serialized. The caller
    wraps this fail-silent (a capture must never affect the pipeline / a valid attest).
    ``mkdir(parents=True)`` uses the umask default, so the spool DIR is explicitly
    chmod'd 0700 (the frozen PHI posture); the STAY-C data ROOT is left untouched (it may
    hold unrelated siblings)."""
    with _sink_lock(sink_path):
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(sink_path.parent, 0o700)
        except OSError:
            pass
        existed = sink_path.exists()
        with open(sink_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        if not existed:
            try:
                os.chmod(sink_path, 0o600)
            except OSError:
                pass


def capture_render_candidates(
    structured: "StructuredNote", transcript: "Transcript", *,
    candidates_dir: str | Path | None, source_id: str,
) -> None:
    """RENDER-time CAPTURE (1a) — the PHI-BEARING half of the #26 loop.

    For each claim carrying an UNGROUNDED negated concept whose cite ALSO negates
    (a lexically-disjoint paraphrase candidate — the #26 target class), append a
    ``candidate`` row keyed ``(source_id, section, claim_index)`` holding the concept-set
    PAIR ``{claim_concepts, cite_concepts}`` + ``disposition=pending``. Concept-SETS
    only — NEVER the raw claim / cite text.

    RE-DERIVES grounding's (B) path from grounding's own helpers so grounding.py stays
    byte-identical. DELIBERATELY NARROWER than ``verify``'s negation flag: it emits ONLY
    when the cite ALSO negates something (a genuine pair to suppress). A pure invented
    negation (cite negates nothing → ``verify`` still flags) produces NO candidate — there
    is nothing to pair, and suppressing it would be wrong.

    SIDE-EFFECT-FREE + fail-silent BY CONSTRUCTION: the whole body is wrapped so a capture
    bug can NEVER affect the rendered note (mirrors the inferred-dx / speaker twins).
    No-op when the spool dir is unset (dormant)."""
    try:
        if not str(candidates_dir or ""):
            return                      # spool DORMANT — never write relative to CWD
        seg_by_id = {s.id: s for s in transcript.segments}
        sink = _candidates_file(candidates_dir)
        captured = 0
        for section, idx, claim in structured.all_claims():
            # MIRROR of verify()'s (B) path (grounding.py:497-508) — same helpers, same
            # per-span cite extraction, same subset test. Kept in lockstep by a test that
            # asserts this fires exactly when verify() mints a negation_mismatch pair.
            claim_neg = _negated_concepts(claim.claim, _CITE_NEGATION_RE)
            if not claim_neg:
                continue
            cite_neg: list[set[str]] = []
            for span_id in claim.source_spans:
                seg = seg_by_id.get(span_id)
                if seg is not None:
                    cite_neg.extend(_negated_concepts(seg.text, _CITE_NEGATION_RE))
            if not cite_neg:
                continue                # cite negates NOTHING → invented negation, not a pair
            ungrounded = [c for c in claim_neg if not any(c <= span for span in cite_neg)]
            if not ungrounded:
                continue                # every claim negation is grounded → verify wouldn't flag
            _append_row(sink, {
                "kind": KIND_CANDIDATE, "ts": _now(),
                "source_id": source_id, "section": section, "claim_index": idx,
                "reason": NEGATION_MISMATCH_REASON,
                # sorted lists — JSON has no set, and a stable order makes the Phase-3
                # join / dedup + operator review deterministic.
                "claim_concepts": [sorted(c) for c in ungrounded],
                "cite_concepts": [sorted(c) for c in cite_neg],
                "disposition": DISPOSITION_PENDING,
            })
            captured += 1
        if captured:
            # The grep-able signal (pinned by a log-emission test). Only on count>0 — the
            # every-render heartbeat is render_verified_note's own flags_finalized line, so
            # a per-render 'nothing captured' would be pure spam.
            log.info(
                "scribe.negation_suppression.candidates_captured",
                source_id=source_id, count=captured,
                detail="render-time negation-paraphrase candidate(s) spooled for "
                       "morning-review (#26 self-correcting Part-1 capture)")
    except Exception:  # noqa: BLE001 — capture must NEVER affect the rendered note
        log.warning(
            "scribe.negation_suppression.render_capture_error", source_id=source_id,
            detail="negation candidate render capture failed — SWALLOWED (note unaffected)")


def record_negation_attest_outcome(
    candidates_dir: str | Path | None, *,
    grounding_flags: Any, attested_body: str, source_id: str,
) -> None:
    """ATTEST-time CAPTURE (1b) — the PHI-FREE dismissal half, the THIRD twin beside the
    inferred-dx (``inferred_dx.record_inferred_dx_attest_outcome``) and speaker
    (``attest._capture_speaker_attest_outcome``) captures.

    For each ``negation_mismatch`` flag in the note's ``grounding_flags`` frontmatter,
    append an ``attest_outcome`` row ``{source_id, section, claim_index, reason, kept}``
    — ids + enums + a boolean ONLY (NO concept-sets, NO claim text → PHI-FREE). ``kept``
    mirrors the speaker twin's ``norm_claim in norm_body`` heuristic: a flagged claim that
    SURVIVED unchanged into the attested body = an implicit 'faithful paraphrase' verdict
    (``kept=True``); an edited / removed claim = the flag was right (``kept=False``). No
    attest UX change — the signal is a read over the body the attest path already holds.

    KNOWN COARSENESS (accepted, identical to both prior twins): the implicit signal cannot
    tell 'judged faithful' from 'didn't notice the flag'; the Phase-3 operator-approval
    gate re-adjudicates. And it matches a flag by its STORED ``reason`` only — a claim whose
    ``negation_mismatch`` is the SECOND reason (a co-located number/ungrounded flag ran
    first) is not matched here; the render candidate then stays ``pending`` until the
    age-cap, which is the safe direction (no false suppression, only a lost signal).

    SIDE-EFFECT-FREE + fail-silent BY CONSTRUCTION: a capture bug must NEVER fail a
    medico-legal attestation. No-op when the spool dir is unset (dormant)."""
    try:
        if not str(candidates_dir or ""):
            return                      # spool DORMANT
        if not isinstance(grounding_flags, list):
            return
        from alfred.scribe.diagnosis_lexicon import normalize_text
        sink = _attest_outcomes_file(candidates_dir)
        norm_body = normalize_text(attested_body)
        for flag in grounding_flags:
            if not isinstance(flag, dict) or flag.get("reason") != NEGATION_MISMATCH_REASON:
                continue
            # kept via normalized-substring survival — the flag's raw claim is used ONLY to
            # compute the boolean; it is NEVER written to the sink (PHI-free by construction).
            norm_claim = normalize_text(str(flag.get("claim") or ""))
            kept = bool(norm_claim) and norm_claim in norm_body
            _append_row(sink, {
                "kind": KIND_ATTEST_OUTCOME, "ts": _now(),
                "source_id": source_id,
                "section": flag.get("section", ""),
                "claim_index": flag.get("claim_index"),
                "reason": NEGATION_MISMATCH_REASON,
                "kept": kept,
            })
    except Exception:  # noqa: BLE001 — capture must NEVER fail a valid attest
        log.warning(
            "scribe.negation_suppression.attest_capture_error", source_id=source_id,
            detail="negation attest-outcome capture failed — SWALLOWED (attestation unaffected)")


# ===========================================================================
# Phase 2 — FEED-BACK: the Tier-2 APPROVED suppression store (pairs-v1)
# ===========================================================================

@dataclass(frozen=True)
class NegationSuppression:
    """The Tier-2 APPROVED suppression store — operator-de-identified concept-set PAIRS.

    v1 = EXACT-set-match pairs (the ratified default; NOT the higher-reuse glossary). Each
    pair ``(claim_concept, cite_concept)`` records ONE operator decision: "this claim
    negated-concept-set, when the cite negates THIS concept-set, is a faithful paraphrase —
    suppress the (B) flag." Encounter-independent, generic clinical vocab (lexicon-class,
    like ``DIAGNOSIS_LEXICON``): survives s.49 destruction, never relayed as pairs.

    Consumed by ``grounding.verify`` via the duck-typed ``NegationSuppressionStore``
    protocol (grounding never imports this class — that would cycle). Empty ⇒ ``suppresses``
    is always ``False`` ⇒ grounding is byte-identical to pre-#26."""

    # Each pair is (claim_concept, cite_concept) as frozensets — order-independent,
    # hashable, exact-comparable. Lowercased/stripped at load to match _negated_concepts.
    pairs: tuple[tuple[frozenset[str], frozenset[str]], ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.pairs

    def suppresses(
        self, claim_concept: set[str], cite_neg_concepts: list[set[str]],
    ) -> bool:
        """True IFF an approved pair ``(A_claim, B_cite)`` EXACT-set-matches BOTH the claim
        concept AND some present cite negated concept: ``claim_concept == A_claim`` AND
        ``any(cite == B_cite)``.

        EXACT set-equality for v1 — fires ONLY on the exact concept-sets the operator
        approved; ANY phrasing drift (a sub/superset, a differently-tokenized cite) → no
        match → the negation STILL FLAGS. That is the SAFE direction on a medico-legal
        detector: a learned suppression can never over-reach to a concept the operator did
        not explicitly approve. Requiring a PRESENT cite concept (never the empty case) means
        an invented negation whose cite negates nothing is never suppressible here either."""
        if not self.pairs:
            return False
        cc = frozenset(claim_concept)
        cite_frozen = [frozenset(span) for span in cite_neg_concepts]
        for a_claim, b_cite in self.pairs:
            if cc == a_claim and any(span == b_cite for span in cite_frozen):
                return True
        return False


def load_suppression(glossary_path: str | Path | None) -> NegationSuppression:
    """Load the Tier-2 approved store from JSON. FAIL-SAFE toward FLAGGING: an unset /
    absent / malformed / wrong-shape file → an EMPTY store (no suppression, grounding
    byte-identical) — a learned override must never activate from a corrupt file, and a
    missing store is the common Phase-2 state (no pairs approved yet).

    Reads the design's Tier-2 schema (``{"version", "pairs": [{"claim_concept": [...],
    "cite_concept": [...], ...}]}``). Concepts are lowercased + whitespace-stripped to
    match ``_negated_concepts``'s output form; entries missing either side are skipped.
    The ``version`` field is READ-tolerant (any int) — forward-compatible, never a
    hard-fail (the load-time schema-tolerance contract)."""
    if not str(glossary_path or ""):
        return NegationSuppression()
    p = Path(glossary_path)
    if not p.is_file():
        return NegationSuppression()          # no store yet → inert (byte-identical)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a torn/invalid file must never activate suppression
        log.warning(
            "scribe.negation_suppression.glossary_load_error", path=str(p),
            detail="approved suppression store is malformed — loading EMPTY (no suppression, "
                   "fail-safe toward flagging). Fix the JSON to re-activate the learned pairs.")
        return NegationSuppression()
    if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
        return NegationSuppression()
    pairs: list[tuple[frozenset[str], frozenset[str]]] = []
    for entry in data["pairs"]:
        if not isinstance(entry, dict):
            continue
        a, b = entry.get("claim_concept"), entry.get("cite_concept")
        if not (isinstance(a, list) and isinstance(b, list) and a and b):
            continue
        a_fs = frozenset(t for x in a if (t := str(x).strip().lower()))
        b_fs = frozenset(t for x in b if (t := str(x).strip().lower()))
        if a_fs and b_fs:
            pairs.append((a_fs, b_fs))
    return NegationSuppression(pairs=tuple(pairs))


def glossary_mtime(glossary_path: str | Path | None) -> float | None:
    """The glossary file's mtime, or ``None`` if absent/unstattable — the mtime-reload key
    (file-absent is a valid steady state, not an error)."""
    try:
        p = Path(glossary_path or "")
        return p.stat().st_mtime if p.is_file() else None
    except OSError:
        return None


def maybe_reload_suppression(
    current: NegationSuppression, glossary_path: str | Path | None, last_mtime: float | None,
) -> tuple[NegationSuppression, float | None]:
    """#26 Phase-3 TIMELY RELOAD — mtime-gated re-read of the Tier-2 store for the running
    daemon. Re-reads ONLY when the glossary mtime changed since ``last_mtime`` (an operator
    approve rewrites the glossary → bumps mtime → the running daemon applies it on the NEXT
    sweep, no restart), at zero cost when unchanged. REUSES the fail-safe :func:`load_suppression`
    (corrupt/missing → empty store → still flags, never crashes the sweep). File-absent is a
    valid steady state. Returns ``(store, mtime)`` for the caller to carry forward."""
    mtime = glossary_mtime(glossary_path)
    if mtime == last_mtime:
        return current, last_mtime            # unchanged → keep the in-memory store
    store = load_suppression(glossary_path)
    log.info(
        "scribe.negation_suppression.reloaded", pairs=len(store.pairs),
        detail="#26 approved-suppression store reloaded (glossary changed — operator approval "
               "applies this sweep, no daemon restart)")
    return store, mtime


# ===========================================================================
# Phase 3 — PROPOSE + APPROVE: join / review backing / glossary write / retention
# ===========================================================================

def candidate_id(source_id: Any, section: Any, claim_index: Any) -> str:
    """The deterministic, PHI-FREE handle for a paraphrase candidate — a truncated sha256 of
    the join key ``(source_id, section, claim_index)``. Stable across runs (DERIVED, not stored),
    so the render row (1a) and attest row (1b) for the same claim map to the SAME id. Serves as:
    the ``negation-candidates`` list handle, the approve/reject argument, the evstore audit
    subject (candidate_hash), AND the glossary pair's provenance id. Carries NO PHI — source_id
    is already a salted-opaque encounter id, and a hash of it + positions is non-reversible."""
    raw = f"{source_id}|{section}|{claim_index}"
    return "npc-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ReviewCandidate:
    """One review-ready paraphrase candidate (the 1a⋈1b join result). PHI-BEARING (concept-sets
    tied to an opaque source_id) — on-box operator review ONLY, NEVER relayed."""

    candidate_id: str
    source_id: str
    section: str
    claim_index: Any
    claim_concepts: tuple[tuple[str, ...], ...]
    cite_concepts: tuple[tuple[str, ...], ...]


def _read_rows(path: Path) -> list[dict]:
    """Read a JSONL sink → list of dict rows. Tolerant: absent → []; a torn/partial line
    (an append-only sink can tear on a crash) is SKIPPED; a corrupt file never raises. So a
    prune over the result also self-heals torn lines."""
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                except Exception:  # noqa: BLE001 — skip a torn line, never fail the read
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:  # noqa: BLE001 — a corrupt sink must never crash the join/prune
        return rows
    return rows


def _row_key(row: dict) -> tuple:
    return (row.get("source_id"), row.get("section"), row.get("claim_index"))


def join_review_ready(
    candidates_dir: str | Path, decided_ids: set[str] | None = None,
) -> list[ReviewCandidate]:
    """The 1a⋈1b JOIN — pair render candidate rows (1a) with attest-outcome rows (1b) on
    ``(source_id, section, claim_index)``. A key is REVIEW-READY iff it has ≥1 candidate row
    AND ≥1 attest_outcome with ``kept=True`` (the clinician signed the flagged paraphrase
    UNCHANGED = an implicit 'faithful' verdict) AND its ``candidate_id`` is NOT already decided
    (approved/rejected). Multi-checkpoint duplicate candidate rows dedupe to the LATEST by ts.

    Returns PHI-bearing :class:`ReviewCandidate`s for on-box operator review ONLY (never relayed)."""
    decided = decided_ids or set()
    cand_by_key: dict[tuple, dict] = {}
    for row in _read_rows(_candidates_file(candidates_dir)):
        if row.get("kind") != KIND_CANDIDATE:
            continue
        k = _row_key(row)
        prev = cand_by_key.get(k)
        if prev is None or str(row.get("ts", "")) >= str(prev.get("ts", "")):
            cand_by_key[k] = row              # latest-by-ts wins (dedupe checkpoint regens)
    kept_keys = {
        _row_key(row) for row in _read_rows(_attest_outcomes_file(candidates_dir))
        if row.get("kind") == KIND_ATTEST_OUTCOME and row.get("kept") is True
    }
    out: list[ReviewCandidate] = []
    for k, row in cand_by_key.items():
        if k not in kept_keys:
            continue                          # not attested-faithful → not review-ready
        cid = candidate_id(row.get("source_id"), row.get("section"), row.get("claim_index"))
        if cid in decided:
            continue                          # already approved/rejected
        out.append(ReviewCandidate(
            candidate_id=cid,
            source_id=str(row.get("source_id", "")),
            section=str(row.get("section", "")),
            claim_index=row.get("claim_index"),
            claim_concepts=tuple(tuple(str(t) for t in c) for c in (row.get("claim_concepts") or [])),
            cite_concepts=tuple(tuple(str(t) for t in c) for c in (row.get("cite_concepts") or [])),
        ))
    out.sort(key=lambda c: c.candidate_id)    # deterministic order
    return out


def count_pending(candidates_dir: str | Path, decided_ids: set[str] | None = None) -> int:
    """PHI-FREE count of review-ready candidates — the ONLY negation-loop number that may
    cross the relay boundary (NEVER the concept-sets). Just the join length."""
    return len(join_review_ready(candidates_dir, decided_ids))


def _prune_sink(sink_path: Path, drop: Callable[[dict], bool]) -> int:
    """Read → keep rows where NOT ``drop(row)`` → atomic rewrite (tmp→rename), UNDER the sink
    lock. Returns the count REMOVED. Absent file → 0. The SAME lock + helper backs BOTH the
    destroy-by-source prune AND the age-cap prune, so they can NEVER race on the same sink
    (team-lead ruling). A torn line is already dropped by :func:`_read_rows`, so a prune also
    self-heals a partially-written sink."""
    if not sink_path.is_file():
        return 0
    with _sink_lock(sink_path):
        rows = _read_rows(sink_path)
        keep = [r for r in rows if not drop(r)]
        removed = len(rows) - len(keep)
        if removed == 0:
            return 0
        tmp = sink_path.with_suffix(sink_path.suffix + ".prune.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for r in keep:
                    f.write(json.dumps(r) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, sink_path)        # atomic swap — never a partial sink
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()                  # never leave a stale .prune.tmp
            raise
    return removed


def prune_candidates_for_source(candidates_dir: str | Path, source_id: str) -> int:
    """DESTROY-WITH-ENCOUNTER — remove every candidate + attest-outcome row for ``source_id``
    from BOTH PHI-bearing sinks (an s.49 destruction wipes the encounter's derived-PHI candidates
    too). Returns total rows removed. The de-identified Tier-2 glossary is NOT touched (it survives
    destruction). Pair with :func:`count_rows_for_source` to GATE the destroyed event."""
    n = _prune_sink(_candidates_file(candidates_dir), lambda r: r.get("source_id") == source_id)
    n += _prune_sink(_attest_outcomes_file(candidates_dir), lambda r: r.get("source_id") == source_id)
    return n


def count_rows_for_source(candidates_dir: str | Path, source_id: str) -> int:
    """The destroy COMPLETENESS verifier — rows still bearing ``source_id`` across BOTH sinks.
    The destroy path GATES ``retention.destroyed`` on this being 0 AFTER the prune: a residual
    derived-PHI row = destruction INCOMPLETE = fail-loud (same posture as a note-unlink failure)."""
    n = sum(1 for r in _read_rows(_candidates_file(candidates_dir)) if r.get("source_id") == source_id)
    n += sum(1 for r in _read_rows(_attest_outcomes_file(candidates_dir)) if r.get("source_id") == source_id)
    return n


def prune_candidates_by_age(candidates_dir: str | Path, cutoff_iso: str) -> int:
    """AGE-CAP prune — remove rows whose ``ts`` is older than ``cutoff_iso`` (an un-reviewed
    candidate must not linger as derived PHI past the cap). ``ts`` is a ``datetime.isoformat()``
    UTC string, so a lexicographic compare is chronological (same format on both sides). A row
    with a MISSING/malformed ts is DROPPED — fail-safe: an unage-able PHI row should not persist.
    Same lock/helper as the destroy prune."""
    def _too_old(r: dict) -> bool:
        ts = r.get("ts")
        return not (isinstance(ts, str) and ts >= cutoff_iso)
    n = _prune_sink(_candidates_file(candidates_dir), _too_old)
    n += _prune_sink(_attest_outcomes_file(candidates_dir), _too_old)
    return n


def append_approved_pair(
    glossary_path: str | Path, *, candidate_id: str,
    claim_concept: list[str], cite_concept: list[str],
    approved_by: str, approved_at: str, dropped_count: int = 0,
) -> int:
    """Append ONE operator-approved pair to the Tier-2 glossary (atomic tmp→rename, 0600) and
    return the new ``revision`` (a monotonic edit counter — the audit's ``glossary_version``).

    The stored pair is de-identified generic vocab: concept-SETS (already post-``--drop``) + the
    PHI-FREE provenance ``id`` = ``candidate_id`` (NOT the raw source_id — a self-contained hash
    that survives s.49 destruction without a dangling encounter reference, team-lead's Contract-A
    refinement). The dropped token STRINGS are NEVER stored (they were the PHI residue removed);
    only ``dropped_count`` is kept. IDEMPOTENT: a pair whose ``id`` already exists is a no-op
    (returns the current revision) — a double-approve can't duplicate a pair."""
    p = Path(glossary_path)
    data: dict[str, Any] = {}
    if p.is_file():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:  # noqa: BLE001 — a corrupt glossary is rebuilt from a clean base
            log.warning(
                "scribe.negation_suppression.glossary_rewrite_over_corrupt", path=str(p),
                detail="approved-store JSON was unreadable at approve — rebuilding from a clean base "
                       "(prior pairs unrecoverable; the evstore approve chain remains authoritative)")
    pairs = data.get("pairs")
    if not isinstance(pairs, list):
        pairs = []
    if any(isinstance(e, dict) and e.get("id") == candidate_id for e in pairs):
        return int(data.get("revision", len(pairs)))     # already approved → no-op
    revision = int(data.get("revision", 0)) + 1
    pairs.append({
        "id": candidate_id,
        "claim_concept": sorted(claim_concept),
        "cite_concept": sorted(cite_concept),
        "approved_by": approved_by,
        "approved_at": approved_at,
        "dropped_count": int(dropped_count),
    })
    out = {"version": GLOSSARY_VERSION, "revision": revision, "pairs": pairs}
    p.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return revision

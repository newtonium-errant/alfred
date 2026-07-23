"""#14 item-11 slice 14a — the note-gen edit-diff CAPTURE core (self-correcting, PHI-free).

The STRUCTURAL GENERALIZATION of the #48 inferred-dx and P4-5 speaker attest captures — the
FOURTH side-effect-free sibling at the SAME attest choke point. At attest, it diffs
``draft_original`` (the AI's frozen last draft, sealed with the note) against the attested body
(the clinician's final) into PHI-FREE per-section correction signals (counts / word-deltas / enum
buckets ONLY — NEVER claim text, phrasing, or diff hunks). The raw diff never leaves the box: both
sides already persist in the vault (``draft_original`` frontmatter + the note body) and are
recomputable on-box for the authorized operator at morning review; the loop stores only the signal.

``flag_survival`` (§2.2-H) is the SUPERSET that generalizes the two legacy twins (inferred-dx +
speaker) to ALL grounding reasons. **14e's readout must treat ``notegen_edit.flag_survival`` as the
authoritative UNIFIED view and NOT double-count it against the legacy inferred-dx / speaker twin
rows** (those rows still record their two reasons independently; this row records every reason).

The PHI-free schema is a CLOSED FROZENSET (:data:`_ROW_FIELDS` etc.), pinned by a widening test via
:func:`phi_free_violations` — the same discipline as the event-store field allowlist. Capture is
fail-silent + side-effect-free BY CONSTRUCTION: a bug here can NEVER alter/fail a medico-legal attest.

ROADMAP (NOT 14a — a real latent gap, boarded): the capture sink currently rides
``<enrollment_dir>/learning/attest_capture.jsonl`` (SHARED with the voice-diarization captures — one
lock, one retention prune, one dormancy gate, design §2.3), which mis-couples note-gen edit feedback
to VOICE-enrollment config: an instance that attests notes without ``scribe.diarize.enrollment_dir``
set captures ZERO notegen_edit rows. On the production box enrollment IS configured, so 14a keeps the
shared sink (moving it would drag the #13-13b retention-prune rewiring into scope). But the dormancy
is made OBSERVABLE (a one-time signal, :func:`record_notegen_edit_outcome`) so absent rows are
diagnosable, never silent. Decouple the PHI-free capture sink onto a scribe-level data path (+ move
the retention-prune coverage with it) in a later slice.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

import structlog

from alfred.scribe.enroll_learning import KIND_NOTEGEN_EDIT
from alfred.scribe.grounding import _normalize
from alfred.scribe.notegen import SOAP_SECTIONS, _SECTION_HEADINGS
from alfred.scribe.notegen_quality import (
    QUALITY_ASSESSMENT_NO_PLAN_REASON,
    QUALITY_REASON_PREFIX,
    QUALITY_REQUIRED_SECTION_EMPTY_REASON,
    QUALITY_VERBOSE_REASON,
)

log = structlog.get_logger(__name__)

# The single ratified SOAP profile (Q4) — the default attribution until 14b's real profile writes
# ``note_profile_id`` / ``note_profile_version`` into frontmatter (consumer-fields-from-day-one).
DEFAULT_TEMPLATE_ID = "soap"
DEFAULT_TEMPLATE_VERSION = 0

# The modified-vs-removed classifier threshold — difflib SequenceMatcher.ratio() on the normalized
# claims. >= ⇒ MODIFIED (the draft claim paired to a similar attested claim); < ⇒ REMOVED (+ the
# attested claim stays unmatched ⇒ ADDED). OVER-classifying "modified" is the SAFE side: it only
# inflates a count that triggers a human raw-diff look, never an auto-action. 0.6 = difflib's own
# ``get_close_matches`` cutoff. Tunable.
_MODIFIED_RATIO_THRESHOLD = 0.6

# The ``high_modification`` (§2.2-J) derivation: total modified / total draft claims >= this ⇒ surface
# the encounter's raw diff at morning review for a terminology generalization. Tunable.
_HIGH_MODIFICATION_RATIO = 0.5

# ── The CLOSED PHI-FREE schema (widening-pinned by phi_free_violations) ──────────
# Per-section (S/O/A/P) AND per-total count signals (A–F). All int.
_SECTION_COUNT_FIELDS = frozenset({
    "claims_draft", "claims_attested", "claims_removed", "claims_added",
    "claims_modified", "claims_kept_verbatim", "words_draft", "words_attested",
})
# Totals add the net word delta (G = attested − draft; may be negative).
_TOTAL_FIELDS = _SECTION_COUNT_FIELDS | {"net_word_delta"}
# flag_survival inner (H): per grounding reason enum → {removed, kept} int counts.
_SURVIVAL_FIELDS = frozenset({"removed", "kept"})
# #14e-ii quality_survival inner: per quality reason enum → {acted, ignored} int counts (did the
# clinician ACT on the advisory quality flag — the check STOPPED firing on the attested body — or
# IGNORE it — it STILL fires ⇒ a tune-down candidate). PHI-free (reason enums + counts).
_QUALITY_SURVIVAL_FIELDS = frozenset({"acted", "ignored"})
# The whole row.
_ROW_FIELDS = frozenset({
    "kind", "ts", "source_id", "template_id", "template_version",
    "sections", "totals", "flag_survival", "quality_survival", "high_modification",
})
# The ONLY string-valued top-level fields — the widening allowlist. Any OTHER string leaf anywhere in
# the row is a claim-text leak and MUST fail the pin. (flag_survival reason KEYS are enum strings —
# allowed as keys, checked separately.)
_STRING_VALUE_FIELDS = frozenset({"kind", "ts", "source_id", "template_id"})

# "## Subjective" → "subjective" etc. — the ONLY headings that anchor a section.
_HEADING_TO_SECTION = {v: k for k, v in _SECTION_HEADINGS.items()}

# One inline flag literal always begins with the ⚠ sentinel; strip from the first " ⚠".
_FLAG_SENTINEL = " ⚠"          # " ⚠"
_TRAILING_CITE_RE = re.compile(r"\s*\[[^\]]*\]\s*$")   # a trailing " [S1, S2]" citation


# One-time dormancy latch (a dict so the flag mutates without a `global`).
_DORMANT = {"warned": False}


def _parse_sections(body: str) -> dict[str, list[str]]:
    """Parse a ``render_soap`` markdown body → ``{section: [claim_text, ...]}``. Deterministic, no LLM.

    Walked against the ACTUAL renderer (notegen.render_soap): section anchors are exactly the four
    ``## Subjective/Objective/Assessment/Plan`` headings; a claim is a ``- {claim}{ [S#..]}{ ⚠..}``
    bullet. Strips the ``- `` prefix, the trailing ``[S#..]`` cite, and inline ``⚠ ..`` flag literals,
    so a FLAGGED draft claim compares equal to the clinician's DE-FLAGGED attested claim (else every
    flagged claim would read as 'modified' merely because the ⚠ was dropped). The ``Not addressed``
    empty-section literal, the ``⚠ REASONING NOT STATED`` line, the ``# title``, and note-level
    banners are NOT ``- `` bullets → naturally excluded.

    DEFENSIVE (clinician restructuring): only the four canonical headings anchor a section; a renamed
    heading resets to no-section (its bullets are dropped, not mis-attributed) and free text is
    ignored — graceful degradation toward the over-classify-modified/added safe side, NEVER a crash."""
    sections: dict[str, list[str]] = {s: [] for s in SOAP_SECTIONS}
    current: str | None = None
    for raw in body.splitlines():
        line = raw.strip()
        if line in _HEADING_TO_SECTION:
            current = _HEADING_TO_SECTION[line]
            continue
        if line.startswith("## "):
            current = None                 # an unrecognized (renamed/added) heading → drop its bullets
            continue
        if current is None or not line.startswith("- "):
            continue
        claim = line[2:].split(_FLAG_SENTINEL, 1)[0]   # drop inline ⚠ flag literals (suffix)
        claim = _TRAILING_CITE_RE.sub("", claim).strip()   # drop the trailing [S#..] cite
        if claim:
            sections[current].append(claim)
    return sections


def _classify_section(draft_claims: list[str], attested_claims: list[str]) -> dict[str, int]:
    """Match draft→attested claims WITHIN one section (deterministic, no LLM) → the count signals.

    (1) exact-normalized match ⇒ kept_verbatim; (2) greedy best-``ratio`` >= threshold to an
    unmatched attested claim ⇒ modified; (3) leftover draft ⇒ removed; (4) leftover attested ⇒ added.
    Normalization reuses ``grounding._normalize`` (lowercase + number-unit glue). Word counts are on
    the RAW claim text (verbosity is the clinician-facing surface, not the normalized form)."""
    norm_draft = [_normalize(c) for c in draft_claims]
    norm_att = [_normalize(c) for c in attested_claims]
    att_used = [False] * len(norm_att)
    kept = modified = removed = 0

    unmatched_draft: list[int] = []
    for di, nd in enumerate(norm_draft):
        hit = next((ai for ai, na in enumerate(norm_att) if not att_used[ai] and na == nd), None)
        if hit is not None:
            att_used[hit] = True
            kept += 1
        else:
            unmatched_draft.append(di)

    for di in unmatched_draft:
        best_ai, best_r = None, 0.0
        for ai, na in enumerate(norm_att):
            if att_used[ai]:
                continue
            r = difflib.SequenceMatcher(None, norm_draft[di], na).ratio()
            if r > best_r:
                best_ai, best_r = ai, r
        if best_ai is not None and best_r >= _MODIFIED_RATIO_THRESHOLD:
            att_used[best_ai] = True
            modified += 1
        else:
            removed += 1

    return {
        "claims_draft": len(draft_claims),
        "claims_attested": len(attested_claims),
        "claims_removed": removed,
        "claims_added": att_used.count(False),
        "claims_modified": modified,
        "claims_kept_verbatim": kept,
        "words_draft": sum(len(c.split()) for c in draft_claims),
        "words_attested": sum(len(c.split()) for c in attested_claims),
    }


def _flag_survival(grounding_flags: Any, attested_body: str) -> dict[str, dict[str, int]]:
    """Per grounding ``reason`` enum → ``{removed, kept}`` counts of whether each flagged claim
    SURVIVED into the attested body (``norm_claim in norm_body`` — the inferred-dx / speaker heuristic,
    GENERALIZED to ALL reasons). PHI-FREE: the flag's claim text is used ONLY to compute the boolean,
    then discarded — only the reason enum + the counts leave."""
    survival: dict[str, dict[str, int]] = {}
    if not isinstance(grounding_flags, list):
        return survival
    norm_body = _normalize(str(attested_body or ""))
    for flag in grounding_flags:
        if not isinstance(flag, dict):
            continue
        reason = flag.get("reason")
        if not reason:
            continue
        norm_claim = _normalize(str(flag.get("claim") or ""))
        kept = bool(norm_claim) and norm_claim in norm_body
        bucket = survival.setdefault(str(reason), {"removed": 0, "kept": 0})
        bucket["kept" if kept else "removed"] += 1
    return survival


def _coerce_version(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_TEMPLATE_VERSION


def _recheck_quality_reasons(
    attested_body: str, succinctness_target: Any, required_sections: Any,
) -> set[str]:
    """#14e-ii — re-run the 3 quality checks against the ATTESTED body's parsed sections → the set of
    ``quality_*`` reasons that STILL fire. Mirrors ``notegen_quality.check_note_quality`` but over the
    parsed body (attest never holds the StructuredNote). required-section-empty + assessment-no-plan are
    profile-INDEPENDENT (only the required-set names which sections to check); ONLY verbose needs the
    target. Deterministic, no LLM."""
    secs = _parse_sections(str(attested_body or ""))
    required = set(required_sections or ())
    reasons: set[str] = set()
    for sec in required:
        if sec in SOAP_SECTIONS and not secs.get(sec):
            reasons.add(QUALITY_REQUIRED_SECTION_EMPTY_REASON)
    if secs.get("assessment") and not secs.get("plan"):
        reasons.add(QUALITY_ASSESSMENT_NO_PLAN_REASON)
    total_claims = sum(len(secs.get(s, [])) for s in SOAP_SECTIONS)
    if total_claims and isinstance(succinctness_target, (int, float)) and succinctness_target:
        total_words = sum(len(c.split()) for s in SOAP_SECTIONS for c in secs.get(s, []))
        if total_words / total_claims > succinctness_target:
            reasons.add(QUALITY_VERBOSE_REASON)
    return reasons


def _quality_survival(
    quality_flags: Any, attested_body: str, succinctness_target: Any, required_sections: Any,
) -> dict[str, dict[str, int]]:
    """Per DRAFT-time ``quality_*`` reason → ``{acted, ignored}`` (§4.3). For each distinct quality
    reason the DRAFT carried, re-check whether it STILL fires on the attested body: STILL fires ⇒
    ``ignored`` (the clinician left it → a tune-down candidate); STOPPED ⇒ ``acted`` (they filled the
    section / cut / added a plan → the check was useful). PHI-FREE (reason enums + a per-attest 0/1)."""
    if not isinstance(quality_flags, list):
        return {}
    draft_reasons = {
        str(f.get("reason")) for f in quality_flags
        if isinstance(f, dict) and str(f.get("reason", "")).startswith(QUALITY_REASON_PREFIX)}
    if not draft_reasons:
        return {}
    still = _recheck_quality_reasons(attested_body, succinctness_target, required_sections)
    return {
        reason: {"acted": 0 if reason in still else 1, "ignored": 1 if reason in still else 0}
        for reason in draft_reasons}


def compute_notegen_edit_row(
    *, draft_original: str, attested_body: str, grounding_flags: Any,
    template_id: Any, template_version: Any, source_id: str,
    quality_flags: Any = None, succinctness_target: Any = None, required_sections: Any = None,
) -> dict[str, Any]:
    """Build the ONE PHI-FREE ``notegen_edit`` row from the draft→attested diff. PURE + deterministic
    (no I/O, no ``ts`` — the writer stamps that). Every value is a count / delta / enum / bool; no
    claim text ever. Guaranteed to satisfy :func:`phi_free_violations` (a test pins compute output).

    #14e-ii: ``quality_flags`` (the note's draft-time advisory quality flags) + ``succinctness_target``
    + ``required_sections`` (from the active profile at attest) drive ``quality_survival`` — the
    quality-check self-correcting signal (acted vs ignored per reason)."""
    draft_secs = _parse_sections(str(draft_original or ""))
    att_secs = _parse_sections(str(attested_body or ""))
    sections: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {f: 0 for f in _TOTAL_FIELDS}
    for sec in SOAP_SECTIONS:
        counts = _classify_section(draft_secs[sec], att_secs[sec])
        sections[sec] = counts
        for k, val in counts.items():
            totals[k] += val
    totals["net_word_delta"] = totals["words_attested"] - totals["words_draft"]
    draft_total = totals["claims_draft"]
    high_mod = draft_total > 0 and (totals["claims_modified"] / draft_total) >= _HIGH_MODIFICATION_RATIO
    return {
        "kind": KIND_NOTEGEN_EDIT,
        "source_id": str(source_id),
        "template_id": str(template_id) if template_id else DEFAULT_TEMPLATE_ID,
        "template_version": _coerce_version(template_version),
        "sections": sections,
        "totals": totals,
        "flag_survival": _flag_survival(grounding_flags, attested_body),
        "quality_survival": _quality_survival(
            quality_flags, attested_body, succinctness_target, required_sections),
        "high_modification": bool(high_mod),
    }


def phi_free_violations(row: dict[str, Any]) -> list[str]:
    """Return the PHI-FREE-contract violations in a WRITTEN ``notegen_edit`` row (empty = clean). The
    widening pin asserts this is ``[]`` for real output AND non-empty for a mutated row. Enforces:
    top-level keys == :data:`_ROW_FIELDS`; the only STRING leaves are the allowlisted top-level ids
    + flag_survival reason KEYS; sections/totals sub-keys ⊆ the count fields with int values;
    flag_survival values are ``{removed,kept}`` int-dicts. A claim-text string anywhere ⇒ a violation."""
    v: list[str] = []
    keys = set(row)
    if keys != set(_ROW_FIELDS):
        v.append(f"top-level keys mismatch: {sorted(keys ^ set(_ROW_FIELDS))}")
    for f in _STRING_VALUE_FIELDS:
        if f in row and not isinstance(row[f], str):
            v.append(f"{f!r} must be str")
    if "template_version" in row and not _is_int(row["template_version"]):
        v.append("template_version must be int")
    if "high_modification" in row and not isinstance(row["high_modification"], bool):
        v.append("high_modification must be bool")
    sec_block = row.get("sections")
    if not isinstance(sec_block, dict):
        v.append("sections must be a dict")
    else:
        for sec, counts in sec_block.items():
            if sec not in SOAP_SECTIONS:
                v.append(f"unknown section {sec!r}")
            v += _count_violations(counts, _SECTION_COUNT_FIELDS, f"sections.{sec}")
    v += _count_violations(row.get("totals"), _TOTAL_FIELDS, "totals")
    v += _survival_violations(row.get("flag_survival"), _SURVIVAL_FIELDS, "flag_survival")
    v += _survival_violations(row.get("quality_survival"), _QUALITY_SURVIVAL_FIELDS, "quality_survival")
    return v


def _survival_violations(block: Any, inner: frozenset, label: str) -> list[str]:
    """A survival sub-map ``{reason_enum: {<inner fields>: int}}`` — reason KEYS are enum strings; the
    values are exactly the inner int-dict. A claim-text string as a key OR value ⇒ a violation."""
    if not isinstance(block, dict):
        return [f"{label} must be a dict"]
    out: list[str] = []
    for reason, sub in block.items():
        if not isinstance(reason, str):
            out.append(f"{label} reason key must be a str enum")
        if not isinstance(sub, dict) or set(sub) != set(inner):
            out.append(f"{label}[{reason!r}] must be exactly {set(inner)}")
        elif not all(_is_int(x) for x in sub.values()):
            out.append(f"{label}[{reason!r}] values must be int")
    return out


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _count_violations(block: Any, allowed: frozenset, label: str) -> list[str]:
    if not isinstance(block, dict):
        return [f"{label} must be a dict"]
    out: list[str] = []
    for k, val in block.items():
        if k not in allowed:
            out.append(f"{label}.{k} not in the count allowlist")
        if not _is_int(val):
            out.append(f"{label}.{k} must be int (got {type(val).__name__})")
    return out


def record_notegen_edit_outcome(
    *, enrollment_dir: Any, grounding_flags: Any, draft_original: str, attested_body: str,
    template_id: Any = None, template_version: Any = None, source_id: str,
    quality_flags: Any = None, succinctness_target: Any = None, required_sections: Any = None,
) -> None:
    """#14 self-correcting Part-1 CAPTURE at attest — the FOURTH read-only, fail-silent sibling.

    Computes the PHI-FREE ``notegen_edit`` row (draft→attested diff) and appends it to the shared
    capture sink. SIDE-EFFECT-FREE + fail-silent BY CONSTRUCTION: any error is swallowed so a capture
    bug can NEVER fail a medico-legal attest.

    DORMANCY IS OBSERVABLE (not silent — the intentionally-left-blank trap): when the sink is dormant
    (``enrollment_dir`` unset ⇒ no resolvable capture sink), emit a ONE-TIME signal so absent rows are
    diagnosable rather than mistaken for 'no edits'. (Roadmap: decouple this sink from the voice
    ``enrollment_dir`` — see the module docstring.)"""
    try:
        if not str(enrollment_dir or ""):
            if not _DORMANT["warned"]:
                _DORMANT["warned"] = True
                log.warning(
                    "scribe.notegen_feedback.capture_dormant", source_id=source_id,
                    detail="notegen_edit capture DORMANT — no capture sink configured "
                           "(scribe.diarize.enrollment_dir unset). Note-gen edit-diff feedback is NOT "
                           "being recorded; absent rows are EXPECTED until a sink is configured. "
                           "One-time signal (further dormant attests stay quiet).")
            return
        row = compute_notegen_edit_row(
            draft_original=draft_original, attested_body=attested_body,
            grounding_flags=grounding_flags, template_id=template_id,
            template_version=template_version, source_id=source_id,
            quality_flags=quality_flags, succinctness_target=succinctness_target,
            required_sections=required_sections)
        from alfred.scribe import enroll_learning
        enroll_learning.record_notegen_edit(enrollment_dir, row=row)
    except Exception:  # noqa: BLE001 — capture must NEVER affect a valid attest
        log.warning(
            "scribe.notegen_feedback.capture_error", source_id=source_id,
            detail="notegen_edit capture failed — SWALLOWED (attestation unaffected)")


# ===========================================================================
# #14e-i — the read surfaces (Part A status readout + Part B on-box raw diff)
# ===========================================================================

def read_notegen_edit_rows(enrollment_dir: Any) -> list[dict]:
    """Read all ``notegen_edit`` rows from the shared capture sink. PHI-FREE (the rows are the closed
    frozenset of counts/enums). Tolerant: unset/absent/torn → the rows read so far (a corrupt sink never
    raises into the read-only readout)."""
    if not str(enrollment_dir or ""):
        return []
    from alfred.scribe.enroll_learning import _capture_path
    path = _capture_path(enrollment_dir)
    rows: list[dict] = []
    try:
        if not path.is_file():
            return []
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                except Exception:  # noqa: BLE001 — a torn line is skipped, not fatal
                    continue
                if isinstance(row, dict) and row.get("kind") == KIND_NOTEGEN_EDIT:
                    rows.append(row)
    except Exception:  # noqa: BLE001 — a corrupt/unreadable sink must never crash the readout
        return rows
    return rows


def aggregate_feedback(rows: list[dict]) -> dict:
    """Aggregate ``notegen_edit`` rows into the PHI-FREE Part-A readout: per-section add/remove/modify/
    keep totals, the median ``net_word_delta``, the grounding-reason FP RANKING (per reason kept vs
    removed → a high kept-rate = "the clinician kept the flagged claim → the flag is a tune-down
    candidate"), and the ``high_modification`` encounter list BY OPAQUE ``source_id``. Counts / enums /
    opaque ids ONLY — no claim text ever leaves the sink."""
    import statistics
    n = len(rows)
    sections = {sec: {k: 0 for k in _SECTION_COUNT_FIELDS} for sec in SOAP_SECTIONS}
    net_word_deltas: list[int] = []
    survival: dict[str, dict[str, int]] = {}
    quality: dict[str, dict[str, int]] = {}   # #14e-ii — {reason: {acted, ignored}}
    high_mod: list[str] = []
    for row in rows:
        for sec in SOAP_SECTIONS:
            sc = (row.get("sections") or {}).get(sec) or {}
            for k in _SECTION_COUNT_FIELDS:
                sections[sec][k] += int(sc.get(k, 0) or 0)
        totals = row.get("totals") or {}
        if isinstance(totals.get("net_word_delta"), int):
            net_word_deltas.append(totals["net_word_delta"])
        for reason, sub in (row.get("flag_survival") or {}).items():
            b = survival.setdefault(str(reason), {"removed": 0, "kept": 0})
            b["removed"] += int((sub or {}).get("removed", 0) or 0)
            b["kept"] += int((sub or {}).get("kept", 0) or 0)
        for reason, sub in (row.get("quality_survival") or {}).items():
            b = quality.setdefault(str(reason), {"acted": 0, "ignored": 0})
            b["acted"] += int((sub or {}).get("acted", 0) or 0)
            b["ignored"] += int((sub or {}).get("ignored", 0) or 0)
        if row.get("high_modification") and row.get("source_id"):
            high_mod.append(str(row["source_id"]))
    fp_ranking = []
    for reason, b in survival.items():
        total = b["removed"] + b["kept"]
        fp_ranking.append({
            "reason": reason, "kept": b["kept"], "removed": b["removed"],
            "kept_rate": (b["kept"] / total if total else 0.0)})
    # highest kept-rate first (the strongest FP / tune-down candidate), then by volume.
    fp_ranking.sort(key=lambda r: (-r["kept_rate"], -(r["kept"] + r["removed"])))
    # #14e-ii — the quality-check tune-down ranking: highest IGNORED-rate first (a quality check the
    # clinician consistently leaves in place = a candidate to tune down / drop), then by volume.
    quality_ranking = []
    for reason, b in quality.items():
        total = b["acted"] + b["ignored"]
        quality_ranking.append({
            "reason": reason, "acted": b["acted"], "ignored": b["ignored"],
            "ignored_rate": (b["ignored"] / total if total else 0.0)})
    quality_ranking.sort(key=lambda r: (-r["ignored_rate"], -(r["acted"] + r["ignored"])))
    return {
        "attests": n,
        "sections": sections,
        "median_net_word_delta": statistics.median(net_word_deltas) if net_word_deltas else 0,
        "flag_survival": survival,
        "fp_ranking": fp_ranking,
        "quality_survival": quality,
        "quality_ranking": quality_ranking,
        "high_modification_source_ids": sorted(set(high_mod)),
    }


def recompute_raw_diff(vault_path: Any, source_id: str) -> str | None:
    """Part B — recompute the raw draft→attested unified diff ON-BOX from vault records (the note's
    ``draft_original`` frontmatter vs its attested body). Returns the diff text (``""`` when draft ==
    attested), or ``None`` when no clinical_note matches ``source_id``.

    ⚠ PHI-BEARING — the diff carries the clinician's raw edits (full PHI). The CALLER displays it to
    STDOUT ONLY: it is NEVER persisted, logged, audited, or egressed (the arc's one authorized-PHI
    on-box surface, ephemeral). This function itself only READS vault files + returns the string; it
    writes nothing."""
    from alfred.scribe.retention import resolve_note_paths
    import frontmatter
    matches, _malformed = resolve_note_paths(Path(vault_path), source_id)
    if not matches:
        return None
    post = frontmatter.load(str(sorted(matches)[-1]))   # latest (an amended supersede shares source_id)
    draft = str(post.metadata.get("draft_original") or "")
    attested = post.content or ""
    return "\n".join(difflib.unified_diff(
        draft.splitlines(), attested.splitlines(),
        fromfile="draft_original", tofile="attested_body", lineterm=""))

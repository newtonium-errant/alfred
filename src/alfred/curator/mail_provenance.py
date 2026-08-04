"""Mail provenance marker — the answer to "is this note actually email?".

#40. An operator screenshot on 2026-08-04 showed an EMAIL TIER / LOW review
card, with SPAM / PARK / CONFIRM verbs, on
``note/Daily Sync HYPATIA Ben McMillan Proposal Fragmentation 2026-05-03.md`` —
a vision-captured screenshot note documenting a May bug, not an email.

## What actually went wrong (the brief's root cause was close but not it)

A not-mail exit was NOT missing. ``email_classifier.is_email_inbox`` has gated
the live path since 2026-04-22, eleven days before the offending note. The
defect is in the BACKFILL's admission heuristic, and the code documented it:

    re.compile(r"\\b(?:email|newsletter|sender|subject line|unsubscribe|inbox)\\b", ...)

    # We keep these permissive — the cost of a false positive (classifying a
    # non-email note) is just one LLM call + one "unclassified" result [...]

A Daily Sync bug-report note contains the words "email" and "inbox", because
the Daily Sync *has* an email-calibration section. One word-match enrolled it.

**The real bug is that the stated cost model was falsified by a consumer that
arrived later.** A false positive does not cost one LLM call. ``classify_record``
STAMPS ``priority`` + ``priority_reasoning`` onto the record, and the Daily Sync
sampler treated a real priority tier as proof of mail provenance — so a false
positive permanently enrolled a non-mail note in the calibration rotation, where
it became a deck card and, if swiped, would poison the email corpus with a
judgment about something that was never email. The comment was true when
written; the priority-as-provenance proxy made it false and nobody revisited it.

The incident also kills the "just one unclassified result" reassurance
empirically: the model returned **LOW**, a real tier, not the sentinel.

## Why the marker lives here and not in either mail post-pass

Both mail post-passes are independently OPT-IN
(``email_classifier.enabled`` / ``email_filing.enabled``), and the filing pass
additionally returns BEFORE writing anything on its no-category branch — which
its own docstring calls "the common case for personal mail / newsletters". So
neither can own a marker that must be present on every email-derived note:

* ``email_message_id`` looks like an existing marker and is a COVERAGE TRAP —
  written only after that early return, on an orthogonal axis. Keying on it
  would silently drop exactly the mail most worth calibrating.
* a marker written by the priority classifier would be absent whenever the
  classifier is disabled, and present on whatever the classifier happened to
  touch — which is the proxy problem again, one field over.

The curator daemon, in contrast, knows at STRUCTURING time that it just built
these notes from an inbox item it can identify as email-shaped. That is the
provenance fact, it is available regardless of either opt-in block, and it is
what this module records.

## What the marker does and does not claim

It claims ARRIVAL: "this note was structured from email-shaped inbox content."
It does not claim the note is correspondence worth triaging.

**Markers record FACTS; judgments live in the calibration loop.** That split is
the design, not a shortfall of it. The one workflow where the two diverge is the
operator emailing screenshots to himself: those notes genuinely DID arrive as
mail, the marker records that correctly, and they stay sampleable. If he marks
one down in review, the corpus learns his self-sent pattern — which is
capture-the-correction doing exactly its job, so the residual shrinks with use
instead of sitting there.

The alternative — having the classifier judge "operational log vs
correspondence" and suppress the stamp — is deliberately NOT attempted. That
puts a judgment call back on the LLM, which is the shape that caused this
incident in the first place.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: The provenance field. Minimal by intent — one boolean, in the existing
#: ``email_*`` frontmatter namespace (``email_category``, ``email_message_id``).
EMAIL_PROVENANCE_FIELD = "email_derived"

#: STRUCTURAL email-shape markers — the curator's own email-note format.
#:
#: Deliberately a STRICT SUBSET of ``backfill._EMAIL_BODY_MARKERS``: it keeps
#: the three header patterns and drops the two that caused #40 — the bare
#: email-address regex (matches any note quoting an address) and the
#: ``email|newsletter|sender|inbox|…`` word-list (matches any note ABOUT email,
#: which is what enrolled a Daily Sync bug report).
#:
#: This is the ONLY predicate allowed to decide provenance retroactively. The
#: distinction matters: a marker backfilled with the permissive heuristic would
#: faithfully re-import every false positive it has already produced, wearing a
#: name that now asserts provenance with confidence — converting a soft, visibly
#: wrong proxy into a hard, trusted, wrong claim.
_STRUCTURAL_HEADER_MARKERS = (
    re.compile(r"^\s*\*\*From:\*\*", re.MULTILINE),
    re.compile(r"^\s*\*\*Subject:\*\*", re.MULTILINE),
    re.compile(r"^\s*\*\*Account:\*\*", re.MULTILINE),
)


def note_is_email_derived(fm: dict[str, Any] | None) -> bool:
    """The READ predicate — does this note carry the provenance marker?

    The single discriminator every consumer must use. Consumers must NOT
    re-derive provenance from ``priority``, from ``email_message_id``, or from
    body text; those are the three proxies #40 was caused by, and each is wrong
    in its own direction (a stray stamp, a conditional field, a word-match).

    Accepts the loose truthy spellings PyYAML produces from hand-edited
    frontmatter (``true`` / ``"true"`` / ``yes``) so an operator who adds the
    field by hand gets what they meant. Anything else — absent, false, empty,
    a stray string — is NOT email-derived. Fail-closed is correct here: the
    cost of a false negative is one email missing from a calibration batch; the
    cost of a false positive is a non-mail judgment in the email corpus.
    """
    if not fm:
        return False
    raw = fm.get(EMAIL_PROVENANCE_FIELD)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "yes", "1"}
    return False


def has_structural_email_headers(body: str) -> bool:
    """Does this note body carry the curator's email-note header shape?

    The retroactive provenance predicate — used ONLY by the one-shot backfill
    that grants the marker to genuine pre-cutover email notes. See
    :data:`_STRUCTURAL_HEADER_MARKERS` for why it is narrower than the
    backfill module's admission heuristic.

    NOT a substitute for the marker at read time. A consumer that calls this
    instead of :func:`note_is_email_derived` has reintroduced body-sniffing as
    a provenance test, which is the defect class, just with better regexes.
    """
    if not body:
        return False
    return any(p.search(body) for p in _STRUCTURAL_HEADER_MARKERS)


def stamp_email_provenance(
    vault_path: Path | str,
    note_paths: list[str],
) -> list[str]:
    """Mark ``note_paths`` as email-derived. Returns the paths actually written.

    Called from the curator daemon when an inbox item is email-shaped and notes
    were created from it. Additive and isolated: writes ONE field and never
    touches ``priority`` / ``action_hint`` / ``priority_reasoning``, mirroring
    ``email_filing``'s orthogonality guarantee — the provenance axis and the
    tier axis must stay independent, since conflating them is the bug.

    Never raises. The curator treats mail post-passes as fire-and-forget, and a
    provenance write must not be the thing that breaks curation; a missing
    marker degrades to "this note won't be offered for calibration", which is
    the safe direction.

    NO mutation-logging, deliberately. An earlier draft carried a
    ``session_path`` parameter for ``log_mutation``; it was dead by
    construction — the curator's ``cleanup_session_file`` runs well before this
    point, so no caller could ever have supplied one. A parameter that implies
    audit-logging which never happens is worse than its absence: it reads, to
    the next person, as a guarantee. The vault audit log still records the
    ``vault_edit`` itself.
    """
    from alfred.vault.ops import VaultError, vault_edit

    note_only = [
        p for p in note_paths if p.startswith("note/") and p.endswith(".md")
    ]
    if not note_only:
        # ILB: an email-shaped inbox item that produced no note records is a
        # real, legible outcome (the curator may have made only person/org
        # records) — not a silent skip.
        log.info("curator.mail_provenance.no_notes", candidates=len(note_paths))
        return []

    written: list[str] = []
    for rel_path in note_only:
        try:
            vault_edit(
                vault_path, rel_path,
                set_fields={EMAIL_PROVENANCE_FIELD: True},
            )
            written.append(rel_path)
        except VaultError as exc:
            log.warning(
                "curator.mail_provenance.write_failed",
                path=rel_path, error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — never crash the curator
            log.warning(
                "curator.mail_provenance.unexpected_error",
                path=rel_path, error=str(exc),
            )

    log.info(
        "curator.mail_provenance.stamped",
        count=len(written), candidates=len(note_only),
    )
    return written


__all__ = [
    "EMAIL_PROVENANCE_FIELD",
    "has_structural_email_headers",
    "note_is_email_derived",
    "stamp_email_provenance",
]

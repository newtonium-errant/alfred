"""#40 — mail provenance marker, and the card that should never have existed.

Operator screenshot 2026-08-04: an EMAIL TIER / LOW review card, with
SPAM / PARK / CONFIRM verbs, on a vision-captured screenshot note documenting a
May Daily Sync bug. Not an email.

The chain: the backfill's admission heuristic matches the bare WORDS "email" and
"inbox" — a Daily Sync bug report contains both, because the Daily Sync has an
email-calibration section — so the note got a real ``priority`` stamp; and the
sampler treated a real tier as proof of mail provenance. A merge-bumped mtime
on 2026-08-04 floated it to the top of the newest-first walk and it dealt.

These pins fix the DISCRIMINATOR. The regression pin is built from the actual
record shape in the screenshot, not a synthetic "note A": a fixture shaped for
convenience would not contain the words that caused the admission.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from alfred.curator.mail_provenance import (
    EMAIL_PROVENANCE_FIELD,
    has_structural_email_headers,
    note_is_email_derived,
    stamp_email_provenance,
)
from alfred.daily_sync.email_section import _read_candidate

# The real record from the screenshot. Its body genuinely discusses email
# triage, which is exactly why the permissive heuristic admitted it.
SCREENSHOT_NOTE = (
    "note/Daily Sync HYPATIA Ben McMillan Proposal Fragmentation 2026-05-03.md"
)
SCREENSHOT_BODY = (
    "# Daily Sync bug — proposal fragmentation\n\n"
    "Screenshot captured from the Daily Sync. The email-calibration section "
    "showed a fragmented proposal for Ben McMillan. The inbox rotation "
    "appears to re-surface the same sender across days.\n"
)


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "note").mkdir(parents=True)
    return v


def _write_note(vault: Path, rel_path: str, fm: str, body: str = "Body.\n") -> Path:
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\n\n{body}", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The regression pin — this exact card
# ---------------------------------------------------------------------------


def test_non_mail_note_with_a_priority_stamp_is_not_sampleable(
    tmp_path: Path,
) -> None:
    """THE #40 pin. A stray ``priority`` stamp on a non-mail record must not
    produce a calibration candidate — regardless of how confident the stamp is.

    Before the fix this record returned a full candidate and became an EMAIL
    TIER / LOW deck card offering SPAM / PARK / CONFIRM. The classifier's own
    reasoning (preserved below verbatim in spirit) shows it recognised what the
    record was and stamped a real tier anyway, which is why "the sentinel will
    catch it" was never true.
    """
    vault = _vault(tmp_path)
    _write_note(
        vault, SCREENSHOT_NOTE,
        "type: note\nname: Daily Sync HYPATIA proposal fragmentation\n"
        "priority: low\n"
        "priority_reasoning: automated internal system note, purely an "
        "operational log entry\n",
        SCREENSHOT_BODY,
    )

    assert _read_candidate(vault, SCREENSHOT_NOTE) is None


def test_an_mtime_bump_does_not_resurface_a_non_mail_note(
    tmp_path: Path,
) -> None:
    """The #21-merge TRIGGER class. The link-rewrite bumped this file's mtime,
    which floated it to the head of the newest-first walk; the corpus
    shown-once filter had never seen it, so it dealt.

    Freshness must not be able to substitute for provenance — otherwise any
    future bulk rewrite re-deals every stray-stamped record in the vault.
    """
    vault = _vault(tmp_path)
    p = _write_note(
        vault, SCREENSHOT_NOTE,
        "type: note\nname: Daily Sync bug\npriority: low\n",
        SCREENSHOT_BODY,
    )
    future = time.time() + 3600
    os.utime(p, (future, future))

    assert _read_candidate(vault, SCREENSHOT_NOTE) is None


def test_a_genuine_email_note_is_still_sampleable(tmp_path: Path) -> None:
    """The paired positive. A fix that also stops real mail reaching
    calibration would be a worse bug than the one it closes — the corpus would
    quietly stop growing and nothing would say so."""
    vault = _vault(tmp_path)
    _write_note(
        vault, "note/Borrowell credit score update.md",
        f"type: note\nname: Borrowell update\n{EMAIL_PROVENANCE_FIELD}: true\n"
        "priority: low\n",
        "**From:** info@email.borrowell.com\n**Subject:** Your score\n\nBody.\n",
    )

    candidate = _read_candidate(vault, "note/Borrowell credit score update.md")
    assert candidate is not None
    assert candidate.priority == "low"


def test_email_message_id_is_not_a_provenance_fallback_AT_THE_SAMPLER(
    tmp_path: Path,
) -> None:
    """The gate's half-refutation, closed.

    The predicate-level pin below (``test_everything_else_is_not_email_derived``)
    proves ``note_is_email_derived`` returns False for an ``email_message_id``
    dict. That says NOTHING about whether the SAMPLER consults the predicate
    exclusively — and the reviewer proved it: mutating ``_read_candidate`` to

        if not (note_is_email_derived(fm) or fm.get("email_message_id")):

    left all 598 tests green. That is the exact fallback a maintainer reaches
    for, and the one the module docstring names as the seductive wrong answer;
    the pin that was supposed to forbid it was testing a different subject.

    So this drives the SAMPLER. ``email_message_id`` is written by
    ``email_filing`` on an orthogonal axis and only AFTER its no-category early
    return — the common case for personal mail — so it is a coverage trap, not
    a marker. A record carrying it without the real marker must not be
    sampleable.
    """
    vault = _vault(tmp_path)
    _write_note(
        vault, SCREENSHOT_NOTE,
        "type: note\nname: Daily Sync bug\npriority: low\n"
        "email_message_id: '<CAF=abc123@mail.example.com>'\n",
        SCREENSHOT_BODY,
    )

    assert _read_candidate(vault, SCREENSHOT_NOTE) is None


def test_email_category_is_not_a_provenance_fallback_either(
    tmp_path: Path,
) -> None:
    """The sibling field from the same orthogonal axis. ``email_category`` is
    written beside ``email_message_id`` and is equally not provenance — pinned
    at the sampler so neither half of that pair can become the fallback."""
    vault = _vault(tmp_path)
    _write_note(
        vault, SCREENSHOT_NOTE,
        "type: note\nname: Daily Sync bug\npriority: low\n"
        "email_category: 'finance/statements'\n",
        SCREENSHOT_BODY,
    )

    assert _read_candidate(vault, SCREENSHOT_NOTE) is None


def test_marker_without_a_real_tier_is_still_not_sampleable(
    tmp_path: Path,
) -> None:
    """Provenance is necessary, not sufficient — the unclassified sentinel is
    still excluded, so calibration only ever sees real classifier decisions.
    The marker ADDS a gate; it must not remove the existing one."""
    vault = _vault(tmp_path)
    _write_note(
        vault, "note/Unclassified mail.md",
        f"type: note\nname: Unclassified\n{EMAIL_PROVENANCE_FIELD}: true\n"
        "priority: unclassified\n",
        "**From:** a@b.com\n",
    )
    assert _read_candidate(vault, "note/Unclassified mail.md") is None


# ---------------------------------------------------------------------------
# The read predicate — fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [True, "true", "True", " yes ", "1"])
def test_truthy_spellings_count_as_email_derived(raw) -> None:
    """Hand-edited frontmatter, so PyYAML's several spellings of yes all mean
    yes — an operator who adds the field by hand gets what they meant."""
    assert note_is_email_derived({EMAIL_PROVENANCE_FIELD: raw}) is True


@pytest.mark.parametrize(
    "fm",
    [
        {},
        None,
        {EMAIL_PROVENANCE_FIELD: False},
        {EMAIL_PROVENANCE_FIELD: "false"},
        {EMAIL_PROVENANCE_FIELD: ""},
        {EMAIL_PROVENANCE_FIELD: "maybe"},
        {"priority": "low"},  # the retired proxy, explicitly not provenance
        {"email_message_id": "<abc@x>"},  # the coverage trap, likewise not
    ],
)
def test_everything_else_is_not_email_derived(fm) -> None:
    """Fail-closed. A missed calibration item costs one batch slot; a false one
    costs a corpus row asserting an email judgment about something that was
    never email.

    The last two cases are the two proxies #40 was caused by — pinned as
    NEGATIVE so a future reader cannot quietly reintroduce either as a fallback.
    """
    assert note_is_email_derived(fm) is False


# ---------------------------------------------------------------------------
# The retroactive predicate — narrower than the one that caused the bug
# ---------------------------------------------------------------------------


def test_structural_predicate_rejects_the_words_that_caused_this(tmp_path: Path) -> None:
    """The backfill predicate must NOT admit the screenshot note.

    ``backfill._EMAIL_BODY_MARKERS`` admits it on a bare word-match for "email"
    or "inbox". The retroactive marker grant uses header shape ONLY, because a
    marker backfilled with the permissive heuristic would faithfully re-import
    every false positive it has already produced — converting a soft, visibly
    wrong proxy into a hard, trusted, wrong claim.
    """
    assert has_structural_email_headers(SCREENSHOT_BODY) is False
    # And the bare-address case the permissive heuristic also admits.
    assert has_structural_email_headers("Contact ben@example.com about it.") is False


@pytest.mark.parametrize(
    "body",
    [
        "**From:** info@email.borrowell.com\n\nBody.",
        "**Subject:** Your statement\n\nBody.",
        "**Account:** andrew@example.com\n**Subject:** x\n",
    ],
)
def test_structural_predicate_admits_real_curator_email_notes(body: str) -> None:
    """Paired positive: the curator's own email-note header shape still counts,
    so the one-shot grant restores history rather than emptying it."""
    assert has_structural_email_headers(body) is True


#: Bodies the PERMISSIVE heuristic admits and the structural predicate MUST NOT.
#: This is the #40 false-positive class, verbatim in shape: word-matches on
#: notes ABOUT email, and bare addresses quoted in ordinary prose.
PERMISSIVE_ONLY_BODIES = [
    SCREENSHOT_BODY,
    "We discussed the newsletter rollout in the meeting.",
    "Contact ben@example.com about the proposal.",
    "Check your inbox for the unsubscribe link.",
    "The sender was unclear about the subject line.",
]

#: Bodies that ARE unambiguous curator header shape — every serialization era.
STRUCTURAL_BODIES = [
    "**From:** info@borrowell.com\n**Subject:** Your score\n",
    "- **From:** team@80000hours.org\n",       # the 493-record bulleted era
    "- **Subject:** Your weekly digest\n",     # bulleted, NO address on the line
    "- **Account:** andrew\n",                 # bulleted, NO address at all
    "  **Account:** andrew@example.com\n**Subject:** x\n",
]


def test_structural_predicate_rejects_the_entire_permissive_only_class() -> None:
    """The property that actually matters, asserted directly.

    This REPLACES a pattern-set ``structural < permissive`` assertion, which was
    a syntactic proxy that did not survive contact with the bulleted era — and,
    on measurement, neither did the semantic version it stood in for. Only
    ``- **From:** addr@x`` still matches the permissive set, and only because
    the address happens to sit on the same line; ``- **Subject:** …`` and
    ``- **Account:** …`` match NOTHING permissive. So structural is no longer a
    subset in either sense.

    That is fine, because subset was never the real invariant. ``permissive`` is
    not a superset of "all reasonable email signals" — it is a differently
    shaped net that happens to be too loose in one direction. The invariant
    worth guarding is narrower and stated here: **the structural predicate must
    reject every body in the #40 false-positive class**, whatever the other
    heuristic does.
    """
    for body in PERMISSIVE_ONLY_BODIES:
        assert has_structural_email_headers(body) is False, (
            f"structural predicate admitted a permissive-only body: {body[:60]!r}"
        )


def test_the_permissive_heuristic_really_does_admit_that_class() -> None:
    """Guard the guard. If the backfill heuristic were ever tightened, the pin
    above would still pass while asserting nothing interesting — it would be
    rejecting bodies nothing admits. Driving the real module keeps the contrast
    live rather than assumed."""
    from alfred.email_classifier import backfill

    for body in PERMISSIVE_ONLY_BODIES:
        assert any(p.search(body) for p in backfill._EMAIL_BODY_MARKERS), (
            f"permissive heuristic no longer admits {body[:60]!r} — this pin's "
            f"contrast has gone stale; re-derive the false-positive class"
        )


def test_structural_predicate_admits_every_known_header_era() -> None:
    """Positive half. The bulleted entries are the 493 records the original
    anchor missed; the two without an address on the line are the ones that
    prove this is about HEADER SHAPE, not about finding an email address."""
    for body in STRUCTURAL_BODIES:
        assert has_structural_email_headers(body) is True, (
            f"structural predicate missed a known-good header shape: {body[:60]!r}"
        )


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_stamp_writes_the_marker_and_touches_nothing_else(tmp_path: Path) -> None:
    """Additive and isolated — the provenance axis must stay independent of the
    tier axis, since conflating them is the bug this closes."""
    import frontmatter

    vault = _vault(tmp_path)
    _write_note(
        vault, "note/Mail A.md",
        "type: note\nname: Mail A\npriority: high\naction_hint: reply\n",
    )

    written = stamp_email_provenance(vault, ["note/Mail A.md"])

    assert written == ["note/Mail A.md"]
    fm = frontmatter.load(str(vault / "note/Mail A.md")).metadata
    assert note_is_email_derived(fm) is True
    # Untouched.
    assert fm["priority"] == "high"
    assert fm["action_hint"] == "reply"


def test_stamp_ignores_non_note_records(tmp_path: Path) -> None:
    """The curator creates person/org/task records from the same inbox item;
    only ``note/*.md`` records are calibration candidates, so only they are
    marked."""
    vault = _vault(tmp_path)
    (vault / "person").mkdir(parents=True, exist_ok=True)
    _write_note(vault, "person/Ben.md", "type: person\nname: Ben\n")

    assert stamp_email_provenance(vault, ["person/Ben.md"]) == []


def test_stamp_never_raises_on_a_missing_record(tmp_path: Path) -> None:
    """Fire-and-forget: a provenance write must never be the thing that breaks
    curation. A missing marker degrades to "not offered for calibration", which
    is the safe direction."""
    vault = _vault(tmp_path)
    assert stamp_email_provenance(vault, ["note/Nope.md"]) == []


# ---------------------------------------------------------------------------
# The box-run cleanup script (team-lead executes; pinned here)
# ---------------------------------------------------------------------------


def test_cleanup_grants_markers_only_to_real_email_notes(tmp_path: Path) -> None:
    """The grant is CONSERVATIVE by design: it under-grants rather than
    over-grants. Granting on the permissive heuristic would re-import every
    false positive it has already produced."""
    from alfred.scripts.mail_provenance_cleanup import grant_markers
    import frontmatter

    vault = _vault(tmp_path)
    _write_note(vault, "note/Real mail.md", "type: note\nname: Real\n",
                "**From:** a@b.com\n**Subject:** hi\n")
    _write_note(vault, SCREENSHOT_NOTE, "type: note\nname: Bug\n", SCREENSHOT_BODY)

    res = grant_markers(vault, apply=True, limit=0)

    assert res.paths == ["note/Real mail.md"]
    assert note_is_email_derived(
        frontmatter.load(str(vault / "note/Real mail.md")).metadata,
    ) is True
    # The screenshot note is untouched — it is exactly what must NOT be granted.
    assert note_is_email_derived(
        frontmatter.load(str(vault / SCREENSHOT_NOTE)).metadata,
    ) is False


def test_cleanup_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Dry-run is the default and must be inert — this is an operator tool run
    against the live vault."""
    from alfred.scripts.mail_provenance_cleanup import grant_markers
    import frontmatter

    vault = _vault(tmp_path)
    p = _write_note(vault, "note/Real mail.md", "type: note\nname: Real\n",
                    "**From:** a@b.com\n")
    before = p.read_bytes()

    res = grant_markers(vault, apply=False, limit=0)

    assert res.matched == 1 and res.changed == 0
    assert p.read_bytes() == before
    assert note_is_email_derived(frontmatter.load(str(p)).metadata) is False


def test_neutralize_strips_the_stray_stamp_but_spares_real_mail(
    tmp_path: Path,
) -> None:
    """The ordering guarantee. An unmarked note that LOOKS like real mail is
    left for the grant pass — stripping it would destroy a real classification,
    which is why neutralize skips anything with header shape."""
    from alfred.scripts.mail_provenance_cleanup import neutralize_stray_stamps
    import frontmatter

    vault = _vault(tmp_path)
    _write_note(vault, SCREENSHOT_NOTE,
                "type: note\nname: Bug\npriority: low\n"
                "priority_reasoning: operational log entry\n", SCREENSHOT_BODY)
    _write_note(vault, "note/Ungranted mail.md",
                "type: note\nname: Mail\npriority: high\n",
                "**From:** a@b.com\n")

    res = neutralize_stray_stamps(vault, apply=True, limit=0)

    assert res.paths == [f"{SCREENSHOT_NOTE}"]
    stripped = frontmatter.load(str(vault / SCREENSHOT_NOTE)).metadata
    assert not str(stripped.get("priority") or "").strip()
    # The unmarked-but-real-looking note keeps its classification.
    spared = frontmatter.load(str(vault / "note/Ungranted mail.md")).metadata
    assert spared["priority"] == "high"


def test_neutralized_record_is_no_longer_sampleable(tmp_path: Path) -> None:
    """End-to-end: the cleanup's output feeds the sampler's input. Retiring the
    live card 'falls out of the stamp neutralization' — pinned rather than
    assumed."""
    from alfred.scripts.mail_provenance_cleanup import neutralize_stray_stamps

    vault = _vault(tmp_path)
    _write_note(vault, SCREENSHOT_NOTE,
                "type: note\nname: Bug\npriority: low\n", SCREENSHOT_BODY)
    neutralize_stray_stamps(vault, apply=True, limit=0)
    assert _read_candidate(vault, SCREENSHOT_NOTE) is None


def test_dedupe_collapses_duplicate_related_preserving_order(
    tmp_path: Path,
) -> None:
    """The #21 collateral. Order-preserving, exact duplicates only — the
    operator's own ordering survives."""
    from alfred.scripts.mail_provenance_cleanup import dedupe_related
    import frontmatter

    vault = _vault(tmp_path)
    _write_note(
        vault, "note/Merged.md",
        "type: note\nname: Merged\nrelated:\n"
        "  - '[[person/Ben McMillan]]'\n"
        "  - '[[project/RRTS]]'\n"
        "  - '[[person/Ben McMillan]]'\n"
        "  - '[[person/Ben McMillan]]'\n",
    )

    res = dedupe_related(vault, apply=True, limit=0)

    assert res.changed == 1
    related = frontmatter.load(str(vault / "note/Merged.md")).metadata["related"]
    assert related == ["[[person/Ben McMillan]]", "[[project/RRTS]]"]


def test_corpus_audit_reports_pollution_without_writing(tmp_path: Path) -> None:
    """A corpus row for a non-email record is an email judgment about something
    that was never email. Reported, never removed — deleting from an
    append-only operator record is not a script's decision."""
    from alfred.scripts.mail_provenance_cleanup import audit_corpus

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        '{"record_path": "' + SCREENSHOT_NOTE + '", "andrew_priority": "spam"}\n'
        '{"record_path": "note/Real mail.md", "andrew_priority": "low"}\n',
        encoding="utf-8",
    )
    before = corpus.read_bytes()

    hits = audit_corpus(corpus, [SCREENSHOT_NOTE])

    assert hits == [SCREENSHOT_NOTE]
    assert corpus.read_bytes() == before


# ---------------------------------------------------------------------------
# Gate delta — --limit parity, and strip-means-strip
# ---------------------------------------------------------------------------


def _many_email_notes(vault: Path, n: int) -> None:
    for i in range(n):
        _write_note(
            vault, f"note/Mail {i}.md", f"type: note\nname: Mail {i}\n",
            f"**From:** s{i}@example.com\n**Subject:** hi\n",
        )


@pytest.mark.parametrize("pass_name", ["grant", "neutralize", "dedupe"])
def test_limit_bounds_the_dry_run_identically_to_apply(
    tmp_path: Path, pass_name: str,
) -> None:
    """The dry run IS the preview of the apply, so ``--limit`` must bound both
    or the preview describes a different operation than the one that runs —
    worse than no preview, because it looks careful.

    Before the fix the limit check sat BELOW ``if not apply: continue``, so a
    dry run reported every candidate while the apply touched N. This is
    operationally load-bearing: the box run is staged with limits.
    """
    from alfred.scripts import mail_provenance_cleanup as cleanup

    vault = _vault(tmp_path)
    if pass_name == "grant":
        _many_email_notes(vault, 10)
        fn = cleanup.grant_markers
    elif pass_name == "neutralize":
        for i in range(10):
            _write_note(
                vault, f"note/Stray {i}.md",
                f"type: note\nname: Stray {i}\npriority: low\n",
                SCREENSHOT_BODY,
            )
        fn = cleanup.neutralize_stray_stamps
    else:
        for i in range(10):
            _write_note(
                vault, f"note/Dup {i}.md",
                f"type: note\nname: Dup {i}\nrelated:\n"
                "  - '[[person/Ben McMillan]]'\n"
                "  - '[[person/Ben McMillan]]'\n",
            )
        fn = cleanup.dedupe_related

    preview = fn(vault, apply=False, limit=3)
    assert preview.matched == 3, (
        f"{pass_name}: dry run reported {preview.matched} under --limit 3 — "
        f"the preview must describe the same operation the apply performs"
    )
    assert preview.changed == 0
    assert len(preview.paths) == 3

    applied = fn(vault, apply=True, limit=3)
    assert applied.matched == preview.matched
    assert applied.changed == 3


def test_neutralize_removes_the_keys_rather_than_nulling_them(
    tmp_path: Path,
) -> None:
    """A strip should strip. Writing ``priority: null`` leaves litter in every
    touched record, and a record carrying ``priority: null`` still reads to a
    human as "the classifier ran here"."""
    import frontmatter
    from alfred.scripts.mail_provenance_cleanup import neutralize_stray_stamps

    vault = _vault(tmp_path)
    _write_note(
        vault, SCREENSHOT_NOTE,
        "type: note\nname: Bug\npriority: low\naction_hint: reply\n"
        "priority_reasoning: operational log\n",
        SCREENSHOT_BODY,
    )

    neutralize_stray_stamps(vault, apply=True, limit=0)

    fm = frontmatter.load(str(vault / SCREENSHOT_NOTE)).metadata
    for key in ("priority", "action_hint", "priority_reasoning"):
        assert key not in fm, f"{key} was nulled, not removed: {fm.get(key)!r}"
    # The record itself survives — this strips stamps, not content.
    assert fm["name"] == "Bug"


def test_stamp_takes_no_session_path_parameter() -> None:
    """The dead parameter is gone, not just unused. It implied audit-logging
    that never happened — a signature that promises a guarantee it does not
    provide is worse than its absence."""
    import inspect

    assert "session_path" not in inspect.signature(stamp_email_provenance).parameters


# ---------------------------------------------------------------------------
# #40b — the bulleted era, and the probe
# ---------------------------------------------------------------------------


def test_bulleted_header_note_is_grantable_and_survives_neutralize(
    tmp_path: Path,
) -> None:
    """THE #40b regression. 493 production records serialize headers as bullets
    (``- **From:** team@80000hours.org``); the original anchor missed all of
    them, so the staged apply would have STRIPPED genuine email records out of
    the calibration pool.

    Driven through both passes, in the order the box runs them — the failure
    mode was never "grant returns False", it was "neutralize then eats it".
    """
    from alfred.scripts.mail_provenance_cleanup import (
        grant_markers,
        neutralize_stray_stamps,
    )
    import frontmatter

    vault = _vault(tmp_path)
    _write_note(
        vault, "note/80000Hours Marketing Email 2026-05-27.md",
        "type: note\nname: 80000Hours marketing\npriority: low\n",
        "- **From:** team@80000hours.org\n- **Subject:** This week\n\nBody.\n",
    )

    granted = grant_markers(vault, apply=True, limit=0)
    assert granted.paths == ["note/80000Hours Marketing Email 2026-05-27.md"]

    neutralize_stray_stamps(vault, apply=True, limit=0)
    fm = frontmatter.load(
        str(vault / "note/80000Hours Marketing Email 2026-05-27.md"),
    ).metadata
    assert note_is_email_derived(fm) is True
    assert fm["priority"] == "low", "neutralize ate a genuine bulleted-era email"


def test_bulleted_note_is_sampleable_after_the_grant(tmp_path: Path) -> None:
    """End-to-end: grant → the record re-enters the calibration pool. The point
    of the grant is the pool, not the field."""
    from alfred.scripts.mail_provenance_cleanup import grant_markers

    vault = _vault(tmp_path)
    _write_note(
        vault, "note/Bulleted era.md",
        "type: note\nname: Bulleted\npriority: low\n",
        "- **From:** team@80000hours.org\n",
    )
    assert _read_candidate(vault, "note/Bulleted era.md") is None  # pre-grant
    grant_markers(vault, apply=True, limit=0)
    assert _read_candidate(vault, "note/Bulleted era.md") is not None


def test_the_screenshot_note_is_still_rejected_after_widening(
    tmp_path: Path,
) -> None:
    """The widening must not have loosened toward the word-list. The #40 record
    has no header line in any serialization — only the words "email" and
    "inbox" in prose — and must still fail."""
    assert has_structural_email_headers(SCREENSHOT_BODY) is False

    from alfred.scripts.mail_provenance_cleanup import grant_markers

    vault = _vault(tmp_path)
    _write_note(vault, SCREENSHOT_NOTE, "type: note\nname: Bug\npriority: low\n",
                SCREENSHOT_BODY)
    assert grant_markers(vault, apply=True, limit=0).paths == []


def test_a_mid_line_bold_from_is_not_a_header(tmp_path: Path) -> None:
    """The anchor still requires line-start. Prose quoting ``**From:**`` inside
    a sentence is not a header, and widening for bullets must not have turned
    the anchored match into a floating one."""
    assert has_structural_email_headers(
        "The note said **From:** was missing from the digest.",
    ) is False


def test_probe_buckets_the_known_eras_and_is_read_only(tmp_path: Path) -> None:
    """The probe's job is to report SHAPE, not to judge mail-ness — the latter
    is what got guessed wrong. Pinned on bucket assignment plus the read-only
    guarantee, since it runs against the production vault."""
    from alfred.scripts.mail_header_shapes import classify_shape

    assert classify_shape("**From:** a@b.com\n") == "plain"
    assert classify_shape("- **From:** team@80000hours.org\n") == "bulleted"
    assert classify_shape("From: a@b.com\nSent: Monday\n") == "labelled_no_markup"
    assert classify_shape("Ping ben@example.com about it.") == "address_somewhere"
    assert classify_shape(SCREENSHOT_BODY) == "wordlist_only"
    assert classify_shape("Just a plain note.") == "no_signal"


def test_probe_writes_nothing(tmp_path: Path) -> None:
    """It runs on the live vault; the read-only claim is load-bearing."""
    from alfred.scripts.mail_header_shapes import main

    vault = _vault(tmp_path)
    p = _write_note(vault, "note/A.md", "type: note\nname: A\npriority: low\n",
                    "- **From:** a@b.com\n")
    before = p.read_bytes()
    assert main([str(vault)]) == 0
    assert p.read_bytes() == before

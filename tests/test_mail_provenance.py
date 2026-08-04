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


def test_the_structural_predicate_is_a_strict_subset_of_the_backfill_heuristic() -> None:
    """Drift guard. If someone widens the retroactive predicate back toward the
    permissive list, this fails — the whole point is that it is NARROWER.

    Asserted against the real module rather than a copied list, so the two
    cannot drift apart silently.
    """
    from alfred.email_classifier import backfill
    from alfred.curator import mail_provenance

    permissive = {p.pattern for p in backfill._EMAIL_BODY_MARKERS}
    structural = {p.pattern for p in mail_provenance._STRUCTURAL_HEADER_MARKERS}
    assert structural < permissive, (
        "the retroactive provenance predicate must stay a STRICT subset of the "
        "backfill's admission heuristic — it exists because that heuristic is "
        "too permissive to decide provenance"
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

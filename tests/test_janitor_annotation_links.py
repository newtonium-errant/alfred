"""Annotation prose is not a link (#49) — closing the self-feeding LINK001 loop.

WHY THIS EXISTS. The janitor writes ``janitor_note: LINK001 — broken wikilink
[[person/Ghost]]`` to explain a break. The scanner extracted wikilinks from the
whole raw file — frontmatter values included — so the NEXT sweep found
``person/Ghost`` inside that very note and reported a SECOND LINK001 on the same
record for the same underlying break. A closed loop: the janitor reporting its
own commentary back to itself, forever.

Measured on the 2026-06-25 vault snapshot: 586 records carry a note quoting a
wikilink (855 quoted occurrences), and excluding them drops LINK001 from 2,090
to 1,663 — 427 of the headline figure was self-inflicted.

THE GUARDRAIL THIS SUITE ENFORCES — exclusion must stay NARROW. Relationship
fields (``related``, ``source_a``, ``process``, …) carry wikilinks too, and a
broken one THERE is a true finding. A fix that suppressed those would trade a
double-count for a blind spot, which is strictly worse: the count would look
better and the vault would be less checked. Every subtraction pin below is
therefore paired with a still-detected pin.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from textwrap import dedent

from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.issues import IssueCode
from alfred.janitor.parser import (
    ANNOTATION_FIELDS,
    annotation_wikilinks,
    structural_wikilinks,
)
from alfred.janitor.scanner import run_structural_scan
from alfred.janitor.state import JanitorState


# ---------------------------------------------------------------------------
# The field list is a contract — widening it is a deliberate act
# ---------------------------------------------------------------------------


def test_annotation_fields_contains_janitor_note() -> None:
    assert "janitor_note" in ANNOTATION_FIELDS


def test_relationship_fields_are_NOT_annotations() -> None:
    """The narrowness guard, pinned as a set.

    These fields hold real references. Adding any of them here would silently
    stop checking a whole class of genuine broken links — a blind spot wearing
    a fixed bug's clothes.
    """
    for field in (
        "related", "source_a", "source_b", "process", "org", "project",
        "assigned", "provider", "source_session",
    ):
        assert field not in ANNOTATION_FIELDS, (
            f"{field!r} carries REAL references — excluding it would hide "
            "genuine broken links, not phantom ones"
        )


# ---------------------------------------------------------------------------
# structural_wikilinks — multiset subtraction, not a set difference
# ---------------------------------------------------------------------------


def test_a_link_in_both_body_and_note_keeps_exactly_one() -> None:
    """THE correctness case. A set difference would delete BOTH and hide a real
    broken link; counting both is the self-feeding loop. Removing exactly as
    many as the annotation contributed leaves precisely the real reference."""
    raw = (
        "---\njanitor_note: LINK001 — broken wikilink [[person/Ghost]]\n---\n\n"
        "See [[person/Ghost]] here.\n"
    )
    fm = {"janitor_note": "LINK001 — broken wikilink [[person/Ghost]]"}
    assert structural_wikilinks(raw, fm) == ["person/Ghost"]


def test_a_note_only_quote_leaves_nothing() -> None:
    """The link was already repaired out of the body; only the note remembers
    it. Nothing is referenced, so nothing should be reported."""
    raw = "---\njanitor_note: LINK001 — was [[person/Ghost]]\n---\n\nClean now.\n"
    fm = {"janitor_note": "LINK001 — was [[person/Ghost]]"}
    assert structural_wikilinks(raw, fm) == []


def test_two_body_links_and_one_quote_keeps_one() -> None:
    """Multiset arithmetic, explicitly: 2 real − 1 quoted = 1 kept."""
    raw = (
        "---\njanitor_note: LINK001 — [[person/Ghost]]\n---\n\n"
        "[[person/Ghost]] and again [[person/Ghost]].\n"
    )
    fm = {"janitor_note": "LINK001 — [[person/Ghost]]"}
    assert structural_wikilinks(raw, fm) == ["person/Ghost", "person/Ghost"]


def test_relationship_field_links_survive_untouched() -> None:
    raw = (
        "---\nrelated:\n- '[[person/Real]]'\n"
        "janitor_note: LINK001 — [[person/Ghost]]\n---\n\nBody.\n"
    )
    fm = {"related": ["[[person/Real]]"],
          "janitor_note": "LINK001 — [[person/Ghost]]"}
    assert structural_wikilinks(raw, fm) == ["person/Real"]


def test_yaml_doubled_apostrophe_is_still_subtracted() -> None:
    """The failure mode the shared normalization exists for.

    In raw text a single-quoted YAML scalar doubles the apostrophe, so the
    extraction sees ``Andrew''s`` while the PARSED note value sees ``Andrew's``.
    Without decoding both sides the subtraction misses — silently, and in the
    over-reporting direction. 14 of the live vault's 855 quoted note links carry
    an apostrophe, so this is a real slice, not a hypothetical.
    """
    raw = (
        "---\njanitor_note: 'LINK001 — broken [[person/Andrew''s Note]]'\n"
        "---\n\nBody.\n"
    )
    fm = {"janitor_note": "LINK001 — broken [[person/Andrew's Note]]"}
    assert structural_wikilinks(raw, fm) == []


def test_annotation_wikilinks_reads_only_annotation_fields() -> None:
    fm = {
        "janitor_note": "LINK001 — [[person/Ghost]]",
        "related": ["[[person/Real]]"],
    }
    assert annotation_wikilinks(fm) == ["person/Ghost"]


def test_non_string_annotation_value_is_ignored() -> None:
    """A malformed record must not crash the scan."""
    assert annotation_wikilinks({"janitor_note": ["not", "a", "string"]}) == []
    assert annotation_wikilinks({"janitor_note": None}) == []


# ---------------------------------------------------------------------------
# End to end through run_structural_scan — the loop actually closes
# ---------------------------------------------------------------------------


def _scan(tmp_path: Path, rel: str, content: str, *, extra: dict | None = None):
    vault = tmp_path / "vault"
    for name, body in (extra or {}).items():
        p = vault / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    cfg = JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=[".obsidian", "_templates", "_bases"],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "s.json")),
    )
    return run_structural_scan(cfg, JanitorState(cfg.state.path, 20))


_RECORD = dedent(
    """\
    ---
    type: note
    name: A
    status: active
    created: 2026-01-01
    tags: []
    {note}---

    See [[person/Ghost]] here.
    """
)


def test_cohort_note_does_not_double_the_link001(tmp_path: Path) -> None:
    """THE PIN. One real broken link plus a note explaining it is ONE issue.

    Before this fix the same record reported the same break twice — which is
    how a headline of 2,090 contained 427 phantoms, and how a fixture asserting
    "cohort counts 1" saw 2.
    """
    issues = _scan(
        tmp_path, "note/A.md",
        _RECORD.format(
            note="janitor_note: LINK001 — broken wikilink [[person/Ghost]]\n",
        ),
    )
    link001 = [i for i in issues if i.code is IssueCode.BROKEN_WIKILINK]
    assert len(link001) == 1, (
        f"the note re-reported the body's break: {[i.message for i in link001]}"
    )
    assert "person/Ghost" in link001[0].message


def test_the_underlying_break_is_still_detected(tmp_path: Path) -> None:
    """PRESERVED DETECTION, paired with the pin above. A build that fixed the
    double-count by dropping LINK001 entirely passes that test and fails this
    one."""
    issues = _scan(tmp_path, "note/A.md", _RECORD.format(note=""))
    assert [i for i in issues if i.code is IssueCode.BROKEN_WIKILINK]


def test_a_broken_relationship_link_still_reports(tmp_path: Path) -> None:
    """The narrowness guard, end to end: a broken link in a REAL reference
    field is still a finding even on a record that also carries a note."""
    content = dedent(
        """\
        ---
        type: note
        name: A
        status: active
        created: 2026-01-01
        tags: []
        related:
        - '[[person/AlsoGone]]'
        janitor_note: LINK001 — broken wikilink [[person/Ghost]]
        ---

        Body with no links.
        """
    )
    issues = _scan(tmp_path, "note/A.md", content)
    link001 = [i for i in issues if i.code is IssueCode.BROKEN_WIKILINK]
    assert len(link001) == 1
    assert "person/AlsoGone" in link001[0].message, (
        "the relationship field's broken link must survive the exclusion"
    )


def test_annotation_mention_does_not_suppress_orphan001(tmp_path: Path) -> None:
    """A janitor_note quoting a record is commentary, not an inbound link.

    Counting it would let the janitor's own prose make a record look connected
    and silence its ORPHAN001.
    """
    citing = dedent(
        """\
        ---
        type: note
        name: Citer
        status: active
        created: 2026-01-01
        tags: []
        janitor_note: LINK001 — mentions [[person/Lonely]]
        ---

        No links in the body.
        """
    )
    lonely = dedent(
        """\
        ---
        type: person
        name: Lonely
        status: active
        created: 2026-01-01
        tags: []
        ---

        Nobody links to me.
        """
    )
    issues = _scan(tmp_path, "person/Lonely.md", lonely,
                   extra={"note/Citer.md": citing})
    orphans = [
        i for i in issues
        if i.code is IssueCode.ORPHANED_RECORD and i.file == "person/Lonely.md"
    ]
    assert orphans, "an annotation mention must not count as an inbound link"


def test_annotation_quote_does_not_suppress_link002(tmp_path: Path) -> None:
    """A target merely QUOTED in a note is not a frontmatter reference.

    LINK002 exists because Obsidian Bases' ``file.hasLink`` only reads
    FRONTMATTER links, so a body-only entity link is invisible in base views.
    A sentence about the record does not make it visible — counting the note as
    frontmatter suppressed a real, autofix-repairable finding.

    Measured on the 2026-06-25 snapshot: 84 records were masked exactly this
    way (LINK002 84 → 168 once annotations stop counting).
    """
    target = dedent(
        """\
        ---
        type: person
        name: Real
        status: active
        created: 2026-01-01
        tags: []
        ---

        I exist.
        """
    )
    citing = dedent(
        """\
        ---
        type: note
        name: Citer
        status: active
        created: 2026-01-01
        tags: []
        janitor_note: SEM005 — see [[person/Real]] for context
        ---

        Body mentions [[person/Real]] but no frontmatter field links it.
        """
    )
    issues = _scan(tmp_path, "note/Citer.md", citing,
                   extra={"person/Real.md": target})
    link002 = [
        i for i in issues
        if i.code is IssueCode.UNLINKED_BODY_ENTITY and i.file == "note/Citer.md"
    ]
    assert link002, (
        "the body entity link is genuinely absent from every relationship "
        "field — a janitor_note quoting it must not count as frontmatter"
    )

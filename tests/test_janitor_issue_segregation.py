"""Janitor issue segregation — actionable vs not-janitor-fixable.

WHY THIS EXISTS. The brief reported "2144 open issues (1723 critical)"
for weeks. That number conflates two populations: work janitor is failing
to do, and work janitor structurally CANNOT do (DIR001 needs a move that
janitor scope denies; STUB001 belongs to the ``janitor_enrich`` scope;
ORPHAN001/SEM001-004 are flag-only in ``autofix._apply_fix``). Reported
as one figure it is unreadable as a health signal.

THE GUARDRAIL THIS SUITE ENFORCES — segregation is NOT suppression.
An earlier draft of this work proposed making the scanner stop
re-reporting already-flagged issues so the count would fall. That is
masking: a scope-blocked DIR001 is a REAL, correctly-detected, open
issue, and hiding it would have bought a healthier-looking number by
concealing findings. The split changes PRESENTATION only.

Every FP/segregation pin below is therefore paired with a
PRESERVED-DETECTION pin: the issue must still be detected, still be
carried in ``SweepResult.issues``, and still be counted in
``issues_found``. A test that only asserts "the split happened" would
pass just as happily against a build that dropped the issues entirely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

import pytest
import structlog

from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.issues import (
    ACTIONABLE_CODES,
    AUTOFIX_FIXABLE_CODES,
    LABEL_ONLY_CODES,
    NOT_JANITOR_FIXABLE_CODES,
    Issue,
    IssueCode,
    Severity,
    SweepResult,
    classify_counts,
    is_spam_cohort_issue,
)
from alfred.janitor.state import JanitorState


# --------------------------------------------------------------------
# Taxonomy structure — catches an unclassified new IssueCode
# --------------------------------------------------------------------

def test_remediation_classes_partition_every_issue_code() -> None:
    """Every IssueCode belongs to exactly one remediation class.

    Structural pin: adding a new IssueCode without classifying it fails
    HERE rather than silently landing in neither bucket (which would
    make it vanish from both reported counts).

    Three classes since #47, not two: LABEL_ONLY_CODES holds the codes with no
    producer at all (SEM005/SEM006 — the agent's own vocabulary). They are
    kept in the taxonomy rather than deleted precisely so this pin keeps
    covering them.
    """
    covered = ACTIONABLE_CODES | NOT_JANITOR_FIXABLE_CODES | LABEL_ONLY_CODES
    missing = set(IssueCode) - covered
    assert not missing, (
        f"unclassified IssueCode(s): {sorted(c.value for c in missing)} — add "
        "each to ACTIONABLE_CODES, NOT_JANITOR_FIXABLE_CODES or "
        "LABEL_ONLY_CODES"
    )


def test_remediation_classes_are_disjoint() -> None:
    """No code may belong to two remediation classes."""
    assert not ACTIONABLE_CODES & NOT_JANITOR_FIXABLE_CODES
    assert not ACTIONABLE_CODES & LABEL_ONLY_CODES
    assert not NOT_JANITOR_FIXABLE_CODES & LABEL_ONLY_CODES


def test_label_only_codes_get_their_own_conservation_term() -> None:
    """#47: a label-only code counts in ``label_only``, its own term.

    Deliberately NOT folded into ``not_janitor_fixable``. Both choices keep
    conservation true, but only this one keeps it MEANINGFUL: if a producer is
    ever wired for SEM005, the issues surface in a bucket that says what they
    are, rather than being absorbed into a bucket that means "scope-blocked"
    and quietly changing what that number reports.

    It cannot occur today (no producer), which is exactly why it needs a pin —
    an invariant that only holds because the input never arrives is one commit
    away from being false.
    """
    split = classify_counts([_issue(IssueCode.VAGUE_NOTE)])
    assert split == {
        "actionable": 0, "not_janitor_fixable": 0, "label_only": 1,
        "spam_cohort": 0, "total": 1,
    }


def test_sem005_is_no_longer_claimed_as_agent_work() -> None:
    """The demotion itself. SEM005/SEM006 out of the actionable bucket.

    Counting them as actionable asserted the agent gets routed them; nothing
    produces them, so that was never true.
    """
    assert IssueCode.VAGUE_NOTE not in ACTIONABLE_CODES
    assert IssueCode.DUPLICATE_SEMANTIC not in ACTIONABLE_CODES


def test_autofix_fixable_is_a_subset_of_actionable() -> None:
    """Autofix-repairable codes are by definition actionable."""
    assert AUTOFIX_FIXABLE_CODES <= ACTIONABLE_CODES


def test_scope_blocked_codes_are_classified_not_fixable() -> None:
    """The specific codes with a verified structural block.

    DIR001 — janitor scope sets ``move: False``.
    STUB001 — enrichment is the separate ``janitor_enrich`` scope.
    """
    assert IssueCode.WRONG_DIRECTORY in NOT_JANITOR_FIXABLE_CODES
    assert IssueCode.STUB_RECORD in NOT_JANITOR_FIXABLE_CODES


def test_broken_wikilink_stays_actionable() -> None:
    """LINK001 is agent-actionable and must NOT drift into the
    not-fixable bucket — it is 80% of the live count, and parking it
    there would hide the vault's largest real backlog."""
    assert IssueCode.BROKEN_WIKILINK in ACTIONABLE_CODES
    assert IssueCode.BROKEN_WIKILINK not in NOT_JANITOR_FIXABLE_CODES


# --------------------------------------------------------------------
# classify_counts — arithmetic conservation
# --------------------------------------------------------------------

def _issue(code: IssueCode, rel: str = "note/X.md") -> Issue:
    return Issue(code=code, severity=Severity.WARNING, file=rel, message="m")


def test_classify_counts_splits_and_conserves() -> None:
    issues = [
        _issue(IssueCode.BROKEN_WIKILINK),      # actionable
        _issue(IssueCode.MISSING_REQUIRED_FIELD),  # actionable (autofix)
        _issue(IssueCode.WRONG_DIRECTORY),      # not fixable
        _issue(IssueCode.STUB_RECORD),          # not fixable
        _issue(IssueCode.ORPHANED_RECORD),      # not fixable
    ]
    split = classify_counts(issues)
    # #19-B adds spam_cohort — a SUBSET of actionable, not a conservation
    # term. 0 here: no cohort_notes supplied, which is the honest default
    # (a caller that has not wired the notes must get 0, not a guess).
    assert split == {
        "actionable": 2, "not_janitor_fixable": 3, "label_only": 0,
        "spam_cohort": 0, "total": 5,
    }


def test_classify_counts_conserves_across_every_code() -> None:
    """Three-way conservation, for one issue of EVERY code.

    ``actionable + not_janitor_fixable + label_only == total`` — one term per
    remediation class. Conservation is the anti-masking invariant: if a future
    edit lets an issue fall out of all three buckets, the split would
    under-report and this fails. The pin was NOT weakened to skip the demoted
    codes when #47 landed; the sum grew a term instead, which is what keeps it
    an honest statement about the whole enum.
    """
    issues = [_issue(code) for code in IssueCode]
    split = classify_counts(issues)
    assert split["total"] == len(list(IssueCode))
    assert (
        split["actionable"]
        + split["not_janitor_fixable"]
        + split["label_only"]
        == split["total"]
    )
    # And the demoted codes are exactly what lands in the third term.
    assert split["label_only"] == len(LABEL_ONLY_CODES)


def test_classify_counts_empty_is_zeroed() -> None:
    assert classify_counts([]) == {
        "actionable": 0, "not_janitor_fixable": 0, "label_only": 0,
        "spam_cohort": 0, "total": 0,
    }


# --------------------------------------------------------------------
# SweepResult serialization — schema tolerance
# --------------------------------------------------------------------

def test_sweep_result_round_trips_split_counts() -> None:
    r = SweepResult(sweep_id="abc", issues_actionable=7,
                    issues_not_janitor_fixable=11)
    back = SweepResult.from_dict(r.to_dict())
    assert back.issues_actionable == 7
    assert back.issues_not_janitor_fixable == 11


def test_sweep_result_loads_legacy_state_without_split_fields() -> None:
    """A state file written before segregation must still load.

    Per the load-time schema-tolerance contract — a rollback or an older
    state file cannot be allowed to crash the loader.
    """
    legacy = {"sweep_id": "old", "issues_found": 5}
    back = SweepResult.from_dict(legacy)
    assert back.issues_actionable == 0
    assert back.issues_not_janitor_fixable == 0
    assert back.issues_found == 5


# --------------------------------------------------------------------
# End-to-end through run_sweep — PRESERVED DETECTION is the point
# --------------------------------------------------------------------

def _build_config(vault: Path, state_dir: Path) -> JanitorConfig:
    return JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=[".obsidian", "_templates", "_bases"],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "janitor_state.json")),
    )


def _write_record(vault: Path, rel: str, frontmatter: str, body: str = "") -> None:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def _sweep_with_misplaced_record(tmp_path: Path):
    """A vault whose record is a DIR001 (type note living in project/)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_record(
        vault, "project/Misplaced.md",
        dedent(
            """\
            type: note
            name: Misplaced
            status: active
            created: 2026-01-01
            tags: []
            """
        ).rstrip(),
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _build_config(vault, state_dir)
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    skills_dir = tmp_path / "skills"
    (skills_dir / "vault-janitor").mkdir(parents=True)
    (skills_dir / "vault-janitor" / "SKILL.md").write_text("# t\n", encoding="utf-8")
    return config, state, skills_dir


def test_scope_blocked_issue_is_still_detected_and_reported(tmp_path: Path) -> None:
    """PRESERVED DETECTION. The load-bearing anti-masking pin.

    A DIR001 is classified not-janitor-fixable, and it MUST still appear
    in ``result.issues`` and in ``issues_found``. This test is what
    distinguishes segregation from suppression — a build that "fixed" the
    count by dropping these issues passes every split-arithmetic test
    above and fails this one.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir = _sweep_with_misplaced_record(tmp_path)
    result = asyncio.run(
        daemon_mod.run_sweep(config, state, skills_dir, structural_only=True)
    )

    dir_issues = [i for i in result.issues if i.code == IssueCode.WRONG_DIRECTORY]
    assert dir_issues, "DIR001 must still be DETECTED, not masked"
    assert result.issues_found >= len(dir_issues), (
        "issues_found must still COUNT the scope-blocked issue"
    )
    # And it is on the not-fixable side of the split, not silently dropped.
    assert result.issues_not_janitor_fixable >= 1


def test_split_counts_conserve_through_run_sweep(tmp_path: Path) -> None:
    """actionable + not_fixable == issues_found on a real sweep."""
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir = _sweep_with_misplaced_record(tmp_path)
    result = asyncio.run(
        daemon_mod.run_sweep(config, state, skills_dir, structural_only=True)
    )
    assert (
        result.issues_actionable + result.issues_not_janitor_fixable
        == result.issues_found
    ), "the split must account for every detected issue"


def test_sweep_emits_issue_split_signal(tmp_path: Path) -> None:
    """ILB: the split is stated every sweep, with its fields.

    Pins the log event AND the field names — a refactor that drops the
    signal (or renames a field an operator greps) fails here rather than
    silently degrading the morning Health read.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir = _sweep_with_misplaced_record(tmp_path)
    with structlog.testing.capture_logs() as captured:
        result = asyncio.run(
            daemon_mod.run_sweep(config, state, skills_dir, structural_only=True)
        )

    matches = [c for c in captured if c.get("event") == "sweep.issue_split"]
    assert len(matches) == 1, f"expected 1 sweep.issue_split, got {len(matches)}"
    ev = matches[0]
    assert ev["total"] == result.issues_found
    assert ev["actionable"] == result.issues_actionable
    assert ev["not_janitor_fixable"] == result.issues_not_janitor_fixable
    assert ev["total"] == ev["actionable"] + ev["not_janitor_fixable"]


# --------------------------------------------------------------------
# #19-B — the spam cohort (D2 ruled)
# --------------------------------------------------------------------

#: Verbatim shapes taken from the vault, not invented for the test. Measured
#: over 8,614 records / 1,674 janitor_notes: 1,407 start with ``LINK001``, of
#: which 1,372 continue with an EM-dash and 35 with no dash at all. Both forms
#: are fixtures here because an earlier draft keyed on ``"LINK001 --"`` (two
#: hyphens, a shape that occurs ZERO times) and would have shipped a permanent
#: "cohort: 0" — green against every hand-written fixture that used the same
#: wrong string.
REAL_COHORT_NOTE = "LINK001 — scanner false positive, _bases/contradiction.base exists"
REAL_COHORT_NOTE_NO_DASH = "LINK001 scanner false positive: _bases/person.base"
#: 539 of the 1,407 real cohort notes quote a wikilink inside the note prose.
#: Kept as a predicate fixture but deliberately NOT used in the sweep fixtures
#: below: the scanner reads wikilinks out of frontmatter VALUES, so a note that
#: mentions ``[[x]]`` adds its own LINK001 to the record it annotates. That is
#: a real effect on the live vault, not a test artifact — it just makes a
#: two-record fixture count three broken links and read as a cohort bug.
REAL_COHORT_NOTE_WITH_LINK = (
    "LINK001 — broken links to _docs/* (6 files). [[person/Your Name]]"
)


def test_cohort_matches_the_note_shapes_the_agent_actually_writes() -> None:
    """The predicate is pinned against REAL note text, both observed forms.

    This is the pin that would have caught the original bug. The cohort marker
    is prose written by the agent under the SKILL's LINK001 procedure, so its
    punctuation varies; only the leading issue code is stable — which is also
    the exact thing the #30 SKILL overwrite-guard keys on ("unless that note
    starts with LINK001"). Keying on anything more than the code couples this
    count to the agent's prose style.
    """
    link = _issue(IssueCode.BROKEN_WIKILINK, rel="note/A.md")
    assert is_spam_cohort_issue(link, REAL_COHORT_NOTE) is True
    assert is_spam_cohort_issue(link, REAL_COHORT_NOTE_NO_DASH) is True
    assert is_spam_cohort_issue(link, REAL_COHORT_NOTE_WITH_LINK) is True
    # A different code's note is not a LINK001 cohort marker.
    assert is_spam_cohort_issue(link, "ORPHAN001 — no inbound links") is False
    # Leading whitespace from a YAML block scalar must not defeat it.
    assert is_spam_cohort_issue(link, "  " + REAL_COHORT_NOTE) is True


def test_the_em_dash_is_the_literal_production_character() -> None:
    """Guards the fixture itself, not the code.

    The whole class of bug this feature nearly shipped was a fixture written in
    a dialect no writer produces. A later edit that "tidied" U+2014 to a hyphen
    would leave every other pin green while silently removing the only fixture
    that represents 1,372 of the 1,407 real markers.
    """
    assert "—" in REAL_COHORT_NOTE
    assert "—" in REAL_COHORT_NOTE_WITH_LINK
    assert "--" not in REAL_COHORT_NOTE


def test_prefix_match_respects_a_word_boundary() -> None:
    """A future ``LINK0011``-shaped code must not fall into this cohort.

    Hardening, not a bug fix: no current IssueCode collides (``LINK002``
    differs at the last character). But a cohort that silently grows because
    someone added an enum member is the same failure mode as a cohort that
    silently reads 0 — the count stops meaning what its name says, and nothing
    goes red. Credit: builder-b2 caught this in handover review.
    """
    link = _issue(IssueCode.BROKEN_WIKILINK, rel="note/A.md")
    assert is_spam_cohort_issue(link, "LINK0011 — some future code") is False
    assert is_spam_cohort_issue(link, "LINK001X — not our code") is False
    assert is_spam_cohort_issue(link, "LINK001_alt — not our code") is False
    # The boundary characters that DO mean "this is a LINK001 note".
    assert is_spam_cohort_issue(link, "LINK001 — with a space") is True
    assert is_spam_cohort_issue(link, "LINK001: colon form") is True
    assert is_spam_cohort_issue(link, "LINK001") is True, (
        "a note that is exactly the bare code is still a marker"
    )


def test_cohort_is_a_subset_of_actionable_not_a_carve_out() -> None:
    """The cohort is REPORTED alongside actionable, not removed from it.

    Deliberate: those links are genuinely fixable — the #44 link001_repair
    campaign drains them — so subtracting them from ``actionable`` would
    understate what the system can still do. They are broken out because they
    are known debt on a drain schedule, not because they are unfixable.
    """
    issues = [
        _issue(IssueCode.BROKEN_WIKILINK, rel="note/A.md"),   # cohort
        _issue(IssueCode.BROKEN_WIKILINK, rel="note/B.md"),   # fresh
        _issue(IssueCode.WRONG_DIRECTORY, rel="note/C.md"),   # not fixable
    ]
    split = classify_counts(
        issues, cohort_notes={"note/A.md": REAL_COHORT_NOTE},
    )
    assert split["spam_cohort"] == 1
    assert split["actionable"] == 2, "cohort stays counted in actionable"
    assert split["actionable"] + split["not_janitor_fixable"] == split["total"]


def test_absent_cohort_notes_yield_zero_not_a_guess() -> None:
    """A caller that hasn't wired the notes gets the pre-#19-B behaviour.
    Inferring a cohort from the issue alone would be a fabricated number on the
    operator's headline line."""
    issues = [_issue(IssueCode.BROKEN_WIKILINK, rel="note/A.md")]
    assert classify_counts(issues)["spam_cohort"] == 0


def test_only_LINK001_can_be_cohort() -> None:
    """A record carrying the marker does not drag its OTHER issues into the
    cohort. The cohort is a LINK001 backlog; admitting other codes would
    quietly shrink the actionable bucket for reasons nobody asked for."""
    assert is_spam_cohort_issue(
        _issue(IssueCode.BROKEN_WIKILINK, rel="note/A.md"), REAL_COHORT_NOTE,
    ) is True
    assert is_spam_cohort_issue(
        _issue(IssueCode.WRONG_DIRECTORY, rel="note/A.md"), REAL_COHORT_NOTE,
    ) is False


def test_a_fresh_link001_is_not_cohort() -> None:
    """The whole point: a NEW break on a record with no marker must stay
    visible as actionable-today rather than being absorbed into the backlog."""
    fresh = _issue(IssueCode.BROKEN_WIKILINK, rel="note/New.md")
    assert is_spam_cohort_issue(fresh, None) is False
    assert is_spam_cohort_issue(fresh, "") is False
    assert is_spam_cohort_issue(fresh, "STUB001 — something else") is False


def _sweep_with_one_marked_and_one_fresh_break(tmp_path: Path):
    """A vault with two dangling links: one already triaged, one brand new.

    Both point at the same nonexistent target, so the ONLY difference between
    them is the janitor_note. That is what makes the fixture discriminating —
    a build that counted every LINK001 as cohort, or none of them, gives the
    same wrong answer on a vault where the two populations differ in any other
    way.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_record(
        vault, "note/Triaged.md",
        dedent(
            f"""\
            type: note
            name: Triaged
            status: active
            created: 2026-01-01
            tags: []
            janitor_note: "{REAL_COHORT_NOTE}"
            """
        ).rstrip(),
        body="Refers to [[note/Ghost]].",
    )
    _write_record(
        vault, "note/Fresh.md",
        dedent(
            """\
            type: note
            name: Fresh
            status: active
            created: 2026-01-01
            tags: []
            """
        ).rstrip(),
        body="Refers to [[note/Ghost]].",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _build_config(vault, state_dir)
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    skills_dir = tmp_path / "skills"
    (skills_dir / "vault-janitor").mkdir(parents=True)
    (skills_dir / "vault-janitor" / "SKILL.md").write_text("# t\n", encoding="utf-8")
    return config, state, skills_dir


def test_cohort_count_is_wired_through_run_sweep(tmp_path: Path) -> None:
    """END-TO-END through the production entry point — the load-bearing pin.

    ``classify_counts(cohort_notes=...)`` defaults to ``None``, which means
    every unit test above passes just as happily against a build where
    ``run_sweep`` never reads a single janitor_note and the operator's cohort
    line is a permanent 0. Only a sweep driven from a real vault proves the
    notes are actually read and threaded. (Same failure shape as the R3 snooze
    gate: write side live, read side dead, all per-layer pins green.)
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir = _sweep_with_one_marked_and_one_fresh_break(tmp_path)
    result = asyncio.run(
        daemon_mod.run_sweep(config, state, skills_dir, structural_only=True)
    )

    broken = [i for i in result.issues if i.code == IssueCode.BROKEN_WIKILINK]
    assert len(broken) == 2, f"fixture must produce 2 LINK001, got {len(broken)}"
    assert result.issues_spam_cohort == 1, (
        "exactly the triaged record is cohort — a 0 here means run_sweep never "
        "read the notes; a 2 means the marker is being ignored"
    )
    # Still a subset of actionable, and conservation is untouched.
    assert result.issues_spam_cohort <= result.issues_actionable
    assert (
        result.issues_actionable + result.issues_not_janitor_fixable
        == result.issues_found
    )


def test_sweep_signal_carries_the_cohort_field(tmp_path: Path) -> None:
    """ILB: the cohort is stated in the same breath as the rest of the split.

    Pins the field NAME as well as its presence — the operator greps this line,
    and a rename that only tests-by-value would sail through.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir = _sweep_with_one_marked_and_one_fresh_break(tmp_path)
    with structlog.testing.capture_logs() as captured:
        result = asyncio.run(
            daemon_mod.run_sweep(config, state, skills_dir, structural_only=True)
        )

    matches = [c for c in captured if c.get("event") == "sweep.issue_split"]
    assert len(matches) == 1
    assert matches[0]["spam_cohort"] == result.issues_spam_cohort == 1


def test_unreadable_record_degrades_to_not_cohort(tmp_path: Path) -> None:
    """A record whose frontmatter will not parse is NOT counted as cohort.

    The safe direction: a mis-read that inflated the cohort would shrink the
    actionable bucket the operator is meant to act on, which is the exact
    failure this feature exists to prevent. Degrading the other way only
    over-reports work.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir = _sweep_with_one_marked_and_one_fresh_break(tmp_path)
    # Corrupt the triaged record's frontmatter, keeping the dangling link.
    (config.vault.vault_path / "note" / "Triaged.md").write_text(
        "---\ntype: note\nname: Triaged\n  bad: [unclosed\n---\n[[note/Ghost]]\n",
        encoding="utf-8",
    )
    result = asyncio.run(
        daemon_mod.run_sweep(config, state, skills_dir, structural_only=True)
    )
    assert result.issues_spam_cohort == 0

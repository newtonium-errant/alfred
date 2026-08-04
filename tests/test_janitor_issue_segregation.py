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
    NOT_JANITOR_FIXABLE_CODES,
    Issue,
    IssueCode,
    Severity,
    SweepResult,
    classify_counts,
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
    """
    covered = ACTIONABLE_CODES | NOT_JANITOR_FIXABLE_CODES
    missing = set(IssueCode) - covered
    assert not missing, (
        f"unclassified IssueCode(s): {sorted(c.value for c in missing)} — "
        "add each to ACTIONABLE_CODES or NOT_JANITOR_FIXABLE_CODES"
    )


def test_remediation_classes_are_disjoint() -> None:
    """No code may be both actionable and not-janitor-fixable."""
    overlap = ACTIONABLE_CODES & NOT_JANITOR_FIXABLE_CODES
    assert not overlap, sorted(c.value for c in overlap)


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
    assert split == {"actionable": 2, "not_janitor_fixable": 3, "total": 5}


def test_classify_counts_conserves_across_every_code() -> None:
    """actionable + not_fixable == total, for one issue of EVERY code.

    Conservation is the anti-masking invariant: if a future edit lets an
    issue fall out of both buckets, the split would under-report and this
    fails.
    """
    issues = [_issue(code) for code in IssueCode]
    split = classify_counts(issues)
    assert split["total"] == len(list(IssueCode))
    assert split["actionable"] + split["not_janitor_fixable"] == split["total"]


def test_classify_counts_empty_is_zeroed() -> None:
    assert classify_counts([]) == {
        "actionable": 0, "not_janitor_fixable": 0, "total": 0,
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

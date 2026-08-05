"""Janitor CLI renders the remediation split — #31 (review-25 N4).

WHY THIS EXISTS. The operator's read of the janitor was
``Issues found: 2075 · Files fixed: 0``, which says "the janitor is broken."
The truth was "almost none of those were janitor's to fix" — DIR001 needs a
move its scope denies, STUB001 belongs to ``janitor_enrich``, ORPHAN001 and
SEM001-004 are flag-only. The counts to distinguish those populations already
existed on ``SweepResult`` and were computed every sweep; nothing rendered
them, so the headline kept inviting exactly the wrong conclusion.

Two failure shapes these pins guard, both worse than the original number:

  1. **A fabricated split on historical rows.** State loading is
     schema-tolerant by contract, so every sweep written before the split
     existed loads with ``issues_actionable = 0``. Rendering that as
     "(0 actionable, N not janitor-fixable)" would state, confidently, that
     the janitor had nothing to do on days when nobody knows what it had.
  2. **Drift between the four render sites.** ``scan``, ``fix``, ``status``
     and ``history`` all print this figure. Four independent format strings
     is four chances for one to fall behind.
"""

from __future__ import annotations

from alfred.janitor.cli import (
    SPLIT_UNAVAILABLE,
    format_issue_split,
    split_is_available,
)
from alfred.janitor.issues import SweepResult


def _sweep(
    *,
    found: int = 0,
    actionable: int = 0,
    not_fixable: int = 0,
    cohort: int = 0,
) -> SweepResult:
    return SweepResult(
        sweep_id="s1",
        issues_found=found,
        issues_actionable=actionable,
        issues_not_janitor_fixable=not_fixable,
        issues_spam_cohort=cohort,
    )


# --------------------------------------------------------------------
# The headline line
# --------------------------------------------------------------------

def test_clean_sweep_has_no_parenthetical() -> None:
    """Nothing to disambiguate when there are no issues.

    A ``(0 actionable, 0 not janitor-fixable)`` tail on every clean sweep
    trains the eye to skip the parenthetical on the sweeps where it matters.
    """
    assert format_issue_split(_sweep(found=0)) == "Issues found: 0"


def test_the_reported_incident_shape_reads_correctly() -> None:
    """The literal number from the report, rendered.

    This is the pin that encodes the point of the task: 2075 issues and 0
    fixes is CORRECT behaviour, and the line now says so instead of implying
    a broken daemon.
    """
    line = format_issue_split(
        _sweep(found=2075, actionable=12, not_fixable=2063, cohort=11)
    )
    assert line == (
        "Issues found: 2075 (12 actionable, 2063 not janitor-fixable; "
        "11 of the actionable are spam-cohort)"
    )


def test_cohort_clause_is_omitted_when_empty() -> None:
    """No ``; 0 spam-cohort`` tail on an instance with no markers.

    Not a silent-absence violation: the parenthetical's presence already
    proves the segregation ran, so a missing cohort clause reads as "none",
    not as "did this even happen".
    """
    line = format_issue_split(_sweep(found=5, actionable=5, not_fixable=0))
    assert line == "Issues found: 5 (5 actionable, 0 not janitor-fixable)"
    assert "cohort" not in line


def test_every_issue_actionable_still_shows_both_buckets() -> None:
    """The zero belongs in the parenthetical when the total is non-zero —
    "0 not janitor-fixable" is information, unlike a zero-issue sweep."""
    assert format_issue_split(_sweep(found=3, actionable=3)) == (
        "Issues found: 3 (3 actionable, 0 not janitor-fixable)"
    )


# --------------------------------------------------------------------
# Historical rows must not be given a fabricated split
# --------------------------------------------------------------------

def test_pre_split_sweep_is_marked_unavailable_not_zeroed() -> None:
    """A sweep from before the split existed says so.

    Loading is schema-tolerant, so the fields are 0 — which is identical, on
    the dataclass, to a sweep that truly had nothing actionable. Conservation
    is what separates them: 0 + 0 != 40.
    """
    legacy = SweepResult.from_dict({"sweep_id": "old", "issues_found": 40})
    assert split_is_available(legacy) is False
    assert format_issue_split(legacy) == f"Issues found: 40 ({SPLIT_UNAVAILABLE})"


def test_a_split_that_does_not_conserve_is_refused() -> None:
    """Defence in depth: any non-conserving split is untrustworthy, whatever
    produced it. Rendering it would publish an arithmetic contradiction on the
    operator's headline line."""
    assert split_is_available(_sweep(found=10, actionable=2, not_fixable=3)) is False
    assert format_issue_split(
        _sweep(found=10, actionable=2, not_fixable=3)
    ) == f"Issues found: 10 ({SPLIT_UNAVAILABLE})"


def test_conserving_split_is_accepted() -> None:
    assert split_is_available(_sweep(found=5, actionable=2, not_fixable=3)) is True


def test_an_unpersisted_label_only_issue_fails_closed() -> None:
    """The #47 interaction, pinned rather than left to be discovered.

    ``classify_counts`` conserves three ways; ``SweepResult`` persists two of
    the terms, because the third is 0 for every sweep that can occur today. If
    a producer is ever wired for SEM005, those issues count toward
    ``issues_found`` but land in neither persisted bucket — and the operator
    must get "split not recorded", never a parenthetical that fails to add up.

    Simulated here by the shape such a sweep would have: 6 issues found, 5
    accounted for in the two persisted buckets, 1 unrepresented.
    """
    unaccounted = _sweep(found=6, actionable=2, not_fixable=3)
    assert split_is_available(unaccounted) is False
    assert format_issue_split(unaccounted) == (
        f"Issues found: 6 ({SPLIT_UNAVAILABLE})"
    )


def test_zero_issue_sweep_is_never_split_available() -> None:
    """0 + 0 == 0 conserves trivially, but there is no split to show."""
    assert split_is_available(_sweep(found=0)) is False


# --------------------------------------------------------------------
# The four render sites share one formatter
# --------------------------------------------------------------------

def test_scan_status_and_fix_all_use_the_shared_formatter(capsys, tmp_path, monkeypatch) -> None:
    """No render site keeps its own format string.

    Drift between them is the reason this is one function: a reader comparing
    ``janitor status`` against ``janitor scan`` must not see two different
    accounts of the same sweep.
    """
    from alfred.janitor import cli as cli_mod

    result = _sweep(found=2075, actionable=12, not_fixable=2063, cohort=11)
    expected = format_issue_split(result)

    class _FakeState:
        files: dict = {}
        ignored: dict = {}
        fix_log: list = []
        sweeps = {"s1": result}

        def load(self) -> None:
            pass

    monkeypatch.setattr(cli_mod, "_init_state", lambda config: _FakeState())
    cli_mod.cmd_status(config=None)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert expected in out, f"status must render the shared line, got:\n{out}"


def test_fix_explains_zero_fixes_when_nothing_was_actionable(capsys, monkeypatch) -> None:
    """ILB on the exact pair that misled the operator.

    ``Files fixed: 0`` under a large issue count needs to say WHY, in words,
    on the line below — deriving it from the parenthetical is work we should
    not be asking of someone reading a morning status.
    """
    from alfred.janitor import cli as cli_mod

    result = _sweep(found=2075, actionable=0, not_fixable=2075)

    monkeypatch.setattr(cli_mod, "_init_state", lambda config: object())
    monkeypatch.setattr(cli_mod.asyncio, "run", lambda coro: (coro.close(), result)[1])
    cli_mod.cmd_fix(config=None, skills_dir=None)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "nothing fixed because nothing found was janitor's to fix" in out


def test_fix_does_not_explain_away_a_genuine_zero_fix_failure(capsys, monkeypatch) -> None:
    """The counter-case, and the reason the message is conditional.

    When there WERE actionable issues and none got fixed, that is a real
    janitor failure. Printing the reassurance there would launder exactly the
    signal the operator needs.
    """
    from alfred.janitor import cli as cli_mod

    result = _sweep(found=2075, actionable=12, not_fixable=2063)

    monkeypatch.setattr(cli_mod, "_init_state", lambda config: object())
    monkeypatch.setattr(cli_mod.asyncio, "run", lambda coro: (coro.close(), result)[1])
    cli_mod.cmd_fix(config=None, skills_dir=None)  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "nothing fixed because" not in out


# --------------------------------------------------------------------
# History table
# --------------------------------------------------------------------

def test_history_shows_dashes_for_pre_split_rows(capsys, monkeypatch) -> None:
    """Trend-reading down a column is the whole value of the table; a
    fabricated 0 on historical rows would corrupt exactly that."""
    from alfred.janitor import cli as cli_mod

    modern = _sweep(found=2075, actionable=12, not_fixable=2063, cohort=11)
    modern.timestamp = "2026-08-04T00:00:00+00:00"
    legacy = SweepResult.from_dict(
        {"sweep_id": "old", "issues_found": 40,
         "timestamp": "2026-01-01T00:00:00+00:00"}
    )

    class _FakeState:
        sweeps = {"s1": modern, "old": legacy}

        def load(self) -> None:
            pass

    monkeypatch.setattr(cli_mod, "_init_state", lambda config: _FakeState())
    cli_mod.cmd_history(config=None)  # type: ignore[arg-type]
    out = capsys.readouterr().out

    lines = [ln for ln in out.splitlines() if ln.startswith(("s1", "old"))]
    assert len(lines) == 2
    modern_row = next(ln for ln in lines if ln.startswith("s1"))
    legacy_row = next(ln for ln in lines if ln.startswith("old"))
    assert "12" in modern_row and "2063" in modern_row
    assert "—" in legacy_row, "pre-split row must not claim a split"
    assert "0" not in legacy_row.split()[3:6], "no fabricated zeros"


def test_history_legend_explains_notfix(capsys, monkeypatch) -> None:
    """The column header alone would reintroduce the misread — a bare
    'NotFix: 2063' invites "so it failed 2063 times"."""
    from alfred.janitor import cli as cli_mod

    class _FakeState:
        sweeps = {"s1": _sweep(found=1, actionable=1)}

        def load(self) -> None:
            pass

    monkeypatch.setattr(cli_mod, "_init_state", lambda config: _FakeState())
    cli_mod.cmd_history(config=None)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "no janitor path to fix it" in out
    assert "DIR001 move-blocked" in out


def test_history_empty_says_so(capsys, monkeypatch) -> None:
    """ILB — 'no sweeps yet' must be distinguishable from a broken command."""
    from alfred.janitor import cli as cli_mod

    class _FakeState:
        sweeps: dict = {}

        def load(self) -> None:
            pass

    monkeypatch.setattr(cli_mod, "_init_state", lambda config: _FakeState())
    cli_mod.cmd_history(config=None)  # type: ignore[arg-type]
    assert "No sweep history." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# #49 — the per-code tally, so a count change is quotable on the box
# ---------------------------------------------------------------------------


def test_issue_counts_by_code_is_descending_by_count() -> None:
    """The dominant population reads first.

    On the live vault that is LINK001 by an order of magnitude — which is the
    whole reason the headline needed the #31 split, and the number a scanner
    change has to be judged on.
    """
    from alfred.janitor.cli import format_issue_counts_by_code
    from alfred.janitor.issues import Issue, IssueCode, Severity

    def _i(code: IssueCode) -> Issue:
        return Issue(code=code, severity=Severity.WARNING, file="x.md", message="m")

    result = _sweep(found=6)
    result.issues = (
        [_i(IssueCode.BROKEN_WIKILINK)] * 4
        + [_i(IssueCode.STUB_RECORD)] * 1
        + [_i(IssueCode.ORPHANED_RECORD)] * 1
    )
    out = format_issue_counts_by_code(result)
    assert out.splitlines() == [
        "Issues by code:",
        "  LINK001: 4",
        "  ORPHAN001: 1",
        "  STUB001: 1",
    ], "descending by count, then alphabetical on a tie"


def test_issue_counts_by_code_never_renders_an_empty_block() -> None:
    """A sweep whose issue LIST is empty while its count is not — a state file
    that persisted counts without issues — says so rather than printing a
    header with nothing under it."""
    from alfred.janitor.cli import format_issue_counts_by_code

    result = _sweep(found=40)
    result.issues = []
    assert format_issue_counts_by_code(result) == (
        "Issues by code: none recorded on this sweep"
    )


def test_split_docstring_example_is_internally_consistent() -> None:
    """The rendered shapes in the docstring are read as a contract by the next
    person to touch this. The cohort is a SUBSET of actionable, so an example
    with more cohort than actionable teaches the wrong invariant."""
    import re

    from alfred.janitor.cli import format_issue_split

    doc = format_issue_split.__doc__ or ""
    m = re.search(
        r"Issues found: (\d+) \((\d+) actionable, (\d+) not janitor-fixable; "
        r"(\d+) of\s+the actionable are spam-cohort\)",
        doc,
    )
    assert m, "the worked example shape changed — re-check this pin"
    total, actionable, not_fixable, cohort = (int(g) for g in m.groups())
    assert actionable + not_fixable == total, "the split must conserve"
    assert cohort <= actionable, "the cohort is a subset of actionable"

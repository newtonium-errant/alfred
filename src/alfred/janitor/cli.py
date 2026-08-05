"""Subcommand implementations for the janitor CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .backends import build_issue_report
from .config import JanitorConfig
from .daemon import run_sweep, run_watch
from .issues import Severity, SweepResult
from .state import JanitorState
from .utils import get_logger

log = get_logger(__name__)


#: Rendered when a sweep predates the segregation counts (#31).
#:
#: The state loader is schema-tolerant by contract, so a sweep written before
#: the split existed loads with ``issues_actionable = 0`` and
#: ``issues_not_janitor_fixable = 0`` — indistinguishable, field-by-field, from
#: a sweep that genuinely found nothing actionable. Printing "(0 actionable,
#: N not janitor-fixable)" for those rows would be a confident lie about
#: history. We detect it by conservation instead: the split is trustworthy
#: only when the two buckets add up to ``issues_found``.
SPLIT_UNAVAILABLE = "split not recorded"


def split_is_available(result: SweepResult) -> bool:
    """Does this sweep carry a trustworthy actionable/not-fixable split?

    Conservation is the test, and it does double duty. ``SweepResult`` persists
    only two of ``classify_counts``' three conservation terms — ``label_only``
    has no field, because it is 0 for every sweep that can currently occur (the
    label-only codes have no producer; see ``issues.LABEL_ONLY_CODES``). If a
    producer is ever wired, those issues count toward ``issues_found`` while
    landing in neither persisted bucket, the sum stops matching, and this
    returns False — so the CLI renders "split not recorded" rather than a
    parenthetical whose numbers do not add up. Fail-closed, in the direction
    that refuses to publish a lying figure. The day that happens, the fix is to
    persist the third term, and the wrong-looking output is the prompt to do it.
    """
    return (
        result.issues_found > 0
        and result.issues_actionable + result.issues_not_janitor_fixable
        == result.issues_found
    )


def format_issue_split(result: SweepResult) -> str:
    """One-line issue count with its remediation split.

    WHY THIS EXISTS (#31 / review-25 N4). ``Issues found: 2075`` beside
    ``Files fixed: 0`` reads as "the janitor is broken." The truth is that
    almost none of those 2,075 were janitor's to fix: DIR001 needs a move its
    scope denies, STUB001 belongs to ``janitor_enrich``, ORPHAN001 and
    SEM001-004 are flag-only. A number that invites exactly the wrong
    conclusion is worse than no number — so the headline now carries the split
    that makes it readable.

    Shapes::

        Issues found: 0
        Issues found: 5 (5 actionable, 0 not janitor-fixable)
        Issues found: 2075 (12 actionable, 2063 not janitor-fixable; 2051 of
                            the actionable are spam-cohort)
        Issues found: 2075 (split not recorded)

    No parenthetical on zero — there is nothing to disambiguate, and a
    ``(0 actionable, 0 not janitor-fixable)`` tail on a clean sweep is noise
    that trains the eye to skip the parenthetical everywhere else.

    The cohort clause appears only when the cohort is non-empty. That is not a
    silent-absence violation: the parenthetical's own presence already proves
    the segregation ran, so a missing cohort clause reads as "none", not as
    "did this even happen". On an instance with no markers at all — a fresh
    vault — every line would otherwise carry a permanent "; 0 spam-cohort".
    """
    n = result.issues_found
    if n == 0:
        return "Issues found: 0"
    if not split_is_available(result):
        return f"Issues found: {n} ({SPLIT_UNAVAILABLE})"

    parts = (
        f"{result.issues_actionable} actionable, "
        f"{result.issues_not_janitor_fixable} not janitor-fixable"
    )
    if result.issues_spam_cohort > 0:
        parts += (
            f"; {result.issues_spam_cohort} of the actionable are spam-cohort"
        )
    return f"Issues found: {n} ({parts})"


def _init_state(config: JanitorConfig) -> JanitorState:
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    state.load()
    return state


def cmd_scan(config: JanitorConfig, skills_dir: Path, structural_only: bool = False) -> None:
    """Run Phase 1 scan, print issue report, no fixes."""
    state = _init_state(config)
    result = asyncio.run(run_sweep(
        config, state, skills_dir,
        structural_only=True,  # scan never invokes agent
        fix_mode=False,
    ))

    # Print report
    if not result.issues:
        print("No issues found.")
        return

    print(f"\n=== Sweep {result.sweep_id} — {result.timestamp} ===")
    print(f"Files scanned: {result.files_scanned}")
    print(format_issue_split(result))
    for sev, count in sorted(result.issues_by_severity.items()):
        print(f"  {sev}: {count}")
    print()
    print(build_issue_report(result.issues))


def cmd_fix(config: JanitorConfig, skills_dir: Path, structural_only: bool = False) -> None:
    """Run scan + invoke agent to fix issues."""
    state = _init_state(config)
    result = asyncio.run(run_sweep(
        config, state, skills_dir,
        structural_only=structural_only,
        fix_mode=True,
    ))

    print(f"\n=== Sweep {result.sweep_id} — {result.timestamp} ===")
    print(f"Files scanned: {result.files_scanned}")
    print(format_issue_split(result))
    print(f"Files fixed: {result.files_fixed}")
    print(f"Files deleted: {result.files_deleted}")
    print(f"Agent invoked: {result.agent_invoked}")

    # The number-that-lies case, said out loud. "Issues found: 2075 / Files
    # fixed: 0" is the exact pair that reads as a broken janitor; when the
    # actionable bucket is empty, zero fixes is the CORRECT outcome and the
    # operator should not have to derive that from the parenthetical.
    if (
        result.files_fixed == 0
        and result.issues_found > 0
        and split_is_available(result)
        and result.issues_actionable == 0
    ):
        print(
            "  (nothing fixed because nothing found was janitor's to fix — "
            "every issue is in the not-janitor-fixable bucket)"
        )

    if result.issues:
        print()
        for sev, count in sorted(result.issues_by_severity.items()):
            print(f"  {sev}: {count}")


def cmd_watch(config: JanitorConfig, skills_dir: Path) -> None:
    """Daemon mode — sweep on interval."""
    state = _init_state(config)
    try:
        asyncio.run(run_watch(config, state, skills_dir))
    except KeyboardInterrupt:
        log.info("daemon.interrupted")
        print("\nStopped.")


def cmd_status(config: JanitorConfig) -> None:
    """Show last sweep result, open issue count, state summary."""
    state = _init_state(config)

    total_files = len(state.files)
    total_ignored = len(state.ignored)
    open_issues: dict[str, int] = {}
    files_with_issues = 0
    files_with_janitor_note = 0

    for fs in state.files.values():
        if fs.open_issues:
            files_with_issues += 1
            for code in fs.open_issues:
                open_issues[code] = open_issues.get(code, 0) + 1

    print(f"=== Janitor Status ===")
    print(f"Tracked files: {total_files}")
    print(f"Ignored files: {total_ignored}")
    print(f"Files with open issues: {files_with_issues}")
    print(f"Total sweeps recorded: {len(state.sweeps)}")
    print(f"Fix log entries: {len(state.fix_log)}")

    if open_issues:
        print(f"\nOpen issues by code:")
        for code, count in sorted(open_issues.items()):
            print(f"  {code}: {count}")

    # Last sweep
    if state.sweeps:
        last = max(state.sweeps.values(), key=lambda s: s.timestamp)
        print(f"\nLast sweep: {last.sweep_id} at {last.timestamp}")
        print(f"  {format_issue_split(last)}")
        print(f"  Files fixed: {last.files_fixed}")
        print(f"  Files deleted: {last.files_deleted}")

    # Recent fix log
    if state.fix_log:
        recent = state.fix_log[-5:]
        print(f"\nRecent fix log:")
        for entry in recent:
            print(f"  [{entry.timestamp}] {entry.action} {entry.file} ({entry.issue_code}) — {entry.detail}")


def cmd_history(config: JanitorConfig, limit: int = 10) -> None:
    """Show past sweep results."""
    state = _init_state(config)

    if not state.sweeps:
        print("No sweep history.")
        return

    sorted_sweeps = sorted(state.sweeps.values(), key=lambda s: s.timestamp, reverse=True)
    shown = sorted_sweeps[:limit]

    print(f"=== Sweep History (last {len(shown)}) ===\n")
    print(
        f"{'ID':<10} {'Timestamp':<28} {'Issues':<8} {'Action':<8} "
        f"{'NotFix':<8} {'Cohort':<8} {'Fixed':<8} {'Deleted':<8}"
    )
    print("-" * 94)
    for sweep in shown:
        # A row whose split predates #19-B shows "—" rather than a plausible
        # 0 — the trend a person reads down this column is exactly what a
        # fabricated zero would corrupt.
        if split_is_available(sweep):
            actionable = str(sweep.issues_actionable)
            not_fix = str(sweep.issues_not_janitor_fixable)
            cohort = str(sweep.issues_spam_cohort)
        else:
            actionable = not_fix = cohort = "—"
        print(
            f"{sweep.sweep_id:<10} {sweep.timestamp:<28} "
            f"{sweep.issues_found:<8} {actionable:<8} {not_fix:<8} "
            f"{cohort:<8} {sweep.files_fixed:<8} {sweep.files_deleted:<8}"
        )
    print(
        "\nAction = janitor can act on it. NotFix = real, correctly detected, "
        "no janitor path to fix it\n(DIR001 move-blocked, STUB001 "
        "janitor_enrich scope, ORPHAN001/SEM001-004 flag-only).\nCohort = the "
        "subset of Action that is spam-cohort (already-triaged LINK001 "
        "debt). '—' = sweep "
        "predates the split."
    )


def cmd_drift(config: JanitorConfig) -> None:
    """Run semantic drift scan and print results."""
    state = _init_state(config)

    from .scanner import run_drift_scan
    issues = run_drift_scan(config, state)

    if not issues:
        print("No drift issues found.")
        return

    print(f"\n=== Drift Scan — {len(issues)} issues ===\n")
    for issue in sorted(issues, key=lambda i: (i.severity.value, i.file)):
        print(f"  [{issue.severity.value}] {issue.file}")
        print(f"    {issue.code.value} — {issue.message}")
        if issue.suggested_fix:
            print(f"    Fix: {issue.suggested_fix}")
        print()


def cmd_ignore(config: JanitorConfig, file_path: str, reason: str = "") -> None:
    """Add a file to the ignore list."""
    state = _init_state(config)

    # Normalize path
    rel = file_path.replace("\\", "/")
    state.ignore_file(rel, reason)
    state.save()

    print(f"Ignored: {rel}")
    if reason:
        print(f"  Reason: {reason}")

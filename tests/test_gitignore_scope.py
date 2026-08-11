"""The ignore rules must not swallow source files (#86 gate BLOCK).

WHAT HAPPENED, because the failure mode is the whole point of this file. The
#83 debris round added `batch/` to `.gitignore` to stop repo-root scan
artifacts being committed. A gitignore pattern with no leading slash matches a
directory of that name at ANY DEPTH, so the rule also covered
`web/pages/api/batch/` and `src/alfred/batch/` — two real source packages that
happen to share the word.

The consequence was invisible from the worktree, and that is what makes it
worth a permanent pin:

  * `git add <ignored path>` is a silent NO-OP — no error, no warning.
  * `git status` reports the tree CLEAN, because the file is ignored.
  * every suite run passes, because the runs read the WORKTREE, where the file
    is present and correct.
  * the COMMITTED tree is missing the file, and only a build from a fresh
    extraction (`git archive`, a CI checkout, a colleague's clone) can see it.

`web/pages/api/batch/targets.ts` was excluded from #90's commit exactly this
way. No existing guard could have caught it: the debris guard watches for files
that APPEAR during a run, and this is a file that never appears at all.

THE THREE ASSERTIONS BELOW ARE ONE CONTRACT, and each covers a different way to
get this wrong:

  A. the root rule still WORKS   — otherwise the next person "fixes" a failing
                                   pin by deleting the line, reopening #83.
  B. the named packages are ADDABLE — the specific regression.
  C. NO source file anywhere is ignored — the general class, which is the only
                                   one of the three that would catch the NEXT
                                   over-broad pattern rather than this one.

(C) is the assertion that generalises; (A) and (B) are there so a failure names
the cause instead of leaving someone to bisect a pattern.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories that are committed source AND share a name with an ignored
#: root-level directory. Each is one collision the ignore rules must not cover.
#: Add a row whenever a new package's name collides with an ignore entry.
SOURCE_DIRS_SHARING_AN_IGNORED_NAME = (
    "src/alfred/batch",        # vs the root-level batch/ debris rule
    "web/pages/api/batch",     # vs the same rule — the one that actually bit
)

#: Extensions that mean "this is code someone wrote", not a build artifact.
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}

#: Directories whose contents are legitimately ignored and legitimately contain
#: source-shaped files. Excluded from the sweep so it stays about OUR files.
_SWEEP_EXCLUDES = ("node_modules", "__pycache__", ".next", ".venv", "venv")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(autouse=True)
def _needs_git() -> None:
    """Skip cleanly where git is unavailable — a wheel build, a sdist test."""
    if shutil.which("git") is None:  # pragma: no cover - CI always has git
        pytest.skip("git not available; ignore-scope cannot be checked")
    if not (REPO_ROOT / ".git").exists():  # pragma: no cover - worktree has one
        pytest.skip("not a git checkout; ignore-scope cannot be checked")


def _is_ignored(rel_path: str) -> bool:
    """True when git refuses to see ``rel_path``.

    ``check-ignore -q`` exits 0 when the path IS ignored, 1 when it is not.
    The path need not exist — the question is about the rules, not the disk,
    which is what lets this probe a file nobody has written yet.
    """
    return _git("check-ignore", "-q", "--", rel_path).returncode == 0


# ---------------------------------------------------------------------------
# A. The rule still does its job
# ---------------------------------------------------------------------------


def test_repo_root_batch_debris_is_still_ignored() -> None:
    """The #83 rule must keep working.

    Without this, the cheapest way to make the pins below pass is to delete the
    ignore line — which silently reopens the hole it was added to close (two
    smoke artifacts, one of them a binary, committed in 251b2a5a).
    """
    assert _is_ignored("batch/salem/20260811-abcd1234/manifest.json")
    assert _is_ignored("batch/salem/20260811-abcd1234/images/deadbeef.jpg")


def test_the_root_data_dir_is_still_ignored() -> None:
    """Sibling rule, same shape — runtime state must stay uncommittable."""
    assert _is_ignored("data/curator_state.json")


# ---------------------------------------------------------------------------
# B. The named source packages are addable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_dir", SOURCE_DIRS_SHARING_AN_IGNORED_NAME)
def test_a_new_file_in_a_source_package_is_not_ignored(source_dir: str) -> None:
    """A file that does not exist yet must still be addable.

    Probing a NON-EXISTENT path on purpose: this asks whether the RULES would
    swallow tomorrow's file, which is the question that went unasked. Checking
    only files already on disk would pass on the day the pattern is widened and
    fail later, on whoever adds the next file.
    """
    probe = f"{source_dir}/__ignore_scope_probe__.py"
    assert not _is_ignored(probe), (
        f"{source_dir}/ is covered by a .gitignore rule, so a new file there "
        f"would be silently dropped from commits (`git add` no-ops, `git "
        f"status` stays clean, and only a fresh extraction shows the loss). "
        f"Run `git check-ignore -v {probe}` for the offending line — the fix "
        f"is almost always a leading slash to anchor the pattern at the root."
    )


def test_the_file_this_pin_was_born_from_is_tracked() -> None:
    """The specific casualty, asserted by name.

    `web/pages/api/batch/targets.ts` existed in the worktree, passed every
    suite, and was absent from the commit. A regression pin that only checked
    the RULES would go green again the moment someone re-broke the pattern
    without re-losing this file; this one also asserts the file is really in
    the index.
    """
    tracked = _git("ls-files", "--", "web/pages/api/batch/targets.ts").stdout
    assert tracked.strip(), (
        "web/pages/api/batch/targets.ts is not tracked — the batch targets "
        "route is missing from the committed tree again"
    )


# ---------------------------------------------------------------------------
# C. The general class
# ---------------------------------------------------------------------------


def test_the_sweep_can_actually_detect_an_invisible_file() -> None:
    """Prove the mechanism fires, so the sweep below is not vacuously green.

    The sweep's healthy result is an EMPTY list, which is also what a broken
    probe returns. Without this, a `check-ignore` invocation that silently
    stopped working — a flag rename, a cwd mistake, a git upgrade — would leave
    the sweep passing forever while detecting nothing.

    Uses a path that does not exist: `check-ignore` answers about RULES, not
    about the filesystem, so the mechanism can be exercised without writing
    anything into the tree (the debris guard would rightly object).
    """
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=REPO_ROOT,
        input="data/__sweep_self_test__.py",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "data/__sweep_self_test__.py", (
        "git check-ignore --stdin did not report a known-ignored path; the "
        "sweep below cannot detect anything and its green is meaningless"
    )


def test_no_source_file_on_disk_is_invisible_to_git() -> None:
    """THE generalising assertion — the one that catches the NEXT such pattern.

    Walks the real source trees and asks git about every file someone wrote. A
    hit means that file is present locally, untracked, and ignored — so it is
    absent from every clone, which is exactly the condition that produced this
    gate's BLOCK.

    Deliberately asks the DISK rather than the index. A "every TRACKED file is
    visible" sweep would have passed throughout the incident, because the
    casualty was never tracked — being untracked is the whole failure.

    MEASURED behaviour of the tool, since it shapes what this can and cannot
    see: `check-ignore --stdin` SKIPS paths that are already tracked (exit 1,
    no output) and reports untracked ones (exit 0). That asymmetry is correct
    here rather than a gap — a tracked file is committed regardless of what the
    ignore rules say, so it is not at risk. The consequence worth knowing is
    that this assertion goes quiet once a swallowed file is rescued; the
    rule-level pins above are what keep watching the pattern itself.
    """
    candidates: list[str] = []
    for root in ("src", "web", "tests"):
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            rel = path.relative_to(REPO_ROOT)
            if any(part in _SWEEP_EXCLUDES for part in rel.parts):
                continue
            candidates.append(str(rel))

    assert candidates, "found no source files to check — the sweep is broken"

    # One batched call rather than thousands: check-ignore reads paths on stdin
    # and prints back only the ones it WOULD ignore.
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=REPO_ROOT,
        input="\n".join(candidates),
        capture_output=True,
        text=True,
        check=False,
    )
    swallowed = [line for line in result.stdout.splitlines() if line.strip()]
    assert not swallowed, (
        "these source files exist on disk but are IGNORED by git, so they are "
        "silently excluded from every commit:\n  "
        + "\n  ".join(swallowed)
        + "\nRun `git check-ignore -v <path>` for the offending rule."
    )

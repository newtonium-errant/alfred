"""Tests for the KAL-LE morning digest assembler.

Covers the data-layer readers (bash_exec, instructor state, git log,
BIT state) AND the markdown renderer. Each reader is tested in
isolation so future schema changes (e.g. instructor state gaining an
``executed_history``) only fail the targeted test rather than the
whole suite.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.brief.kalle_digest import (
    TICKET_TERMINAL_DISPOSITIONS,
    DigestData,
    _first_cross_referenced_pr,
    assemble_digest,
    assemble_ticket_pipeline_section,
    check_ticket_outcomes,
    gather_digest_data,
    read_bash_exec_log,
    read_bit_state,
    read_git_commits,
    read_instructor_state,
    render_digest_markdown,
    render_ticket_pipeline_section,
)
from alfred.integrations.github_ops import GitHubOpsConfig, read_github_audit
from alfred.transport.ticket_intake import (
    TicketIntakeEntry,
    TicketIntakeState,
)


TODAY = date(2026, 4, 23)
YESTERDAY = "2026-04-22"


# ---------------------------------------------------------------------------
# bash_exec reader
# ---------------------------------------------------------------------------


def test_bash_exec_log_missing_returns_zero(tmp_path: Path) -> None:
    log_path = tmp_path / "bash_exec.jsonl"
    count, cwds = read_bash_exec_log(log_path, TODAY)
    assert count == 0
    assert cwds == {}


def test_bash_exec_log_yesterday_only(tmp_path: Path) -> None:
    """Counts only yesterday's entries; today + day-before-yesterday excluded."""
    log_path = tmp_path / "bash_exec.jsonl"
    entries = [
        # Yesterday — counted.
        {"ts": "2026-04-22T15:09:37.914425+00:00", "cwd": "/home/andrew/aftermath-alfred"},
        {"ts": "2026-04-22T18:30:00+00:00", "cwd": "/home/andrew/aftermath-lab"},
        {"ts": "2026-04-22T23:59:59+00:00", "cwd": "/home/andrew/aftermath-alfred"},
        # Today — excluded.
        {"ts": "2026-04-23T01:00:00+00:00", "cwd": "/home/andrew/aftermath-alfred"},
        # Day before — excluded.
        {"ts": "2026-04-21T20:00:00+00:00", "cwd": "/home/andrew/aftermath-alfred"},
    ]
    log_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )

    count, cwds = read_bash_exec_log(log_path, TODAY)
    assert count == 3
    assert cwds["aftermath-alfred"] == 2
    assert cwds["aftermath-lab"] == 1


def test_bash_exec_log_skips_malformed_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "bash_exec.jsonl"
    log_path.write_text(
        "\n".join([
            json.dumps({"ts": "2026-04-22T10:00:00+00:00", "cwd": "/x"}),
            "this is not json",
            "",
            json.dumps({"ts": "2026-04-22T11:00:00+00:00", "cwd": "/y"}),
        ]),
        encoding="utf-8",
    )
    count, cwds = read_bash_exec_log(log_path, TODAY)
    assert count == 2


# ---------------------------------------------------------------------------
# instructor state reader
# ---------------------------------------------------------------------------


def test_instructor_state_missing_returns_empty(tmp_path: Path) -> None:
    executed, pending, retrying = read_instructor_state(
        tmp_path / "missing.json", TODAY,
    )
    assert executed == []
    assert pending == []
    assert retrying == []


def test_instructor_state_extracts_retrying(tmp_path: Path) -> None:
    state_path = tmp_path / "instructor_state.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "file_hashes": {"a.md": "h1", "b.md": "h2", "c.md": "h3"},
        "retry_counts": {"b.md": 2, "c.md": 0, "d.md": 1},
    }), encoding="utf-8")
    executed, pending, retrying = read_instructor_state(state_path, TODAY)
    # Only files with retry_count > 0 appear.
    assert "b.md" in retrying
    assert "d.md" in retrying
    assert "c.md" not in retrying
    assert sorted(retrying) == ["b.md", "d.md"]


def test_instructor_state_invalid_json_returns_empty(tmp_path: Path) -> None:
    state_path = tmp_path / "instructor_state.json"
    state_path.write_text("{ not valid json", encoding="utf-8")
    executed, pending, retrying = read_instructor_state(state_path, TODAY)
    assert executed == []
    assert pending == []
    assert retrying == []


# ---------------------------------------------------------------------------
# git_commits reader
# ---------------------------------------------------------------------------


def _make_repo_with_commits(repo: Path, dates_and_msgs: list[tuple[str, str]]) -> None:
    """Initialise a tiny git repo with backdated commits for testing.

    Uses ISO 8601 with explicit ``+0000`` so the commit timestamps land
    in UTC regardless of the test machine's timezone — keeps the
    yesterday-window assertions deterministic in CI vs. WSL2 dev.
    """
    import os as _os
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    for i, (commit_date, msg) in enumerate(dates_and_msgs):
        f = repo / f"file_{i}.txt"
        f.write_text(f"content {i}", encoding="utf-8")
        subprocess.run(["git", "add", str(f.name)], cwd=repo, check=True)
        # Pin the commit time to UTC so git log's --since/--until window
        # (which our reader passes as bare YYYY-MM-DD interpreted in
        # the caller's TZ) lines up with the assertion date in any TZ.
        iso_utc = f"{commit_date}+0000"
        env = {
            **_os.environ,
            "GIT_AUTHOR_DATE": iso_utc,
            "GIT_COMMITTER_DATE": iso_utc,
            "TZ": "UTC",
        }
        subprocess.run(
            ["git", "commit", "-q", "-m", msg],
            cwd=repo, check=True, env=env,
        )


def test_git_commits_yesterday_window(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    _make_repo_with_commits(repo, [
        ("2026-04-21T10:00:00", "old commit (day before)"),
        ("2026-04-22T10:00:00", "yesterday morning commit"),
        ("2026-04-22T18:00:00", "yesterday evening commit"),
        ("2026-04-23T08:00:00", "today commit"),
    ])

    commits = read_git_commits([repo], TODAY)
    msgs = commits.get("myrepo", [])
    assert any("yesterday morning" in m for m in msgs)
    assert any("yesterday evening" in m for m in msgs)
    assert not any("today commit" in m for m in msgs)
    assert not any("day before" in m for m in msgs)


def test_git_commits_missing_repo_skipped(tmp_path: Path) -> None:
    """A non-existent or non-git directory is skipped without error."""
    commits = read_git_commits([tmp_path / "does-not-exist"], TODAY)
    assert commits == {}


def test_git_commits_empty_yesterday_returns_no_entry(tmp_path: Path) -> None:
    repo = tmp_path / "quietrepo"
    _make_repo_with_commits(repo, [
        ("2026-04-21T10:00:00", "old work"),
    ])
    commits = read_git_commits([repo], TODAY)
    # Repos with no yesterday commits don't appear in the dict at all.
    assert "quietrepo" not in commits


# ---------------------------------------------------------------------------
# BIT state reader
# ---------------------------------------------------------------------------


def test_bit_state_missing_returns_empty() -> None:
    status, summary = read_bit_state(None)
    assert status == ""
    assert summary == ""


def test_bit_state_ok_with_counts(tmp_path: Path) -> None:
    bit_path = tmp_path / "bit_state.json"
    bit_path.write_text(json.dumps({
        "runs": [
            {"overall_status": "ok", "tool_counts": {"ok": 6, "warn": 0}},
        ],
    }), encoding="utf-8")
    status, summary = read_bit_state(bit_path)
    assert status == "ok"
    assert "6 ok" in summary


def test_bit_state_warn(tmp_path: Path) -> None:
    bit_path = tmp_path / "bit_state.json"
    bit_path.write_text(json.dumps({
        "runs": [
            {"overall_status": "warn", "tool_counts": {"ok": 5, "warn": 1}},
        ],
    }), encoding="utf-8")
    status, _ = read_bit_state(bit_path)
    assert status == "warn"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_render_empty_day_uses_blank_line_for_yesterday() -> None:
    md = render_digest_markdown(DigestData())
    assert "**Yesterday:**" in md
    assert "No tracked activity yesterday." in md
    assert "**Today:**" in md
    assert "standing by for new directives" in md
    assert "**Posture:** green" in md
    assert "no BIT data" in md


def test_render_with_git_commits_first() -> None:
    data = DigestData(
        git_commits_by_repo={
            "aftermath-lab": ["Add overview.md to stack/railway"],
            "aftermath-alfred": [
                "Tighten scope check",
                "Add test for dispatch race",
            ],
        },
    )
    md = render_digest_markdown(data)
    assert "1 commit in `aftermath-lab`" in md
    assert "2 commits in `aftermath-alfred`" in md


def test_render_bash_exec_summary() -> None:
    data = DigestData(
        bash_exec_count=12,
        bash_exec_cwd_counts={"aftermath-alfred": 8, "aftermath-lab": 4},
    )
    md = render_digest_markdown(data)
    assert "12 bash_exec runs" in md
    assert "8 in `aftermath-alfred`" in md
    assert "4 in `aftermath-lab`" in md


def test_render_bullets_capped_at_five() -> None:
    """Yesterday section never grows past 5 bullets."""
    many_repos = {f"repo{i}": [f"commit msg {i}"] for i in range(10)}
    data = DigestData(git_commits_by_repo=many_repos)
    md = render_digest_markdown(data)
    yesterday_block = md.split("**Today:**")[0]
    bullet_count = sum(1 for line in yesterday_block.splitlines()
                       if line.startswith("- "))
    assert bullet_count <= 5


def test_render_today_caps_at_three() -> None:
    data = DigestData(
        instructor_pending=["a.md", "b.md", "c.md", "d.md"],
        instructor_retrying=["e.md", "f.md"],
    )
    md = render_digest_markdown(data)
    today_block = md.split("**Today:**")[1].split("**Posture")[0]
    bullet_count = sum(1 for line in today_block.splitlines()
                       if line.startswith("- "))
    assert bullet_count <= 3


def test_render_posture_red_on_fail() -> None:
    data = DigestData(
        bit_overall_status="fail",
        bit_summary_line="2 fail, 4 ok",
    )
    md = render_digest_markdown(data)
    assert "**Posture:** red" in md
    assert "2 fail, 4 ok" in md


def test_render_posture_yellow_on_warn() -> None:
    data = DigestData(bit_overall_status="warn", bit_summary_line="1 warn, 5 ok")
    md = render_digest_markdown(data)
    assert "**Posture:** yellow" in md


def test_render_posture_green_on_ok() -> None:
    data = DigestData(bit_overall_status="ok", bit_summary_line="6 ok")
    md = render_digest_markdown(data)
    assert "**Posture:** green" in md
    assert "6 ok" in md


def test_render_word_count_within_target() -> None:
    """A realistic digest stays under the ~400 word soft cap."""
    data = DigestData(
        bash_exec_count=15,
        bash_exec_cwd_counts={"aftermath-alfred": 12, "aftermath-lab": 3},
        git_commits_by_repo={
            "aftermath-alfred": ["Ship feature X", "Fix bug Y", "Test Z"],
            "aftermath-lab": ["Document new pattern"],
        },
        instructor_executed=["d1", "d2"],
        instructor_pending=["d3"],
        bit_overall_status="ok",
        bit_summary_line="7 ok",
    )
    md = render_digest_markdown(data)
    word_count = len(md.split())
    assert word_count < 400, f"Digest exceeded soft cap: {word_count} words"


# ---------------------------------------------------------------------------
# End-to-end via assemble_digest
# ---------------------------------------------------------------------------


def test_assemble_digest_with_no_data_dir(tmp_path: Path) -> None:
    """Empty data_dir → still returns a valid one-slide digest."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    md = assemble_digest(
        today=TODAY, data_dir=data_dir, repo_paths=[],
    )
    assert "**Yesterday:**" in md
    assert "**Today:**" in md
    assert "**Posture:**" in md


def test_assemble_digest_full_pipeline(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # bash_exec log with one yesterday entry
    (data_dir / "bash_exec.jsonl").write_text(
        json.dumps({"ts": "2026-04-22T12:00:00+00:00", "cwd": "/x/aftermath-alfred"}) + "\n",
        encoding="utf-8",
    )
    # instructor state with one retry-queued
    (data_dir / "instructor_state.json").write_text(
        json.dumps({"retry_counts": {"foo.md": 1}}),
        encoding="utf-8",
    )
    # Tiny repo with a yesterday commit
    repo = tmp_path / "myrepo"
    _make_repo_with_commits(repo, [
        ("2026-04-22T10:00:00", "Yesterday work"),
    ])

    md = assemble_digest(
        today=TODAY,
        data_dir=data_dir,
        repo_paths=[repo],
    )
    assert "1 commit in `myrepo`" in md
    assert "1 bash_exec runs" in md or "bash_exec runs" in md
    assert "queued for retry" in md
    assert "**Posture:** green" in md


def test_gather_digest_data_aggregates(tmp_path: Path) -> None:
    """gather_digest_data returns a populated DigestData value object."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "bash_exec.jsonl").write_text(
        json.dumps({"ts": "2026-04-22T12:00:00+00:00", "cwd": "/x"}) + "\n",
        encoding="utf-8",
    )
    data = gather_digest_data(
        today=TODAY,
        bash_exec_log=data_dir / "bash_exec.jsonl",
        instructor_state=data_dir / "instructor_state.json",
        repo_paths=[],
    )
    assert data.bash_exec_count == 1
    assert data.bit_overall_status == ""


# ---------------------------------------------------------------------------
# Ticket pipeline section (pipeline c5) — effectiveness loop + render
# ---------------------------------------------------------------------------


NOW = datetime(2026, 6, 11, 8, 30, tzinfo=timezone.utc)

IDLE_LINE = "Ticket pipeline: no tickets received yet; GitHub ops idle."


def _ago(**kw: float) -> str:
    """ISO timestamp ``timedelta(**kw)`` before NOW."""
    return (NOW - timedelta(**kw)).isoformat()


def _log_events(captured: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [c for c in captured if c.get("event") == event]


class FakeDigestGitHubClient:
    """Read-op surface of GitHubOpsClient for the digest caller.

    Same method names + keyword signatures as the real client's
    issue_timeline / pr_get / pr_reviews; no network, no audit (the
    summarizing audit row is the code-under-test's job).
    """

    def __init__(
        self,
        audit_path: Path,
        *,
        timelines: dict[int, list[dict[str, Any]]] | None = None,
        prs: dict[int, dict[str, Any]] | None = None,
        reviews: dict[int, list[dict[str, Any]]] | None = None,
        timeline_exc: dict[int, BaseException] | None = None,
        forge_type: str = "github",
    ) -> None:
        self.config = GitHubOpsConfig(
            repo="acme/site",
            pat="DUMMY_GITHUB_TEST_PAT",
            instance="KAL-LE",
            forge_type=forge_type,
            audit_log_path=str(audit_path),
        )
        self.timelines = timelines or {}
        self.prs = prs or {}
        self.reviews = reviews or {}
        self.timeline_exc = timeline_exc or {}
        self.timeline_calls: list[int] = []
        self.pr_get_calls: list[int] = []
        self.pr_reviews_calls: list[int] = []

    async def issue_timeline(
        self, *, number: int, caller: str, correlation_id: str = "",
    ) -> list[dict[str, Any]]:
        self.timeline_calls.append(number)
        exc = self.timeline_exc.get(number)
        if exc is not None:
            raise exc
        return self.timelines.get(number, [])

    async def pr_get(
        self, *, number: int, caller: str, correlation_id: str = "",
    ) -> dict[str, Any]:
        self.pr_get_calls.append(number)
        return self.prs.get(number, {})

    async def pr_reviews(
        self, *, number: int, caller: str, correlation_id: str = "",
    ) -> list[dict[str, Any]]:
        self.pr_reviews_calls.append(number)
        return self.reviews.get(number, [])


def _xref_event(pr_number: int) -> dict[str, Any]:
    """A GitHub timeline cross-referenced event whose source is a PR.

    GitHub shape (the DEFAULT FakeDigestGitHubClient forge_type): an
    ``event == "cross-referenced"`` + ``source.issue`` carrying a
    ``pull_request``. The forgejo variant is ``_xref_event_forgejo``.
    """
    return {
        "event": "cross-referenced",
        "source": {
            "issue": {
                "number": pr_number,
                "pull_request": {
                    "url": (
                        "https://api.github.com/repos/acme/site/pulls/"
                        f"{pr_number}"
                    ),
                },
            },
        },
    }


def _xref_event_forgejo(pr_number: int) -> dict[str, Any]:
    """A Forgejo timeline cross-ref comment whose ref_issue is a PR.

    Forgejo shape: a ``type`` (cross-ref values pull_ref/issue_ref/...) +
    a ``ref_issue`` (a full Issue; a PR carries a truthy ``pull_request``
    field). NOT GitHub's ``event: cross-referenced`` + ``source.issue``.
    """
    return {
        "type": "pull_ref",
        "ref_issue": {
            "number": pr_number,
            "pull_request": {
                "merged": False,
                "url": f"https://git.algernon.test/acme/site/pulls/{pr_number}",
            },
        },
    }


def _mem_state(tmp_path: Path, entries: dict[str, TicketIntakeEntry]) -> TicketIntakeState:
    state = TicketIntakeState(path=tmp_path / "ticket_intake_state.json")
    state.entries.update(entries)
    return state


def _raw_with_state(state_path: Path, *, github: bool = False) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "ticket_intake": {
            "enabled": True,
            "state": {"path": str(state_path)},
        },
        "telegram": {"instance": {"name": "KAL-LE"}},
    }
    if github:
        raw["github"] = {
            "repo": "acme/site",
            "pat": "DUMMY_GITHUB_TEST_PAT",
            "instance": "KAL-LE",
        }
    return raw


def test_terminal_dispositions_pinned() -> None:
    """Terminal = the merged_*/closed_unmerged three; stalled is NOT
    terminal (keeps re-checking, can upgrade to merged_*)."""
    assert TICKET_TERMINAL_DISPOSITIONS == frozenset({
        "merged_clean", "merged_after_rework", "closed_unmerged",
    })
    assert "stalled" not in TICKET_TERMINAL_DISPOSITIONS
    assert "" not in TICKET_TERMINAL_DISPOSITIONS


async def test_ticket_pipeline_idle_no_state_file(tmp_path: Path) -> None:
    """No state file → exactly the quiet ILB line + the idle log."""
    raw = _raw_with_state(tmp_path / "missing_state.json")
    with structlog.testing.capture_logs() as captured:
        out = await assemble_ticket_pipeline_section(raw, now=NOW)
    assert out == IDLE_LINE
    idle = _log_events(captured, "kalle.digest.ticket_pipeline_idle")
    assert len(idle) == 1
    assert idle[0]["state_path"] == str(tmp_path / "missing_state.json")


async def test_ticket_pipeline_idle_zero_entries(tmp_path: Path) -> None:
    """A state file with zero entries is the same quiet line."""
    state = _mem_state(tmp_path, {})
    state.save()
    raw = _raw_with_state(state.path)
    out = await assemble_ticket_pipeline_section(raw, now=NOW)
    assert out == IDLE_LINE


async def test_ticket_pipeline_no_credential_path_still_renders(
    tmp_path: Path,
) -> None:
    """No github: section → counts + state lines render, PR check is
    skipped with the quiet unavailable note (e.g. Salem running this
    assembler). The section never disappears."""
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(hours=2),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(minutes=90),
            ticket_type="bug",
        ),
    })
    state.save()
    raw = _raw_with_state(state.path)  # github deliberately absent
    with structlog.testing.capture_logs() as captured:
        out = await assemble_ticket_pipeline_section(raw, now=NOW)
    assert "**Ticket pipeline:**" in out
    assert "- Last 24h: 1 received, 1 issue created" in out
    assert "- (outcome check unavailable: github not configured)" in out
    assert "- GH#7 → no PR yet (issue open 0 nights)" in out
    unavailable = _log_events(
        captured,
        "kalle.digest.ticket_pipeline_outcome_check_unavailable",
    )
    assert len(unavailable) == 1
    assert unavailable[0]["reason"] == "github_section_absent"
    # No idle log on the non-empty path.
    assert _log_events(captured, "kalle.digest.ticket_pipeline_idle") == []


def test_ticket_pipeline_counts_24h_window(tmp_path: Path) -> None:
    """Only activity within 24h of assembly time counts."""
    state = _mem_state(tmp_path, {
        "uid-new": TicketIntakeEntry(
            recorded_at=_ago(hours=2),
            kalle_relpath="ticket/New.md",
            issue_number=7,
            issue_created_at=_ago(hours=1),
        ),
        "uid-old": TicketIntakeEntry(
            recorded_at=_ago(days=3),
            kalle_relpath="ticket/Old.md",
            issue_number=5,
            issue_created_at=_ago(days=3),
        ),
    })
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- Last 24h: 1 received, 1 issue created" in out
    assert "idle since" not in out
    # Both issue-bearing entries still get status lines.
    assert "- GH#5 → no PR yet" in out
    assert "- GH#7 → no PR yet" in out


def test_ticket_pipeline_pending_retry_renders_loud(tmp_path: Path) -> None:
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(hours=2),
            kalle_relpath="ticket/A.md",
            retry_count=2,
        ),
    })
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- 1 issue post(s) pending retry — see github_ops_audit" in out


def test_ticket_pipeline_abandoned_entry_not_counted_pending(
    tmp_path: Path,
) -> None:
    """KAL-LE flag FIX 2 (digest side): an ``intake_abandoned`` entry —
    a stale retry counter for a terminal (wont_fix) ticket — is NOT
    counted as pending retry. This is the false-yellow-posture driver:
    vera-20260609 (retry_count=61, no issue) whose ticket is wont_fix
    must stop showing as pending."""
    state = _mem_state(tmp_path, {
        "uid-wontfix": TicketIntakeEntry(
            recorded_at=_ago(hours=2),
            kalle_relpath="ticket/Abandoned.md",
            retry_count=61,
            intake_abandoned=True,  # reconciled — terminal ticket
        ),
    })
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "pending retry" not in out, (
        "an intake_abandoned (reconciled-terminal) entry must NOT count "
        "as pending retry — it's a stale counter, not an active retry."
    )


def test_ticket_pipeline_abandoned_does_not_mask_real_pending(
    tmp_path: Path,
) -> None:
    """Mixed: an abandoned entry + a genuinely-pending one → the count
    reflects ONLY the real pending (abandoned excluded, real preserved)."""
    state = _mem_state(tmp_path, {
        "uid-wontfix": TicketIntakeEntry(
            recorded_at=_ago(hours=2),
            kalle_relpath="ticket/Abandoned.md",
            retry_count=61,
            intake_abandoned=True,
        ),
        "uid-real": TicketIntakeEntry(
            recorded_at=_ago(hours=2),
            kalle_relpath="ticket/Real.md",
            retry_count=2,
        ),
    })
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- 1 issue post(s) pending retry — see github_ops_audit" in out


def test_ticket_pipeline_idle_since_when_no_24h_activity(
    tmp_path: Path,
) -> None:
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at="2026-06-08T10:00:00+00:00",
            kalle_relpath="ticket/A.md",
            issue_number=3,
            issue_created_at="2026-06-08T11:00:00+00:00",
        ),
    })
    out = render_ticket_pipeline_section(state, now=NOW)
    assert (
        "- Last 24h: 0 received, 0 issues created (idle since 2026-06-08)"
        in out
    )


async def test_check_outcomes_discovers_open_pr(tmp_path: Path) -> None:
    """Timeline cross-reference discovers the PR; open PR on a fresh
    issue leaves disposition '' (no audit row — nothing changed)."""
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(hours=20),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(hours=19),
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        timelines={7: [_xref_event(456)]},
        prs={456: {"state": "open", "merged_at": None}},
    )
    with structlog.testing.capture_logs() as captured:
        changed = await check_ticket_outcomes(
            state, fake, now=NOW, audit_log_path=str(audit_path),
        )
    assert changed == 0
    entry = state.entries["uid-1"]
    assert entry.pr_number == 456
    assert entry.pr_state == "open"
    assert entry.disposition == ""
    assert entry.outcome_checked_at == NOW.isoformat()
    # Every completed evaluation logs, with the changed flag.
    checked = _log_events(
        captured, "kalle.digest.ticket_pipeline_outcome_checked",
    )
    assert len(checked) == 1
    assert checked[0]["uid"] == "uid-1"
    assert checked[0]["disposition"] == ""
    assert checked[0]["changed"] is False
    # No disposition change → no summarizing audit row.
    assert read_github_audit(audit_path) == []
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- GH#7 → PR#456 OPEN — review this morning" in out


async def test_check_outcomes_merged_clean_latency_and_audit(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at="2026-06-09T00:00:00+00:00",
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at="2026-06-09T00:00:00+00:00",
            ticket_type="bug",
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        timelines={7: [_xref_event(456)]},
        # 2 days + 2.4 hours = exactly 2.1 days.
        prs={456: {"state": "closed", "merged_at": "2026-06-11T02:24:00Z"}},
        reviews={456: [{"state": "APPROVED"}]},
    )
    with structlog.testing.capture_logs() as captured:
        changed = await check_ticket_outcomes(
            state, fake, now=NOW, audit_log_path=str(audit_path),
        )
    assert changed == 1
    entry = state.entries["uid-1"]
    assert entry.pr_number == 456
    assert entry.pr_state == "merged"
    assert entry.disposition == "merged_clean"
    assert entry.ticket_to_pr_latency_days == pytest.approx(2.1)
    checked = _log_events(
        captured, "kalle.digest.ticket_pipeline_outcome_checked",
    )
    assert len(checked) == 1
    assert checked[0]["disposition"] == "merged_clean"
    assert checked[0]["changed"] is True
    # ONE summarizing audit row, with the effectiveness fields.
    rows = read_github_audit(audit_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["op"] == "issue_get"
    assert row["outcome"] == "ok"
    assert row["caller"] == "digest"
    assert row["ticket_uid"] == "uid-1"
    assert row["issue_number"] == 7
    assert row["pr_number"] == 456
    assert row["pr_state"] == "merged"
    assert row["disposition"] == "merged_clean"
    assert row["ticket_to_pr_latency_days"] == pytest.approx(2.1)
    # Fresh flip renders the loud line (latches out after 24h).
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- GH#7 → PR#456 MERGED ✓ (2.1d)" in out


async def test_check_outcomes_merged_after_rework(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(days=2),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(days=2),
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        timelines={7: [_xref_event(456)]},
        prs={456: {"state": "closed", "merged_at": "2026-06-11T00:00:00Z"}},
        reviews={456: [
            {"state": "CHANGES_REQUESTED"},   # GitHub's enum (default fake)
            {"state": "APPROVED"},
        ]},
    )
    changed = await check_ticket_outcomes(
        state, fake, now=NOW, audit_log_path=str(audit_path),
    )
    assert changed == 1
    assert state.entries["uid-1"].disposition == "merged_after_rework"


# ---------------------------------------------------------------------------
# Forge-aware — PR-discovery (timeline shape) + review-enum pins
# ---------------------------------------------------------------------------


def test_first_cross_referenced_pr_github_shape() -> None:
    """BACKWARD-COMPAT: the github branch reads ``event ==
    "cross-referenced"`` + ``source.issue`` (a PR). Default forge_type."""
    assert _first_cross_referenced_pr([_xref_event(456)]) == 456
    assert _first_cross_referenced_pr([_xref_event(456)], "github") == 456


def test_first_cross_referenced_pr_forgejo_shape() -> None:
    """PR-discovery pin (forgejo). A Forgejo timeline comment uses
    ``type`` (cross-ref values) + ``ref_issue`` (a PR carries a truthy
    ``pull_request``). FAILS against the pre-port code, which read only
    GitHub's ``event``/``source.issue`` → returned None → every ticket
    reports false 'stalled' forever."""
    timeline = [
        # a plain comment — no cross-ref type
        {"type": "comment", "body": "looking into it"},
        # a cross-ref whose ref_issue is a plain ISSUE, not a PR (skip)
        {"type": "issue_ref", "ref_issue": {"number": 99}},
        # the real PR cross-ref
        _xref_event_forgejo(456),
    ]
    assert _first_cross_referenced_pr(timeline, "forgejo") == 456


def test_cross_ref_shapes_dont_leak_across_forges() -> None:
    """The branch is REAL: a github-shaped timeline parsed as forgejo (and
    vice-versa) finds nothing — proving forge_type actually selects the
    parser, not a both-shapes-accidentally-work fallback."""
    assert _first_cross_referenced_pr([_xref_event(456)], "forgejo") is None
    assert _first_cross_referenced_pr([_xref_event_forgejo(456)], "github") is None


async def test_review_enum_accepts_both_forge_values(tmp_path: Path) -> None:
    """Review-enum pin: BOTH GitHub's ``CHANGES_REQUESTED`` and Forgejo's
    ``REQUEST_CHANGES`` drive merged_after_rework (set membership, no
    branch). The REQUEST_CHANGES half FAILS against the pre-port code
    (which matched only CHANGES_REQUESTED → rework never detected on
    Forgejo → metric silently always merged_clean)."""
    for enum_value in ("CHANGES_REQUESTED", "REQUEST_CHANGES"):
        audit_path = tmp_path / f"audit_{enum_value}.jsonl"
        state = _mem_state(tmp_path, {
            "uid-1": TicketIntakeEntry(
                recorded_at=_ago(days=2),
                kalle_relpath="ticket/A.md",
                issue_number=7,
                issue_created_at=_ago(days=2),
            ),
        })
        fake = FakeDigestGitHubClient(
            audit_path,
            timelines={7: [_xref_event(456)]},
            prs={456: {"state": "closed", "merged_at": "2026-06-11T00:00:00Z"}},
            reviews={456: [{"state": enum_value}]},
        )
        await check_ticket_outcomes(
            state, fake, now=NOW, audit_log_path=str(audit_path),
        )
        assert state.entries["uid-1"].disposition == "merged_after_rework", enum_value


async def test_forgejo_end_to_end_threads_forge_type_from_config(
    tmp_path: Path,
) -> None:
    """End-to-end forgejo pin: a forge_type='forgejo' client → the digest
    parses the Forgejo timeline shape (PR discovery) AND accepts
    REQUEST_CHANGES. Proves forge_type threads config → client →
    _check_one_ticket_outcome → _first_cross_referenced_pr."""
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(days=2),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(days=2),
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        forge_type="forgejo",
        timelines={7: [_xref_event_forgejo(456)]},
        prs={456: {"state": "closed", "merged_at": "2026-06-11T00:00:00Z"}},
        reviews={456: [{"state": "REQUEST_CHANGES"}]},
    )
    await check_ticket_outcomes(
        state, fake, now=NOW, audit_log_path=str(audit_path),
    )
    entry = state.entries["uid-1"]
    assert entry.pr_number == 456            # forgejo timeline parsed
    assert entry.disposition == "merged_after_rework"  # REQUEST_CHANGES accepted


async def test_check_outcomes_closed_unmerged(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(days=2),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(days=2),
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        timelines={7: [_xref_event(456)]},
        prs={456: {"state": "closed", "merged_at": None}},
    )
    changed = await check_ticket_outcomes(
        state, fake, now=NOW, audit_log_path=str(audit_path),
    )
    assert changed == 1
    entry = state.entries["uid-1"]
    assert entry.disposition == "closed_unmerged"
    assert entry.pr_state == "closed"
    # No latency on unmerged outcomes.
    assert entry.ticket_to_pr_latency_days is None
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- GH#7 → PR#456 CLOSED unmerged" in out


async def test_check_outcomes_stalled_after_three_nights(
    tmp_path: Path,
) -> None:
    """No PR + issue >= 3 nights old → stalled; a fresh issue stays ''."""
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-old": TicketIntakeEntry(
            recorded_at=_ago(days=4),
            kalle_relpath="ticket/Old.md",
            issue_number=7,
            issue_created_at=_ago(days=4),
        ),
        "uid-new": TicketIntakeEntry(
            recorded_at=_ago(days=1),
            kalle_relpath="ticket/New.md",
            issue_number=8,
            issue_created_at=_ago(days=1),
        ),
    })
    fake = FakeDigestGitHubClient(audit_path)  # empty timelines — no PRs
    changed = await check_ticket_outcomes(
        state, fake, now=NOW, audit_log_path=str(audit_path),
    )
    assert changed == 1  # only the stalled flip
    assert state.entries["uid-old"].disposition == "stalled"
    assert state.entries["uid-new"].disposition == ""
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- GH#7 → no PR yet (issue open 4 nights) — stalled" in out
    assert "- GH#8 → no PR yet (issue open 1 night)" in out
    assert "(issue open 1 night) — stalled" not in out


async def test_terminal_latch_skips_queries_and_render_latches_out(
    tmp_path: Path,
) -> None:
    """Terminal dispositions are never re-queried; their loud line
    renders only within 24h of the flip; the scoreboard keeps them."""
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(days=5),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(days=5),
            ticket_type="bug",
            pr_number=456,
            pr_state="merged",
            disposition="merged_clean",
            ticket_to_pr_latency_days=2.1,
            outcome_checked_at=_ago(days=3),
        ),
    })
    fake = FakeDigestGitHubClient(audit_path)
    changed = await check_ticket_outcomes(
        state, fake, now=NOW, audit_log_path=str(audit_path),
    )
    assert changed == 0
    assert fake.timeline_calls == []
    assert fake.pr_get_calls == []
    assert fake.pr_reviews_calls == []
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- GH#7" not in out  # latched out of per-ticket lines
    assert "- Auto-fix scoreboard: bug 1/1 merged" in out


async def test_stalled_upgrades_to_merged_clean(tmp_path: Path) -> None:
    """stalled is NON-terminal: a later merged PR upgrades it."""
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(days=5),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(days=5),
            disposition="stalled",
            outcome_checked_at=_ago(days=1),
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        timelines={7: [_xref_event(456)]},
        prs={456: {"state": "closed", "merged_at": _ago(hours=5)}},
        reviews={456: []},
    )
    changed = await check_ticket_outcomes(
        state, fake, now=NOW, audit_log_path=str(audit_path),
    )
    assert changed == 1
    assert state.entries["uid-1"].disposition == "merged_clean"
    assert fake.timeline_calls == [7]  # stalled entries DO keep checking


async def test_known_pr_skips_timeline_discovery(tmp_path: Path) -> None:
    """Once pr_number is in state, checks go straight to pr_get."""
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(days=1),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(days=1),
            pr_number=456,
            pr_state="open",
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        # A timeline call would raise — proving it is never made.
        timeline_exc={7: RuntimeError("timeline must not be queried")},
        prs={456: {"state": "open", "merged_at": None}},
    )
    changed = await check_ticket_outcomes(
        state, fake, now=NOW, audit_log_path=str(audit_path),
    )
    assert changed == 0
    assert fake.timeline_calls == []
    assert fake.pr_get_calls == [456]


async def test_per_ticket_failure_contained(tmp_path: Path) -> None:
    """One issue's API failure logs + skips that entry only."""
    audit_path = tmp_path / "audit.jsonl"
    state = _mem_state(tmp_path, {
        "uid-a": TicketIntakeEntry(
            recorded_at=_ago(days=1),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(days=1),
        ),
        "uid-b": TicketIntakeEntry(
            recorded_at=_ago(days=1),
            kalle_relpath="ticket/B.md",
            issue_number=8,
            issue_created_at=_ago(days=1),
        ),
    })
    fake = FakeDigestGitHubClient(
        audit_path,
        timeline_exc={7: RuntimeError("github 500")},
        timelines={8: [_xref_event(460)]},
        prs={460: {"state": "closed", "merged_at": _ago(hours=2)}},
        reviews={460: []},
    )
    with structlog.testing.capture_logs() as captured:
        changed = await check_ticket_outcomes(
            state, fake, now=NOW, audit_log_path=str(audit_path),
        )
    assert changed == 1
    assert state.entries["uid-b"].disposition == "merged_clean"
    # Failed entry untouched — outcome_checked_at stays empty (stale
    # visible) and disposition unchanged.
    assert state.entries["uid-a"].disposition == ""
    assert state.entries["uid-a"].outcome_checked_at == ""
    failed = _log_events(
        captured, "kalle.digest.ticket_pipeline_outcome_check_failed",
    )
    assert len(failed) == 1
    assert failed[0]["uid"] == "uid-a"
    assert failed[0]["error_type"] == "RuntimeError"


def test_scoreboard_split_by_type_with_unspecified_bucket(
    tmp_path: Path,
) -> None:
    """Scoreboard aggregates ALL terminal entries by ticket_type;
    pre-c5 entries without the state field bucket as 'unspecified'."""
    old_check = _ago(days=3)  # all flips old → latched out of lines
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(days=9), kalle_relpath="t/1.md",
            issue_number=1, ticket_type="bug",
            pr_number=10, pr_state="merged",
            disposition="merged_clean", outcome_checked_at=old_check,
        ),
        "uid-2": TicketIntakeEntry(
            recorded_at=_ago(days=8), kalle_relpath="t/2.md",
            issue_number=2, ticket_type="bug",
            pr_number=11, pr_state="closed",
            disposition="closed_unmerged", outcome_checked_at=old_check,
        ),
        "uid-3": TicketIntakeEntry(
            recorded_at=_ago(days=7), kalle_relpath="t/3.md",
            issue_number=3, ticket_type="enhancement",
            pr_number=12, pr_state="merged",
            disposition="merged_after_rework", outcome_checked_at=old_check,
        ),
        "uid-4": TicketIntakeEntry(
            recorded_at=_ago(days=6), kalle_relpath="t/4.md",
            issue_number=4, ticket_type="",  # pre-c5 entry
            pr_number=13, pr_state="closed",
            disposition="closed_unmerged", outcome_checked_at=old_check,
        ),
        "uid-5": TicketIntakeEntry(  # non-terminal — not on scoreboard
            recorded_at=_ago(hours=2), kalle_relpath="t/5.md",
            issue_number=5, ticket_type="bug",
            issue_created_at=_ago(hours=1),
        ),
    })
    out = render_ticket_pipeline_section(state, now=NOW)
    assert (
        "- Auto-fix scoreboard: bug 1/2 merged · "
        "enhancement 1/1 merged · unspecified 0/1 merged"
    ) in out


def test_scoreboard_no_outcomes_yet(tmp_path: Path) -> None:
    state = _mem_state(tmp_path, {
        "uid-1": TicketIntakeEntry(
            recorded_at=_ago(hours=2),
            kalle_relpath="ticket/A.md",
            issue_number=7,
            issue_created_at=_ago(hours=1),
        ),
    })
    out = render_ticket_pipeline_section(state, now=NOW)
    assert "- Auto-fix scoreboard: no outcomes yet." in out


async def test_section_failure_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§-boundary: ANY internal failure renders the unavailable line
    and logs — never raises into the digest assembler."""
    def _boom(path: Any) -> Any:
        raise RuntimeError("state layer exploded")

    monkeypatch.setattr(
        "alfred.transport.ticket_intake.TicketIntakeState.load", _boom,
    )
    raw = _raw_with_state(tmp_path / "state.json")
    with structlog.testing.capture_logs() as captured:
        out = await assemble_ticket_pipeline_section(raw, now=NOW)
    assert out == "Ticket pipeline: section unavailable (RuntimeError)"
    failed = _log_events(
        captured, "kalle.digest.ticket_pipeline_section_failed",
    )
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"


async def test_assemble_end_to_end_saves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full section assembly with a (faked) configured github client:
    outcomes are checked, the state file is atomically re-saved with
    the effectiveness fields, and the audit row lands."""
    state_path = tmp_path / "ticket_intake_state.json"
    audit_path = tmp_path / "github_ops_audit.jsonl"
    state = TicketIntakeState(path=state_path)
    state.entries["uid-1"] = TicketIntakeEntry(
        recorded_at="2026-06-09T00:00:00+00:00",
        kalle_relpath="ticket/A.md",
        issue_number=7,
        issue_created_at="2026-06-09T00:00:00+00:00",
        ticket_type="bug",
    )
    state.save()

    fake = FakeDigestGitHubClient(
        audit_path,
        timelines={7: [_xref_event(456)]},
        prs={456: {"state": "closed", "merged_at": "2026-06-11T02:24:00Z"}},
        reviews={456: []},
    )

    def _fake_build(raw: dict[str, Any], instance_name: str) -> Any:
        assert instance_name == "KAL-LE"
        return fake

    monkeypatch.setattr(
        "alfred.integrations.github_ops.build_github_client", _fake_build,
    )
    raw = _raw_with_state(state_path, github=True)
    out = await assemble_ticket_pipeline_section(raw, now=NOW)

    assert "- GH#7 → PR#456 MERGED ✓ (2.1d)" in out
    assert "- Auto-fix scoreboard: bug 1/1 merged" in out
    # State persisted to disk (atomic save) with the captured fields.
    reloaded = TicketIntakeState.load(state_path)
    entry = reloaded.entries["uid-1"]
    assert entry.pr_number == 456
    assert entry.pr_state == "merged"
    assert entry.disposition == "merged_clean"
    assert entry.ticket_to_pr_latency_days == pytest.approx(2.1)
    assert entry.outcome_checked_at == NOW.isoformat()
    # The summarizing audit row landed at the client's audit path.
    rows = read_github_audit(audit_path)
    assert len(rows) == 1
    assert rows[0]["disposition"] == "merged_clean"

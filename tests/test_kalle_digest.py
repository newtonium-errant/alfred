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
from datetime import date
from pathlib import Path

import pytest

from alfred.brief.kalle_digest import (
    DigestData,
    assemble_digest,
    gather_digest_data,
    read_bash_exec_log,
    read_bit_state,
    read_git_commits,
    read_instructor_state,
    render_digest_markdown,
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

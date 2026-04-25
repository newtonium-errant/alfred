"""Deterministic digest writer — section-level edge cases.

Each section must render even when empty (with "None this week.")
because idle and broken must stay distinguishable. The cross-project
patterns section must always emit the LLM-TODO HTML comment so the
future synthesis layer has its slot.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.reviews.store import REVIEWS_SUBPATH
from alfred.digest.writer import (
    Promotion,
    Recurrence,
    _parse_rename_log,
    build_payload,
    collect_decisions,
    collect_open_questions,
    collect_promotions,
    collect_recurrences,
    render,
    write_digest,
)


_HUMAN_REVIEW = dedent(
    """\
    ---
    type: review
    from: origin
    to: teams/alfred
    date: 2026-04-16
    subject: classification-temperature-drift
    decision: promote
    confidence: high
    ---

    Body.
    """
)


def _kalle_review(
    *,
    status: str,
    created: str,
    addressed: str | None = None,
    topic: str = "topic",
) -> str:
    lines = [
        "---",
        "type: review",
        "author: kal-le",
        "project: alfred",
        f"status: {status}",
        f"created: '{created}'",
    ]
    if addressed:
        lines.append(f"addressed: '{addressed}'")
    lines.extend([
        f"topic: {topic}",
        "---",
        "",
        f"Body for {topic}.",
        "",
    ])
    return "\n".join(lines)


@pytest.fixture
def two_projects(tmp_path: Path) -> dict[str, Path]:
    """Two project vaults (each with a reviews dir) under tmp."""
    p1 = tmp_path / "proj-a"
    p2 = tmp_path / "proj-b"
    (p1 / REVIEWS_SUBPATH).mkdir(parents=True)
    (p2 / REVIEWS_SUBPATH).mkdir(parents=True)
    return {"proj-a": p1, "proj-b": p2}


def _write(vault: Path, name: str, content: str) -> Path:
    path = vault / REVIEWS_SUBPATH / name
    path.write_text(content, encoding="utf-8")
    return path


def test_decisions_filter_window_and_skip_human(two_projects) -> None:
    p_a = two_projects["proj-a"]
    p_b = two_projects["proj-b"]
    _write(p_a, "human.md", _HUMAN_REVIEW)
    _write(p_a, "in-window.md", _kalle_review(
        status="addressed",
        created="2026-04-10T12:00:00+00:00",
        addressed="2026-04-22T12:00:00+00:00",
        topic="addressed in window",
    ))
    _write(p_a, "out-of-window.md", _kalle_review(
        status="addressed",
        created="2026-03-10T12:00:00+00:00",
        addressed="2026-03-15T12:00:00+00:00",
        topic="too old",
    ))
    _write(p_b, "open-skip.md", _kalle_review(
        status="open",
        created="2026-04-20T12:00:00+00:00",
        topic="should skip — open",
    ))
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    decisions, project_map = collect_decisions(
        two_projects,
        window_start=today.replace(day=18),
        window_end=today,
    )
    filenames = [r.filename for r in decisions]
    assert filenames == ["in-window.md"]
    assert project_map["in-window.md"] == "proj-a"


def test_decisions_sort_most_recent_first(two_projects) -> None:
    p = two_projects["proj-a"]
    _write(p, "older.md", _kalle_review(
        status="addressed",
        created="2026-04-19T00:00:00+00:00",
        addressed="2026-04-20T00:00:00+00:00",
        topic="older",
    ))
    _write(p, "newer.md", _kalle_review(
        status="addressed",
        created="2026-04-21T00:00:00+00:00",
        addressed="2026-04-23T00:00:00+00:00",
        topic="newer",
    ))
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    decisions, _ = collect_decisions(
        two_projects,
        window_start=today.replace(day=18),
        window_end=today,
    )
    assert [r.filename for r in decisions] == ["newer.md", "older.md"]


def test_open_questions_no_window(two_projects) -> None:
    p = two_projects["proj-a"]
    _write(p, "ancient.md", _kalle_review(
        status="open", created="2025-09-01T12:00:00+00:00", topic="ancient",
    ))
    _write(p, "yesterday.md", _kalle_review(
        status="open", created="2026-04-24T12:00:00+00:00", topic="yesterday",
    ))
    _write(p, "addressed.md", _kalle_review(
        status="addressed",
        created="2026-04-10T12:00:00+00:00",
        addressed="2026-04-12T12:00:00+00:00",
        topic="addressed — should skip",
    ))
    open_q, _ = collect_open_questions(two_projects)
    filenames = [r.filename for r in open_q]
    assert "ancient.md" in filenames
    assert "yesterday.md" in filenames
    assert "addressed.md" not in filenames
    assert filenames.index("ancient.md") < filenames.index("yesterday.md")


def test_collect_recurrences_finds_emdash_sibling(two_projects) -> None:
    p = two_projects["proj-a"]
    _write(p, "2026-04-22-bug.md", _kalle_review(
        status="open", created="2026-04-22T00:00:00+00:00", topic="bug",
    ))
    _write(
        p, "2026-04-22-bug—claude-disagreement.md",
        "Disagreement body.\n",
    )
    _write(p, "2026-04-23-other.md", _kalle_review(
        status="open", created="2026-04-23T00:00:00+00:00", topic="other",
    ))
    rec = collect_recurrences(two_projects)
    assert len(rec) == 1
    r = rec[0]
    assert r.project == "proj-a"
    assert r.filename == "2026-04-22-bug.md"
    assert "claude-disagreement" in r.disagreement_filename


def test_collect_recurrences_finds_doubledash_sibling(two_projects) -> None:
    """Double-dash fallback for keyboards that don't produce em-dash."""
    p = two_projects["proj-a"]
    _write(p, "2026-04-22-bug.md", _kalle_review(
        status="open", created="2026-04-22T00:00:00+00:00", topic="bug",
    ))
    _write(p, "2026-04-22-bug--claude-disagreement.md", "body")
    rec = collect_recurrences(two_projects)
    assert len(rec) == 1


def test_collect_recurrences_empty(two_projects) -> None:
    assert collect_recurrences(two_projects) == []


def test_parse_rename_log_extracts_canonical_destinations() -> None:
    raw = dedent("""\
        COMMIT abc12345 Promote pattern X to canonical
        R092	teams/alfred/candidate-gotchas/x.md	architecture/x.md
        COMMIT def56789 Promote pattern Y to stack
        R100	teams/alfred/candidate-stack/y.md	stack/y.md
        COMMIT 9999aaaa Move outside canonical
        R055	docs/old.md	docs/new.md
        """)
    promos = _parse_rename_log("aftermath-lab", raw)
    sources = [p.source for p in promos]
    destinations = [p.destination for p in promos]
    assert "architecture/x.md" in destinations
    assert "stack/y.md" in destinations
    assert "docs/new.md" not in destinations
    assert all(p.repo == "aftermath-lab" for p in promos)
    sha_map = {p.source: p.sha for p in promos}
    assert sha_map["teams/alfred/candidate-gotchas/x.md"] == "abc12345"


def test_collect_promotions_skips_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    promotions = collect_promotions(
        {"x": not_a_repo},
        window_start=today.replace(day=18),
        window_end=today,
    )
    assert promotions == []


def test_render_empty_payload_has_every_section() -> None:
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(project_paths={}, today=today, window_days=7)
    body = render(payload)
    assert "# Weekly digest — 2026-04-25" in body
    assert "## Decisions made" in body
    assert "## Promotions to canonical" in body
    assert "## Open questions" in body
    assert "## Cross-project patterns" in body
    assert "## Recurrences" in body
    assert body.count("None this week.") >= 4
    assert (
        "<!-- TODO: LLM synthesis layer not yet implemented -->" in body
    )
    assert body.endswith("\n")


def test_render_populated_payload(two_projects) -> None:
    p = two_projects["proj-a"]
    _write(p, "decision.md", _kalle_review(
        status="addressed",
        created="2026-04-19T00:00:00+00:00",
        addressed="2026-04-23T12:34:00+00:00",
        topic="A decision",
    ))
    _write(p, "open-q.md", _kalle_review(
        status="open",
        created="2026-04-20T00:00:00+00:00",
        topic="An open question",
    ))
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths=two_projects, today=today, window_days=7,
    )
    body = render(payload)
    assert "A decision" in body
    assert "An open question" in body
    assert "decision.md" in body
    assert "open-q.md" in body
    assert "<!-- TODO: LLM synthesis layer not yet implemented -->" in body


def test_render_recurrence_section_lists_disagreement(two_projects) -> None:
    p = two_projects["proj-a"]
    _write(p, "2026-04-22-bug.md", _kalle_review(
        status="open", created="2026-04-22T00:00:00+00:00", topic="bug",
    ))
    _write(p, "2026-04-22-bug—claude-disagreement.md", "body")
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths=two_projects, today=today, window_days=7,
    )
    body = render(payload)
    assert "## Recurrences" in body
    assert "claude-disagreement" in body
    assert "bug" in body


def test_write_digest_persists_with_weekly_prefix(two_projects, tmp_path) -> None:
    output_dir = tmp_path / "digests"
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    out_path, body, payload = write_digest(
        output_dir=output_dir,
        project_paths=two_projects,
        today=today,
        window_days=7,
    )
    assert out_path.name == "2026-04-25-weekly-digest.md"
    on_disk = out_path.read_text(encoding="utf-8")
    assert on_disk == body
    assert "Window: 2026-04-18 → 2026-04-25" in body


def test_collect_promotions_lab_repo_smoke() -> None:
    """Sanity smoke: live aftermath-lab clone returns a list (no crash).

    The query may legitimately be empty — promotions in this codebase
    so far have been added-files, not git renames. This test pins the
    "no exception, returns list" contract; the empty result is expected.
    """
    repo = Path.home() / "aftermath-lab"
    if not (repo / ".git").exists():
        pytest.skip("aftermath-lab not available")
    today = datetime.now(timezone.utc)
    out = collect_promotions(
        {"aftermath-lab": repo},
        window_start=today.replace(year=2026, month=1, day=1),
        window_end=today,
    )
    assert isinstance(out, list)
    for p in out:
        assert p.repo == "aftermath-lab"
        assert p.destination.startswith(("stack/", "principles/", "architecture/"))


def test_collect_promotions_handles_subprocess_failure(monkeypatch, tmp_path) -> None:
    """git command failure is a logged skip, not a crash."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # so the existence gate passes

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(
            args=a, returncode=128, stdout="", stderr="not a git repository",
        )

    monkeypatch.setattr("alfred.digest.writer.subprocess.run", fake_run)
    today = datetime.now(timezone.utc)
    out = collect_promotions(
        {"fake": repo},
        window_start=today.replace(year=2026, month=1, day=1),
        window_end=today,
    )
    assert out == []

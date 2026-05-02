"""Deterministic digest writer — section-level edge cases.

Each section must render even when empty. Sections 1, 2, and 5 emit an
explicit "what we checked" line plus a "last detected" pointer when
empty so a quiet week is unambiguously distinct from a broken pipeline.
Sections 3 and 4 keep their existing empty behavior (open-questions
has no window; cross-project is a literal LLM-TODO marker).

The cross-project patterns section must always emit the LLM-TODO HTML
comment so the future synthesis layer has its slot.
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
    build_payload,
    collect_decisions,
    collect_open_questions,
    collect_promotions,
    collect_recurrences,
    find_last_addressed,
    find_last_promotion,
    find_last_recurrence,
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


def _init_repo(repo: Path) -> None:
    """Initialize a git repo so the .git existence gate passes and
    git log/show invocations succeed. Uses a deterministic identity so
    the working tree state doesn't depend on host gitconfig."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
    )


def _commit(
    repo: Path,
    *,
    files: dict[str, str],
    subject: str,
    body: str = "",
    when: str | None = None,
) -> str:
    """Create files (intermediate dirs as needed), commit, return SHA.

    ``when`` is forwarded as both author and committer date so window
    filtering is testable.
    """
    for rel, content in files.items():
        full = repo / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    env = {}
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    msg = subject if not body else f"{subject}\n\n{body}"
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", msg],
        check=True,
        env={**dict(__import__("os").environ), **env},
    )
    rev = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return rev.stdout.strip()


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


def test_promotions_and_gate_keyword_only_does_not_surface(tmp_path) -> None:
    """Keyword in subject but no canonical-dir ADD: must NOT surface.

    Pinning the AND-gate behavior — the prior --diff-filter=R approach
    missed adds entirely; this one must reject keyword-only matches.
    """
    repo = tmp_path / "lab"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        files={"docs/readme.md": "init"},
        subject="seed",
        when="2026-04-10T00:00:00+00:00",
    )
    # keyword in subject but adds land in docs/, not architecture/etc.
    _commit(
        repo,
        files={"docs/notes.md": "more notes"},
        subject="Expand standing orders with curated context",
        when="2026-04-22T00:00:00+00:00",
    )
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    out = collect_promotions(
        {"lab": repo},
        window_start=today.replace(day=18),
        window_end=today,
    )
    assert out == []


def test_promotions_and_gate_full_match_surfaces(tmp_path) -> None:
    """Keyword AND a canonical-dir ADD: surfaces with files attached."""
    repo = tmp_path / "lab"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        files={"docs/readme.md": "init"},
        subject="seed",
        when="2026-04-10T00:00:00+00:00",
    )
    sha = _commit(
        repo,
        files={
            "architecture/llm-gotchas.md": "gotcha body",
            "architecture/another-pattern.md": "pattern body",
            "docs/aside.md": "aside",
        },
        subject="Promote LLM gotchas to canonical",
        when="2026-04-22T00:00:00+00:00",
    )
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    out = collect_promotions(
        {"lab": repo},
        window_start=today.replace(day=18),
        window_end=today,
    )
    assert len(out) == 1
    p = out[0]
    assert p.repo == "lab"
    assert p.sha == sha[:7]
    assert "Promote" in p.subject
    assert "architecture/llm-gotchas.md" in p.files
    assert "architecture/another-pattern.md" in p.files
    # Non-canonical sibling is filtered out of the file list.
    assert "docs/aside.md" not in p.files


def test_promotions_canonical_only_no_keyword_does_not_surface(tmp_path) -> None:
    """Canonical-dir ADD with no keyword in subject/body: not a promotion."""
    repo = tmp_path / "lab"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        files={"architecture/x.md": "x body"},
        subject="ship something unrelated",
        when="2026-04-22T00:00:00+00:00",
    )
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    out = collect_promotions(
        {"lab": repo},
        window_start=today.replace(day=18),
        window_end=today,
    )
    assert out == []


def test_promotions_keyword_in_body_matches(tmp_path) -> None:
    """Keyword may live in the body, not just the subject."""
    repo = tmp_path / "lab"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        files={"stack/railway.md": "railway notes"},
        subject="Add railway notes",
        body="Canonicalized after a week of notes settling.",
        when="2026-04-22T00:00:00+00:00",
    )
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    out = collect_promotions(
        {"lab": repo},
        window_start=today.replace(day=18),
        window_end=today,
    )
    assert len(out) == 1
    assert "stack/railway.md" in out[0].files


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


def test_collect_promotions_lab_repo_smoke() -> None:
    """Sanity smoke: live aftermath-lab clone returns a list (no crash)."""
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
        assert any(
            f.startswith(("architecture/", "stack/", "principles/"))
            for f in p.files
        )


def test_render_empty_payload_has_every_section() -> None:
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(project_paths={}, today=today, window_days=7)
    body = render(payload)
    assert "# Weekly digest — 2026-04-25" in body
    assert "## Decisions made" in body
    assert "## Promotions to canonical" in body
    assert "## Open questions" in body
    # Section 4 renamed from "Cross-project patterns" to "Cross-arc
    # patterns" 2026-05-01 — the corpus is single-project-dominant, so
    # cross-PROJECT framing is wrong v1. Cross-PROJECT returns when
    # V.E.R.A./STAY-C launch.
    assert "## Cross-arc patterns" in body
    assert "## Cross-project patterns" not in body
    assert "## Recurrences" in body
    # Empty-state strings (sections 1, 2, 5) — explicit "checked X, last Y".
    assert "No KAL-LE reviews flipped to addressed" in body
    assert "Last addressed: never." in body
    assert "No canonical promotions detected" in body
    assert "Last detected: never." in body
    assert "No recurring topics with sibling disagreement archives" in body
    assert "Last recurrence: never." in body
    # Section 5 must NOT use "last N days" framing (unbounded).
    assert "in the last 7 days. Checked:" not in body.split("## Recurrences", 1)[1]
    # Section 3 keeps its existing empty text; section 4 now renders an
    # explicit empty-state line (was a literal LLM-TODO marker before
    # Phase 2 wired the synthesis ranker).
    assert "None this week." in body
    assert "No cross-arc patterns surfaced this week." in body
    assert "<!-- TODO: LLM synthesis layer not yet implemented -->" not in body
    assert body.endswith("\n")


def _write_synthesis_record(
    vault: Path,
    *,
    record_type: str,
    name: str,
    created: str,
    sources: list[str],
    entities: list[str],
    claim: str = "Some claim text.",
) -> Path:
    """Write a minimal synthesis-shaped record into ``vault/<type>/<name>.md``."""
    type_dir = vault / record_type
    type_dir.mkdir(parents=True, exist_ok=True)
    head = ["---", f"name: {name}", f"type: {record_type}", f"claim: {claim}",
            f"created: '{created}'"]
    if sources:
        head.append("source_links:")
        for s in sources:
            head.append(f"  - '{s}'")
    if entities:
        head.append("entity_links:")
        for e in entities:
            head.append(f"  - '{e}'")
    head.append("---")
    path = type_dir / f"{name}.md"
    path.write_text("\n".join(head) + "\n\nbody\n", encoding="utf-8")
    return path


def test_render_section_4_emits_ranker_bullets(tmp_path: Path) -> None:
    """When the synthesis ranker returns records, section 4 renders one
    bullet per record with the title, source list, and claim summary."""
    vault = tmp_path / "vault"
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    _write_synthesis_record(
        vault, record_type="synthesis", name="Pattern Alpha",
        created="2026-04-24",
        sources=["[[session/Source A]]", "[[session/Source B]]"],
        entities=["[[project/Alfred]]"],
        claim="Cross-source pattern carries the most signal.",
    )
    _write_synthesis_record(
        vault, record_type="decision", name="Decision Beta",
        created="2026-04-23",
        sources=["[[session/Source C]]"],
        entities=["[[project/Alfred]]"],
        claim="Single-source decision lower signal.",
    )
    payload = build_payload(
        project_paths={}, today=today, window_days=7,
        synthesis_vault=vault, synthesis_top_n=5,
    )
    body = render(payload)

    # Section 4 header renamed; empty-state line absent because we have
    # ranked records.
    assert "## Cross-arc patterns" in body
    assert "No cross-arc patterns surfaced this week." not in body
    # Both records render as bullets with **title** prefix.
    assert "- **Pattern Alpha**" in body
    assert "- **Decision Beta**" in body
    # Source wikilinks are emitted in the bullet's parenthesized list.
    assert "[[session/Source A]]" in body
    assert "[[session/Source B]]" in body
    # Claim falls into the post-em-dash slot.
    assert "Cross-source pattern carries the most signal." in body
    # Pattern Alpha (synthesis, 2 sources) outranks Decision Beta
    # (decision, 1 source) — verify ordering by index.
    section4 = body.split("## Cross-arc patterns", 1)[1].split(
        "## Recurrences", 1,
    )[0]
    assert section4.index("Pattern Alpha") < section4.index("Decision Beta")


def test_render_section_4_top_n_zero_disables_ranker(tmp_path: Path) -> None:
    """``synthesis_top_n=0`` skips the ranker call → empty-state line."""
    vault = tmp_path / "vault"
    _write_synthesis_record(
        vault, record_type="synthesis", name="Would Rank",
        created="2026-04-24",
        sources=["[[session/x]]"],
        entities=["[[project/Alfred]]"],
        claim="Should not appear.",
    )
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths={}, today=today, window_days=7,
        synthesis_vault=vault, synthesis_top_n=0,
    )
    body = render(payload)
    assert "No cross-arc patterns surfaced this week." in body
    assert "Would Rank" not in body
    assert payload.cross_arc_patterns == []


def test_render_section_4_no_synthesis_vault_empty_state(tmp_path: Path) -> None:
    """``synthesis_vault=None`` (default) → empty-state line, ranker not called."""
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(project_paths={}, today=today, window_days=7)
    assert payload.cross_arc_patterns == []
    body = render(payload)
    assert "No cross-arc patterns surfaced this week." in body


def test_render_empty_decisions_names_last_addressed(two_projects) -> None:
    """When current window is empty but there's a prior addressed
    review, the empty-state must surface project@filename on date."""
    p = two_projects["proj-a"]
    _write(p, "ancient-decision.md", _kalle_review(
        status="addressed",
        created="2025-12-01T00:00:00+00:00",
        addressed="2025-12-15T00:00:00+00:00",
        topic="ancient decision",
    ))
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths=two_projects, today=today, window_days=7,
    )
    body = render(payload)
    assert "No KAL-LE reviews flipped to addressed in the last 7 days." in body
    assert "Checked: proj-a, proj-b" in body
    assert "Last addressed: proj-a@ancient-decision.md on 2025-12-15." in body


def test_recurrences_unbounded_ancient_topic_surfaces(two_projects) -> None:
    """Ancient recurrence (with disagreement sibling) surfaces regardless
    of digest window. Recurrences are now unbounded, like Open Questions.

    The prior behavior gated by ``window_start``/``window_end`` would
    have hidden a 6-month-old disagreement; this test pins the new
    contract that timestamp doesn't matter for inclusion.
    """
    p = two_projects["proj-a"]
    _write(p, "2025-11-01-throttling.md", _kalle_review(
        status="addressed",
        created="2025-11-01T00:00:00+00:00",
        addressed="2025-11-05T00:00:00+00:00",
        topic="throttling",
    ))
    _write(p, "2025-11-01-throttling—claude-disagreement.md", "body")
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths=two_projects, today=today, window_days=7,
    )
    # The ancient topic must now appear in the populated section, not
    # be punted to the "Last recurrence" empty-state pointer.
    assert len(payload.recurrences) == 1
    assert payload.recurrences[0].filename == "2025-11-01-throttling.md"
    body = render(payload)
    assert "throttling" in body
    assert "claude-disagreement" in body
    # Section header still present with content listed under it.
    assert "## Recurrences" in body


def test_render_empty_recurrences_uses_unbounded_phrasing(two_projects) -> None:
    """Empty-state line: no "last N days" framing; "across <projects>"
    + "Last recurrence: <X>" stays so the explicit-empty signal is
    preserved (falls back to "never" when no recurrence on record)."""
    # No reviews, no disagreement archives anywhere → section is empty.
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths=two_projects, today=today, window_days=7,
    )
    body = render(payload)
    # Isolate the recurrences section so we don't accidentally match
    # phrasing from an earlier section.
    rec_section = body.split("## Recurrences", 1)[1]
    assert "No recurring topics with sibling disagreement archives" in rec_section
    # Drop "in the last N days" framing entirely.
    assert "in the last" not in rec_section
    assert "last 7 days" not in rec_section
    # Use "across" + project list.
    assert "across proj-a, proj-b" in rec_section
    # Keep "Last recurrence:" so empty-state stays explicit.
    assert "Last recurrence: never." in rec_section


def test_render_empty_promotions_names_last_promotion(tmp_path) -> None:
    """A prior canonical promotion (older than the window) must show
    up in the "Last detected" pointer when this window is quiet."""
    repo = tmp_path / "lab"
    repo.mkdir()
    _init_repo(repo)
    sha = _commit(
        repo,
        files={"principles/old-principle.md": "ancient"},
        subject="Promote old principle",
        when="2025-12-01T00:00:00+00:00",
    )
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths={"lab": repo}, today=today, window_days=7,
    )
    body = render(payload)
    assert "No canonical promotions detected in the last 7 days." in body
    assert "/i + ADDs in {architecture,stack,principles}/" in body
    assert "Checked: keyword /\\b(promot|canonical|curat)/i" in body
    assert f"Last detected: lab@{sha[:7]} on 2025-12-01." in body


def test_render_populated_promotions_uses_file_list_format(tmp_path) -> None:
    """In-window promotions render as `repo@sha — subject` plus an
    indented file list."""
    repo = tmp_path / "lab"
    repo.mkdir()
    _init_repo(repo)
    sha = _commit(
        repo,
        files={
            "architecture/llm-gotchas.md": "gotcha",
            "architecture/another.md": "another",
        },
        subject="Promote pattern X to canonical",
        when="2026-04-22T00:00:00+00:00",
    )
    today = datetime(2026, 4, 25, tzinfo=timezone.utc)
    payload = build_payload(
        project_paths={"lab": repo}, today=today, window_days=7,
    )
    body = render(payload)
    assert f"- lab@{sha[:7]} — Promote pattern X to canonical" in body
    assert "  - architecture/llm-gotchas.md" in body
    assert "  - architecture/another.md" in body


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
    # Section 4 was the literal LLM-TODO marker pre-Phase-2; now it
    # renders the empty-state line because no synthesis_vault was passed.
    assert "## Cross-arc patterns" in body
    assert "No cross-arc patterns surfaced this week." in body


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


def test_find_last_addressed_returns_none_when_no_addressed(two_projects) -> None:
    p = two_projects["proj-a"]
    _write(p, "open.md", _kalle_review(
        status="open", created="2026-04-22T00:00:00+00:00", topic="open",
    ))
    assert find_last_addressed(two_projects) is None


def test_find_last_promotion_returns_none_for_clean_repo(tmp_path) -> None:
    repo = tmp_path / "lab"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        files={"docs/x.md": "x"},
        subject="seed",
        when="2026-04-22T00:00:00+00:00",
    )
    assert find_last_promotion({"lab": repo}) is None


def test_find_last_recurrence_returns_none_when_no_disagreement(two_projects) -> None:
    p = two_projects["proj-a"]
    _write(p, "a.md", _kalle_review(
        status="open", created="2026-04-22T00:00:00+00:00", topic="thing",
    ))
    _write(p, "b.md", _kalle_review(
        status="open", created="2026-04-22T00:00:00+00:00", topic="thing",
    ))
    # Topic recurs but no sibling disagreement archive → no recurrence.
    assert find_last_recurrence(two_projects) is None

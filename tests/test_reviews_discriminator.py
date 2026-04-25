"""Frontmatter discriminator + storage operations for ``alfred reviews``.

The shared ``teams/alfred/reviews/`` directory holds two coexisting
review formats — KAL-LE-authored (``author: kal-le``) and the existing
human-authored ``from``/``to``/``date``/``subject``/``in_reply_to``
shape. The reviews CLI MUST never list, read, or mutate a non-KAL-LE
file. The tests here pin that invariant.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alfred.reviews.store import (
    KALLE_AUTHOR,
    REVIEWS_SUBPATH,
    STATUS_ADDRESSED,
    STATUS_OPEN,
    ReviewsError,
    list_all_kalle_reviews_with_paths,
    list_reviews,
    mark_addressed,
    read_review,
    reviews_dir,
    slugify,
    write_review,
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

    ## Promote "Classification Temperature Drift" to Canonical
    Body content here.
    """
)


_KALLE_REVIEW_OPEN = dedent(
    """\
    ---
    type: review
    author: kal-le
    project: alfred
    status: open
    created: 2026-04-25T10:00:00+00:00
    topic: scheduler off-by-one
    ---

    Saw a one-tick drift in the scheduler.
    """
)


_KALLE_REVIEW_ADDRESSED = dedent(
    """\
    ---
    type: review
    author: kal-le
    project: alfred
    status: addressed
    created: 2026-04-22T09:00:00+00:00
    addressed: 2026-04-23T15:00:00+00:00
    topic: prior fix
    ---

    Reviewed and resolved.
    """
)


@pytest.fixture
def project_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "project"
    (vault / REVIEWS_SUBPATH).mkdir(parents=True)
    return vault


def _write_raw(vault: Path, name: str, content: str) -> Path:
    path = reviews_dir(vault) / name
    path.write_text(content, encoding="utf-8")
    return path


def test_slugify_basic() -> None:
    assert slugify("Hello World") == "hello-world"
    assert slugify("  multiple   spaces  ") == "multiple-spaces"
    assert slugify("Don't @ me") == "dont-me"
    assert slugify("") == "review"
    assert slugify("---") == "review"
    assert slugify("Mixed_CASE-and 99") == "mixed_case-and-99" or slugify("Mixed_CASE-and 99") == "mixedcase-and-99"


def test_write_creates_file_with_kalle_frontmatter(project_vault: Path) -> None:
    rec = write_review(
        project_vault,
        project="alfred",
        topic="Scheduler glitch",
        body="Saw a glitch.",
        today="2026-04-25",
        now_iso="2026-04-25T10:00:00+00:00",
    )
    assert rec.filename == "2026-04-25-scheduler-glitch.md"
    content = rec.abs_path.read_text(encoding="utf-8")
    assert f"author: {KALLE_AUTHOR}" in content
    assert "status: open" in content
    assert "project: alfred" in content
    assert "type: review" in content
    assert "topic: Scheduler glitch" in content
    assert "Saw a glitch." in content


def test_write_collision_appends_numeric_suffix(project_vault: Path) -> None:
    a = write_review(
        project_vault, project="alfred", topic="x", body="b1", today="2026-04-25",
    )
    b = write_review(
        project_vault, project="alfred", topic="x", body="b2", today="2026-04-25",
    )
    c = write_review(
        project_vault, project="alfred", topic="x", body="b3", today="2026-04-25",
    )
    assert a.filename == "2026-04-25-x.md"
    assert b.filename == "2026-04-25-x-2.md"
    assert c.filename == "2026-04-25-x-3.md"


def test_list_skips_human_authored_files(project_vault: Path) -> None:
    _write_raw(project_vault, "2026-04-16-origin-promote.md", _HUMAN_REVIEW)
    _write_raw(project_vault, "2026-04-25-kal-le-open.md", _KALLE_REVIEW_OPEN)
    records = list_reviews(project_vault, status="open")
    assert len(records) == 1
    assert records[0].filename == "2026-04-25-kal-le-open.md"
    assert records[0].frontmatter["author"] == KALLE_AUTHOR


def test_list_status_filter(project_vault: Path) -> None:
    _write_raw(project_vault, "open.md", _KALLE_REVIEW_OPEN)
    _write_raw(project_vault, "addressed.md", _KALLE_REVIEW_ADDRESSED)
    open_only = list_reviews(project_vault, status="open")
    addressed_only = list_reviews(project_vault, status="addressed")
    all_recs = list_reviews(project_vault, status="all")
    assert [r.filename for r in open_only] == ["open.md"]
    assert [r.filename for r in addressed_only] == ["addressed.md"]
    assert {r.filename for r in all_recs} == {"open.md", "addressed.md"}


def test_list_invalid_status_raises(project_vault: Path) -> None:
    with pytest.raises(ReviewsError):
        list_reviews(project_vault, status="bogus")


def test_list_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    vault = tmp_path / "no-reviews-dir-here"
    vault.mkdir()
    assert list_reviews(vault, status="all") == []


def test_read_human_authored_raises_loudly(project_vault: Path) -> None:
    _write_raw(project_vault, "human.md", _HUMAN_REVIEW)
    with pytest.raises(ReviewsError) as excinfo:
        read_review(project_vault, filename="human.md")
    msg = str(excinfo.value)
    assert KALLE_AUTHOR in msg
    assert "human.md" in msg
    assert "from" in msg or "origin" in msg


def test_read_missing_file_raises(project_vault: Path) -> None:
    with pytest.raises(ReviewsError):
        read_review(project_vault, filename="nope.md")


def test_read_kalle_authored_returns_record(project_vault: Path) -> None:
    _write_raw(project_vault, "kalle.md", _KALLE_REVIEW_OPEN)
    rec = read_review(project_vault, filename="kalle.md")
    assert rec.frontmatter["author"] == KALLE_AUTHOR
    assert rec.frontmatter["status"] == STATUS_OPEN
    assert "Saw a one-tick drift" in rec.body


def test_mark_addressed_human_authored_raises(project_vault: Path) -> None:
    _write_raw(project_vault, "human.md", _HUMAN_REVIEW)
    with pytest.raises(ReviewsError) as excinfo:
        mark_addressed(project_vault, filename="human.md")
    assert KALLE_AUTHOR in str(excinfo.value)
    # The error must surface the actual author/from field — that's
    # the "loud" part: the operator knows what shape the file has.
    assert "origin" in str(excinfo.value) or "from" in str(excinfo.value)
    # Confirm the file was NOT modified — re-read and check no
    # ``addressed:`` field was written.
    raw = (reviews_dir(project_vault) / "human.md").read_text(encoding="utf-8")
    assert "addressed:" not in raw
    assert "status:" not in raw


def test_mark_addressed_flips_status_and_stamps(project_vault: Path) -> None:
    _write_raw(project_vault, "kalle.md", _KALLE_REVIEW_OPEN)
    rec = mark_addressed(
        project_vault, filename="kalle.md", now_iso="2026-04-25T12:00:00+00:00",
    )
    assert rec.frontmatter["status"] == STATUS_ADDRESSED
    assert rec.frontmatter["addressed"] == "2026-04-25T12:00:00+00:00"
    on_disk = (reviews_dir(project_vault) / "kalle.md").read_text(encoding="utf-8")
    assert "status: addressed" in on_disk
    assert "2026-04-25T12:00:00+00:00" in on_disk
    # ``created`` must round-trip as an ISO 8601 string (not a YAML
    # datetime literal with a space). YAML may surround with quotes.
    assert "2026-04-25T10:00:00+00:00" in on_disk
    assert "2026-04-25 10:00:00" not in on_disk


def test_mark_addressed_idempotent_refresh(project_vault: Path) -> None:
    _write_raw(project_vault, "kalle.md", _KALLE_REVIEW_ADDRESSED)
    rec = mark_addressed(
        project_vault, filename="kalle.md", now_iso="2026-04-26T00:00:00+00:00",
    )
    assert rec.frontmatter["status"] == STATUS_ADDRESSED
    assert rec.frontmatter["addressed"] == "2026-04-26T00:00:00+00:00"


def test_list_all_kalle_reviews_with_paths_skips_human_files(tmp_path: Path) -> None:
    v1 = tmp_path / "p1"
    v2 = tmp_path / "p2"
    (v1 / REVIEWS_SUBPATH).mkdir(parents=True)
    (v2 / REVIEWS_SUBPATH).mkdir(parents=True)
    (reviews_dir(v1) / "human.md").write_text(_HUMAN_REVIEW, encoding="utf-8")
    (reviews_dir(v1) / "kalle.md").write_text(_KALLE_REVIEW_OPEN, encoding="utf-8")
    (reviews_dir(v2) / "kalle.md").write_text(_KALLE_REVIEW_ADDRESSED, encoding="utf-8")
    rows = list_all_kalle_reviews_with_paths([v1, v2, tmp_path / "missing"])
    assert len(rows) == 2
    filenames = {r.filename for _, r in rows}
    assert filenames == {"kalle.md"}
    for _, rec in rows:
        assert rec.frontmatter["author"] == KALLE_AUTHOR

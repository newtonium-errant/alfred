"""Phase 4 Sub-arc A — Topic-MOC member auto-append (Hypatia
Zettelkasten redesign, 2026-05-18).

Per ``project_hypatia_zettelkasten_redesign.md`` auto-maintenance
behavior #7: when a record of type ``zettel`` / ``source`` /
``question`` / ``research-pointer`` is created or edited with a
non-empty ``mocs:`` frontmatter list, Hypatia appends
``- [[<type>/<Title>]]`` to each referenced MOC's ``# Contents``
section. Append-only — removing a MOC from the record's ``mocs:``
does NOT cascade a remove from the MOC's Contents (operator-paced
cleanup, mirroring the Phase 3 author Contents discipline).

Coverage:
  * Helper-level (``_normalize_mocs_field``, ``_build_moc_contents_
    rewriter``, ``_resolve_moc_target``, ``append_to_moc_contents``,
    ``dispatch_moc_appends``)
  * Idempotent re-fire (no duplicate bullet)
  * Pipe-alias tolerance (regression pin for the recurring trap)
  * Missing MOC record — log + skip, originating record survives
  * Pre-Phase-4 MOC missing ``# Contents`` — auto-created
  * Aliases-chain resolution in MOC/ directory
  * Non-trigger types (memo, MOC, note) → no dispatch
  * vault_create end-to-end (zettel / source / question /
    research-pointer all wire through)
  * vault_edit dispatch (firing only when ``mocs`` in
    ``fields_changed``)
  * Multi-MOC list — all bullets land
  * Mixed missing + present MOCs — partial success
  * Log emissions (per ``feedback_log_emission_test_pattern.md``):
    moc_dispatch_summary, moc_contents_appended, moc_target_missing
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
import structlog

from alfred._data import get_scaffold_dir
from alfred.vault.ops import vault_create, vault_edit, vault_read
from alfred.vault.zettel_hooks import (
    _build_moc_contents_rewriter,
    _normalize_mocs_field,
    _resolve_moc_target,
    append_to_moc_contents,
    dispatch_moc_appends,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_moc(
    vault: Path,
    name: str,
    *,
    aliases: list[str] | None = None,
    body: str | None = None,
) -> str:
    """Write a minimal ``MOC/<name>.md`` via raw FS."""
    (vault / "MOC").mkdir(exist_ok=True)
    fm: dict = {
        "type": "MOC",
        "name": name,
        "created": "2026-05-18",
        "parent_mocs": [],
        "tags": [],
    }
    if aliases:
        fm["aliases"] = aliases
    rel_path = f"MOC/{name}.md"
    file_path = vault / rel_path
    default_body = "# Premise\n\n# Contents\n\n# Notes\n\n# Tags\n\n# See Also\n"
    post = frontmatter.Post(body if body is not None else default_body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


def _seed_zettel_raw(
    vault: Path,
    name: str,
    *,
    fm: dict | None = None,
    body: str = "",
) -> str:
    """Write a minimal zettel via raw FS (bypassing vault_create
    so the test can control which hooks fire)."""
    (vault / "zettel").mkdir(exist_ok=True)
    base_fm: dict = {
        "type": "zettel",
        "name": name,
        "created": "2026-05-18",
        "mocs": [],
        "tags": [],
        "status": "open",
    }
    if fm:
        base_fm.update(fm)
    rel_path = f"zettel/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post(body, **base_fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


@pytest.fixture
def hypatia_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in (
        "zettel", "source", "question", "research-pointer", "memo",
        "MOC", "_templates",
    ):
        (vault / sub).mkdir()
    # Copy the bundled scaffold templates the trigger types use.
    scaffold = get_scaffold_dir() / "_templates"
    for name in (
        "zettel.md", "source.md", "question.md", "research-pointer.md",
        "memo.md", "MOC.md",
    ):
        src = scaffold / name
        if src.exists():
            (vault / "_templates" / name).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8",
            )
    return vault


# ---------------------------------------------------------------------------
# _normalize_mocs_field — coercion + de-dup
# ---------------------------------------------------------------------------


def test_normalize_mocs_field_list_of_wikilinks() -> None:
    out = _normalize_mocs_field(["[[MOC/Stoicism]]", "[[MOC/HEMA]]"])
    assert out == ["MOC/Stoicism", "MOC/HEMA"]


def test_normalize_mocs_field_list_of_bare_paths() -> None:
    out = _normalize_mocs_field(["MOC/Stoicism", "MOC/HEMA"])
    assert out == ["MOC/Stoicism", "MOC/HEMA"]


def test_normalize_mocs_field_mixed_list() -> None:
    out = _normalize_mocs_field(["[[MOC/Stoicism]]", "MOC/HEMA"])
    assert out == ["MOC/Stoicism", "MOC/HEMA"]


def test_normalize_mocs_field_scalar_string() -> None:
    """Defense-in-depth: operator-typo scalar instead of list."""
    out = _normalize_mocs_field("[[MOC/Stoicism]]")
    assert out == ["MOC/Stoicism"]


def test_normalize_mocs_field_pipe_aliased() -> None:
    out = _normalize_mocs_field(["[[MOC/Practical Stoicism|Stoicism]]"])
    assert out == ["MOC/Practical Stoicism"]


def test_normalize_mocs_field_none_returns_empty() -> None:
    assert _normalize_mocs_field(None) == []


def test_normalize_mocs_field_empty_list_returns_empty() -> None:
    assert _normalize_mocs_field([]) == []


def test_normalize_mocs_field_drops_empty_entries() -> None:
    """Empty/whitespace-only entries (placeholders) are dropped."""
    out = _normalize_mocs_field(["[[MOC/Stoicism]]", "", "  ", "[[]]"])
    assert out == ["MOC/Stoicism"]


# ---------------------------------------------------------------------------
# _resolve_moc_target
# ---------------------------------------------------------------------------


def test_resolve_moc_exact_path(hypatia_vault: Path) -> None:
    _seed_moc(hypatia_vault, "Practical Stoicism MOC")
    rel = _resolve_moc_target(hypatia_vault, "[[MOC/Practical Stoicism MOC]]")
    assert rel == "MOC/Practical Stoicism MOC.md"


def test_resolve_moc_bare_name_no_prefix(hypatia_vault: Path) -> None:
    """Bare name (no ``MOC/`` prefix) resolves via fallback to MOC/."""
    _seed_moc(hypatia_vault, "Stoicism")
    rel = _resolve_moc_target(hypatia_vault, "Stoicism")
    assert rel == "MOC/Stoicism.md"


def test_resolve_moc_aliased_short_form(hypatia_vault: Path) -> None:
    """Operator types short-form ``[[MOC/Stoicism]]`` against an MOC
    record stored with aliases that include the short form."""
    _seed_moc(
        hypatia_vault, "Practical Stoicism MOC",
        aliases=["Stoicism", "Stoicism MOC"],
    )
    rel = _resolve_moc_target(hypatia_vault, "[[MOC/Stoicism]]")
    assert rel == "MOC/Practical Stoicism MOC.md"


def test_resolve_moc_pipe_aliased(hypatia_vault: Path) -> None:
    _seed_moc(hypatia_vault, "Practical Stoicism MOC")
    rel = _resolve_moc_target(
        hypatia_vault, "[[MOC/Practical Stoicism MOC|Stoicism]]",
    )
    assert rel == "MOC/Practical Stoicism MOC.md"


def test_resolve_moc_unknown_returns_none(hypatia_vault: Path) -> None:
    assert _resolve_moc_target(
        hypatia_vault, "[[MOC/Nonexistent]]",
    ) is None


def test_resolve_moc_empty_returns_none(hypatia_vault: Path) -> None:
    assert _resolve_moc_target(hypatia_vault, "") is None
    assert _resolve_moc_target(hypatia_vault, None) is None
    assert _resolve_moc_target(hypatia_vault, "[[]]") is None


def test_resolve_moc_wrong_directory_prefix(hypatia_vault: Path) -> None:
    """A ``person/...`` prefix doesn't get rerouted to MOC/."""
    _seed_moc(hypatia_vault, "Stoicism")
    assert _resolve_moc_target(hypatia_vault, "[[person/Stoicism]]") is None


# ---------------------------------------------------------------------------
# _build_moc_contents_rewriter — direct
# ---------------------------------------------------------------------------


def test_moc_rewriter_appends_to_existing_section() -> None:
    body = (
        "# Premise\n\nStoic philosophy\n\n"
        "# Contents\n\n"
        "- [[zettel/Older]]\n"
    )
    rw = _build_moc_contents_rewriter("[[zettel/New]]")
    out = rw(body)
    assert "[[zettel/Older]]" in out
    assert "[[zettel/New]]" in out


def test_moc_rewriter_is_idempotent() -> None:
    body = "# Contents\n\n- [[zettel/Existing]]\n"
    rw = _build_moc_contents_rewriter("[[zettel/Existing]]")
    out = rw(body)
    assert out.count("[[zettel/Existing]]") == 1


def test_moc_rewriter_creates_missing_section() -> None:
    body = "# Premise\n\nStoic philosophy\n"
    rw = _build_moc_contents_rewriter("[[zettel/First]]")
    out = rw(body)
    assert "# Contents" in out
    assert "- [[zettel/First]]" in out


def test_moc_rewriter_pipe_alias_idempotent() -> None:
    """Pipe-aliased existing bullet does NOT cause a duplicate
    append. Regression pin for the recurring pipe-alias trap."""
    body = (
        "# Contents\n\n"
        "- [[zettel/Dichotomy|Dichotomy of Control]]\n"
    )
    rw = _build_moc_contents_rewriter("[[zettel/Dichotomy]]")
    out = rw(body)
    # Only ONE reference to the bare target — the existing pipe-
    # aliased form, not a new duplicate.
    assert out.count("[[zettel/Dichotomy") == 1


def test_moc_rewriter_h2_sub_headings_dont_bound_section() -> None:
    """H3 / deeper sub-headings inside ``# Contents`` are sub-trees,
    not section boundaries. Appended bullet lands AFTER existing
    sub-tree content, before the next H1/H2."""
    body = (
        "# Contents\n\n"
        "- [[zettel/A]]\n"
        "  - [[source/A-source]]\n"
        "### Sub-topic\n"
        "- [[zettel/B]]\n"
        "# Notes\n"
    )
    rw = _build_moc_contents_rewriter("[[zettel/C]]")
    out = rw(body)
    assert "[[zettel/C]]" in out
    # The new bullet lands BEFORE the next H1 (# Notes).
    contents_idx = out.find("# Contents")
    notes_idx = out.find("# Notes")
    new_bullet_idx = out.find("- [[zettel/C]]")
    assert contents_idx < new_bullet_idx < notes_idx


# ---------------------------------------------------------------------------
# append_to_moc_contents — helper level
# ---------------------------------------------------------------------------


def test_append_first_member_to_moc(hypatia_vault: Path) -> None:
    moc_rel = _seed_moc(hypatia_vault, "Stoicism")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    result = append_to_moc_contents(
        hypatia_vault, "[[MOC/Stoicism]]", z_rel, scope="hypatia",
    )
    assert result is True

    moc = vault_read(hypatia_vault, moc_rel)
    assert "- [[zettel/Idea-1]]" in moc["body"]


def test_append_to_moc_is_idempotent(hypatia_vault: Path) -> None:
    _seed_moc(hypatia_vault, "Stoicism")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    append_to_moc_contents(
        hypatia_vault, "[[MOC/Stoicism]]", z_rel, scope="hypatia",
    )
    append_to_moc_contents(
        hypatia_vault, "[[MOC/Stoicism]]", z_rel, scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert moc["body"].count("[[zettel/Idea-1]]") == 1


def test_append_creates_missing_contents_section(hypatia_vault: Path) -> None:
    """Pre-Phase-4 MOC without ``# Contents`` gets the section
    auto-created."""
    _seed_moc(
        hypatia_vault, "Stoicism",
        body="# Premise\n\nStoic philosophy\n",  # No # Contents
    )
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    result = append_to_moc_contents(
        hypatia_vault, "[[MOC/Stoicism]]", z_rel, scope="hypatia",
    )
    assert result is True

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "# Contents" in moc["body"]
    assert "- [[zettel/Idea-1]]" in moc["body"]
    # Original premise preserved.
    assert "Stoic philosophy" in moc["body"]


def test_append_follows_aliases_chain(hypatia_vault: Path) -> None:
    """Operator types short-form ``[[MOC/Stoicism]]`` against an MOC
    record stored as ``MOC/Practical Stoicism MOC.md`` with aliases.
    Hook resolves and appends to the canonical record."""
    canonical_rel = _seed_moc(
        hypatia_vault, "Practical Stoicism MOC",
        aliases=["Stoicism", "Stoicism MOC"],
    )
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    result = append_to_moc_contents(
        hypatia_vault, "[[MOC/Stoicism]]", z_rel, scope="hypatia",
    )
    assert result is True

    moc = vault_read(hypatia_vault, canonical_rel)
    assert "- [[zettel/Idea-1]]" in moc["body"]


def test_append_missing_moc_returns_false(hypatia_vault: Path) -> None:
    """MOC record doesn't exist → fail-open. New record survives."""
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")
    result = append_to_moc_contents(
        hypatia_vault, "[[MOC/Phantom]]", z_rel, scope="hypatia",
    )
    assert result is False


def test_append_empty_moc_returns_false(hypatia_vault: Path) -> None:
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")
    assert append_to_moc_contents(
        hypatia_vault, "", z_rel, scope="hypatia",
    ) is False
    assert append_to_moc_contents(
        hypatia_vault, None, z_rel, scope="hypatia",
    ) is False


def test_append_pipe_aliased_existing_no_duplicate(
    hypatia_vault: Path,
) -> None:
    """Regression pin for the pipe-alias idempotency hole (third
    recurrence: Phase 2 + Phase 3 author + this Phase 4 hook all
    share the trap)."""
    moc_rel = _seed_moc(
        hypatia_vault, "Stoicism",
        body=(
            "# Premise\n\n"
            "# Contents\n\n"
            "- [[zettel/Dichotomy|Dichotomy of Control]]\n"
        ),
    )
    z_rel = _seed_zettel_raw(hypatia_vault, "Dichotomy")

    append_to_moc_contents(
        hypatia_vault, "[[MOC/Stoicism]]", z_rel, scope="hypatia",
    )

    moc = vault_read(hypatia_vault, moc_rel)
    # Count occurrences of the bare target stem — exactly 1 regardless
    # of alias display form.
    assert moc["body"].count("[[zettel/Dichotomy") == 1


# ---------------------------------------------------------------------------
# dispatch_moc_appends — type gate + multi-MOC iteration
# ---------------------------------------------------------------------------


def test_dispatch_zettel_with_multiple_mocs(hypatia_vault: Path) -> None:
    """All MOCs in the list receive the bullet."""
    _seed_moc(hypatia_vault, "Stoicism")
    _seed_moc(hypatia_vault, "HEMA")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    count = dispatch_moc_appends(
        hypatia_vault, z_rel, "zettel",
        ["[[MOC/Stoicism]]", "[[MOC/HEMA]]"],
        scope="hypatia",
    )
    assert count == 2

    stoic = vault_read(hypatia_vault, "MOC/Stoicism.md")
    hema = vault_read(hypatia_vault, "MOC/HEMA.md")
    assert "- [[zettel/Idea-1]]" in stoic["body"]
    assert "- [[zettel/Idea-1]]" in hema["body"]


def test_dispatch_partial_success_when_one_missing(
    hypatia_vault: Path,
) -> None:
    """One MOC missing → other MOCs still receive bullets. Return
    count reflects only successful appends."""
    _seed_moc(hypatia_vault, "Stoicism")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    count = dispatch_moc_appends(
        hypatia_vault, z_rel, "zettel",
        ["[[MOC/Stoicism]]", "[[MOC/Phantom]]"],
        scope="hypatia",
    )
    assert count == 1

    stoic = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "- [[zettel/Idea-1]]" in stoic["body"]


def test_dispatch_non_trigger_type_returns_zero(
    hypatia_vault: Path,
) -> None:
    """``memo`` is NOT in _MOC_TRIGGER_TYPES — no dispatch."""
    _seed_moc(hypatia_vault, "Stoicism")
    count = dispatch_moc_appends(
        hypatia_vault, "memo/M1.md", "memo",
        ["[[MOC/Stoicism]]"],
        scope="hypatia",
    )
    assert count == 0
    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "memo/M1" not in moc["body"]


def test_dispatch_moc_record_type_returns_zero(
    hypatia_vault: Path,
) -> None:
    """An MOC itself is NOT a trigger type — its ``parent_mocs:``
    field is the MOC-to-MOC linkage surface; ``mocs:`` doesn't apply
    here. Future Phase 5+ may add hierarchical MOC tree maintenance."""
    _seed_moc(hypatia_vault, "Stoicism")
    count = dispatch_moc_appends(
        hypatia_vault, "MOC/Practical.md", "MOC",
        ["[[MOC/Stoicism]]"],
        scope="hypatia",
    )
    assert count == 0


def test_dispatch_empty_mocs_returns_zero(hypatia_vault: Path) -> None:
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")
    assert dispatch_moc_appends(
        hypatia_vault, z_rel, "zettel", [], scope="hypatia",
    ) == 0
    assert dispatch_moc_appends(
        hypatia_vault, z_rel, "zettel", None, scope="hypatia",
    ) == 0


# ---------------------------------------------------------------------------
# Log emissions (per feedback_log_emission_test_pattern.md)
# ---------------------------------------------------------------------------


def test_log_moc_contents_appended_on_success(hypatia_vault: Path) -> None:
    moc_rel = _seed_moc(hypatia_vault, "Stoicism")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    with structlog.testing.capture_logs() as captured:
        append_to_moc_contents(
            hypatia_vault, "[[MOC/Stoicism]]", z_rel, scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.moc_contents_appended"
    ]
    assert len(matches) == 1
    assert matches[0]["member_rel_path"] == z_rel
    assert matches[0]["moc_rel_path"] == moc_rel


def test_log_moc_target_missing(hypatia_vault: Path) -> None:
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    with structlog.testing.capture_logs() as captured:
        append_to_moc_contents(
            hypatia_vault, "[[MOC/Phantom]]", z_rel, scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.moc_target_missing"
    ]
    assert len(matches) == 1
    assert matches[0]["member_rel_path"] == z_rel
    assert "Phantom" in matches[0]["moc_value"]


def test_log_dispatch_summary_on_success(hypatia_vault: Path) -> None:
    _seed_moc(hypatia_vault, "Stoicism")
    _seed_moc(hypatia_vault, "HEMA")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    with structlog.testing.capture_logs() as captured:
        dispatch_moc_appends(
            hypatia_vault, z_rel, "zettel",
            ["[[MOC/Stoicism]]", "[[MOC/HEMA]]"],
            scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.moc_dispatch_summary"
    ]
    assert len(matches) == 1
    assert matches[0]["mocs_count"] == 2
    assert matches[0]["appended_count"] == 2
    assert matches[0]["member_type"] == "zettel"


def test_log_dispatch_summary_for_non_trigger_type(
    hypatia_vault: Path,
) -> None:
    """Per the intentionally-left-blank discipline: idle is
    distinguishable from broken — non-trigger types still emit the
    summary log with reason=type_not_in_moc_trigger_types."""
    with structlog.testing.capture_logs() as captured:
        dispatch_moc_appends(
            hypatia_vault, "memo/M1.md", "memo",
            ["[[MOC/Stoicism]]"],
            scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.moc_dispatch_summary"
    ]
    assert len(matches) == 1
    assert matches[0]["reason"] == "type_not_in_moc_trigger_types"
    assert matches[0]["mocs_count"] == 0
    assert matches[0]["appended_count"] == 0


def test_log_dispatch_summary_for_empty_mocs(hypatia_vault: Path) -> None:
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")
    with structlog.testing.capture_logs() as captured:
        dispatch_moc_appends(
            hypatia_vault, z_rel, "zettel", [], scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.moc_dispatch_summary"
    ]
    assert len(matches) == 1
    assert matches[0]["reason"] == "empty_mocs_field"


# ---------------------------------------------------------------------------
# vault_create end-to-end — Phase 4 dispatch on creation
# ---------------------------------------------------------------------------


def test_vault_create_zettel_with_mocs_appends(hypatia_vault: Path) -> None:
    _seed_moc(hypatia_vault, "Stoicism")

    vault_create(
        hypatia_vault,
        "zettel",
        "Dichotomy of Control",
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "- [[zettel/Dichotomy of Control]]" in moc["body"]


def test_vault_create_source_with_mocs_appends(hypatia_vault: Path) -> None:
    _seed_moc(hypatia_vault, "Stoicism")

    vault_create(
        hypatia_vault,
        "source",
        "Meditations",
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "- [[source/Meditations]]" in moc["body"]


def test_vault_create_question_with_mocs_appends(hypatia_vault: Path) -> None:
    _seed_moc(hypatia_vault, "Stoicism")

    vault_create(
        hypatia_vault,
        "question",
        "What is the dichotomy",
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "- [[question/What is the dichotomy]]" in moc["body"]


def test_vault_create_research_pointer_with_mocs_appends(
    hypatia_vault: Path,
) -> None:
    _seed_moc(hypatia_vault, "Stoicism")

    vault_create(
        hypatia_vault,
        "research-pointer",
        "Read Hadot on Stoic spiritual exercises",
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert (
        "- [[research-pointer/Read Hadot on Stoic spiritual exercises]]"
        in moc["body"]
    )


def test_vault_create_memo_does_not_dispatch(hypatia_vault: Path) -> None:
    """Memos are write-once-by-design + NOT in trigger types. The
    template doesn't have ``mocs:`` either; this test sets it
    manually to confirm even an operator-typed mocs is ignored."""
    _seed_moc(hypatia_vault, "Stoicism")

    vault_create(
        hypatia_vault,
        "memo",
        "M1",
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "memo/M1" not in moc["body"]


def test_vault_create_missing_moc_does_not_fail(hypatia_vault: Path) -> None:
    """vault_create succeeds even when an MOC referenced in mocs:
    doesn't exist (hook failure-isolated)."""
    result = vault_create(
        hypatia_vault,
        "zettel",
        "Orphan-Z",
        set_fields={"mocs": ["[[MOC/Phantom]]"]},
        scope="hypatia",
    )
    assert result["path"] == "zettel/Orphan-Z.md"
    z = vault_read(hypatia_vault, "zettel/Orphan-Z.md")
    assert "[[MOC/Phantom]]" in str(z["frontmatter"]["mocs"])


def test_vault_create_without_mocs_skips_hook(hypatia_vault: Path) -> None:
    """No mocs → no hook. Sanity check that empty list does not write."""
    _seed_moc(hypatia_vault, "Stoicism")

    vault_create(
        hypatia_vault,
        "zettel",
        "Z-No-MOCs",
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "Z-No-MOCs" not in moc["body"]


# ---------------------------------------------------------------------------
# vault_edit dispatch — fires only when ``mocs`` in fields_changed
# ---------------------------------------------------------------------------


def test_vault_edit_zettel_setting_mocs_appends(hypatia_vault: Path) -> None:
    """Operator adds an MOC to an existing zettel via vault_edit;
    the hook fires."""
    _seed_moc(hypatia_vault, "Stoicism")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    vault_edit(
        hypatia_vault,
        z_rel,
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "- [[zettel/Idea-1]]" in moc["body"]


def test_vault_edit_unrelated_field_does_not_dispatch(
    hypatia_vault: Path,
) -> None:
    """Editing an unrelated field (status, tags) does NOT re-fire the
    MOC hook. Only ``mocs`` in fields_changed triggers."""
    _seed_moc(hypatia_vault, "Stoicism")
    # Seed a zettel with mocs already set on disk (raw FS).
    z_rel = _seed_zettel_raw(
        hypatia_vault, "Idea-1",
        fm={"mocs": ["[[MOC/Stoicism]]"]},
    )

    # Edit only status — mocs is unchanged on the record.
    vault_edit(
        hypatia_vault,
        z_rel,
        set_fields={"status": "refined"},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    # No bullet was added — the dispatch site filters on
    # ``mocs`` in fields_changed.
    assert "[[zettel/Idea-1]]" not in moc["body"]


def test_vault_edit_setting_same_mocs_idempotent(
    hypatia_vault: Path,
) -> None:
    """Re-setting ``mocs`` to the same value re-fires the hook but
    the helper-level idempotency check prevents duplicate bullets."""
    _seed_moc(hypatia_vault, "Stoicism")
    z_rel = _seed_zettel_raw(hypatia_vault, "Idea-1")

    vault_edit(
        hypatia_vault,
        z_rel,
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )
    vault_edit(
        hypatia_vault,
        z_rel,
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert moc["body"].count("[[zettel/Idea-1]]") == 1


def test_vault_edit_source_with_mocs_dispatches(
    hypatia_vault: Path,
) -> None:
    """vault_edit on a source record (trigger type) also fires the
    Phase 4 hook."""
    _seed_moc(hypatia_vault, "Stoicism")
    # Create the source via vault_create first, then edit it.
    vault_create(
        hypatia_vault,
        "source",
        "Meditations",
        scope="hypatia",
    )

    vault_edit(
        hypatia_vault,
        "source/Meditations.md",
        set_fields={"mocs": ["[[MOC/Stoicism]]"]},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/Stoicism.md")
    assert "- [[source/Meditations]]" in moc["body"]

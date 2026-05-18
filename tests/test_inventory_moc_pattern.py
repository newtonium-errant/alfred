"""Phase 4 Sub-arc B — Inventory MOC pattern (Hypatia Zettelkasten
redesign, 2026-05-18).

Per ``project_hypatia_zettelkasten_redesign.md`` auto-maintenance
behavior #7b: the underscore-prefix ``MOC/_<Name>.md`` convention
marks system-maintained inventory MOCs. Two instances ship in
Sub-arc B:

  * ``MOC/_Open Questions.md`` — ``question/`` records with
    ``status in {"open", "refined"}``
  * ``MOC/_Open Research Pointers.md`` — ``research-pointer/``
    records with ``status == "open"``

Distinct from Sub-arc A's topic-MOC pattern:

  - Removal cleanup REQUIRED on status transitions (open → resolved
    drops the bullet; the whole point is "what's currently open").
  - Auto-create the inventory MOC if absent (Hypatia owns these
    files; first qualifying record fires the create).
  - Registration via dispatch table, not frontmatter ``mocs:``.

Coverage:
  * ``_build_remove_bullet_rewriter`` unit (pipe-alias tolerance,
    idempotent, sibling preservation, surrounding-prose preservation)
  * ``INVENTORY_MOC_DISPATCH`` table contents (regression-pin: 2
    entries on this ship)
  * ``_ensure_inventory_moc`` auto-create
  * ``_apply_inventory_moc_action`` add + remove
  * ``dispatch_inventory_mocs`` predicate-transition truth table
  * ``vault_create`` end-to-end — question + research-pointer fresh
    creates with qualifying status
  * ``vault_edit`` end-to-end — status transitions trigger add /
    remove
  * Multi-record state (3 questions, 1 resolved → MOC has 2 bullets)
  * Idempotent re-fire (add when bullet present → no duplicate;
    remove when bullet absent → no-op)
  * Log emissions (per ``feedback_log_emission_test_pattern``):
    inventory_moc_dispatch_summary, inventory_moc_created,
    inventory_moc_written (add + remove)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred._data import get_scaffold_dir
from alfred.vault.ops import vault_create, vault_edit, vault_read
from alfred.vault.zettel_hooks import (
    INVENTORY_MOC_DISPATCH,
    _apply_inventory_moc_action,
    _build_remove_bullet_rewriter,
    _ensure_inventory_moc,
    dispatch_inventory_mocs,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def hypatia_vault(tmp_path: Path) -> Path:
    """Vault with the four templates the inventory-MOC tests need."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in (
        "question", "research-pointer", "zettel", "source",
        "MOC", "_templates",
    ):
        (vault / sub).mkdir()
    scaffold = get_scaffold_dir() / "_templates"
    for name in (
        "question.md", "research-pointer.md", "MOC.md", "zettel.md",
        "source.md",
    ):
        src = scaffold / name
        if src.exists():
            (vault / "_templates" / name).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8",
            )
    return vault


# ---------------------------------------------------------------------------
# INVENTORY_MOC_DISPATCH table — regression pin
# ---------------------------------------------------------------------------


def test_dispatch_table_contains_two_entries() -> None:
    """Sub-arc B ships exactly two inventory MOCs. If this changes,
    update the SKILL too — the dispatch table contents are the
    capability surface."""
    assert len(INVENTORY_MOC_DISPATCH) == 2


def test_dispatch_table_first_entry_is_open_questions() -> None:
    entry_type, predicate, moc_rel_path, moc_name = INVENTORY_MOC_DISPATCH[0]
    assert entry_type == "question"
    assert moc_rel_path == "MOC/_Open Questions.md"
    assert moc_name == "_Open Questions"
    assert predicate({"status": "open"}) is True
    assert predicate({"status": "refined"}) is True
    assert predicate({"status": "answered"}) is False
    assert predicate({"status": "superseded"}) is False
    assert predicate({}) is False


def test_dispatch_table_second_entry_is_open_research_pointers() -> None:
    entry_type, predicate, moc_rel_path, moc_name = INVENTORY_MOC_DISPATCH[1]
    assert entry_type == "research-pointer"
    assert moc_rel_path == "MOC/_Open Research Pointers.md"
    assert moc_name == "_Open Research Pointers"
    assert predicate({"status": "open"}) is True
    assert predicate({"status": "in-progress"}) is False
    assert predicate({"status": "completed"}) is False
    assert predicate({"status": "dropped"}) is False


def test_dispatch_table_underscore_prefix_discipline() -> None:
    """Every inventory MOC must use the underscore-prefix convention
    to mark it system-maintained (parallel to _templates/ / _bases/)."""
    for _, _, moc_rel_path, moc_name in INVENTORY_MOC_DISPATCH:
        assert moc_rel_path.startswith("MOC/_"), (
            f"Inventory MOC {moc_rel_path!r} missing underscore prefix"
        )
        assert moc_name.startswith("_"), (
            f"Inventory MOC name {moc_name!r} missing underscore prefix"
        )


# ---------------------------------------------------------------------------
# _build_remove_bullet_rewriter — unit
# ---------------------------------------------------------------------------


def test_remove_bullet_removes_match() -> None:
    body = (
        "# Premise\n\n"
        "# Contents\n\n"
        "- [[question/Q1]]\n"
        "- [[question/Q2]]\n"
        "- [[question/Q3]]\n"
        "# Tags\n"
    )
    rw = _build_remove_bullet_rewriter("[[question/Q2]]")
    out = rw(body)
    assert "[[question/Q1]]" in out
    assert "[[question/Q2]]" not in out
    assert "[[question/Q3]]" in out
    # Heading and surrounding sections preserved.
    assert "# Premise" in out
    assert "# Contents" in out
    assert "# Tags" in out


def test_remove_bullet_idempotent_when_absent() -> None:
    """Removing a bullet that isn't there is a no-op."""
    body = (
        "# Contents\n\n"
        "- [[question/Q1]]\n"
    )
    rw = _build_remove_bullet_rewriter("[[question/Phantom]]")
    out = rw(body)
    assert out == body


def test_remove_bullet_handles_pipe_alias() -> None:
    """A pipe-aliased bullet form ``- [[question/Q2|display]]`` is
    removed when we ask to remove ``[[question/Q2]]`` — same logical
    target."""
    body = (
        "# Contents\n\n"
        "- [[question/Q1]]\n"
        "- [[question/Q2|My Display Form]]\n"
        "- [[question/Q3]]\n"
    )
    rw = _build_remove_bullet_rewriter("[[question/Q2]]")
    out = rw(body)
    assert "[[question/Q2" not in out
    assert "[[question/Q1]]" in out
    assert "[[question/Q3]]" in out


def test_remove_bullet_preserves_body_prose_with_same_link() -> None:
    """A wikilink that appears INLINE in body prose (not as a bullet)
    is NOT removed — the regex anchors to ``- `` bullet prefix."""
    body = (
        "# Contents\n\n"
        "- [[question/Q1]]\n\n"
        "See also [[question/Q2]] for context.\n"
    )
    rw = _build_remove_bullet_rewriter("[[question/Q2]]")
    out = rw(body)
    # The inline reference survives.
    assert "[[question/Q2]] for context" in out


def test_remove_bullet_no_contents_section_no_op() -> None:
    body = "# Premise\n\nSome content\n"
    rw = _build_remove_bullet_rewriter("[[question/Q1]]")
    out = rw(body)
    assert out == body


def test_remove_bullet_leaves_empty_contents_when_last_removed() -> None:
    """Removing the only bullet leaves an empty # Contents section —
    the section header stays (preserves empty-placeholder discipline)."""
    body = (
        "# Premise\n\n"
        "# Contents\n\n"
        "- [[question/Q1]]\n"
        "# Tags\n"
    )
    rw = _build_remove_bullet_rewriter("[[question/Q1]]")
    out = rw(body)
    assert "# Contents" in out
    assert "[[question/Q1]]" not in out
    assert "# Tags" in out


def test_remove_bullet_malformed_input_no_op() -> None:
    """Caller passes a malformed wikilink (no brackets) → no-op
    rewriter (defensive)."""
    rw = _build_remove_bullet_rewriter("question/Q1")
    assert rw("# Contents\n- [[question/Q1]]\n") == "# Contents\n- [[question/Q1]]\n"


# ---------------------------------------------------------------------------
# _ensure_inventory_moc — auto-create
# ---------------------------------------------------------------------------


def test_ensure_creates_inventory_moc_when_absent(hypatia_vault: Path) -> None:
    rel_path = "MOC/_Open Questions.md"
    assert not (hypatia_vault / rel_path).exists()

    result = _ensure_inventory_moc(
        hypatia_vault, rel_path, "_Open Questions", scope="hypatia",
    )
    assert result is True
    assert (hypatia_vault / rel_path).exists()

    rec = vault_read(hypatia_vault, rel_path)
    assert rec["frontmatter"]["type"] == "MOC"
    assert rec["frontmatter"]["name"] == "_Open Questions"
    assert "# Contents" in rec["body"]


def test_ensure_no_op_when_moc_exists(hypatia_vault: Path) -> None:
    """If the MOC already exists, ``_ensure_inventory_moc`` returns
    True without re-creating (no near-match refusal, no overwrite)."""
    # Pre-create with custom body so we can detect re-creation.
    vault_create(
        hypatia_vault, "MOC", "_Open Questions", scope="hypatia",
    )
    rec_before = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    body_before = rec_before["body"]

    result = _ensure_inventory_moc(
        hypatia_vault, "MOC/_Open Questions.md", "_Open Questions",
        scope="hypatia",
    )
    assert result is True

    rec_after = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert rec_after["body"] == body_before


# ---------------------------------------------------------------------------
# _apply_inventory_moc_action — direct
# ---------------------------------------------------------------------------


def test_apply_add_creates_moc_and_adds_bullet(hypatia_vault: Path) -> None:
    result = _apply_inventory_moc_action(
        hypatia_vault,
        "MOC/_Open Questions.md",
        "_Open Questions",
        "question/Q1.md",
        action="add",
        scope="hypatia",
    )
    assert result is True

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Q1]]" in moc["body"]


def test_apply_remove_when_moc_missing_no_op(hypatia_vault: Path) -> None:
    """Remove against a non-existent MOC returns False (nothing to
    remove from)."""
    result = _apply_inventory_moc_action(
        hypatia_vault,
        "MOC/_Open Questions.md",
        "_Open Questions",
        "question/Q1.md",
        action="remove",
        scope="hypatia",
    )
    assert result is False
    assert not (hypatia_vault / "MOC/_Open Questions.md").exists()


def test_apply_remove_removes_bullet(hypatia_vault: Path) -> None:
    # Seed: MOC with bullet present.
    _apply_inventory_moc_action(
        hypatia_vault,
        "MOC/_Open Questions.md", "_Open Questions",
        "question/Q1.md",
        action="add", scope="hypatia",
    )

    result = _apply_inventory_moc_action(
        hypatia_vault,
        "MOC/_Open Questions.md", "_Open Questions",
        "question/Q1.md",
        action="remove", scope="hypatia",
    )
    assert result is True
    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "[[question/Q1]]" not in moc["body"]


def test_apply_unknown_action_logs_and_returns_false(
    hypatia_vault: Path,
) -> None:
    with structlog.testing.capture_logs() as captured:
        result = _apply_inventory_moc_action(
            hypatia_vault,
            "MOC/_Open Questions.md", "_Open Questions",
            "question/Q1.md",
            action="weird", scope="hypatia",
        )
    assert result is False
    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.inventory_moc_unknown_action"
    ]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# dispatch_inventory_mocs — predicate transition truth table
# ---------------------------------------------------------------------------


def test_dispatch_fresh_create_qualifying_status_adds(
    hypatia_vault: Path,
) -> None:
    """pre_fm=None + post predicate True → add."""
    counts = dispatch_inventory_mocs(
        hypatia_vault,
        "question/Q1.md",
        "question",
        pre_fm=None,
        post_fm={"status": "open"},
        scope="hypatia",
    )
    assert counts["added"] == 1
    assert counts["removed"] == 0


def test_dispatch_status_transition_open_to_resolved_removes(
    hypatia_vault: Path,
) -> None:
    """pre predicate True + post predicate False → remove."""
    # Seed: MOC with bullet present.
    _apply_inventory_moc_action(
        hypatia_vault,
        "MOC/_Open Questions.md", "_Open Questions",
        "question/Q1.md",
        action="add", scope="hypatia",
    )

    counts = dispatch_inventory_mocs(
        hypatia_vault,
        "question/Q1.md",
        "question",
        pre_fm={"status": "open"},
        post_fm={"status": "answered"},
        scope="hypatia",
    )
    assert counts["removed"] == 1
    assert counts["added"] == 0

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "[[question/Q1]]" not in moc["body"]


def test_dispatch_status_transition_resolved_to_open_adds(
    hypatia_vault: Path,
) -> None:
    """pre predicate False + post predicate True → add (re-open)."""
    counts = dispatch_inventory_mocs(
        hypatia_vault,
        "question/Q1.md",
        "question",
        pre_fm={"status": "answered"},
        post_fm={"status": "open"},
        scope="hypatia",
    )
    assert counts["added"] == 1

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Q1]]" in moc["body"]


def test_dispatch_no_transition_qualifying_skips_write_but_idempotent(
    hypatia_vault: Path,
) -> None:
    """pre True + post True → idempotent add. Helper-level bullet
    check prevents duplicate."""
    # Seed: bullet present.
    _apply_inventory_moc_action(
        hypatia_vault,
        "MOC/_Open Questions.md", "_Open Questions",
        "question/Q1.md",
        action="add", scope="hypatia",
    )
    moc_before = vault_read(hypatia_vault, "MOC/_Open Questions.md")

    counts = dispatch_inventory_mocs(
        hypatia_vault,
        "question/Q1.md",
        "question",
        pre_fm={"status": "open"},
        post_fm={"status": "refined"},  # Both predicates → True
        scope="hypatia",
    )
    # Add fires but the bullet is already there — body unchanged.
    assert counts["added"] == 1
    moc_after = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert moc_before["body"].count("[[question/Q1]]") == 1
    assert moc_after["body"].count("[[question/Q1]]") == 1


def test_dispatch_no_transition_non_qualifying_skips(
    hypatia_vault: Path,
) -> None:
    """pre False + post False → no-op, skipped count incremented."""
    counts = dispatch_inventory_mocs(
        hypatia_vault,
        "question/Q1.md",
        "question",
        pre_fm={"status": "answered"},
        post_fm={"status": "superseded"},
        scope="hypatia",
    )
    assert counts["added"] == 0
    assert counts["removed"] == 0
    assert counts["skipped"] == 1


def test_dispatch_non_trigger_type_no_match(
    hypatia_vault: Path,
) -> None:
    """A zettel doesn't match any dispatch entry — counts empty."""
    counts = dispatch_inventory_mocs(
        hypatia_vault,
        "zettel/Z1.md",
        "zettel",
        pre_fm=None,
        post_fm={"status": "open"},
        scope="hypatia",
    )
    assert counts["added"] == 0
    assert counts["removed"] == 0
    assert counts["skipped"] == 0  # No entries iterated


def test_dispatch_research_pointer_completion_removes(
    hypatia_vault: Path,
) -> None:
    """research-pointer status: open → completed removes from
    _Open Research Pointers.md."""
    # Seed via fresh-create flow.
    dispatch_inventory_mocs(
        hypatia_vault,
        "research-pointer/RP1.md",
        "research-pointer",
        pre_fm=None,
        post_fm={"status": "open"},
        scope="hypatia",
    )
    moc = vault_read(hypatia_vault, "MOC/_Open Research Pointers.md")
    assert "- [[research-pointer/RP1]]" in moc["body"]

    counts = dispatch_inventory_mocs(
        hypatia_vault,
        "research-pointer/RP1.md",
        "research-pointer",
        pre_fm={"status": "open"},
        post_fm={"status": "completed"},
        scope="hypatia",
    )
    assert counts["removed"] == 1
    moc = vault_read(hypatia_vault, "MOC/_Open Research Pointers.md")
    assert "[[research-pointer/RP1]]" not in moc["body"]


# ---------------------------------------------------------------------------
# vault_create end-to-end
# ---------------------------------------------------------------------------


def test_vault_create_question_open_status_auto_creates_moc_and_adds_bullet(
    hypatia_vault: Path,
) -> None:
    """Fresh create of question with default status=open → MOC auto-
    created + bullet added."""
    vault_create(
        hypatia_vault,
        "question",
        "What is the dichotomy of control",
        scope="hypatia",
    )

    moc_path = hypatia_vault / "MOC/_Open Questions.md"
    assert moc_path.exists()
    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/What is the dichotomy of control]]" in moc["body"]


def test_vault_create_question_refined_status_also_adds(
    hypatia_vault: Path,
) -> None:
    """status=refined is also a qualifying status per the predicate."""
    vault_create(
        hypatia_vault,
        "question",
        "Why does the Buddha smile",
        set_fields={"status": "refined"},
        scope="hypatia",
    )
    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Why does the Buddha smile]]" in moc["body"]


def test_vault_create_question_answered_status_does_not_add(
    hypatia_vault: Path,
) -> None:
    """Operator imports a pre-answered question with status=answered.
    The predicate is False → no MOC create, no bullet."""
    vault_create(
        hypatia_vault,
        "question",
        "Pre-answered Q",
        set_fields={"status": "answered"},
        scope="hypatia",
    )
    assert not (hypatia_vault / "MOC/_Open Questions.md").exists()


def test_vault_create_research_pointer_open_status_adds(
    hypatia_vault: Path,
) -> None:
    vault_create(
        hypatia_vault,
        "research-pointer",
        "Read Hadot on spiritual exercises",
        scope="hypatia",
    )
    moc = vault_read(hypatia_vault, "MOC/_Open Research Pointers.md")
    assert (
        "- [[research-pointer/Read Hadot on spiritual exercises]]"
        in moc["body"]
    )


def test_vault_create_research_pointer_in_progress_does_not_add(
    hypatia_vault: Path,
) -> None:
    """Only status=open qualifies for research-pointer; in-progress
    does not."""
    vault_create(
        hypatia_vault,
        "research-pointer",
        "Already started",
        set_fields={"status": "in-progress"},
        scope="hypatia",
    )
    assert not (
        hypatia_vault / "MOC/_Open Research Pointers.md"
    ).exists()


def test_vault_create_multiple_questions_all_listed(
    hypatia_vault: Path,
) -> None:
    """Three questions, all open → MOC has 3 bullets."""
    for n in range(1, 4):
        vault_create(
            hypatia_vault,
            "question",
            f"Q{n}",
            scope="hypatia",
        )
    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Q1]]" in moc["body"]
    assert "- [[question/Q2]]" in moc["body"]
    assert "- [[question/Q3]]" in moc["body"]


# ---------------------------------------------------------------------------
# vault_edit end-to-end — status transitions
# ---------------------------------------------------------------------------


def test_vault_edit_close_question_removes_bullet(
    hypatia_vault: Path,
) -> None:
    """Operator answers a question (status: open → answered);
    inventory MOC bullet is removed."""
    vault_create(
        hypatia_vault, "question", "Q1", scope="hypatia",
    )
    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Q1]]" in moc["body"]

    vault_edit(
        hypatia_vault,
        "question/Q1.md",
        set_fields={"status": "answered"},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "[[question/Q1]]" not in moc["body"]


def test_vault_edit_reopen_question_re_adds_bullet(
    hypatia_vault: Path,
) -> None:
    """Operator re-opens an answered question; bullet re-appears."""
    vault_create(
        hypatia_vault, "question", "Q1",
        set_fields={"status": "answered"}, scope="hypatia",
    )
    # No MOC created yet.
    assert not (hypatia_vault / "MOC/_Open Questions.md").exists()

    vault_edit(
        hypatia_vault,
        "question/Q1.md",
        set_fields={"status": "open"},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Q1]]" in moc["body"]


def test_vault_edit_unrelated_field_no_remove_no_add(
    hypatia_vault: Path,
) -> None:
    """Editing tags on a still-open question doesn't change the MOC."""
    vault_create(
        hypatia_vault, "question", "Q1", scope="hypatia",
    )
    moc_before = vault_read(hypatia_vault, "MOC/_Open Questions.md")

    vault_edit(
        hypatia_vault,
        "question/Q1.md",
        set_fields={"tags": ["#Stoicism"]},
        scope="hypatia",
    )

    moc_after = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    # Bullet still present, no duplicate.
    assert moc_after["body"].count("[[question/Q1]]") == 1
    # Bullet unchanged.
    assert "[[question/Q1]]" in moc_before["body"]
    assert "[[question/Q1]]" in moc_after["body"]


def test_vault_edit_refined_to_answered_removes(
    hypatia_vault: Path,
) -> None:
    """refined is still qualifying; refined → answered is the
    transition that removes."""
    vault_create(
        hypatia_vault, "question", "Q1",
        set_fields={"status": "refined"}, scope="hypatia",
    )
    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Q1]]" in moc["body"]

    vault_edit(
        hypatia_vault,
        "question/Q1.md",
        set_fields={"status": "answered"},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "[[question/Q1]]" not in moc["body"]


def test_vault_edit_open_to_refined_keeps_bullet(
    hypatia_vault: Path,
) -> None:
    """open → refined: both qualifying, bullet stays (no duplicate)."""
    vault_create(
        hypatia_vault, "question", "Q1", scope="hypatia",
    )

    vault_edit(
        hypatia_vault,
        "question/Q1.md",
        set_fields={"status": "refined"},
        scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert moc["body"].count("[[question/Q1]]") == 1


def test_vault_edit_multi_record_state(hypatia_vault: Path) -> None:
    """Three questions exist (Q1=open, Q2=refined, Q3=answered).
    Q1 → answered. Then the MOC has only Q2."""
    vault_create(hypatia_vault, "question", "Q1", scope="hypatia")
    vault_create(
        hypatia_vault, "question", "Q2",
        set_fields={"status": "refined"}, scope="hypatia",
    )
    vault_create(
        hypatia_vault, "question", "Q3",
        set_fields={"status": "answered"}, scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "- [[question/Q1]]" in moc["body"]
    assert "- [[question/Q2]]" in moc["body"]
    assert "[[question/Q3]]" not in moc["body"]

    vault_edit(
        hypatia_vault, "question/Q1.md",
        set_fields={"status": "answered"}, scope="hypatia",
    )

    moc = vault_read(hypatia_vault, "MOC/_Open Questions.md")
    assert "[[question/Q1]]" not in moc["body"]
    assert "- [[question/Q2]]" in moc["body"]
    assert "[[question/Q3]]" not in moc["body"]


# ---------------------------------------------------------------------------
# Log emissions (per feedback_log_emission_test_pattern.md)
# ---------------------------------------------------------------------------


def test_log_inventory_moc_created_on_first_qualifying_create(
    hypatia_vault: Path,
) -> None:
    with structlog.testing.capture_logs() as captured:
        vault_create(
            hypatia_vault, "question", "Q1", scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.inventory_moc_created"
    ]
    assert len(matches) == 1
    assert matches[0]["moc_rel_path"] == "MOC/_Open Questions.md"
    assert matches[0]["moc_name"] == "_Open Questions"


def test_log_inventory_moc_written_on_add(hypatia_vault: Path) -> None:
    with structlog.testing.capture_logs() as captured:
        vault_create(
            hypatia_vault, "question", "Q1", scope="hypatia",
        )

    add_matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.inventory_moc_written"
        and c.get("action") == "add"
    ]
    assert len(add_matches) == 1
    assert add_matches[0]["moc_rel_path"] == "MOC/_Open Questions.md"
    assert add_matches[0]["member_rel_path"] == "question/Q1.md"


def test_log_inventory_moc_written_on_remove(hypatia_vault: Path) -> None:
    vault_create(
        hypatia_vault, "question", "Q1", scope="hypatia",
    )

    with structlog.testing.capture_logs() as captured:
        vault_edit(
            hypatia_vault, "question/Q1.md",
            set_fields={"status": "answered"}, scope="hypatia",
        )

    remove_matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.inventory_moc_written"
        and c.get("action") == "remove"
    ]
    assert len(remove_matches) == 1
    assert remove_matches[0]["member_rel_path"] == "question/Q1.md"


def test_log_inventory_moc_dispatch_summary_emits_with_counts(
    hypatia_vault: Path,
) -> None:
    """Per feedback_intentionally_left_blank: every dispatch call
    emits a summary log even for no-op cases."""
    with structlog.testing.capture_logs() as captured:
        dispatch_inventory_mocs(
            hypatia_vault,
            "question/Q1.md",
            "question",
            pre_fm={"status": "answered"},
            post_fm={"status": "superseded"},
            scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.inventory_moc_dispatch_summary"
    ]
    assert len(matches) == 1
    assert matches[0]["member_type"] == "question"
    assert matches[0]["matched_entries"] == 1
    assert matches[0]["added"] == 0
    assert matches[0]["removed"] == 0
    assert matches[0]["skipped"] == 1


def test_log_inventory_moc_dispatch_summary_no_match(
    hypatia_vault: Path,
) -> None:
    """Non-trigger type — summary log still emits with
    matched_entries=0 so operator can grep dispatch activity."""
    with structlog.testing.capture_logs() as captured:
        dispatch_inventory_mocs(
            hypatia_vault,
            "zettel/Z1.md",
            "zettel",
            pre_fm=None,
            post_fm={"status": "open"},
            scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.inventory_moc_dispatch_summary"
    ]
    assert len(matches) == 1
    assert matches[0]["matched_entries"] == 0

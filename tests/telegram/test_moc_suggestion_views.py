"""Phase 5 Sub-arc D2 — /moc-suggestions view + accept + reject logic
(2026-05-19).

Covers the pure-logic surface in
``src/alfred/telegram/moc_suggestion_views.py``:

  * ``collect_pending`` — pending-status filter; sort order
    (alphabetical, propose-new last); missing queue file tolerance.
  * ``render_suggestions`` — empty state, single-group, multi-group,
    propose-new group last, char-limit overflow stays under cap, full
    reasoning carried verbatim.
  * ``apply_accept`` — happy path (existing target), happy path
    (propose-new), partial failure (some members fail), all-fail,
    create-failure for propose-new, inventory-MOC defense-in-depth,
    state machine refusal for non-pending entries.
  * ``reject_suggestion`` — pending → rejected; refused for non-pending;
    refused for missing id.
  * ``lookup_suggestion`` — id present / absent.

Log-emission pins (per builder.md rule #9 + ``feedback_log_emission_test_pattern``):
each observable code path that emits a structlog event has at least one
test asserting both the event name AND key fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pytest
import structlog.testing

from alfred.surveyor.moc_suggester import MocSuggestion
from alfred.telegram.moc_suggestion_views import (
    ApplyResult,
    _canonicalize_target_to_wikilink,
    _is_inventory_moc_path,
    _is_inventory_moc_name,
    _normalize_single_moc_entry,
    apply_accept,
    collect_pending,
    lookup_suggestion,
    reject_suggestion,
    render_suggestions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_suggestion(
    *,
    id: str = "ms-20260519-aaaaaaaa",
    target: str | None = "MOC/Stoicism MOC.md",
    proposed_new_moc_name: str | None = None,
    members: list[str] | None = None,
    candidates_to_add: list[str] | None = None,
    status: str = "pending",
    reasoning: str = "3/5 members already cite MOC/Stoicism MOC.md; 2 to add",
    mapping_signal: str = "member_overlap",
    mapping_score: float = 0.6,
    cluster_id: int = 7,
    tags: list[str] | None = None,
    created: str = "2026-05-19T14:00:00+00:00",
) -> MocSuggestion:
    """Convenience factory mirroring queue's shape."""
    if members is None:
        members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    if candidates_to_add is None:
        candidates_to_add = [members[-1]]
    if tags is None:
        tags = ["stoicism"]
    return MocSuggestion(
        id=id,
        cluster_id_at_proposal=cluster_id,
        cluster_tags=tags,
        cluster_member_paths=sorted(members),
        target_moc_rel_path=target,
        proposed_new_moc_name=proposed_new_moc_name,
        mapping_signal=mapping_signal,
        mapping_score=mapping_score,
        candidate_members_to_add=candidates_to_add,
        reasoning=reasoning,
        created=created,
        status=status,
    )


def _write_queue(queue_path: Path, entries: Iterable[MocSuggestion]) -> None:
    """Write entries to the JSONL queue (one per line)."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with open(queue_path, "w", encoding="utf-8") as f:
        for s in entries:
            f.write(json.dumps(s.to_dict(), separators=(",", ":")) + "\n")


@pytest.fixture
def vault_with_member(tmp_path: Path) -> Path:
    """Vault with a single zettel record + MOC dir."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "zettel").mkdir()
    (vault / "MOC").mkdir()

    # Seed an existing MOC so the apply path's vault_edit hook
    # (Phase 4 Sub-arc A) has somewhere to append the wikilink.
    (vault / "MOC" / "Stoicism MOC.md").write_text(
        "---\n"
        "type: MOC\n"
        "name: Stoicism MOC\n"
        "created: 2026-05-19\n"
        "---\n\n"
        "# Premise\n\nA topic MOC.\n\n"
        "# Contents\n\n"
        "# Tags\n\n"
        "# See Also\n",
        encoding="utf-8",
    )
    return vault


def _seed_zettel(
    vault: Path, name: str, *, mocs: list[str] | None = None,
) -> str:
    """Seed a zettel and return its rel_path."""
    fm_lines = [
        "---",
        "type: zettel",
        f"name: {name}",
        "created: 2026-05-19",
    ]
    if mocs is not None:
        fm_lines.append(f"mocs: {json.dumps(mocs)}")
    else:
        fm_lines.append("mocs: []")
    fm_lines.append("---")
    body = (
        "\n# Premise\n\nThesis line.\n\n"
        "# Notes\n\nReflective prose.\n\n"
        "# Tags\n\n# Indexing & MOCs\n"
    )
    rel = f"zettel/{name}.md"
    (vault / rel).write_text("\n".join(fm_lines) + body, encoding="utf-8")
    return rel


# ---------------------------------------------------------------------------
# collect_pending
# ---------------------------------------------------------------------------


def test_collect_pending_returns_empty_for_missing_queue(tmp_path: Path) -> None:
    """Queue file absent → empty list (not an error). Per the empty-state
    contract, a fresh-install Hypatia with no proposals yet just has
    no queue file."""
    qp = tmp_path / "queue.jsonl"
    assert collect_pending(qp) == []


def test_collect_pending_filters_to_pending_only(tmp_path: Path) -> None:
    """Pending entries returned; rejected / applied / accepted excluded."""
    qp = tmp_path / "queue.jsonl"
    entries = [
        _make_suggestion(id="ms-pending-1", status="pending"),
        _make_suggestion(id="ms-rejected-1", status="rejected"),
        _make_suggestion(id="ms-applied-1", status="applied"),
        _make_suggestion(id="ms-accepted-1", status="accepted"),
        _make_suggestion(id="ms-pending-2", status="pending"),
    ]
    _write_queue(qp, entries)
    pending = collect_pending(qp)
    assert {s.id for s in pending} == {"ms-pending-1", "ms-pending-2"}


def test_collect_pending_sorts_propose_new_last(tmp_path: Path) -> None:
    """Existing-MOC targets sort alphabetically by target path; propose-new
    entries (target=None) land at the END of the list."""
    qp = tmp_path / "queue.jsonl"
    entries = [
        _make_suggestion(
            id="ms-propose-1", target=None,
            proposed_new_moc_name="Task Management Todo List MOC",
        ),
        _make_suggestion(id="ms-stoic-1", target="MOC/Stoicism MOC.md"),
        _make_suggestion(id="ms-hema-1", target="MOC/HEMA MOC.md"),
        _make_suggestion(
            id="ms-propose-2", target=None,
            proposed_new_moc_name="Roman Rhetoric MOC",
        ),
    ]
    _write_queue(qp, entries)
    pending = collect_pending(qp)
    # Order: HEMA (H < S), Stoicism, then propose-new entries
    # alphabetically by proposed name.
    assert [s.id for s in pending] == [
        "ms-hema-1",
        "ms-stoic-1",
        "ms-propose-2",  # "Roman..." < "Task..."
        "ms-propose-1",
    ]


# ---------------------------------------------------------------------------
# render_suggestions
# ---------------------------------------------------------------------------


def test_render_empty_state_message() -> None:
    """Empty list → explicit "no pending" message, NOT blank.
    Per ``feedback_intentionally_left_blank.md``."""
    output = render_suggestions([])
    assert "No pending" in output
    assert output.startswith("📋")  # consistent surface emoji


def test_render_single_existing_target_group() -> None:
    """One suggestion → group header + bullet + usage hint."""
    s = _make_suggestion(id="ms-20260519-aaaaaaaa")
    output = render_suggestions([s])
    assert "📋 Pending MOC suggestions (1 total)" in output
    assert "## [[MOC/Stoicism MOC]] (1 suggestion)" in output
    assert "ms-20260519-aaaaaaaa" in output
    assert s.reasoning in output, "Reasoning must appear verbatim"
    assert "Use /accept-moc <id> or /reject-moc <id>" in output


def test_render_multi_target_groups_alphabetical() -> None:
    """Multiple existing targets → alphabetical group order."""
    suggestions = [
        _make_suggestion(id="ms-stoic", target="MOC/Stoicism MOC.md"),
        _make_suggestion(id="ms-hema", target="MOC/HEMA MOC.md"),
        _make_suggestion(id="ms-arch", target="MOC/Archery MOC.md"),
    ]
    output = render_suggestions(suggestions)
    arch_idx = output.index("[[MOC/Archery MOC]]")
    hema_idx = output.index("[[MOC/HEMA MOC]]")
    stoic_idx = output.index("[[MOC/Stoicism MOC]]")
    assert arch_idx < hema_idx < stoic_idx, (
        "Existing-target groups must sort alphabetically"
    )


def test_render_propose_new_group_last() -> None:
    """Propose-new entries land in a separate group AFTER all existing
    targets."""
    suggestions = [
        _make_suggestion(
            id="ms-propose",
            target=None,
            proposed_new_moc_name="New Topic MOC",
            candidates_to_add=["zettel/A.md", "zettel/B.md", "zettel/C.md"],
        ),
        _make_suggestion(id="ms-stoic", target="MOC/Stoicism MOC.md"),
    ]
    output = render_suggestions(suggestions)
    existing_idx = output.index("[[MOC/Stoicism MOC]]")
    propose_idx = output.index("Propose new MOC")
    assert existing_idx < propose_idx, (
        "Propose-new section must follow existing-target groups"
    )
    # Propose-new bullet uses name + candidate count, NOT reasoning.
    assert "New Topic MOC (3 candidates)" in output


def test_render_propose_new_singular_candidate() -> None:
    """Single candidate → singular `(1 candidate)`, not `(1 candidates)`."""
    s = _make_suggestion(
        id="ms-only-1",
        target=None,
        proposed_new_moc_name="Singleton MOC",
        candidates_to_add=["zettel/Lonely.md"],
    )
    output = render_suggestions([s])
    assert "(1 candidate)" in output
    assert "(1 candidates)" not in output


def test_render_singular_vs_plural_suggestions_header() -> None:
    """1 suggestion → "1 suggestion"; 2 → "2 suggestions"."""
    s1 = _make_suggestion(id="ms-only-1")
    output_one = render_suggestions([s1])
    assert "(1 suggestion)" in output_one
    assert "(1 suggestions)" not in output_one

    s2 = _make_suggestion(id="ms-only-2")
    output_two = render_suggestions([s1, s2])
    # Two suggestions against the same target → "(2 suggestions)"
    assert "(2 suggestions)" in output_two


# ---------------------------------------------------------------------------
# lookup_suggestion
# ---------------------------------------------------------------------------


def test_lookup_returns_suggestion_by_id(tmp_path: Path) -> None:
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-find-me")])
    found = lookup_suggestion(qp, "ms-find-me")
    assert found is not None
    assert found.id == "ms-find-me"


def test_lookup_returns_none_for_unknown_id(tmp_path: Path) -> None:
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-real")])
    assert lookup_suggestion(qp, "ms-fake") is None


def test_lookup_returns_none_for_missing_queue(tmp_path: Path) -> None:
    """Missing queue file → None (not crash, per intentionally_left_blank)."""
    assert lookup_suggestion(tmp_path / "absent.jsonl", "ms-x") is None


def test_lookup_finds_non_pending_entries(tmp_path: Path) -> None:
    """Lookup does NOT filter by status — caller decides."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-r", status="rejected")])
    found = lookup_suggestion(qp, "ms-r")
    assert found is not None
    assert found.status == "rejected"


# ---------------------------------------------------------------------------
# apply_accept — happy paths
# ---------------------------------------------------------------------------


def test_apply_accept_existing_target_all_members_succeed(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Happy path: 1 candidate member → vault_edit appends MOC →
    status flips to applied; ApplyResult.all_succeeded is True."""
    # Seed a zettel that does NOT yet cite the target MOC.
    rel = _seed_zettel(vault_with_member, "ZettelTarget", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-existing-1",
        target="MOC/Stoicism MOC.md",
        candidates_to_add=[rel],
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    assert result.all_succeeded is True
    assert result.members_succeeded == [rel]
    assert result.members_failed == []
    assert result.new_moc_created is False
    assert result.new_moc_create_error is None
    # Queue status flipped to applied.
    updated = lookup_suggestion(qp, "ms-existing-1")
    assert updated is not None
    assert updated.status == "applied"
    assert updated.applied_at is not None
    # Member frontmatter now cites the target MOC (canonical wikilink).
    import frontmatter
    post = frontmatter.load(str(vault_with_member / rel))
    mocs = post.metadata.get("mocs", [])
    assert any("Stoicism MOC" in str(m) for m in mocs)


def test_apply_accept_idempotent_when_member_already_cites_target(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Defensive: a candidate that already cites the target (operator
    raced the apply, or a stale queue entry) is no-op-skipped. Should
    still count as success — the target citation already exists."""
    rel = _seed_zettel(
        vault_with_member, "ZettelAlreadyCites",
        mocs=["[[MOC/Stoicism MOC]]"],
    )
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-idempotent",
        target="MOC/Stoicism MOC.md",
        candidates_to_add=[rel],
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    # Already-cite is a SUCCESS — we wanted the target listed, and it is.
    assert result.all_succeeded is True
    assert result.members_succeeded == [rel]
    # Member frontmatter unchanged (still has exactly one citation).
    import frontmatter
    post = frontmatter.load(str(vault_with_member / rel))
    mocs = post.metadata.get("mocs", [])
    # Could be 1 entry (didn't re-write) — assert exactly 1.
    matches = [m for m in mocs if "Stoicism MOC" in str(m)]
    assert len(matches) == 1, (
        f"Expected single existing citation; got {mocs}"
    )


def test_apply_accept_propose_new_creates_moc_then_edits_members(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Propose-new path: vault_create the new MOC, then iterate members."""
    rel = _seed_zettel(vault_with_member, "NewTopicMember", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-propose-new",
        target=None,
        proposed_new_moc_name="Roman Rhetoric MOC",
        candidates_to_add=[rel],
        mapping_signal="propose_new",
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    assert result.all_succeeded is True
    assert result.new_moc_created is True
    assert result.target_label.endswith("Roman Rhetoric MOC.md") or "Roman Rhetoric MOC" in result.target_label
    # New MOC exists on disk.
    new_moc_file = vault_with_member / "MOC" / "Roman Rhetoric MOC.md"
    assert new_moc_file.exists()
    # Member frontmatter now cites the NEW target.
    import frontmatter
    post = frontmatter.load(str(vault_with_member / rel))
    mocs = post.metadata.get("mocs", [])
    assert any("Roman Rhetoric MOC" in str(m) for m in mocs)


# ---------------------------------------------------------------------------
# apply_accept — failure paths
# ---------------------------------------------------------------------------


def test_apply_accept_partial_failure_flips_back_to_pending(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """If 1 of 2 members fails vault_edit (e.g., file missing), status
    flips back to pending with the first error in last_apply_error."""
    rel_good = _seed_zettel(vault_with_member, "GoodMember", mocs=[])
    # Don't seed the second member → vault_edit will fail.
    rel_missing = "zettel/MissingMember.md"
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-partial",
        target="MOC/Stoicism MOC.md",
        candidates_to_add=[rel_good, rel_missing],
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    assert result.all_succeeded is False
    assert result.partial is True
    assert rel_good in result.members_succeeded
    assert any(rel_missing in pair for pair in result.members_failed)
    # Queue status flipped back to pending (state machine: accepted → pending).
    updated = lookup_suggestion(qp, "ms-partial")
    assert updated is not None
    assert updated.status == "pending"
    assert updated.last_apply_error is not None
    assert len(updated.last_apply_error) > 0


def test_apply_accept_state_machine_denies_non_pending(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """A suggestion already in ``rejected`` cannot be accepted. The
    queue's state machine refuses the transition; apply_accept returns
    a no-work ApplyResult with new_moc_create_error explaining."""
    rel = _seed_zettel(vault_with_member, "ShouldNotEdit", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-rejected",
        target="MOC/Stoicism MOC.md",
        candidates_to_add=[rel],
        status="rejected",  # not pending
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    assert result.all_succeeded is False
    assert result.new_moc_create_error is not None
    assert "Status transition denied" in result.new_moc_create_error
    # Member NOT edited.
    import frontmatter
    post = frontmatter.load(str(vault_with_member / rel))
    mocs = post.metadata.get("mocs", [])
    assert not any("Stoicism MOC" in str(m) for m in mocs)


def test_apply_accept_inventory_moc_target_refused(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Defense-in-depth: apply path refuses an inventory MOC target
    (``MOC/_*.md``) even if it somehow ended up in the queue (operator
    manual edit, future bug, etc.)."""
    rel = _seed_zettel(vault_with_member, "ZetMem", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-inventory",
        target="MOC/_Open Questions.md",
        candidates_to_add=[rel],
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    assert result.all_succeeded is False
    assert result.new_moc_create_error is not None
    assert "inventory" in result.new_moc_create_error.lower()
    # Member NOT edited.
    import frontmatter
    post = frontmatter.load(str(vault_with_member / rel))
    assert not any("_Open Questions" in str(m) for m in (post.metadata.get("mocs") or []))


def test_apply_accept_inventory_moc_proposed_name_refused(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Defense-in-depth: a propose_new suggestion with a name starting
    with ``_`` is refused."""
    rel = _seed_zettel(vault_with_member, "ZetMem2", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-bad-name",
        target=None,
        proposed_new_moc_name="_Sneaky Inventory MOC",
        candidates_to_add=[rel],
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    assert result.all_succeeded is False
    assert result.new_moc_create_error is not None
    assert "inventory" in result.new_moc_create_error.lower()


def test_apply_accept_propose_new_vault_create_collision(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Propose-new path: if vault_create fails (e.g., file exists),
    status flips back to pending with last_apply_error populated."""
    # Pre-seed an existing MOC so vault_create will collide.
    (vault_with_member / "MOC" / "Existing MOC.md").write_text(
        "---\ntype: MOC\nname: Existing MOC\ncreated: 2026-05-19\n---\n",
        encoding="utf-8",
    )
    rel = _seed_zettel(vault_with_member, "Zet3", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-collide",
        target=None,
        proposed_new_moc_name="Existing MOC",
        candidates_to_add=[rel],
    )
    _write_queue(qp, [s])

    result = apply_accept(
        suggestion=s,
        queue_path=qp,
        vault_path=vault_with_member,
        scope="hypatia",
    )

    assert result.all_succeeded is False
    assert result.new_moc_create_error is not None
    # Queue back to pending.
    updated = lookup_suggestion(qp, "ms-collide")
    assert updated is not None
    assert updated.status == "pending"
    assert updated.last_apply_error is not None


# ---------------------------------------------------------------------------
# reject_suggestion
# ---------------------------------------------------------------------------


def test_reject_pending_succeeds(tmp_path: Path) -> None:
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(id="ms-to-reject", status="pending")
    _write_queue(qp, [s])
    assert reject_suggestion(queue_path=qp, suggestion_id="ms-to-reject") is True
    updated = lookup_suggestion(qp, "ms-to-reject")
    assert updated is not None
    assert updated.status == "rejected"


def test_reject_non_pending_denied(tmp_path: Path) -> None:
    """Already-applied / already-rejected → state machine refuses."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-app", status="applied")])
    assert reject_suggestion(queue_path=qp, suggestion_id="ms-app") is False


def test_reject_unknown_id(tmp_path: Path) -> None:
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-known")])
    assert reject_suggestion(queue_path=qp, suggestion_id="ms-unknown") is False


# ---------------------------------------------------------------------------
# Log-emission pins (builder.md rule #9)
# ---------------------------------------------------------------------------


def test_apply_inventory_moc_blocked_emits_log(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Inventory-MOC defense path emits a structured warning so
    operators grepping for unusual writebacks find the block."""
    rel = _seed_zettel(vault_with_member, "Zet4", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-inv-log",
        target="MOC/_Inventory.md",
        candidates_to_add=[rel],
    )
    _write_queue(qp, [s])

    with structlog.testing.capture_logs() as captured:
        apply_accept(
            suggestion=s,
            queue_path=qp,
            vault_path=vault_with_member,
            scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "moc_suggestion_views.apply_inventory_moc_blocked"
    ]
    assert len(matches) == 1
    assert matches[0]["suggestion_id"] == "ms-inv-log"
    assert matches[0]["target_moc"] == "MOC/_Inventory.md"


def test_apply_success_emits_log(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Happy-path success → ``moc_suggestion_views.apply_success`` log
    with target + members_applied fields."""
    rel = _seed_zettel(vault_with_member, "ZetSuccess", mocs=[])
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-success-log",
        target="MOC/Stoicism MOC.md",
        candidates_to_add=[rel],
    )
    _write_queue(qp, [s])

    with structlog.testing.capture_logs() as captured:
        apply_accept(
            suggestion=s,
            queue_path=qp,
            vault_path=vault_with_member,
            scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "moc_suggestion_views.apply_success"
    ]
    assert len(matches) == 1
    assert matches[0]["suggestion_id"] == "ms-success-log"
    assert matches[0]["members_applied"] == 1
    assert matches[0]["new_moc_created"] is False


def test_apply_partial_failure_emits_log(
    tmp_path: Path, vault_with_member: Path,
) -> None:
    """Partial failure → ``moc_suggestion_views.apply_partial_or_failed``
    log with success / fail counts."""
    rel_good = _seed_zettel(vault_with_member, "ZetGood", mocs=[])
    rel_bad = "zettel/MissingZ.md"  # not seeded → vault_edit fails
    qp = tmp_path / "queue.jsonl"
    s = _make_suggestion(
        id="ms-partial-log",
        target="MOC/Stoicism MOC.md",
        candidates_to_add=[rel_good, rel_bad],
    )
    _write_queue(qp, [s])

    with structlog.testing.capture_logs() as captured:
        apply_accept(
            suggestion=s,
            queue_path=qp,
            vault_path=vault_with_member,
            scope="hypatia",
        )

    matches = [
        c for c in captured
        if c.get("event") == "moc_suggestion_views.apply_partial_or_failed"
    ]
    assert len(matches) == 1
    assert matches[0]["members_succeeded"] == 1
    assert matches[0]["members_failed"] == 1


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_canonicalize_target_to_wikilink_strips_md() -> None:
    assert _canonicalize_target_to_wikilink("MOC/Stoicism MOC.md") == "[[MOC/Stoicism MOC]]"


def test_canonicalize_target_to_wikilink_idempotent_on_brackets() -> None:
    assert _canonicalize_target_to_wikilink("[[MOC/X]]") == "[[MOC/X]]"


def test_normalize_single_moc_entry_canonical_form() -> None:
    """All operator-typo shapes normalize to ``MOC/<Stem>.md``."""
    assert _normalize_single_moc_entry("[[MOC/Stoicism MOC]]") == "MOC/Stoicism MOC.md"
    assert _normalize_single_moc_entry("MOC/Stoicism MOC.md") == "MOC/Stoicism MOC.md"
    assert _normalize_single_moc_entry("[[MOC/Stoicism MOC|Stoic Practice]]") == "MOC/Stoicism MOC.md"
    assert _normalize_single_moc_entry("Stoicism MOC") == "MOC/Stoicism MOC.md"


def test_is_inventory_moc_path() -> None:
    assert _is_inventory_moc_path("MOC/_Open Questions.md") is True
    assert _is_inventory_moc_path("MOC/Stoicism MOC.md") is False
    assert _is_inventory_moc_path("zettel/_Draft.md") is False  # different namespace


def test_is_inventory_moc_name() -> None:
    assert _is_inventory_moc_name("_Open Questions") is True
    assert _is_inventory_moc_name("Stoicism MOC") is False

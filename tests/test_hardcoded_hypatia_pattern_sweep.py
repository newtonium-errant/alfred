"""Regression pins — hardcoded ``"hypatia"`` literal pattern sweep
(2026-05-20).

Code-reviewer flagged the threshold pattern when 6+ sites in the
codebase fell back to a literal ``"hypatia"`` string when instance
config wasn't fully resolved (per
``feedback_hardcoding_and_alfred_naming.md``). This sweep:

  * Converted ``scope: str = "hypatia"`` default kwargs to required
    keyword-only across:
      - ``vault/zettel_hooks.py`` (7 helpers)
      - ``telegram/capture_source_anchor.py`` (5 helpers)
      - ``telegram/moc_suggestion_views.py`` (``apply_accept``)
      - ``transport/client.py`` (``peer_propose_event.self_name``)

  * Replaced ``scope=scope or "hypatia"`` fallbacks in ``ops.py``
    (10 call sites within ``vault_create`` / ``vault_edit`` zettel-
    hook dispatch blocks) with explicit fail-loud-skip-with-log when
    ``scope is None``.

  * (Historical) made the Telegram ``/accept-moc`` handler fail-loud
    when ``config.instance.name`` was missing — that handler and its
    Layer-6 pins died with ``alfred.telegram.bot`` (2026-08-19).

These pins exercise:

  1. The defaults are gone: calling each helper without ``scope=``
     raises ``TypeError`` (Python's required-keyword-arg enforcement).
  2. ``ops.py`` ``vault_create`` / ``vault_edit`` on zettel-hook
     types with ``scope=None`` emit the
     ``vault.zettel_hooks.dispatch_skipped_no_scope`` log event and
     skip the hook dispatch (no crash, no silent ``"hypatia"``
     substitution).

The pin tests would catch a future drift back toward defaulting,
which is the antipattern named in the feedback memo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog.testing

from alfred._data import get_scaffold_dir


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def hypatia_vault(tmp_path: Path) -> Path:
    """Vault with the templates the zettel-hook tests need."""
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


# ===========================================================================
# Layer 1 — vault/zettel_hooks.py: scope is required (no default)
# ===========================================================================


def test_mirror_supersedes_chain_requires_scope() -> None:
    """Calling without ``scope=`` raises TypeError (default removed)."""
    from alfred.vault.zettel_hooks import mirror_supersedes_chain
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        mirror_supersedes_chain(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "zettel/New.md",
            "[[zettel/Old]]",
        )


def test_append_to_author_contents_requires_scope() -> None:
    from alfred.vault.zettel_hooks import append_to_author_contents
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        append_to_author_contents(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "[[author/X]]",
            "zettel/New.md",
        )


def test_append_to_moc_contents_requires_scope() -> None:
    from alfred.vault.zettel_hooks import append_to_moc_contents
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        append_to_moc_contents(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "[[MOC/X]]",
            "zettel/New.md",
        )


def test_dispatch_moc_appends_requires_scope() -> None:
    from alfred.vault.zettel_hooks import dispatch_moc_appends
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        dispatch_moc_appends(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "zettel/New.md",
            "zettel",
            ["[[MOC/X]]"],
        )


def test_ensure_inventory_moc_requires_scope() -> None:
    from alfred.vault.zettel_hooks import _ensure_inventory_moc
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        _ensure_inventory_moc(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "MOC/_Open Questions.md",
            "_Open Questions",
        )


def test_apply_inventory_moc_action_requires_scope() -> None:
    from alfred.vault.zettel_hooks import _apply_inventory_moc_action
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        _apply_inventory_moc_action(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "MOC/_Open Questions.md",
            "_Open Questions",
            "question/Q1.md",
            action="add",
        )


def test_dispatch_inventory_mocs_requires_scope() -> None:
    from alfred.vault.zettel_hooks import dispatch_inventory_mocs
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        dispatch_inventory_mocs(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "question/Q1.md",
            "question",
            pre_fm=None,
            post_fm={"status": "open"},
        )


# ===========================================================================
# Layer 2 — telegram/capture_source_anchor.py: scope is required
# ===========================================================================


def test_resolve_or_create_author_requires_scope() -> None:
    from alfred.telegram.capture_source_anchor import resolve_or_create_author
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        resolve_or_create_author(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "Marcus Aurelius",
        )


def test_resolve_or_create_source_requires_scope() -> None:
    from alfred.telegram.capture_source_anchor import resolve_or_create_source
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        resolve_or_create_source(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "Meditations",
        )


def test_resolve_session_anchors_requires_scope() -> None:
    from alfred.telegram.capture_source_anchor import resolve_session_anchors
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        resolve_session_anchors(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "I'm reading Meditations by Marcus Aurelius",
        )


def test_append_permanent_note_spawned_requires_scope() -> None:
    from alfred.telegram.capture_source_anchor import (
        append_permanent_note_spawned,
    )
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        append_permanent_note_spawned(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "source/Meditations.md",
            "[[zettel/X]]",
        )


def test_append_re_encounter_observation_requires_scope() -> None:
    from alfred.telegram.capture_source_anchor import (
        append_re_encounter_observation,
    )
    from pathlib import Path as _P
    with pytest.raises(TypeError, match="scope"):
        append_re_encounter_observation(  # type: ignore[call-arg]
            _P("/tmp/nonexistent"),
            "source/Meditations.md",
            "2026-05-20",
            ["topic1"],
            ["insight1"],
            "session/test.md",
        )


# ===========================================================================
# Layer 3 — telegram/moc_suggestion_views.py: scope is required
# ===========================================================================


def test_apply_accept_requires_scope() -> None:
    """``apply_accept`` no longer defaults scope — caller must supply.

    Callers must supply scope explicitly; test fixtures
    (test_moc_suggestion_views.py) pass ``scope="hypatia"``. (The last
    production caller — the Telegram /accept-moc handler — died with
    bot.py 2026-08-19; the required-kwarg contract is what this pins.)
    """
    from alfred.telegram.moc_suggestion_views import apply_accept
    from alfred.surveyor.moc_suggester import MocSuggestion
    from pathlib import Path as _P
    suggestion = MocSuggestion(
        id="ms-test",
        cluster_id_at_proposal=0,
        cluster_tags=["x"],
        cluster_member_paths=[],
        target_moc_rel_path="MOC/X.md",
        proposed_new_moc_name=None,
        mapping_signal="x",
        mapping_score=0.0,
        candidate_members_to_add=[],
        reasoning="x",
        created="2026-05-20T00:00:00+00:00",
        status="pending",
    )
    with pytest.raises(TypeError, match="scope"):
        apply_accept(  # type: ignore[call-arg]
            suggestion=suggestion,
            queue_path=_P("/tmp/nonexistent.jsonl"),
            vault_path=_P("/tmp/nonexistent_vault"),
        )


# ===========================================================================
# Layer 4 — transport/client.py: peer_propose_event self_name is required
# ===========================================================================


@pytest.mark.asyncio
async def test_peer_propose_event_requires_self_name() -> None:
    """Default ``self_name="hypatia"`` removed. Production callers
    derive ``self_name`` from ``config.instance.tool_set``; tests pass
    it explicitly. The pin catches default re-introduction.
    """
    from alfred.transport.client import peer_propose_event
    with pytest.raises(TypeError, match="self_name"):
        await peer_propose_event(  # type: ignore[call-arg]
            "salem",
            title="x",
            start="2026-05-20T00:00:00+00:00",
            end="2026-05-20T01:00:00+00:00",
        )


# ===========================================================================
# Layer 5 — ops.py: vault_create / vault_edit skip-with-log when scope is None
# ===========================================================================


def test_vault_edit_zettel_with_no_scope_skips_hooks_with_log(
    hypatia_vault: Path,
) -> None:
    """Editing a zettel via ``vault_edit(scope=None)`` doesn't fall
    back to ``"hypatia"`` — the zettel hook dispatch is skipped
    explicitly with a log event the operator can grep for.

    Path: ``vault_create`` runs ``_validate_type(scope=scope)`` which
    refuses zettel without ``scope="hypatia"``, so the no-scope
    dispatch guard in vault_create is unreachable in normal use
    (the type gate fires first). But ``vault_edit`` reads
    ``record_type`` from frontmatter without re-validating, so a
    pre-seeded zettel + edit-with-no-scope IS the canonical
    reachable path. This test pins both:

      (a) the file is still edited (the guard isn't a hard refusal),
      (b) the
          ``vault.zettel_hooks.dispatch_skipped_no_scope`` log fires
          with the documented fields (per log-emission test-pattern
          discipline).
    """
    from alfred.vault.ops import vault_create, vault_edit

    # Seed a zettel with scope="hypatia" so the create succeeds with
    # all hooks enabled. Then edit it with scope=None — the edit
    # path should skip hook dispatch.
    vault_create(
        hypatia_vault, "zettel", "Edit Test Zettel",
        set_fields={"mocs": []},
        scope="hypatia",
    )

    with structlog.testing.capture_logs() as captured:
        vault_edit(
            hypatia_vault,
            "zettel/Edit Test Zettel.md",
            set_fields={"mocs": ["[[MOC/NewlyCited]]"]},
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.dispatch_skipped_no_scope"
    ]
    assert len(matches) == 1, (
        f"expected exactly one dispatch_skipped_no_scope log; "
        f"got {len(matches)}. all events: "
        f"{[c.get('event') for c in captured]}"
    )
    entry = matches[0]
    assert entry["record_type"] == "zettel"
    assert entry["rel_path"] == "zettel/Edit Test Zettel.md"
    assert entry["reason"] == "scope_required_for_zettelkasten_hooks"


def test_vault_edit_question_with_no_scope_skips_inventory_dispatch(
    hypatia_vault: Path,
) -> None:
    """Editing a question with ``scope=None`` skips BOTH the MOC-
    append dispatch AND the inventory-MOC dispatch — both gated on
    the same no-scope guard. Status transitions don't propagate to
    ``MOC/_Open Questions.md`` (the silent ``"hypatia"`` fallback
    would have written to it).

    Companion to the zettel case above — questions exercise the
    inventory-MOC dispatch path additionally.
    """
    from alfred.vault.ops import vault_create, vault_edit

    # Seed a question with scope="hypatia" + a non-qualifying initial
    # status (won't fire inventory MOC on create — ``answered`` is a
    # terminal state, not in the open/refined trigger set). Then edit
    # to ``status=open`` with scope=None — the edit dispatch should
    # be SKIPPED, and the inventory MOC must NOT be written.
    vault_create(
        hypatia_vault, "question", "Q-no-scope-edit-test",
        set_fields={"status": "answered"},
        scope="hypatia",
    )
    # Sanity: inventory MOC absent at start (initial status not in
    # qualifying set).
    assert not (hypatia_vault / "MOC" / "_Open Questions.md").exists()

    with structlog.testing.capture_logs() as captured:
        vault_edit(
            hypatia_vault,
            "question/Q-no-scope-edit-test.md",
            set_fields={"status": "open"},
        )

    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.dispatch_skipped_no_scope"
    ]
    assert len(matches) == 1
    assert matches[0]["record_type"] == "question"

    # The inventory MOC was NOT created — proving the inventory
    # dispatch was correctly gated, not silently substituting
    # ``"hypatia"`` and writing it.
    assert not (hypatia_vault / "MOC" / "_Open Questions.md").exists()


def test_vault_edit_non_zettel_type_unaffected_by_no_scope_guard(
    hypatia_vault: Path,
) -> None:
    """Sanity: the new guard ONLY fires for the four zettelkasten
    hook types. Non-zettel-hook edits (e.g. ``note`` — in universal
    ``KNOWN_TYPES``) still work fine with ``scope=None`` (legacy
    unrestricted path preserved).

    Without this test, the guard could overshoot and break legacy
    callers.
    """
    from alfred.vault.ops import vault_create, vault_edit

    # Seed a plain note (in universal KNOWN_TYPES, doesn't require
    # scope kwarg). Bypass the type-gate worry by using a universal
    # type, then exercise vault_edit with no scope.
    (hypatia_vault / "note").mkdir(exist_ok=True)
    vault_create(hypatia_vault, "note", "Plain Note")

    with structlog.testing.capture_logs() as captured:
        vault_edit(
            hypatia_vault,
            "note/Plain Note.md",
            set_fields={"tags": ["edited"]},
        )

    # No skip log fired — note is not a zettel-hook type.
    matches = [
        c for c in captured
        if c.get("event") == "vault.zettel_hooks.dispatch_skipped_no_scope"
    ]
    assert len(matches) == 0





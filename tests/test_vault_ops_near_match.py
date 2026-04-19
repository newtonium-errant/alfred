"""Tests for ``alfred.vault.ops._check_near_match``.

The near-match check is the dedup hard-gate inside ``vault_create``.
Without it, records like ``person/PocketPills.md`` and
``person/Pocketpills.md`` would coexist as two separate entities with
identical meaning, silently breaking every downstream link resolution
and clustering step.

These tests cover the three important branches:
  - no collision → returns ``None``
  - case-only collision → returns ``(canonical_path, message)``
  - exact match → returns ``None`` (same casing is the "already exists"
    path, handled elsewhere — not a near-match)
"""

from __future__ import annotations

from pathlib import Path

from alfred.vault.ops import _check_near_match


def _seed_person(vault: Path, name: str) -> None:
    """Create a bare person record with the given filename stem."""
    (vault / "person" / f"{name}.md").write_text(
        "---\ntype: person\nname: x\ncreated: 2026-04-19\n---\n", encoding="utf-8"
    )


class TestCheckNearMatch:
    def test_returns_none_when_no_collision(self, tmp_vault: Path) -> None:
        # Clean directory (aside from the fixture's seeded record) with no
        # similarly-named entries → nothing to warn about.
        result = _check_near_match(tmp_vault, "person", "Brand New Person")
        assert result is None

    def test_flags_case_only_collision(self, tmp_vault: Path) -> None:
        # Existing ``person/PocketPills.md``, new request for ``Pocketpills``
        # → must return the canonical existing path + a message pointing the
        # agent at ``vault_edit`` instead of creating a duplicate.
        _seed_person(tmp_vault, "PocketPills")

        result = _check_near_match(tmp_vault, "person", "Pocketpills")

        assert result is not None
        canonical, message = result
        assert canonical == "person/PocketPills.md"
        assert "PocketPills" in message
        assert "vault_edit" in message

    def test_exact_match_is_not_a_near_match(self, tmp_vault: Path) -> None:
        # Identical casing is the "already exists" case, handled by the
        # caller's exists-check — not something ``_check_near_match``
        # should flag. Keeping this as a hard contract prevents the
        # near-match path from hijacking the exact-duplicate error.
        _seed_person(tmp_vault, "Exact Name")

        result = _check_near_match(tmp_vault, "person", "Exact Name")

        assert result is None

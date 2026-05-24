"""Test cross-instance filesystem read for Shape B preferences.

Per ``project_operator_preferences_v1.md`` Hard Contract #7 + #8:
- Hypatia/KAL-LE read Salem's preferences via FILESYSTEM
  (``/home/andrew/alfred/vault/preference/``), NOT peer-protocol.
- Salem reads her own vault only.
- KAL-LE has no local preference records in V1 (but DOES read
  Salem's for inheriting universal Shape B rules).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from alfred.preferences.loader import load_active_preferences
from alfred.telegram.conversation import load_voice_preferences_block

from ._fixtures import write_preference


def test_hypatia_reads_salem_canonical_b1_universal(tmp_path: Path) -> None:
    """Hypatia's session-start picks up Salem's B1 universal records."""
    salem_vault = tmp_path / "salem-vault"
    write_preference(
        salem_vault, "universal-tone",
        name="Universal tone",
        shape="voice", scope="universal",
        policy_body="Universal voice directive — applies everywhere.",
    )

    hypatia_vault = tmp_path / "library-alexandria"
    (hypatia_vault / "preference").mkdir(parents=True)
    # No local Hypatia prefs.

    block = load_voice_preferences_block(
        vault_path=hypatia_vault,
        instance_name="Hypatia",
        canonical_vault_path=salem_vault,
    )
    assert block is not None
    assert "Universal voice directive" in block


def test_hypatia_reads_salem_canonical_b2_instance_for_hypatia(
    tmp_path: Path,
) -> None:
    """Salem's B2 record ``applies_to_instance: Hypatia`` reaches Hypatia."""
    salem_vault = tmp_path / "salem-vault"
    write_preference(
        salem_vault, "hyp-specific-canonical",
        name="Hypatia-specific from canonical",
        shape="voice", scope="instance",
        applies_to_instance="Hypatia",
        policy_body="Hypatia-only directive set by Salem (canonical authority).",
    )

    hypatia_vault = tmp_path / "library-alexandria"
    (hypatia_vault / "preference").mkdir(parents=True)

    block = load_voice_preferences_block(
        vault_path=hypatia_vault,
        instance_name="Hypatia",
        canonical_vault_path=salem_vault,
    )
    assert block is not None
    assert "Hypatia-only directive set by Salem" in block


def test_salem_reads_only_own_vault_not_hypatia(tmp_path: Path) -> None:
    """Salem does NOT read Hypatia's local preference directory.

    Salem IS the canonical authority — she has no reason to read
    Hypatia's local instance-application records. Hard Contract #7.
    """
    salem_vault = tmp_path / "salem-vault"
    (salem_vault / "preference").mkdir(parents=True)
    # No Salem prefs.

    hypatia_vault = tmp_path / "library-alexandria"
    write_preference(
        hypatia_vault, "hypatia-local-only",
        name="Hypatia local",
        shape="voice", scope="universal",
        policy_body="This is in Hypatia's vault — Salem should NOT see it.",
    )

    # Salem call site: canonical_vault_path is her own vault.
    block = load_voice_preferences_block(
        vault_path=salem_vault,
        instance_name="Salem",
        canonical_vault_path=salem_vault,
    )
    assert block is None  # Salem sees nothing — Hypatia's vault not read


def test_kalle_reads_salem_canonical_for_universal(tmp_path: Path) -> None:
    """KAL-LE has no local prefs in V1 but DOES inherit Salem's universals."""
    salem_vault = tmp_path / "salem-vault"
    write_preference(
        salem_vault, "universal-tone",
        name="Universal tone",
        shape="voice", scope="universal",
        policy_body="Universal voice directive.",
    )

    kalle_vault = tmp_path / "aftermath-lab"
    (kalle_vault / "preference").mkdir(parents=True)

    block = load_voice_preferences_block(
        vault_path=kalle_vault,
        instance_name="KAL-LE",
        canonical_vault_path=salem_vault,
    )
    assert block is not None
    assert "Universal voice directive" in block


def test_instance_filter_excludes_wrong_instance(tmp_path: Path) -> None:
    """A Salem record ``applies_to_instance: KAL-LE`` doesn't fire for Hypatia."""
    salem_vault = tmp_path / "salem-vault"
    write_preference(
        salem_vault, "kalle-only-from-salem",
        name="KAL-LE only",
        shape="voice", scope="instance",
        applies_to_instance="KAL-LE",
        policy_body="KAL-LE-targeted directive — should NOT reach Hypatia.",
    )

    hypatia_vault = tmp_path / "library-alexandria"
    (hypatia_vault / "preference").mkdir(parents=True)

    block = load_voice_preferences_block(
        vault_path=hypatia_vault,
        instance_name="Hypatia",
        canonical_vault_path=salem_vault,
    )
    assert block is None  # KAL-LE-targeted record doesn't apply to Hypatia


def test_loader_reads_arbitrary_vault_path(tmp_path: Path) -> None:
    """Loader is inert about which instance asks — reads whatever path it's given."""
    arbitrary = tmp_path / "any-vault"
    write_preference(
        arbitrary, "x",
        name="X",
        shape="voice", scope="universal",
    )

    prefs = load_active_preferences(arbitrary)
    assert len(prefs) == 1
    assert prefs[0].slug == "x"


def test_case_insensitive_instance_match(tmp_path: Path) -> None:
    """Instance-name comparison is case-insensitive (Hypatia / hypatia / HYPATIA)."""
    salem_vault = tmp_path / "salem"
    write_preference(
        salem_vault, "case-test",
        name="Case test",
        shape="voice", scope="instance",
        applies_to_instance="Hypatia",
        policy_body="Body.",
    )

    hypatia_vault = tmp_path / "hypatia"
    (hypatia_vault / "preference").mkdir(parents=True)

    for variant in ("Hypatia", "hypatia", "HYPATIA"):
        block = load_voice_preferences_block(
            vault_path=hypatia_vault,
            instance_name=variant,
            canonical_vault_path=salem_vault,
        )
        assert block is not None, f"variant {variant!r} should match"
        assert "Body" in block

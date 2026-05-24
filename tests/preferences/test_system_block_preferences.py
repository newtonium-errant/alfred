"""Test for talker system-block — Shape B voice preferences.

Per ``project_operator_preferences_v1.md`` Hard Contract:
- B records load via filesystem (not peer-protocol).
- Hypatia/KAL-LE read BOTH local vault AND Salem's canonical vault.
- Conflict resolution: local wins over canonical (via
  ``cites_canonical`` OR slug match).
- Empty block → omitted entirely (no header for zero preferences).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.telegram.conversation import (
    _build_system_blocks,
    load_voice_preferences_block,
)

from ._fixtures import write_preference


def test_voice_block_present_when_records_exist(tmp_path: Path) -> None:
    """Active Shape B record produces a block with the policy body."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "no-stop-opener",
        name="Avoid stop-prefix replies",
        shape="voice", scope="universal",
        policy_body="Don't open replies with the word 'stop'.",
    )

    block = load_voice_preferences_block(
        vault, "Salem",
        canonical_vault_path=vault,  # Salem reads only her own
    )
    assert block is not None
    assert "Operator voice preferences" in block
    assert "Don't open replies with the word 'stop'" in block
    assert "Avoid stop-prefix replies" in block


def test_voice_block_omitted_when_zero_active(tmp_path: Path) -> None:
    """Zero active voice preferences → None (caller omits block entirely)."""
    vault = tmp_path / "vault"
    (vault / "preference").mkdir(parents=True)
    # No records.

    block = load_voice_preferences_block(
        vault, "Salem", canonical_vault_path=vault,
    )
    assert block is None


def test_voice_block_omitted_when_pref_dir_missing(tmp_path: Path) -> None:
    """No preference/ directory at all → None."""
    vault = tmp_path / "empty"
    vault.mkdir()

    block = load_voice_preferences_block(
        vault, "Salem", canonical_vault_path=vault,
    )
    assert block is None


def test_universal_pref_applies_to_all_instances(tmp_path: Path) -> None:
    """A universal Shape B pref applies to whichever instance asks."""
    salem_vault = tmp_path / "salem"
    write_preference(
        salem_vault, "universal-rule",
        name="Universal",
        shape="voice", scope="universal",
        policy_body="Universal directive body.",
    )

    # Hypatia reads Salem's canonical vault; universal record applies.
    hypatia_vault = tmp_path / "hypatia"
    (hypatia_vault / "preference").mkdir(parents=True)

    block = load_voice_preferences_block(
        hypatia_vault, "Hypatia",
        canonical_vault_path=salem_vault,
    )
    assert block is not None
    assert "Universal directive body" in block


def test_instance_scoped_pref_targets_named_instance(tmp_path: Path) -> None:
    """``applies_to_instance: Hypatia`` does NOT fire for Salem."""
    salem_vault = tmp_path / "salem"
    write_preference(
        salem_vault, "hypatia-specific",
        name="Hypatia avoid stop",
        shape="voice", scope="instance",
        applies_to_instance="Hypatia",
        policy_body="Don't open with stop.",
    )

    # Salem asks → instance-specific record for Hypatia should NOT apply.
    block_salem = load_voice_preferences_block(
        salem_vault, "Salem", canonical_vault_path=salem_vault,
    )
    assert block_salem is None  # nothing applies to Salem

    # Hypatia asks → record applies (reads via canonical_vault_path).
    hypatia_vault = tmp_path / "hypatia"
    (hypatia_vault / "preference").mkdir(parents=True)
    block_hyp = load_voice_preferences_block(
        hypatia_vault, "Hypatia",
        canonical_vault_path=salem_vault,
    )
    assert block_hyp is not None
    assert "Don't open with stop" in block_hyp


def test_local_wins_over_canonical_via_cites_canonical(tmp_path: Path) -> None:
    """Local pref citing a canonical slug supersedes the canonical record."""
    salem_vault = tmp_path / "salem"
    write_preference(
        salem_vault, "canonical-tone",
        name="Canonical tone",
        shape="voice", scope="universal",
        policy_body="Canonical body — should NOT appear.",
    )

    hypatia_vault = tmp_path / "hypatia"
    write_preference(
        hypatia_vault, "hypatia-tone-override",
        name="Hypatia tone override",
        shape="voice", scope="instance",
        applies_to_instance="Hypatia",
        cites_canonical="[[preference/canonical-tone]]",
        policy_body="Local override body — should appear.",
    )

    block = load_voice_preferences_block(
        hypatia_vault, "Hypatia",
        canonical_vault_path=salem_vault,
    )
    assert block is not None
    assert "Local override body" in block
    assert "Canonical body" not in block


def test_local_wins_over_canonical_via_slug_collision(tmp_path: Path) -> None:
    """Local pref with the same slug as canonical → local wins (no cites needed)."""
    salem_vault = tmp_path / "salem"
    write_preference(
        salem_vault, "shared-slug",
        name="Salem version",
        shape="voice", scope="universal",
        policy_body="Salem body.",
    )

    hypatia_vault = tmp_path / "hypatia"
    write_preference(
        hypatia_vault, "shared-slug",
        name="Hypatia version",
        shape="voice", scope="universal",
        policy_body="Hypatia body.",
    )

    block = load_voice_preferences_block(
        hypatia_vault, "Hypatia",
        canonical_vault_path=salem_vault,
    )
    assert block is not None
    assert "Hypatia body" in block
    assert "Salem body" not in block


def test_build_system_blocks_includes_voice_block_when_present() -> None:
    """``_build_system_blocks`` accepts the new voice_preferences_block kwarg."""
    blocks = _build_system_blocks(
        "system prompt",
        "vault context",
        calibration_str="calibration",
        pushback_level=3,
        voice_preferences_block="## Operator voice preferences\n\nRule body.",
    )
    # 5 cacheable blocks + 1 today-block = 6 total (when voice block is present).
    assert len(blocks) == 6
    voice_blocks = [
        b for b in blocks
        if "Operator voice preferences" in b.get("text", "")
    ]
    assert len(voice_blocks) == 1


def test_build_system_blocks_omits_voice_block_when_none() -> None:
    """``voice_preferences_block=None`` → no voice block in output."""
    blocks = _build_system_blocks(
        "system prompt",
        "vault context",
        calibration_str="calibration",
        pushback_level=3,
        voice_preferences_block=None,
    )
    voice_blocks = [
        b for b in blocks
        if "Operator voice preferences" in b.get("text", "")
    ]
    assert len(voice_blocks) == 0


def test_voice_block_built_log_fires_with_count() -> None:
    """Per ``feedback_log_emission_test_pattern.md`` — pin the log event."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        vault = tmp / "vault"
        write_preference(
            vault, "test",
            name="Test",
            shape="voice", scope="universal",
            policy_body="Body.",
        )

        with structlog.testing.capture_logs() as captured:
            load_voice_preferences_block(
                vault, "Salem", canonical_vault_path=vault,
            )

        built = [
            c for c in captured
            if c.get("event") == "talker.preferences.voice_block_built"
        ]
        assert len(built) == 1
        assert built[0]["active_count"] == 1
        assert built[0]["instance"] == "Salem"

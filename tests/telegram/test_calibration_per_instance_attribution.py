"""Per-instance attribution slug for the calibration writer.

Item 3 of the deferred Hypatia hardcoding sweep
(``project_hardcoding_followups.md``): ``calibration.apply_proposals``
was hardcoded to ``agent="salem"``. Hypatia/KAL-LE-side calibration
prose was attributed to Salem in the audit log — bug-in-waiting once
cross-instance audit reconciliation logic ships.

This test exercises the new ``agent_slug`` keyword on
``apply_proposals``. The legacy default of ``"salem"`` is preserved by
the existing ``test_calibration_attribution.py`` suite.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.telegram import calibration


def _seed_person_with_calibration_block(tmp_path: Path, name: str = "Andrew") -> str:
    (tmp_path / "person").mkdir(exist_ok=True)
    file_path = tmp_path / "person" / f"{name}.md"
    file_path.write_text(
        f"---\ntype: person\nname: {name}\ncreated: '2026-04-26'\n---\n\n"
        f"# {name}\n\n"
        f"{calibration.CALIBRATION_MARKER_START}\n"
        "## Workflow Preferences\n\n"
        f"{calibration.CALIBRATION_MARKER_END}\n",
        encoding="utf-8",
    )
    return f"person/{name}.md"


def _read(file_path: Path) -> tuple[dict, str]:
    post = frontmatter.load(str(file_path))
    return dict(post.metadata), post.content


def _proposal() -> calibration.Proposal:
    return calibration.Proposal(
        subsection="Workflow Preferences",
        bullet="Wants concise replies in tactical mode.",
        confidence=0.85,
        source_session_rel="session/Voice Session — 2026-04-26 abc",
    )


def test_apply_proposals_stamps_hypatia_slug_when_passed(tmp_path: Path):
    """Hypatia-side calibration write stamps ``agent: hypatia``."""
    rel = _seed_person_with_calibration_block(tmp_path)

    result = calibration.apply_proposals(
        tmp_path,
        rel,
        [_proposal()],
        "session/Voice Session — 2026-04-26 abc",
        confirmation_dial=4,
        agent_slug="hypatia",
    )
    assert result["written"] is True

    fm, _ = _read(tmp_path / rel)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "hypatia"


def test_apply_proposals_stamps_salem_slug_explicit_pass(tmp_path: Path):
    """Explicit ``agent_slug="salem"`` lands ``agent: salem``."""
    rel = _seed_person_with_calibration_block(tmp_path)

    result = calibration.apply_proposals(
        tmp_path,
        rel,
        [_proposal()],
        "session/Voice Session — 2026-04-26 abc",
        confirmation_dial=4,
        agent_slug="salem",
    )
    assert result["written"] is True

    fm, _ = _read(tmp_path / rel)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "salem"


def test_apply_proposals_default_slug_preserves_legacy_behavior(tmp_path: Path):
    """No-keyword call keeps the historical ``"salem"`` default."""
    rel = _seed_person_with_calibration_block(tmp_path)

    result = calibration.apply_proposals(
        tmp_path,
        rel,
        [_proposal()],
        "session/Voice Session — 2026-04-26 abc",
        confirmation_dial=4,
    )
    assert result["written"] is True

    fm, _ = _read(tmp_path / rel)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "salem"

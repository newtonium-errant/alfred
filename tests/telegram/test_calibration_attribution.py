"""c4: ``apply_proposals`` stamps an attribution_audit entry per
subsection + wraps each subsection's bullets in BEGIN_INFERRED markers.

Closes the audit gap on the calibration write path — every bullet that
lands here is Sonnet-inferred from the session transcript and now
carries the marker contract that powers the Daily Sync confirm/reject
flow.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.telegram import calibration


def _seed_person_with_calibration_block(tmp_path: Path, name: str = "Andrew") -> str:
    (tmp_path / "person").mkdir(exist_ok=True)
    file_path = tmp_path / "person" / f"{name}.md"
    file_path.write_text(
        f"---\ntype: person\nname: {name}\ncreated: '2026-04-23'\n---\n\n"
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


def test_apply_proposals_wraps_bullets_in_inferred_markers(tmp_path: Path):
    rel = _seed_person_with_calibration_block(tmp_path)

    proposals = [
        calibration.Proposal(
            subsection="Workflow Preferences",
            bullet="Prefers terse replies in tactical mode.",
            confidence=0.8,
            source_session_rel="session/Voice Session — 2026-04-23 1234 abc",
        ),
    ]

    result = calibration.apply_proposals(
        tmp_path,
        rel,
        proposals,
        "session/Voice Session — 2026-04-23 1234 abc",
        confirmation_dial=4,
    )
    assert result["written"] is True

    fm, content = _read(tmp_path / rel)
    # Body contains the marker pair around the subsection bullet group.
    assert "BEGIN_INFERRED" in content
    assert "END_INFERRED" in content
    assert "Prefers terse replies" in content

    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    assert len(audit) == 1
    entry = audit[0]
    assert entry["agent"] == "salem"
    assert entry["section_title"] == "Calibration — Workflow Preferences"
    assert "calibration update" in entry["reason"]
    assert "Voice Session" in entry["reason"]
    assert entry["confirmed_by_andrew"] is False


def test_apply_proposals_one_audit_entry_per_subsection(tmp_path: Path):
    """Two proposals across two subsections produce two distinct
    audit entries — one per subsection."""
    rel = _seed_person_with_calibration_block(tmp_path)

    proposals = [
        calibration.Proposal(
            subsection="Workflow Preferences",
            bullet="Pref 1",
            confidence=0.8,
            source_session_rel="session/A",
        ),
        calibration.Proposal(
            subsection="Communication Style",
            bullet="Style 1",
            confidence=0.8,
            source_session_rel="session/A",
        ),
    ]

    result = calibration.apply_proposals(
        tmp_path, rel, proposals, "session/A", confirmation_dial=4,
    )
    assert result["written"] is True

    fm, content = _read(tmp_path / rel)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    assert len(audit) == 2
    titles = {e["section_title"] for e in audit}
    assert "Calibration — Workflow Preferences" in titles
    assert "Calibration — Communication Style" in titles
    # Each subsection group has its own marker pair (count of BEGIN
    # markers should be 2 — one per subsection).
    assert content.count("BEGIN_INFERRED") == 2


def test_apply_proposals_preserves_existing_audit(tmp_path: Path):
    """A subsequent calibration write must merge with prior
    attribution_audit entries on the record."""
    (tmp_path / "person").mkdir(exist_ok=True)
    file_path = tmp_path / "person" / "Andrew.md"
    file_path.write_text(
        "---\n"
        "type: person\n"
        "name: Andrew\n"
        "created: '2026-04-23'\n"
        "attribution_audit:\n"
        "  - marker_id: inf-20260420-salem-existing\n"
        "    agent: salem\n"
        "    date: '2026-04-20T00:00:00+00:00'\n"
        "    section_title: Old Section\n"
        "    reason: prior write\n"
        "    confirmed_by_andrew: false\n"
        "    confirmed_at: null\n"
        "---\n\n"
        f"# Andrew\n\n"
        f"{calibration.CALIBRATION_MARKER_START}\n"
        "## Workflow Preferences\n\n"
        f"{calibration.CALIBRATION_MARKER_END}\n",
        encoding="utf-8",
    )

    proposals = [
        calibration.Proposal(
            subsection="Workflow Preferences",
            bullet="New pref",
            confidence=0.8,
            source_session_rel="session/A",
        ),
    ]
    calibration.apply_proposals(
        tmp_path, "person/Andrew.md", proposals, "session/A",
        confirmation_dial=4,
    )

    fm, _ = _read(file_path)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    assert len(audit) == 2
    ids = {e["marker_id"] for e in audit}
    assert "inf-20260420-salem-existing" in ids


def test_apply_proposals_dial_zero_skips_attribution(tmp_path: Path):
    """Dial 0 returns early — no write happens, no audit_audit lands."""
    rel = _seed_person_with_calibration_block(tmp_path)

    proposals = [
        calibration.Proposal(
            subsection="Workflow Preferences",
            bullet="Won't land",
            confidence=0.9,
            source_session_rel="session/A",
        ),
    ]
    result = calibration.apply_proposals(
        tmp_path, rel, proposals, "session/A", confirmation_dial=0,
    )
    assert result["written"] is False

    fm, content = _read(tmp_path / rel)
    assert "BEGIN_INFERRED" not in content
    assert "attribution_audit" not in fm

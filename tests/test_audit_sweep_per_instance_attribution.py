"""Per-instance attribution slug for the c3 retroactive sweep.

Item 1 of the deferred Hypatia hardcoding sweep
(``project_hardcoding_followups.md``): ``audit.sweep`` was hardcoded
to ``agent="salem"`` regardless of which instance ran it. Hypatia or
KAL-LE running ``alfred audit infer-marker`` would land "salem" in
the audit entry — cosmetically wrong today (per-instance vault roots,
no cross-instance reading), bug-in-waiting once cross-instance audit
reconciliation logic ships.

This test file exercises the new ``agent_slug`` parameter on
:func:`alfred.audit.sweep.sweep_paths` so multi-instance attribution
attributes correctly. The default-slug shape is preserved by the
existing ``test_audit_sweep.py`` suite — the assertions there still
pass with ``agents == {"salem"}``.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.audit.sweep import sweep_paths


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _record_with_source(name: str, source: str) -> str:
    body = (
        f"# {name}\n\n"
        "<!-- ALFRED:CALIBRATION -->\n\n"
        "## Workflow Preferences\n\n"
        f"- Prefers terse replies. _source: {source}_\n\n"
        "<!-- END ALFRED:CALIBRATION -->\n"
    )
    return (
        "---\n"
        f"type: person\n"
        f"name: {name}\n"
        "created: '2026-04-26'\n"
        "---\n\n"
        f"{body}"
    )


def test_sweep_attributes_to_hypatia_when_slug_passed(tmp_path: Path):
    """A Hypatia-side sweep stamps ``agent: hypatia`` on the audit entry.

    The legacy default of ``"salem"`` is overridden by passing the
    running instance's slug — mirror of the
    :func:`alfred.audit.agent_slug_for` contract.
    """
    rec = _write(
        tmp_path, "person/Andrew.md",
        _record_with_source("Andrew", "session/Voice 2026-04-26 abc"),
    )

    result = sweep_paths(
        tmp_path, ["person/Andrew.md"], apply=True, agent_slug="hypatia",
    )
    assert result.marked == 1
    assert result.errors == []

    post = frontmatter.load(str(rec))
    audit = post.metadata.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "hypatia"


def test_sweep_attributes_to_kalle_when_slug_passed(tmp_path: Path):
    """KAL-LE-side sweep stamps ``agent: kalle`` (lowercased — the
    canonical name is K.A.L.L.E. but the slug strips dots for the
    marker_id contract).
    """
    # Source string mustn't contain underscores (the bullet-with-source
    # regex uses ``_..._`` italics as the delimiter — a path like
    # ``memory/feedback_use_aftermath_team.md`` matches the closing
    # delimiter at the first underscore inside the path).
    rec = _write(
        tmp_path, "person/Andrew.md",
        _record_with_source("Andrew", "memory/aftermath-team-feedback.md"),
    )

    result = sweep_paths(
        tmp_path, ["person/Andrew.md"], apply=True, agent_slug="kalle",
    )
    assert result.marked == 1

    post = frontmatter.load(str(rec))
    audit = post.metadata.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "kalle"


def test_sweep_default_slug_preserves_legacy_behavior(tmp_path: Path):
    """Calling ``sweep_paths`` without an explicit ``agent_slug`` keeps
    the historical ``"salem"`` default — the existing
    ``test_audit_sweep`` suite (which never threads a slug) continues
    to pass unchanged.
    """
    rec = _write(
        tmp_path, "person/Andrew.md",
        _record_with_source("Andrew", "session/X"),
    )

    result = sweep_paths(tmp_path, ["person/Andrew.md"], apply=True)
    assert result.marked == 1

    post = frontmatter.load(str(rec))
    audit = post.metadata.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "salem"

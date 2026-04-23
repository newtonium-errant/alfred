"""c4: curator Stage 2 ``_resolve_entities`` wraps the agent-composed
manifest body in BEGIN_INFERRED markers and appends an attribution_audit
entry to the new entity's frontmatter.

Stage 4 enrichment writes happen inside the curator agent subprocess
(out of reach for direct wrapping in c4 v1) — Stage 2 covers the
initial composition, which is the highest-stakes path.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.curator.pipeline import _resolve_entities


def _read(vault: Path, rel: str) -> tuple[dict, str]:
    post = frontmatter.load(str(vault / rel))
    return dict(post.metadata), post.content


def test_stage2_resolve_creates_entity_with_inferred_marker(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)

    manifest = [
        {
            "type": "person",
            "name": "Margo Belliveau",
            "description": "Veteran integration circle leader.",
            "fields": {"role": "leader"},
            "body": "# Margo Belliveau\n\nVeteran integration circle leader.\n",
        },
    ]
    resolved = _resolve_entities(manifest, vault, session_path="")
    assert "person/Margo Belliveau" in resolved

    fm, content = _read(vault, "person/Margo Belliveau.md")
    assert "BEGIN_INFERRED" in content
    assert "END_INFERRED" in content
    assert "Veteran integration circle leader" in content

    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    entry = audit[0]
    assert entry["agent"] == "curator"
    assert entry["section_title"] == "Margo Belliveau"
    assert "curator stage 2" in entry["reason"]


def test_stage2_resolve_existing_entity_no_new_marker(tmp_path: Path):
    """An entity that already exists in the vault must NOT be touched
    by Stage 2 — no new marker, no new audit entry."""
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    existing = vault / "person" / "Andrew.md"
    existing.write_text(
        "---\ntype: person\nname: Andrew\ncreated: '2026-04-23'\n---\n\n"
        "# Andrew\n\nUser-typed content.\n",
        encoding="utf-8",
    )

    manifest = [
        {
            "type": "person",
            "name": "Andrew",
            "description": "...",
            "fields": {},
            "body": "# Andrew\n\nAgent re-summary.\n",
        },
    ]
    _resolve_entities(manifest, vault, session_path="")

    fm, content = _read(vault, "person/Andrew.md")
    assert "BEGIN_INFERRED" not in content  # untouched
    assert "User-typed content" in content
    assert "attribution_audit" not in fm

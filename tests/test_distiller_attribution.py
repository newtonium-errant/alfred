"""c4: ``_mark_learn_record_inferred`` post-processes a freshly-
created learn record to wrap the body in BEGIN_INFERRED markers and
append an attribution_audit entry to frontmatter.

The distiller agent runs as a subprocess and writes via the
``alfred vault`` CLI; we can't intercept the write itself, so the
post-process pass is the contract surface for c4.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.distiller.pipeline import LearningSpec, _mark_learn_record_inferred


def _seed_learn_record(vault: Path, rel: str, body: str = "Salem inferred this claim from prior session.\n") -> Path:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "---\n"
        "type: assumption\n"
        "name: My Assumption\n"
        "created: '2026-04-23'\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return full


def test_mark_learn_record_wraps_body_and_adds_audit(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "assumption/My Assumption.md"
    full = _seed_learn_record(vault, rel)

    spec = LearningSpec(
        learn_type="assumption",
        title="My Assumption",
        confidence="high",
        status="proposed",
        claim="A claim",
        evidence_excerpts=[],
        source_links=["session/voice 1"],
        entity_links=[],
        project="",
    )
    _mark_learn_record_inferred(vault, rel, spec)

    post = frontmatter.load(str(full))
    fm = dict(post.metadata)
    body = post.content

    assert "BEGIN_INFERRED" in body
    assert "END_INFERRED" in body
    assert "Salem inferred this claim" in body

    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    assert len(audit) == 1
    entry = audit[0]
    assert entry["agent"] == "distiller"
    assert entry["section_title"] == "My Assumption"
    assert "distiller pipeline" in entry["reason"]
    assert "type=assumption" in entry["reason"]


def test_mark_learn_record_idempotent(tmp_path: Path):
    """Re-running the marker on the same body produces the same
    marker_id (deterministic) and the audit entry is replaced, not
    duplicated."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "decision/Test.md"
    full = _seed_learn_record(vault, rel, body="Inferred decision body.\n")
    spec = LearningSpec(
        learn_type="decision",
        title="Test",
        confidence="medium",
        status="proposed",
        claim="A",
        evidence_excerpts=[],
        source_links=[],
        entity_links=[],
        project="",
    )
    _mark_learn_record_inferred(vault, rel, spec)
    _mark_learn_record_inferred(vault, rel, spec)

    post = frontmatter.load(str(full))
    fm = dict(post.metadata)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    # Idempotent: still one entry.
    assert len(audit) == 1
    # Body still has exactly one BEGIN/END pair.
    assert post.content.count("BEGIN_INFERRED") == 1
    assert post.content.count("END_INFERRED") == 1


def test_mark_learn_record_skips_empty_body(tmp_path: Path):
    """A learn record with no body doesn't get a marker."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "constraint/Empty.md"
    full = _seed_learn_record(vault, rel, body="")

    spec = LearningSpec(
        learn_type="constraint",
        title="Empty",
        confidence="low",
        status="proposed",
        claim="x",
        evidence_excerpts=[],
        source_links=[],
        entity_links=[],
        project="",
    )
    _mark_learn_record_inferred(vault, rel, spec)
    post = frontmatter.load(str(full))
    assert "BEGIN_INFERRED" not in post.content
    assert "attribution_audit" not in dict(post.metadata)


def test_mark_learn_record_swallows_missing_file(tmp_path: Path):
    """Missing target file is logged + skipped, no exception raised."""
    vault = tmp_path / "vault"
    vault.mkdir()
    spec = LearningSpec(
        learn_type="synthesis",
        title="Missing",
        confidence="high",
        status="proposed",
        claim="x",
        evidence_excerpts=[],
        source_links=[],
        entity_links=[],
        project="",
    )
    # Should not raise.
    _mark_learn_record_inferred(vault, "synthesis/Nope.md", spec)

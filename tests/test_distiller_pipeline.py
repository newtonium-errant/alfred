"""Regression tests for ``_stage2_dedup_merge``'s defensive flatten guard.

The LLM occasionally returns ``source_links`` / ``entity_links`` as nested
``[[wikilink]]`` lists instead of flat ``[wikilink]`` strings. Without the
guard this caused ``TypeError`` at ``", ".join(spec.source_links)`` in
stage 3. Cherry-picked from upstream commit ``40f3df4``.
"""

from __future__ import annotations

from alfred.distiller.pipeline import _stage2_dedup_merge


def test_pipeline_handles_nested_source_links_in_llm_output():
    """Stage2 dedup-merge must flatten nested-list source_links/entity_links.

    Targets the merge-into-existing branch in ``_stage2_dedup_merge`` (the
    branch upstream commit ``40f3df4`` patched). Seed candidate has flat
    links; a second candidate with the same title carries nested-list links
    that must be flattened during merge, not appended as nested lists.
    """
    all_manifests = {
        "session/2026-04-28.md": [
            {
                "type": "assumption",
                "title": "Test Assumption",
                "claim": "test claim",
                "confidence": "medium",
                "status": "draft",
                # Seed: flat strings (this becomes the merged target).
                "source_links": ["[[session/2026-04-28]]"],
                "entity_links": ["[[person/Andrew]]"],
                "evidence_excerpt": "evidence A",
            },
        ],
        "session/2026-04-29.md": [
            {
                "type": "assumption",
                "title": "Test Assumption",  # same title — should merge into seed
                "claim": "test claim",
                "confidence": "medium",
                "status": "draft",
                # Merge candidate: mixed flat string + nested list (LLM quirk).
                "source_links": ["[[session/2026-04-29]]", ["[[session/2026-04-30]]"]],
                "entity_links": [["[[person/Bob]]", "[[person/Carol]]"]],
                "evidence_excerpt": "evidence B",
            },
        ],
    }

    specs = _stage2_dedup_merge(all_manifests, existing_learns=[])

    assert len(specs) == 1, "Same-title candidates should merge into one spec"
    spec = specs[0]

    # All source_links must be flat strings, not nested lists.
    for sl in spec.source_links:
        assert isinstance(sl, str), f"source_link {sl!r} should be a string, not {type(sl)}"
    for el in spec.entity_links:
        assert isinstance(el, str), f"entity_link {el!r} should be a string, not {type(el)}"

    # The nested wikilinks should have been flattened during merge.
    assert "[[session/2026-04-28]]" in spec.source_links  # from seed
    assert "[[session/2026-04-29]]" in spec.source_links  # from flat element
    assert "[[session/2026-04-30]]" in spec.source_links  # flattened from nested
    assert "[[person/Andrew]]" in spec.entity_links  # from seed
    assert "[[person/Bob]]" in spec.entity_links  # flattened from nested
    assert "[[person/Carol]]" in spec.entity_links  # flattened from nested

    # And the join that motivated the guard must not raise.
    joined = ", ".join(spec.source_links)
    assert "session/2026-04-28" in joined


def test_pipeline_flat_source_links_unchanged():
    """Flat string source_links must still pass through unchanged (no double-wrap)."""
    all_manifests = {
        "session/2026-04-28.md": [
            {
                "type": "decision",
                "title": "Flat Decision",
                "claim": "decided",
                "confidence": "high",
                "status": "draft",
                "source_links": ["[[session/2026-04-28]]"],
                "entity_links": ["[[person/Andrew]]"],
                "evidence_excerpt": "ev",
            },
        ],
    }

    specs = _stage2_dedup_merge(all_manifests, existing_learns=[])
    assert len(specs) == 1
    assert specs[0].source_links == ["[[session/2026-04-28]]"]
    assert specs[0].entity_links == ["[[person/Andrew]]"]

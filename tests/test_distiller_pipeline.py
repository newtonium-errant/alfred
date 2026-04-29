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


def test_pipeline_handles_nested_links_on_new_candidate_path():
    """Stage2 dedup-merge must flatten nested-list links on the NEW-candidate path.

    The cherry-pick in 6e76496 patched only the merge-into-existing
    branch (where a candidate is folded into an already-merged
    dict). The new-candidate branch (first candidate seen for a
    given title — no prior merge target) needs the same guard:
    if the LLM emits ``source_links=[[wikilink]]`` on the FIRST
    candidate, the nested list gets stored unchanged and stage 3's
    ``", ".join(spec.source_links)`` raises TypeError.
    """
    all_manifests = {
        "session/2026-04-28.md": [
            {
                "type": "assumption",
                "title": "Solo Assumption",
                "claim": "test claim",
                "confidence": "medium",
                "status": "draft",
                # First candidate carries nested-list links — no prior
                # merge target, so this hits the new-candidate branch.
                "source_links": [["[[session/2026-04-28]]"]],
                "entity_links": [["[[person/Andrew]]", "[[person/Bob]]"]],
                "evidence_excerpt": "ev",
            },
        ],
    }

    specs = _stage2_dedup_merge(all_manifests, existing_learns=[])

    assert len(specs) == 1
    spec = specs[0]
    # All entries must be flat strings.
    for sl in spec.source_links:
        assert isinstance(sl, str), f"source_link {sl!r} not flat"
    for el in spec.entity_links:
        assert isinstance(el, str), f"entity_link {el!r} not flat"
    # Nested wikilinks should have been flattened in.
    assert "[[session/2026-04-28]]" in spec.source_links
    assert "[[person/Andrew]]" in spec.entity_links
    assert "[[person/Bob]]" in spec.entity_links
    # And the join that motivated the guard must not raise.
    ", ".join(spec.source_links)
    ", ".join(spec.entity_links)


def test_pipeline_new_candidate_dedup_within_nested():
    """The new-candidate flatten must dedup as it flattens (matches merge path)."""
    all_manifests = {
        "session/2026-04-28.md": [
            {
                "type": "decision",
                "title": "Dedup Decision",
                "claim": "x",
                "confidence": "medium",
                "status": "draft",
                # Mixed flat + nested with duplicate inside the nested
                # list. Output must dedup so downstream rendering
                # doesn't show the same wikilink twice.
                "source_links": [
                    "[[session/2026-04-28]]",
                    ["[[session/2026-04-28]]", "[[session/2026-04-29]]"],
                ],
                "entity_links": [],
                "evidence_excerpt": "ev",
            },
        ],
    }

    specs = _stage2_dedup_merge(all_manifests, existing_learns=[])
    assert len(specs) == 1
    spec = specs[0]
    # Each link appears exactly once even though nested + flat both
    # carried [[session/2026-04-28]].
    assert spec.source_links.count("[[session/2026-04-28]]") == 1
    assert "[[session/2026-04-29]]" in spec.source_links

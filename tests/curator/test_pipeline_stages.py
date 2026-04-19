"""Stage-by-stage happy-path coverage for the curator pipeline.

Each test drives ``run_pipeline`` with a seeded inbox file and a fake LLM
backend, then inspects the vault on disk + the ``PipelineResult`` dataclass
to verify one stage's contract:

- Stage 1: manifest body field lands as the entity body (upstream port
  cbedd04 — full-record output shape).
- Stage 2: stub fallback when manifest entry has no ``body`` field; case-
  insensitive dedup short-circuits to the existing record.
- Stage 3: Stage 3 writes a ``related`` wikilink onto the note pointing
  at every resolved entity, and appends a back-link onto each entity.
- Stage 4: gated behind ``config.skip_entity_enrichment`` (upstream port
  ba1f7d0 — defaults ``True``). Flag set → zero extra LLM calls.

The fake backend is primed per-test with the entity manifest the LLM would
have emitted; the vault is inspected after the pipeline returns to confirm
the pure-Python stages actually wrote what they claimed.
"""

from __future__ import annotations

import frontmatter
import pytest

from ._fakes import FakeLLMResponse


# ---------------------------------------------------------------------------
# Stage 1 — full-record output shape (upstream cbedd04)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_manifest_body_becomes_entity_body(
    pipeline_runner, seeded_inbox
) -> None:
    """Stage 1 emits a full markdown body per entity; Stage 2 writes it.

    Before cbedd04 the manifest carried only ``description`` and Stage 2
    synthesised a stub body. The schema shift moved body composition into
    the LLM, which now emits ``entities[].body`` with the full markdown.
    This test pins the new contract: when the manifest provides a body,
    the created entity's file-on-disk contains that body verbatim.
    """

    inbox_files = seeded_inbox(contents=["Meeting with Jane Doe about Q2 planning."])
    full_body = "# Jane Doe\n\nSenior PM at Acme. Drives Q2 roadmap.\n"

    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Q2 planning sync",
            manifest_entities=[
                {
                    "type": "person",
                    "name": "Jane Doe",
                    "description": "A PM at Acme",  # Should be IGNORED when body is present
                    "body": full_body,
                    "fields": {"org": "[[org/Acme]]"},
                }
            ],
        ),
    )
    # Skip Stage 4 so we can make narrow assertions about Stages 1-3 only.
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    assert result.success is True
    assert result.note_path == "note/Q2 planning sync.md"

    # Stage 2 should have written the full manifest body, NOT the
    # description-as-body stub.
    person_file = pipeline_runner.vault_path / "person" / "Jane Doe.md"
    assert person_file.exists()
    post = frontmatter.load(str(person_file))
    assert "Senior PM at Acme" in post.content
    assert "A PM at Acme" not in post.content  # description fallback NOT used


@pytest.mark.asyncio
async def test_stage1_name_normalisation_titlecases_persons(
    pipeline_runner, seeded_inbox
) -> None:
    """_normalize_name title-cases person names before Stage 2 creates them.

    Guards the dedup invariant — if Stage 1 emits ``"jane doe"`` and Stage
    2 stores ``"jane doe.md"``, a subsequent run that emits ``"Jane Doe"``
    would create a duplicate. Pinning title-case-on-write keeps the
    canonical name stable.
    """

    inbox_files = seeded_inbox(contents=["Email about the project."])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Inbound email 1",
            manifest_entities=[
                {"type": "person", "name": "jane doe", "description": "someone"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    await pipeline_runner.run(inbox_files[0])

    assert (pipeline_runner.vault_path / "person" / "Jane Doe.md").exists()


# ---------------------------------------------------------------------------
# Stage 2 — entity resolution + body-less fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2_body_less_entry_uses_description_stub(
    pipeline_runner, seeded_inbox
) -> None:
    """When the manifest entry lacks ``body``, Stage 2 falls back to the
    description-as-stub path (pre-cbedd04 schema).

    Keeps the pipeline forward-compatible with in-flight retries whose
    prompts predate the schema shift.
    """

    inbox_files = seeded_inbox(contents=["Meeting with Bob."])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Bob sync",
            manifest_entities=[
                {
                    "type": "person",
                    "name": "Bob Builder",
                    "description": "Contractor on the renovation",
                    # no "body" key at all
                }
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    await pipeline_runner.run(inbox_files[0])

    person_file = pipeline_runner.vault_path / "person" / "Bob Builder.md"
    post = frontmatter.load(str(person_file))
    # Stub form: "# <name>\n\n<description>\n"
    assert "# Bob Builder" in post.content
    assert "Contractor on the renovation" in post.content


@pytest.mark.asyncio
async def test_stage2_existing_entity_is_not_recreated(
    pipeline_runner, seeded_inbox, curator_vault
) -> None:
    """If the entity already exists (case-insensitive), Stage 2 reuses it.

    Path preservation matters: the test pre-seeds ``person/Bob.md`` and
    then has Stage 1 emit ``"bob"``. The pipeline must NOT create
    ``person/bob.md`` — it must resolve to the existing ``Bob.md``.
    """

    # Pre-seed the existing person record
    (curator_vault / "person" / "Bob.md").write_text(
        "---\ntype: person\nname: Bob\ncreated: 2026-04-01\n"
        "tags: []\nrelated: []\n---\n\n# Bob\n\nExisting.\n",
        encoding="utf-8",
    )

    inbox_files = seeded_inbox(contents=["Another email about bob."])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Second email",
            manifest_entities=[
                {"type": "person", "name": "bob", "description": "same Bob"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    # Only one person file should exist in the person dir — the original
    # casing is preserved even though the manifest said "bob".
    person_files = list((curator_vault / "person").glob("*.md"))
    assert len(person_files) == 1
    assert person_files[0].name == "Bob.md"
    # The pipeline should have resolved to the existing path
    assert "person/Bob.md" in result.entities_created


# ---------------------------------------------------------------------------
# Stage 3 — interlink wikilinks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage3_note_receives_related_wikilinks_to_entities(
    pipeline_runner, seeded_inbox, curator_vault
) -> None:
    """After Stage 3 the note's frontmatter.related holds wikilinks to
    every resolved entity; each entity's frontmatter.related has a
    back-link to the note.
    """

    inbox_files = seeded_inbox(contents=["Meeting with Alice and Bob."])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Meeting notes",
            manifest_entities=[
                {"type": "person", "name": "Alice Anderson", "description": "an attendee"},
                {"type": "person", "name": "Bob Builder", "description": "another attendee"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    await pipeline_runner.run(inbox_files[0])

    note_file = curator_vault / "note" / "Meeting notes.md"
    note_post = frontmatter.load(str(note_file))
    related = note_post.metadata.get("related", [])
    assert "[[person/Alice Anderson]]" in related
    assert "[[person/Bob Builder]]" in related

    # Each entity should back-link to the note.
    alice_post = frontmatter.load(str(curator_vault / "person" / "Alice Anderson.md"))
    assert "[[note/Meeting notes]]" in alice_post.metadata.get("related", [])
    bob_post = frontmatter.load(str(curator_vault / "person" / "Bob Builder.md"))
    assert "[[note/Meeting notes]]" in bob_post.metadata.get("related", [])


@pytest.mark.asyncio
async def test_stage3_noop_when_note_missing(
    pipeline_runner, seeded_inbox, curator_vault
) -> None:
    """No note_path from Stage 1 → Stage 3 is a clean no-op (no crash).

    Edge case: entities get created but the note doesn't. Stage 3 still
    runs; it must not raise when asked to link a non-existent note.
    """

    inbox_files = seeded_inbox(contents=["stray content."])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path=None,  # no note created
            manifest_entities=[
                {"type": "person", "name": "Carol", "description": "someone"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    # Should return a valid result — not raise — even without a note path.
    result = await pipeline_runner.run(inbox_files[0])

    # No note path was produced, but the pipeline should still succeed
    # so long as *something* (an entity) was produced.
    assert result.note_path == ""
    assert (curator_vault / "person" / "Carol.md").exists()


# ---------------------------------------------------------------------------
# Stage 4 — enrich gate (skip_entity_enrichment = True by default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage4_skipped_when_flag_true(
    pipeline_runner, seeded_inbox
) -> None:
    """``skip_entity_enrichment=True`` → zero Stage 4 LLM calls.

    Upstream ba1f7d0 added the flag and defaulted it True for token
    efficiency. Test verifies the gate closes Stage 4 cleanly: entities
    keep their Stage 2 stub content, no ``s4-*`` calls fire, enriched
    list on the result is empty.
    """

    inbox_files = seeded_inbox(contents=["trigger content"])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Trigger note",
            manifest_entities=[
                {"type": "person", "name": "Dave", "description": "someone"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    assert result.entities_enriched == []
    # Only the Stage 1 call should have fired; no s4-* calls.
    stage4_calls = [c for c in pipeline_runner.backend.calls if c["stage"].startswith("s4-")]
    assert len(stage4_calls) == 0


@pytest.mark.asyncio
async def test_stage4_runs_when_flag_false(
    pipeline_runner, seeded_inbox
) -> None:
    """``skip_entity_enrichment=False`` → one Stage 4 call per enrichable entity.

    Location/event are in ``_SKIP_ENRICH_TYPES`` and must still be skipped
    even when the global flag is off.
    """

    inbox_files = seeded_inbox(contents=["trigger content"])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Trigger note",
            manifest_entities=[
                {"type": "person", "name": "Eve", "description": "someone"},
                {"type": "location", "name": "Halifax", "description": "a place"},
                {"type": "org", "name": "Acme", "description": "an org"},
            ],
        ),
    )
    # Queue stage-4 no-op responses; the fake simply records the call.
    for _ in range(3):
        pipeline_runner.backend.queue_response("s4-", FakeLLMResponse())

    pipeline_runner.config.skip_entity_enrichment = False

    result = await pipeline_runner.run(inbox_files[0])

    # Location is in _SKIP_ENRICH_TYPES, so only person + org are enriched.
    stage4_calls = [c for c in pipeline_runner.backend.calls if c["stage"].startswith("s4-")]
    assert len(stage4_calls) == 2
    assert len(result.entities_enriched) == 2


# ---------------------------------------------------------------------------
# Pipeline happy-path + result shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_result_populated_on_success(
    pipeline_runner, seeded_inbox
) -> None:
    """``PipelineResult`` carries the full set of computed fields on success.

    A regression here (e.g. summary left empty) would surface in the
    daemon's audit log as a silently blank run record.
    """

    inbox_files = seeded_inbox(contents=["happy path content"])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Happy note",
            manifest_entities=[
                {"type": "person", "name": "Frank", "description": "whatever"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    assert result.success is True
    assert result.note_path == "note/Happy note.md"
    assert "person/Frank.md" in result.entities_created
    assert "Happy note" in result.summary
    assert "resolved 1 entities" in result.summary


@pytest.mark.asyncio
async def test_pipeline_returns_unsuccessful_when_stage1_total_failure(
    pipeline_runner, seeded_inbox
) -> None:
    """Stage 1 produces neither a note nor a manifest → result.success = False.

    The watcher-level ``mark_processed`` fallback lives in ``daemon.py``;
    this test pins the pipeline's own failure-signalling contract so the
    caller can act on it.
    """

    inbox_files = seeded_inbox(contents=["completely unparseable"])
    # Three retries with no manifest + no note.
    for _ in range(3):
        pipeline_runner.backend.queue_response(
            "s1-analyze",
            FakeLLMResponse(create_note_path=None, manifest_entities=None),
        )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    assert result.success is False
    assert result.note_path == ""
    assert "Stage 1 failed" in result.summary

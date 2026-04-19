"""Error-path coverage for the curator pipeline.

Covers the small-but-load-bearing helpers that keep the pipeline
survivable under weird LLM output:

- :func:`_parse_entity_manifest` — tiered extraction from stdout:
  fenced ``json`` blocks → raw ``"entities":`` anywhere → whole-stdout
  JSON. Every branch must handle malformed input without raising.
- :func:`_extract_entities_from_text` — the brace-depth tracker.
- Stdout fallback: if Stage 1's temp manifest file is missing, the
  pipeline must fall back to parsing stdout for the manifest.
- :func:`_find_created_note` — only returns paths that start with
  ``note/``; mutations logging a person-create mustn't misfire.
"""

from __future__ import annotations

import json

import pytest

from alfred.curator.pipeline import (
    _extract_entities_from_text,
    _find_created_note,
    _parse_entity_manifest,
)
from alfred.vault.mutation_log import create_session_file, log_mutation

from ._fakes import FakeLLMResponse


# ---------------------------------------------------------------------------
# _parse_entity_manifest — tiered extraction
# ---------------------------------------------------------------------------


def test_parse_manifest_from_fenced_json_block() -> None:
    r"""Tier 1: ```json\n{"entities": [...]}\n``` inside the stdout."""
    stdout = (
        "I've analysed the inbox file.\n\n"
        '```json\n'
        '{"entities": [{"type": "person", "name": "Alice"}]}\n'
        "```\n"
        "Done.\n"
    )
    entities = _parse_entity_manifest(stdout)
    assert entities == [{"type": "person", "name": "Alice"}]


def test_parse_manifest_from_raw_json_in_stdout() -> None:
    """Tier 2: no fence, JSON object lives inline with surrounding text."""
    stdout = 'Prefix text {"entities": [{"type": "org", "name": "Acme"}]} suffix text.'
    entities = _parse_entity_manifest(stdout)
    assert entities == [{"type": "org", "name": "Acme"}]


def test_parse_manifest_whole_stdout_json() -> None:
    """Tier 3: stdout is exactly a JSON document."""
    stdout = json.dumps({"entities": [{"type": "task", "name": "X"}]})
    entities = _parse_entity_manifest(stdout)
    assert entities == [{"type": "task", "name": "X"}]


def test_parse_manifest_returns_empty_on_empty_stdout() -> None:
    """Empty stdout → empty list (never raises)."""
    assert _parse_entity_manifest("") == []


def test_parse_manifest_returns_empty_when_entities_key_absent() -> None:
    """Bare ``{}`` with no ``entities`` key → empty list."""
    stdout = '{"other_key": "value"}'
    assert _parse_entity_manifest(stdout) == []


def test_parse_manifest_returns_empty_when_stdout_has_no_entities_substring() -> None:
    """Short-circuit: if the literal substring ``"entities"`` is absent,
    the function returns ``[]`` without attempting parse.

    This guard is load-bearing for performance on very large stdout (the
    agent logs can be many MB) — regressing it would quietly slow every
    curator run.
    """
    stdout = "No JSON whatsoever, just prose."
    assert _parse_entity_manifest(stdout) == []


def test_parse_manifest_handles_nested_braces_in_body() -> None:
    """Entity bodies can contain ``{`` / ``}`` (e.g., code snippets).

    The brace-depth tracker must correctly close the outer JSON object
    even when an inner string value contains curly-brace characters.
    """
    payload = {
        "entities": [
            {
                "type": "note",
                "name": "Code sample",
                "body": "Here's a dict: {'a': 1, 'b': 2}",
            }
        ]
    }
    stdout = f"Some output...\n```json\n{json.dumps(payload)}\n```\n"
    entities = _parse_entity_manifest(stdout)
    assert len(entities) == 1
    assert entities[0]["name"] == "Code sample"


def test_parse_manifest_prefers_fenced_block_over_inline() -> None:
    """When both exist, the fenced block wins (tier order).

    An agent that pastes a partial inline example and then emits the
    real manifest in a fenced block should still land on the real one.
    """
    good = json.dumps({"entities": [{"type": "person", "name": "Real"}]})
    bad = json.dumps({"entities": [{"type": "person", "name": "Inline"}]})
    stdout = f"Some partial: {bad}\n\n```json\n{good}\n```\n"
    entities = _parse_entity_manifest(stdout)
    assert entities[0]["name"] == "Real"


# ---------------------------------------------------------------------------
# _extract_entities_from_text — brace-depth tracker
# ---------------------------------------------------------------------------


def test_extract_entities_none_when_no_match() -> None:
    assert _extract_entities_from_text("no json here") is None


def test_extract_entities_malformed_json_returns_none() -> None:
    """Unclosed brace after the entities key → can't parse → None."""
    text = '{"entities": [broken, no close}'
    assert _extract_entities_from_text(text) is None


def test_extract_entities_ignores_non_list_entities_value() -> None:
    """``entities`` must be a list; string/int values don't match."""
    text = '{"entities": "not a list"}'
    assert _extract_entities_from_text(text) is None


# ---------------------------------------------------------------------------
# _find_created_note — note/ path filtering
# ---------------------------------------------------------------------------


def test_find_created_note_returns_first_note_path(tmp_path) -> None:
    """Given a session file with a person-create followed by a note-create,
    the function returns the note's rel_path — the person is ignored.

    This is what routes the pipeline past Stage 1 when an agent creates
    person records alongside a note.
    """
    session_path = create_session_file()
    log_mutation(session_path, "create", "person/Jane Doe.md")
    log_mutation(session_path, "create", "note/Meeting notes.md")

    assert _find_created_note("stdout ignored", session_path) == "note/Meeting notes.md"


def test_find_created_note_returns_empty_when_no_note_created(tmp_path) -> None:
    """No note/ create → empty string. Stage 1 treats this as a soft
    failure and warns; the pipeline continues if entities exist."""
    session_path = create_session_file()
    log_mutation(session_path, "create", "person/Jane Doe.md")
    log_mutation(session_path, "create", "org/Acme.md")

    assert _find_created_note("stdout ignored", session_path) == ""


# ---------------------------------------------------------------------------
# Stage 1 stdout-fallback — manifest file missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_falls_back_to_stdout_when_manifest_file_missing(
    pipeline_runner, seeded_inbox, monkeypatch
) -> None:
    """If Stage 1's temp manifest file is never written, stdout parsing
    must still produce the entity list.

    This is the openclaw-mount-mismatch fix from upstream 44cf675 —
    the agent writes a file *inside* the OpenClaw container that isn't
    visible on the host, so the pipeline falls back to parsing the
    JSON manifest out of the agent's stdout.
    """
    inbox_files = seeded_inbox(contents=["inline manifest content"])

    # Build a stdout that contains the manifest in a fenced block, and
    # queue a fake response that does NOT write the manifest file.
    manifest_payload = {
        "entities": [
            {"type": "person", "name": "Stdout Sarah", "description": "inlined"}
        ]
    }
    stdout = (
        "Analysing now...\n"
        "```json\n"
        f"{json.dumps(manifest_payload)}\n"
        "```\n"
        "Done.\n"
    )

    # We need the fake to create a note but NOT write the manifest file.
    # The FakeLLMResponse writes the manifest file only when
    # manifest_entities is set; leaving it None forces the stdout path.
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Stdout fallback note",
            manifest_entities=None,  # don't write file
            raw_stdout=stdout,
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    assert result.success is True
    assert "person/Stdout Sarah.md" in result.entities_created


@pytest.mark.asyncio
async def test_stage1_retries_when_manifest_empty(
    pipeline_runner, seeded_inbox
) -> None:
    """Stage 1 retries up to 3 times when the manifest comes back empty.

    Exercises the ``max_attempts`` loop: first two tries yield empty,
    third yields entities — the third attempt's output wins.
    """
    inbox_files = seeded_inbox(contents=["retry me"])
    # First two attempts return nothing (no manifest, no note).
    for _ in range(2):
        pipeline_runner.backend.queue_response(
            "s1-analyze",
            FakeLLMResponse(create_note_path=None, manifest_entities=None),
        )
    # Third attempt succeeds.
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Retry success",
            manifest_entities=[{"type": "person", "name": "Retry Ron"}],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    # Three Stage 1 calls; the pipeline ultimately succeeds.
    stage1_calls = [c for c in pipeline_runner.backend.calls if c["stage"] == "s1-analyze"]
    assert len(stage1_calls) == 3
    assert result.success is True


@pytest.mark.asyncio
async def test_stage2_near_match_collision_reuses_canonical_path(
    pipeline_runner, seeded_inbox, curator_vault
) -> None:
    """Near-match on create raises ``VaultError(reason=near_match)``; the
    pipeline catches it and reuses the canonical path from ``details``.

    Guards the "don't silently drop entities when casing diverges" fix.
    Without this branch the entity would vanish from the resolved map
    and Stage 3 would never link to it.
    """
    # Pre-seed ``org/PocketPills.md`` so the near-match collision fires
    # when Stage 1 asks to create ``org/Pocketpills``.
    (curator_vault / "org" / "PocketPills.md").write_text(
        "---\ntype: org\nname: PocketPills\ncreated: 2026-04-01\n"
        "tags: []\nrelated: []\n---\n\n# PocketPills\n",
        encoding="utf-8",
    )

    inbox_files = seeded_inbox(contents=["Order from Pocketpills."])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Order email",
            manifest_entities=[
                {"type": "org", "name": "Pocketpills", "description": "Pharmacy"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    # Only the canonical file should exist.
    org_files = [p.name for p in (curator_vault / "org").glob("*.md")]
    assert "PocketPills.md" in org_files
    assert "Pocketpills.md" not in org_files  # never created
    # And the pipeline resolved the manifest entry to the canonical path.
    assert "org/PocketPills.md" in result.entities_created


@pytest.mark.asyncio
async def test_stage3_note_link_failure_is_logged_but_not_fatal(
    pipeline_runner, seeded_inbox, curator_vault, monkeypatch
) -> None:
    """If ``vault_edit`` on the note raises, Stage 3 logs and continues.

    Covers the inner try/except around the note-linking call — a failure
    there must not abort the pipeline mid-run and leave the entities
    orphaned without back-links.
    """
    from alfred.curator import pipeline as pipeline_module
    from alfred.vault.ops import VaultError

    # Patch vault_edit on the pipeline module so the FIRST call (note
    # edit) raises, and subsequent calls (entity back-links) succeed.
    calls = {"count": 0}
    real_edit = pipeline_module.vault_edit

    def flaky_edit(vault_path, rel_path, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise VaultError("simulated note edit failure")
        return real_edit(vault_path, rel_path, **kwargs)

    monkeypatch.setattr(pipeline_module, "vault_edit", flaky_edit)

    inbox_files = seeded_inbox(contents=["something"])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Resilient note",
            manifest_entities=[
                {"type": "person", "name": "Iris"},
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    assert result.success is True
    # Multiple edit calls happened — the first raised, later ones went
    # through for the entity back-link.
    assert calls["count"] >= 2


@pytest.mark.asyncio
async def test_stage2_skips_manifest_entries_missing_type_or_name(
    pipeline_runner, seeded_inbox
) -> None:
    """Invalid manifest entries (missing type or name) are skipped, not fatal.

    LLM output is untrusted; a single bad entry can't take out the pipeline.
    """
    inbox_files = seeded_inbox(contents=["noisy manifest"])
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            create_note_path="Noisy",
            manifest_entities=[
                {"type": "person", "name": "Clean Claire"},
                {"type": "", "name": "NoType"},  # skipped
                {"type": "person", "name": ""},  # skipped
                {"name": "OnlyName"},  # skipped
            ],
        ),
    )
    pipeline_runner.config.skip_entity_enrichment = True

    result = await pipeline_runner.run(inbox_files[0])

    # Only the well-formed entry survived; pipeline still succeeds.
    assert any("person/Clean Claire.md" in p for p in result.entities_created)
    # Exactly 1 valid entity
    assert len(result.entities_created) == 1

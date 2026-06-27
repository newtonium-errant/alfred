"""Tests for surveyor labeler — entity slug inclusion + merge behaviour."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import structlog

from alfred.surveyor.config import LabelerConfig, OpenRouterConfig
from alfred.surveyor.labeler import (
    ENTITY_RECORD_TYPES,
    Labeler,
    _slug_from_rel_path,
)
from alfred.surveyor.parser import VaultRecord


def _record(rel_path: str, record_type: str, name: str = "") -> VaultRecord:
    return VaultRecord(
        rel_path=rel_path,
        frontmatter={"type": record_type, "name": name},
        body="body content",
        record_type=record_type,
    )


def test_slug_from_rel_path_basic():
    assert _slug_from_rel_path("matter/alfred-product-development-launch.md") == (
        "alfred-product-development-launch"
    )


def test_slug_from_rel_path_nested():
    assert _slug_from_rel_path("person/some-nested/jazmin-rapali.md") == "jazmin-rapali"


def test_slug_from_rel_path_no_extension():
    assert _slug_from_rel_path("matter/foo") == "foo"


def test_entity_record_types_snapshot():
    # Lock the entity set so accidental changes require a test update.
    assert ENTITY_RECORD_TYPES == {"matter", "person", "org", "project"}


def _make_labeler(monkeypatch, llm_response: str | None) -> Labeler:
    openrouter = OpenRouterConfig(
        api_key="test-key",
        base_url="http://test",
        model="test-model",
        temperature=0.3,
    )
    labeler_cfg = LabelerConfig(
        max_files_per_cluster_context=20,
        body_preview_chars=200,
        min_cluster_size_to_label=2,
    )
    labeler = Labeler(openrouter, labeler_cfg)
    labeler._llm_call = AsyncMock(return_value=llm_response)  # type: ignore[method-assign]
    return labeler


@pytest.mark.asyncio
async def test_label_cluster_includes_entity_slug_first(monkeypatch):
    labeler = _make_labeler(
        monkeypatch,
        llm_response='["construction/residential", "project-management"]',
    )
    records = {
        "matter/alfred-product-development-launch.md": _record(
            "matter/alfred-product-development-launch.md", "matter", "Alfred"
        ),
        "event/2026-04-10-foo.md": _record("event/2026-04-10-foo.md", "event"),
        "event/2026-04-11-bar.md": _record("event/2026-04-11-bar.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=1,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags[0] == "alfred-product-development-launch"
    assert "construction/residential" in tags
    assert "project-management" in tags


@pytest.mark.asyncio
async def test_label_cluster_includes_multiple_entity_slugs(monkeypatch):
    labeler = _make_labeler(monkeypatch, llm_response='["makerspace"]')
    records = {
        "matter/erste-makerspace.md": _record("matter/erste-makerspace.md", "matter"),
        "person/jazmin-rapali.md": _record("person/jazmin-rapali.md", "person"),
        "org/erste-bank.md": _record("org/erste-bank.md", "org"),
        "event/foo.md": _record("event/foo.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=2,
        member_paths=list(records.keys()),
        records=records,
    )
    entity_slugs = {"erste-makerspace", "jazmin-rapali", "erste-bank"}
    assert entity_slugs.issubset(set(tags))
    # Entity slugs should come first
    assert set(tags[: len(entity_slugs)]) == entity_slugs
    assert "makerspace" in tags


@pytest.mark.asyncio
async def test_label_cluster_no_entity_returns_llm_only(monkeypatch):
    labeler = _make_labeler(
        monkeypatch,
        llm_response='["infrastructure", "devops"]',
    )
    records = {
        "note/x.md": _record("note/x.md", "note"),
        "event/y.md": _record("event/y.md", "event"),
        "observation/z.md": _record("observation/z.md", "observation"),
    }
    tags = await labeler.label_cluster(
        cluster_id=3,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags == ["infrastructure", "devops"]


@pytest.mark.asyncio
async def test_label_cluster_llm_response_none_still_returns_entity_slugs(monkeypatch):
    labeler = _make_labeler(monkeypatch, llm_response=None)
    records = {
        "matter/important.md": _record("matter/important.md", "matter"),
        "event/a.md": _record("event/a.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=4,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags == ["important"]


@pytest.mark.asyncio
async def test_label_cluster_llm_tag_matching_slug_is_deduped(monkeypatch):
    # LLM returns a tag that happens to match the entity slug — should not duplicate
    labeler = _make_labeler(
        monkeypatch,
        llm_response='["erste-makerspace", "banking"]',
    )
    records = {
        "matter/erste-makerspace.md": _record("matter/erste-makerspace.md", "matter"),
        "event/q.md": _record("event/q.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=5,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags.count("erste-makerspace") == 1
    assert "banking" in tags


@pytest.mark.asyncio
async def test_label_cluster_below_min_size_returns_empty(monkeypatch):
    labeler = _make_labeler(monkeypatch, llm_response='["x"]')
    records = {
        "matter/solo.md": _record("matter/solo.md", "matter"),
    }
    tags = await labeler.label_cluster(
        cluster_id=6,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags == []


# ---------------------------------------------------------------------------
# Tag-parser code-fence handling (labeler bake-off fix)
# ---------------------------------------------------------------------------
#
# label_cluster must strip ```json fences before json.loads the tag
# response — exactly like suggest_relationships already does. A
# fence-wrapping model (Claude Haiku, confirmed in the bake-off) would
# otherwise throw → llm_tags=[] → all its descriptive tags silently
# dropped. Live Groq emits bare JSON, so the bare case must still work.


@pytest.mark.asyncio
async def test_label_cluster_strips_json_code_fences(monkeypatch):
    """(a) THE fix: a fenced tag response now parses (it previously threw
    JSONDecodeError → tags dropped). No-entity cluster so the result is
    LLM-tags-only, making the parse outcome unambiguous."""
    labeler = _make_labeler(
        monkeypatch,
        llm_response='```json\n["construction/residential"]\n```',
    )
    records = {
        "note/x.md": _record("note/x.md", "note"),
        "event/y.md": _record("event/y.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=10,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags == ["construction/residential"]


@pytest.mark.asyncio
async def test_label_cluster_strips_bare_fences_no_lang_tag(monkeypatch):
    """A fence without a language tag (```\\n[...]\\n```) also parses —
    mirrors _strip_code_fences's no-tag branch."""
    labeler = _make_labeler(
        monkeypatch,
        llm_response='```\n["devops", "infra"]\n```',
    )
    records = {
        "note/x.md": _record("note/x.md", "note"),
        "event/y.md": _record("event/y.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=11,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags == ["devops", "infra"]


@pytest.mark.asyncio
async def test_label_cluster_bare_json_still_parses_no_regression(monkeypatch):
    """(b) Regression guard: the live Groq shape (bare JSON, no fences)
    still parses unchanged — _strip_code_fences passes raw JSON through."""
    labeler = _make_labeler(
        monkeypatch,
        llm_response='["infrastructure", "devops"]',
    )
    records = {
        "note/x.md": _record("note/x.md", "note"),
        "event/y.md": _record("event/y.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=12,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags == ["infrastructure", "devops"]


@pytest.mark.asyncio
async def test_label_cluster_malformed_response_yields_empty_with_log(
    monkeypatch,
):
    """(c) A genuinely-malformed (non-JSON) response still yields [] AND
    emits the labeler.parse_error log — the fence strip doesn't mask a
    real parse failure."""
    labeler = _make_labeler(
        monkeypatch,
        llm_response="not json at all, just prose",
    )
    records = {
        "note/x.md": _record("note/x.md", "note"),
        "event/y.md": _record("event/y.md", "event"),
    }
    with structlog.testing.capture_logs() as captured:
        tags = await labeler.label_cluster(
            cluster_id=13,
            member_paths=list(records.keys()),
            records=records,
        )
    assert tags == []
    errs = [c for c in captured if c.get("event") == "labeler.parse_error"]
    assert len(errs) == 1
    assert errs[0]["cluster_id"] == 13


@pytest.mark.asyncio
async def test_label_cluster_non_list_json_yields_empty(monkeypatch):
    """A valid-JSON-but-not-a-list response (e.g. an object) → [] (the
    isinstance(list) guard), not a crash — fence strip is orthogonal to
    the shape check."""
    labeler = _make_labeler(
        monkeypatch,
        llm_response='```json\n{"tag": "nope"}\n```',
    )
    records = {
        "note/x.md": _record("note/x.md", "note"),
        "event/y.md": _record("event/y.md", "event"),
    }
    tags = await labeler.label_cluster(
        cluster_id=14,
        member_paths=list(records.keys()),
        records=records,
    )
    assert tags == []

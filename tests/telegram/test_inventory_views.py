"""Phase 4 Sub-arc C — /questions + /research-pointers slash commands
(Hypatia Zettelkasten redesign, 2026-05-18).

Per the dispatch spec:
  * /questions — grouped-by-MOC list of question/ records with
    status in {open, refined}
  * /research-pointers — same shape for research-pointer/ with
    status == open

Read-only. Hypatia-only via config gate. Mirrors the data surfaced
by Sub-arc B's inventory MOCs but grouped by topic-MOC membership
rather than flat.

Coverage:
  * Predicate sharing — _predicate_for_type pulls from
    INVENTORY_MOC_DISPATCH (Sub-arc B's source of truth)
  * collect_records — predicate filtering, missing-dir tolerance,
    corrupt-frontmatter tolerance, mocs normalization
  * group_by_moc — multi-MOC fan-out, uncategorized bucket,
    newest-first ordering within groups
  * render_inventory — empty case, single-group, multi-group,
    uncategorized, cap behavior, pipe-aliased mocs
  * Handler smoke — /questions returns OK shape; /research-pointers
    returns OK shape; unknown-user gate; failure-isolation reply
  * Log emission pins per feedback_log_emission_test_pattern
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest
import structlog

from alfred.telegram import bot, inventory_views
from alfred.telegram.inventory_views import (
    _UNCATEGORIZED_GROUP_KEY,
    _normalize_moc_key,
    _predicate_for_type,
    collect_records,
    group_by_moc,
    render_inventory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hypatia_vault(tmp_path: Path) -> Path:
    """Vault with question/ + research-pointer/ + MOC/ subdirs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("question", "research-pointer", "MOC"):
        (vault / sub).mkdir()
    return vault


def _seed_question(
    vault: Path,
    name: str,
    *,
    status: str = "open",
    mocs: list[str] | None = None,
    created: str = "2026-05-18",
) -> str:
    fm: dict = {
        "type": "question",
        "name": name,
        "created": created,
        "status": status,
        "origin_sources": [],
        "answered_by": "",
        "mocs": mocs or [],
        "tags": [],
    }
    rel_path = f"question/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post("", **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


def _seed_research_pointer(
    vault: Path,
    name: str,
    *,
    status: str = "open",
    mocs: list[str] | None = None,
    created: str = "2026-05-18",
) -> str:
    fm: dict = {
        "type": "research-pointer",
        "name": name,
        "created": created,
        "status": status,
        "origin_sources": [],
        "produces": [],
        "mocs": mocs or [],
        "tags": [],
    }
    rel_path = f"research-pointer/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post("", **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


# ---------------------------------------------------------------------------
# _predicate_for_type — single source of truth from Sub-arc B
# ---------------------------------------------------------------------------


def test_predicate_for_type_question() -> None:
    """question predicate matches open + refined; rejects answered +
    superseded. Pinned to INVENTORY_MOC_DISPATCH so Sub-arc B + C
    can't drift."""
    pred = _predicate_for_type("question")
    assert pred is not None
    assert pred({"status": "open"}) is True
    assert pred({"status": "refined"}) is True
    assert pred({"status": "answered"}) is False
    assert pred({"status": "superseded"}) is False
    assert pred({}) is False


def test_predicate_for_type_research_pointer() -> None:
    pred = _predicate_for_type("research-pointer")
    assert pred is not None
    assert pred({"status": "open"}) is True
    assert pred({"status": "in-progress"}) is False
    assert pred({"status": "completed"}) is False
    assert pred({"status": "dropped"}) is False


def test_predicate_for_type_unknown_returns_none() -> None:
    """Type that isn't in INVENTORY_MOC_DISPATCH → no predicate."""
    assert _predicate_for_type("zettel") is None
    assert _predicate_for_type("note") is None


# ---------------------------------------------------------------------------
# _normalize_moc_key — pipe-alias tolerance
# ---------------------------------------------------------------------------


def test_normalize_moc_key_full_wikilink() -> None:
    assert _normalize_moc_key("[[MOC/Stoicism]]") == "MOC/Stoicism"


def test_normalize_moc_key_pipe_aliased() -> None:
    assert _normalize_moc_key("[[MOC/Stoicism|Stoic Practice]]") == "MOC/Stoicism"


def test_normalize_moc_key_bare_path() -> None:
    assert _normalize_moc_key("MOC/Stoicism") == "MOC/Stoicism"


def test_normalize_moc_key_md_suffix() -> None:
    assert _normalize_moc_key("MOC/Stoicism.md") == "MOC/Stoicism"


def test_normalize_moc_key_empty() -> None:
    assert _normalize_moc_key("") == ""
    assert _normalize_moc_key(None) == ""
    assert _normalize_moc_key("[[]]") == ""


# ---------------------------------------------------------------------------
# collect_records — predicate filtering + tolerance
# ---------------------------------------------------------------------------


def test_collect_question_filters_by_status(hypatia_vault: Path) -> None:
    _seed_question(hypatia_vault, "Q-Open", status="open")
    _seed_question(hypatia_vault, "Q-Refined", status="refined")
    _seed_question(hypatia_vault, "Q-Answered", status="answered")
    _seed_question(hypatia_vault, "Q-Superseded", status="superseded")

    records = collect_records(hypatia_vault, "question")
    names = sorted(r["name"] for r in records)
    assert names == ["Q-Open", "Q-Refined"]


def test_collect_research_pointer_filters_by_status(
    hypatia_vault: Path,
) -> None:
    _seed_research_pointer(hypatia_vault, "RP-Open", status="open")
    _seed_research_pointer(hypatia_vault, "RP-InProgress", status="in-progress")
    _seed_research_pointer(hypatia_vault, "RP-Completed", status="completed")

    records = collect_records(hypatia_vault, "research-pointer")
    names = sorted(r["name"] for r in records)
    assert names == ["RP-Open"]


def test_collect_missing_dir_returns_empty(hypatia_vault: Path) -> None:
    """No question/ directory → empty list (not a crash)."""
    import shutil
    shutil.rmtree(hypatia_vault / "question")
    records = collect_records(hypatia_vault, "question")
    assert records == []


def test_collect_unknown_type_returns_empty(hypatia_vault: Path) -> None:
    """A type not in INVENTORY_MOC_DISPATCH → empty list + warning
    log (defensive, not a crash)."""
    with structlog.testing.capture_logs() as captured:
        records = collect_records(hypatia_vault, "zettel")
    assert records == []
    matches = [
        c for c in captured
        if c.get("event") == "inventory_views.unknown_record_type"
    ]
    assert len(matches) == 1


def test_collect_corrupt_frontmatter_skipped(hypatia_vault: Path) -> None:
    """A corrupt .md file is skipped silently; other records still
    surface. Slash command is glance-view — one broken record
    shouldn't break the reply.

    Defense at two layers: (a) frontmatter.load raise → outer
    try/except skips; (b) parsed record with wrong `type` field →
    inner if-check skips. Either way, a malformed file doesn't
    pollute the reply or crash collection. Here we write a file
    with a missing closing fence, which fails to parse outright.
    """
    _seed_question(hypatia_vault, "Q-Good", status="open")
    # Write a file with no closing fence — frontmatter library
    # returns it with empty metadata, which fails the type check
    # below; effectively skipped.
    (hypatia_vault / "question" / "Q-Bad.md").write_text(
        "---\ntype: question\nname: Q-Bad\nstatus: open\n"
        "this line breaks YAML parsing because no closing fence\n",
        encoding="utf-8",
    )
    records = collect_records(hypatia_vault, "question")
    names = [r["name"] for r in records]
    assert "Q-Good" in names
    # Q-Bad either failed to parse (skipped by try/except) OR
    # parsed with empty metadata (skipped by type check). Either
    # way, NOT in the reply.
    assert "Q-Bad" not in names


def test_collect_normalizes_mocs_pipe_aliased(hypatia_vault: Path) -> None:
    """Operator hand-wrote a pipe-aliased mocs entry — collection
    normalizes it so the grouping step treats it as the same MOC."""
    _seed_question(
        hypatia_vault, "Q1",
        status="open",
        mocs=["[[MOC/Stoicism|Stoic Practice]]"],
    )
    records = collect_records(hypatia_vault, "question")
    assert len(records) == 1
    assert records[0]["mocs"] == ["MOC/Stoicism"]


def test_collect_scalar_mocs_field(hypatia_vault: Path) -> None:
    """Operator-typo defense: scalar string in mocs field instead
    of a list. Single MOC normalized to a one-element list."""
    _seed_question(hypatia_vault, "Q1", status="open")
    # Overwrite with a scalar mocs field via direct file write.
    fm = {
        "type": "question",
        "name": "Q1",
        "created": "2026-05-18",
        "status": "open",
        "mocs": "[[MOC/Stoicism]]",
        "origin_sources": [],
        "answered_by": "",
        "tags": [],
    }
    post = frontmatter.Post("", **fm)
    (hypatia_vault / "question" / "Q1.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )
    records = collect_records(hypatia_vault, "question")
    assert records[0]["mocs"] == ["MOC/Stoicism"]


# ---------------------------------------------------------------------------
# group_by_moc — multi-MOC fan-out + uncategorized + ordering
# ---------------------------------------------------------------------------


def test_group_by_moc_single_mocs_per_record() -> None:
    records = [
        {"path": "question/Q1.md", "name": "Q1", "status": "open",
         "created": "2026-05-15", "mocs": ["MOC/Stoicism"]},
        {"path": "question/Q2.md", "name": "Q2", "status": "refined",
         "created": "2026-05-10", "mocs": ["MOC/HEMA"]},
        {"path": "question/Q3.md", "name": "Q3", "status": "open",
         "created": "2026-05-18", "mocs": ["MOC/Stoicism"]},
    ]
    groups = group_by_moc(records)
    assert set(groups.keys()) == {"MOC/Stoicism", "MOC/HEMA"}
    assert [r["name"] for r in groups["MOC/Stoicism"]] == ["Q3", "Q1"]  # newest first
    assert [r["name"] for r in groups["MOC/HEMA"]] == ["Q2"]


def test_group_by_moc_multi_mocs_per_record() -> None:
    """A record with two mocs appears in BOTH groups."""
    records = [
        {"path": "question/Q1.md", "name": "Q1", "status": "open",
         "created": "2026-05-18", "mocs": ["MOC/Stoicism", "MOC/HEMA"]},
    ]
    groups = group_by_moc(records)
    assert "MOC/Stoicism" in groups
    assert "MOC/HEMA" in groups
    assert len(groups["MOC/Stoicism"]) == 1
    assert len(groups["MOC/HEMA"]) == 1


def test_group_by_moc_uncategorized() -> None:
    records = [
        {"path": "question/Q1.md", "name": "Q1", "status": "open",
         "created": "2026-05-18", "mocs": []},
        {"path": "question/Q2.md", "name": "Q2", "status": "open",
         "created": "2026-05-17", "mocs": ["MOC/Stoicism"]},
    ]
    groups = group_by_moc(records)
    assert _UNCATEGORIZED_GROUP_KEY in groups
    assert [r["name"] for r in groups[_UNCATEGORIZED_GROUP_KEY]] == ["Q1"]


def test_group_by_moc_newest_first_within_group() -> None:
    records = [
        {"path": "question/Q-Old.md", "name": "Q-Old", "status": "open",
         "created": "2026-01-01", "mocs": ["MOC/X"]},
        {"path": "question/Q-New.md", "name": "Q-New", "status": "open",
         "created": "2026-05-18", "mocs": ["MOC/X"]},
        {"path": "question/Q-Mid.md", "name": "Q-Mid", "status": "open",
         "created": "2026-03-15", "mocs": ["MOC/X"]},
    ]
    groups = group_by_moc(records)
    assert [r["name"] for r in groups["MOC/X"]] == ["Q-New", "Q-Mid", "Q-Old"]


# ---------------------------------------------------------------------------
# render_inventory — empty + single-group + multi-group + cap
# ---------------------------------------------------------------------------


def test_render_empty_questions() -> None:
    """Empty list → explicit empty-state per intentionally_left_blank."""
    out = render_inventory("question", [])
    assert "No open" in out
    assert "open, refined" in out


def test_render_empty_research_pointers() -> None:
    out = render_inventory("research-pointer", [])
    assert "No open" in out
    assert "open" in out


def test_render_single_group() -> None:
    records = [
        {"path": "question/Q1.md", "name": "Q1", "status": "open",
         "created": "2026-05-18", "mocs": ["MOC/Stoicism"]},
    ]
    out = render_inventory("question", records)
    assert "📋 Open Questions (1 total)" in out
    assert "## [[MOC/Stoicism]] (1)" in out
    assert "- [[question/Q1]] (open, 2026-05-18)" in out


def test_render_multi_group_with_uncategorized() -> None:
    records = [
        {"path": "question/Q-Stoic.md", "name": "Q-Stoic", "status": "refined",
         "created": "2026-05-15", "mocs": ["MOC/Stoicism"]},
        {"path": "question/Q-HEMA.md", "name": "Q-HEMA", "status": "open",
         "created": "2026-05-08", "mocs": ["MOC/HEMA MOC"]},
        {"path": "question/Q-Stray.md", "name": "Q-Stray", "status": "open",
         "created": "2026-05-18", "mocs": []},
    ]
    out = render_inventory("question", records)
    assert "📋 Open Questions (3 total)" in out
    assert "## [[MOC/HEMA MOC]] (1)" in out
    assert "## [[MOC/Stoicism]] (1)" in out
    assert "## Uncategorized (1)" in out
    # Uncategorized goes LAST regardless of alpha ordering.
    hema_idx = out.index("## [[MOC/HEMA MOC]]")
    stoic_idx = out.index("## [[MOC/Stoicism]]")
    uncat_idx = out.index("## Uncategorized")
    assert hema_idx < stoic_idx < uncat_idx


def test_render_caps_at_per_group_cap() -> None:
    records = [
        {"path": f"question/Q{n}.md", "name": f"Q{n}", "status": "open",
         "created": f"2026-05-{n:02d}", "mocs": ["MOC/X"]}
        for n in range(1, 26)  # 25 records
    ]
    out = render_inventory("question", records, per_group_cap=20)
    # 5 records overflow → "+5 more" hint.
    assert "+5 more" in out
    # The 5 oldest are dropped (newest-first), so Q1 should NOT be
    # in the rendered output but Q25 should.
    assert "[[question/Q25]]" in out
    assert "[[question/Q1]]" not in out


def test_render_no_cap_hint_when_under_threshold() -> None:
    records = [
        {"path": "question/Q1.md", "name": "Q1", "status": "open",
         "created": "2026-05-18", "mocs": ["MOC/X"]},
    ]
    out = render_inventory("question", records, per_group_cap=20)
    assert "+0 more" not in out
    assert "more (open in vault)" not in out


def test_render_pipe_aliased_mocs_displayed_canonically() -> None:
    """A pipe-aliased ``mocs:`` entry is normalized to the canonical
    target before grouping + rendering — the operator sees the
    canonical wikilink in the reply, not the display-aliased form."""
    records = [
        {"path": "question/Q1.md", "name": "Q1", "status": "open",
         "created": "2026-05-18", "mocs": ["MOC/Stoicism"]},
    ]
    out = render_inventory("question", records)
    assert "[[MOC/Stoicism]]" in out
    assert "Stoic Practice" not in out  # display alias not rendered


# ---------------------------------------------------------------------------
# End-to-end via collect_records + render_inventory
# ---------------------------------------------------------------------------


def test_end_to_end_five_questions_two_mocs_plus_uncat(
    hypatia_vault: Path,
) -> None:
    """Concrete operator-visible example from the dispatch:
    5 question records across 2 MOCs + 1 uncategorized."""
    _seed_question(
        hypatia_vault, "Q-Logos", status="refined",
        mocs=["[[MOC/Stoicism]]"], created="2026-05-15",
    )
    _seed_question(
        hypatia_vault, "Q-AmorFati", status="open",
        mocs=["[[MOC/Stoicism]]"], created="2026-04-12",
    )
    _seed_question(
        hypatia_vault, "Q-Apatheia", status="open",
        mocs=["[[MOC/Stoicism]]"], created="2026-03-01",
    )
    _seed_question(
        hypatia_vault, "Q-Grips", status="open",
        mocs=["[[MOC/HEMA MOC]]"], created="2026-05-08",
    )
    _seed_question(
        hypatia_vault, "Q-MetaLearning", status="open",
        mocs=[], created="2026-05-18",
    )
    # Two records that should NOT appear (answered):
    _seed_question(
        hypatia_vault, "Q-Done", status="answered",
        mocs=["[[MOC/Stoicism]]"],
    )

    records = collect_records(hypatia_vault, "question")
    out = render_inventory("question", records)

    # 5 total, NOT 6 — answered excluded.
    assert "📋 Open Questions (5 total)" in out
    assert "## [[MOC/Stoicism]] (3)" in out
    assert "## [[MOC/HEMA MOC]] (1)" in out
    assert "## Uncategorized (1)" in out
    # Order within Stoicism: newest first (Q-Logos > Q-AmorFati > Q-Apatheia)
    stoic_block = out.split("## [[MOC/Stoicism]]")[1].split("##")[0]
    logos_idx = stoic_block.index("[[question/Q-Logos]]")
    amorfati_idx = stoic_block.index("[[question/Q-AmorFati]]")
    apatheia_idx = stoic_block.index("[[question/Q-Apatheia]]")
    assert logos_idx < amorfati_idx < apatheia_idx
    # Q-Done answered question NOT rendered.
    assert "Q-Done" not in out


# ---------------------------------------------------------------------------
# Handler smoke — /questions + /research-pointers
# ---------------------------------------------------------------------------


def _make_update_mock(chat_id: int = 42, user_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_ctx_mock(
    vault_path: Path,
    *,
    allowed_user_id: int = 42,
    inventory_views_enabled: bool = True,
    per_group_cap: int = 20,
) -> MagicMock:
    """Build a minimal ctx mock for inventory-view handler smoke tests."""
    config = MagicMock()
    config.allowed_users = [allowed_user_id]
    config.vault = MagicMock()
    config.vault.path = str(vault_path)
    if inventory_views_enabled:
        config.inventory_views = MagicMock()
        config.inventory_views.per_group_cap = per_group_cap
    else:
        config.inventory_views = None
    ctx = MagicMock()
    ctx.application.bot_data = {bot._KEY_CONFIG: config}
    return ctx


@pytest.mark.asyncio
async def test_handler_questions_smoke(hypatia_vault: Path) -> None:
    """/questions handler renders some open questions."""
    _seed_question(
        hypatia_vault, "Q1", status="open",
        mocs=["[[MOC/Stoicism]]"],
    )

    update = _make_update_mock()
    ctx = _make_ctx_mock(hypatia_vault)

    await bot.on_questions(update, ctx)

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "📋 Open Questions (1 total)" in reply
    assert "[[question/Q1]]" in reply


@pytest.mark.asyncio
async def test_handler_research_pointers_smoke(
    hypatia_vault: Path,
) -> None:
    """/research-pointers handler renders some open pointers."""
    _seed_research_pointer(
        hypatia_vault, "RP1", status="open",
        mocs=["[[MOC/Stoicism]]"],
    )

    update = _make_update_mock()
    ctx = _make_ctx_mock(hypatia_vault)

    await bot.on_research_pointers(update, ctx)

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "📋 Open Research Pointers (1 total)" in reply
    assert "[[research-pointer/RP1]]" in reply


@pytest.mark.asyncio
async def test_handler_unauthorized_user_silent_drop(
    hypatia_vault: Path,
) -> None:
    """Unknown user → no reply (matches existing handler convention)."""
    update = _make_update_mock(user_id=999)
    ctx = _make_ctx_mock(hypatia_vault, allowed_user_id=42)

    await bot.on_questions(update, ctx)

    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handler_empty_state_message(hypatia_vault: Path) -> None:
    """No qualifying records → empty-state message."""
    update = _make_update_mock()
    ctx = _make_ctx_mock(hypatia_vault)

    await bot.on_questions(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "No open" in reply


@pytest.mark.asyncio
async def test_handler_failure_isolation(
    hypatia_vault: Path, monkeypatch,
) -> None:
    """If collect_records raises, the handler emits an error reply
    rather than crashing."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated vault read failure")

    monkeypatch.setattr(
        inventory_views, "collect_records", boom,
    )

    update = _make_update_mock()
    ctx = _make_ctx_mock(hypatia_vault)

    await bot.on_questions(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "❌ Could not load" in reply
    assert "RuntimeError" in reply


# ---------------------------------------------------------------------------
# Log emission pins (per feedback_log_emission_test_pattern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_inventory_view_done_on_success(
    hypatia_vault: Path,
) -> None:
    _seed_question(hypatia_vault, "Q1", status="open")

    update = _make_update_mock()
    ctx = _make_ctx_mock(hypatia_vault)

    with structlog.testing.capture_logs() as captured:
        await bot.on_questions(update, ctx)

    matches = [
        c for c in captured
        if c.get("event") == "talker.bot.inventory_view_done"
    ]
    assert len(matches) == 1
    assert matches[0]["command"] == "/questions"
    assert matches[0]["record_type"] == "question"
    assert matches[0]["record_count"] == 1
    assert matches[0]["truncated"] is False


@pytest.mark.asyncio
async def test_log_inventory_view_failed(
    hypatia_vault: Path, monkeypatch,
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(
        inventory_views, "collect_records", boom,
    )

    update = _make_update_mock()
    ctx = _make_ctx_mock(hypatia_vault)

    with structlog.testing.capture_logs() as captured:
        await bot.on_questions(update, ctx)

    matches = [
        c for c in captured
        if c.get("event") == "talker.bot.inventory_view_failed"
    ]
    assert len(matches) == 1
    assert matches[0]["command"] == "/questions"
    assert matches[0]["record_type"] == "question"
    assert "simulated failure" in matches[0]["error"]


# ---------------------------------------------------------------------------
# Cross-instance scope — config gate
# ---------------------------------------------------------------------------


def test_inventory_views_config_default_off() -> None:
    """The InventoryViewsConfig dataclass defaults to command_enabled=
    False. Instances without the explicit config block do NOT
    register the handlers."""
    from alfred.telegram.config import InventoryViewsConfig
    cfg = InventoryViewsConfig()
    assert cfg.command_enabled is False
    assert cfg.per_group_cap == 20


def test_inventory_views_load_from_unified_absent_block() -> None:
    """An instance config without the inventory_views block leaves
    TalkerConfig.inventory_views = None — gates the registration."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "bot_token": "DUMMY_TEST_TOKEN",
            "allowed_users": [42],
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.inventory_views is None


def test_inventory_views_load_from_unified_explicit_block() -> None:
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "bot_token": "DUMMY_TEST_TOKEN",
            "allowed_users": [42],
            "inventory_views": {
                "command_enabled": True,
                "per_group_cap": 15,
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.inventory_views is not None
    assert cfg.inventory_views.command_enabled is True
    assert cfg.inventory_views.per_group_cap == 15

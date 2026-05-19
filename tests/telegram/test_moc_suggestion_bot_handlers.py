"""Phase 5 Sub-arc D2 — bot handler smoke tests (2026-05-19).

Exercises the three slash handlers (``on_moc_suggestions``,
``on_accept_moc``, ``on_reject_moc``) with mocked Update + Context
objects. Verifies reply-text shape + queue status transitions +
auth-gate silent-drop for unknown users.

Mirror of ``test_inventory_views.py``'s handler smoke section. Same
fixture pattern; same auth-gate pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.surveyor.moc_suggester import MocSuggestion
from alfred.telegram import bot
from alfred.telegram import moc_suggestion_views as msv


# ---------------------------------------------------------------------------
# Fixtures (mirror test_inventory_views.py shape)
# ---------------------------------------------------------------------------


def _make_update_mock(
    *,
    chat_id: int = 42,
    user_id: int = 42,
    text: str = "",
) -> MagicMock:
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_ctx_mock(
    vault_path: Path,
    *,
    queue_path: Path | None,
    allowed_user_id: int = 42,
    moc_suggestions_enabled: bool = True,
    instance_name: str = "Hypatia",
) -> MagicMock:
    """Build a ctx mock with the talker config shape D2 reads."""
    config = MagicMock()
    config.allowed_users = [allowed_user_id]
    config.vault = MagicMock()
    config.vault.path = str(vault_path)
    # ``config.instance.name`` is read as a real string by the
    # /accept-moc handler (passed as scope to vault_edit via
    # ``.lower()``). Setting it to a real string here makes
    # ``.lower()`` return a real lowercased string — without this,
    # MagicMock's ``.name.lower()`` returns another MagicMock which
    # would trip check_scope's dict-key lookup downstream.
    config.instance = MagicMock()
    config.instance.name = instance_name
    if moc_suggestions_enabled:
        config.moc_suggestions = MagicMock()
        config.moc_suggestions.command_enabled = True
        config.moc_suggestions.queue_path = str(queue_path) if queue_path else None
    else:
        config.moc_suggestions = None
    ctx = MagicMock()
    ctx.application.bot_data = {bot._KEY_CONFIG: config}
    return ctx


def _make_suggestion(
    *,
    id: str = "ms-20260519-aaaaaaaa",
    target: str | None = "MOC/Stoicism MOC.md",
    proposed_new_moc_name: str | None = None,
    members: list[str] | None = None,
    candidates_to_add: list[str] | None = None,
    status: str = "pending",
    reasoning: str = "test reasoning",
) -> MocSuggestion:
    if members is None:
        members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    if candidates_to_add is None:
        candidates_to_add = [members[-1]]
    return MocSuggestion(
        id=id,
        cluster_id_at_proposal=7,
        cluster_tags=["stoicism"],
        cluster_member_paths=sorted(members),
        target_moc_rel_path=target,
        proposed_new_moc_name=proposed_new_moc_name,
        mapping_signal=("propose_new" if target is None else "member_overlap"),
        mapping_score=0.0 if target is None else 0.6,
        candidate_members_to_add=candidates_to_add,
        reasoning=reasoning,
        created="2026-05-19T14:00:00+00:00",
        status=status,
    )


def _write_queue(queue_path: Path, entries) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with open(queue_path, "w", encoding="utf-8") as f:
        for s in entries:
            f.write(json.dumps(s.to_dict(), separators=(",", ":")) + "\n")


@pytest.fixture
def hypatia_vault(tmp_path: Path) -> Path:
    """Vault with zettel + MOC dirs and one seeded MOC."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "zettel").mkdir()
    (vault / "MOC").mkdir()
    (vault / "MOC" / "Stoicism MOC.md").write_text(
        "---\ntype: MOC\nname: Stoicism MOC\ncreated: 2026-05-19\n---\n\n"
        "# Premise\n\n# Contents\n\n# Tags\n",
        encoding="utf-8",
    )
    return vault


def _seed_zettel(
    vault: Path, name: str, *, mocs: list[str] | None = None,
) -> str:
    rel = f"zettel/{name}.md"
    fm = ["---", "type: zettel", f"name: {name}", "created: 2026-05-19"]
    fm.append(f"mocs: {json.dumps(mocs or [])}")
    fm.append("---")
    body = "\n# Premise\n\n# Notes\n\n# Tags\n\n# Indexing & MOCs\n"
    (vault / rel).write_text("\n".join(fm) + body, encoding="utf-8")
    return rel


# ---------------------------------------------------------------------------
# /moc-suggestions handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moc_suggestions_handler_lists_pending(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Happy path: pending suggestions → grouped Markdown reply."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(id="ms-show-1", target="MOC/Stoicism MOC.md"),
    ])

    update = _make_update_mock(text="/moc_suggestions")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_moc_suggestions(update, ctx)

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "📋 Pending MOC suggestions" in reply
    assert "ms-show-1" in reply
    assert "[[MOC/Stoicism MOC]]" in reply


@pytest.mark.asyncio
async def test_moc_suggestions_handler_empty_state(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """No queue file → empty-state reply, not crash."""
    qp = tmp_path / "no_queue.jsonl"
    update = _make_update_mock(text="/moc_suggestions")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_moc_suggestions(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "No pending" in reply


@pytest.mark.asyncio
async def test_moc_suggestions_handler_missing_queue_config(
    hypatia_vault: Path,
) -> None:
    """Config block missing queue_path → recognizable error reply."""
    update = _make_update_mock(text="/moc_suggestions")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=None)

    await bot.on_moc_suggestions(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "not configured" in reply


@pytest.mark.asyncio
async def test_moc_suggestions_handler_unauthorized_silent_drop(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Unknown user → no reply (matches other handlers' auth gate)."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-x")])

    update = _make_update_mock(text="/moc_suggestions", user_id=999)
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp, allowed_user_id=42)

    await bot.on_moc_suggestions(update, ctx)

    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_moc_suggestions_handler_failure_isolation(
    hypatia_vault: Path, tmp_path: Path, monkeypatch,
) -> None:
    """If collect_pending raises, handler emits error reply not crash."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated queue read failure")
    monkeypatch.setattr(msv, "collect_pending", boom)

    qp = tmp_path / "queue.jsonl"
    update = _make_update_mock(text="/moc_suggestions")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_moc_suggestions(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "❌ Could not load" in reply


# ---------------------------------------------------------------------------
# /accept-moc handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_moc_happy_path(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Happy: pending → applied; reply shape names the target."""
    rel = _seed_zettel(hypatia_vault, "AcceptZ", mocs=[])
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(
            id="ms-acc-1",
            target="MOC/Stoicism MOC.md",
            candidates_to_add=[rel],
        ),
    ])

    update = _make_update_mock(text="/accept_moc ms-acc-1")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_accept_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "✓ Applied suggestion" in reply
    assert "ms-acc-1" in reply
    assert "1 member" in reply
    # Queue advanced to applied.
    from alfred.telegram.moc_suggestion_views import lookup_suggestion
    updated = lookup_suggestion(qp, "ms-acc-1")
    assert updated is not None
    assert updated.status == "applied"


@pytest.mark.asyncio
async def test_accept_moc_propose_new_creates_moc(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Propose-new accept creates the MOC, reply says ``created new MOC``."""
    rel = _seed_zettel(hypatia_vault, "ProposeMember", mocs=[])
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(
            id="ms-proposed",
            target=None,
            proposed_new_moc_name="Roman Rhetoric MOC",
            candidates_to_add=[rel],
        ),
    ])

    update = _make_update_mock(text="/accept_moc ms-proposed")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_accept_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "✓ Applied suggestion" in reply
    assert "created new MOC" in reply
    assert "Roman Rhetoric MOC" in reply
    # MOC file now exists.
    assert (hypatia_vault / "MOC" / "Roman Rhetoric MOC.md").exists()


@pytest.mark.asyncio
async def test_accept_moc_unknown_id(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Unknown id → recognizable error message naming the id."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-real")])

    update = _make_update_mock(text="/accept_moc ms-bogus")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_accept_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "❌ No suggestion found" in reply
    assert "ms-bogus" in reply


@pytest.mark.asyncio
async def test_accept_moc_non_pending(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """A rejected suggestion cannot be accepted → handler refuses with
    explicit status in the reply."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(id="ms-rj", status="rejected"),
    ])

    update = _make_update_mock(text="/accept_moc ms-rj")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_accept_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "is `rejected`" in reply
    assert "not pending" in reply


@pytest.mark.asyncio
async def test_accept_moc_no_id_usage_hint(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """``/accept-moc`` with no arg → usage hint, not unknown-id error."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-any")])

    update = _make_update_mock(text="/accept_moc")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_accept_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Usage:" in reply
    assert "/accept-moc" in reply


@pytest.mark.asyncio
async def test_accept_moc_partial_failure_reply(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Partial failure → reply names success / fail counts + first error."""
    rel_good = _seed_zettel(hypatia_vault, "Good", mocs=[])
    rel_bad = "zettel/Missing.md"  # not seeded → vault_edit fails

    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(
            id="ms-partial",
            target="MOC/Stoicism MOC.md",
            candidates_to_add=[rel_good, rel_bad],
        ),
    ])

    update = _make_update_mock(text="/accept_moc ms-partial")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_accept_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "partially applied" in reply
    assert "1 succeeded" in reply
    assert "1 failed" in reply
    assert "reverted to pending" in reply
    # Queue back to pending with error.
    from alfred.telegram.moc_suggestion_views import lookup_suggestion
    updated = lookup_suggestion(qp, "ms-partial")
    assert updated is not None
    assert updated.status == "pending"
    assert updated.last_apply_error is not None


@pytest.mark.asyncio
async def test_accept_moc_unauthorized_silent_drop(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Unknown user → silent drop, no vault write."""
    rel = _seed_zettel(hypatia_vault, "ShouldNotEdit", mocs=[])
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(
            id="ms-blocked",
            target="MOC/Stoicism MOC.md",
            candidates_to_add=[rel],
        ),
    ])

    update = _make_update_mock(text="/accept_moc ms-blocked", user_id=999)
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp, allowed_user_id=42)

    await bot.on_accept_moc(update, ctx)

    update.message.reply_text.assert_not_called()
    # Member NOT edited.
    import frontmatter
    post = frontmatter.load(str(hypatia_vault / rel))
    assert not any("Stoicism MOC" in str(m) for m in (post.metadata.get("mocs") or []))


# ---------------------------------------------------------------------------
# /reject-moc handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_moc_happy_path(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Pending → rejected; reply confirms."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(id="ms-r1", target="MOC/Stoicism MOC.md"),
    ])

    update = _make_update_mock(text="/reject_moc ms-r1")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_reject_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "✓ Rejected suggestion" in reply
    assert "ms-r1" in reply
    assert "will not be re-proposed" in reply
    # Queue advanced to rejected.
    from alfred.telegram.moc_suggestion_views import lookup_suggestion
    updated = lookup_suggestion(qp, "ms-r1")
    assert updated is not None
    assert updated.status == "rejected"


@pytest.mark.asyncio
async def test_reject_moc_unknown_id(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Unknown id → recognizable error."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-real")])

    update = _make_update_mock(text="/reject_moc ms-bogus")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_reject_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "❌ No suggestion found" in reply


@pytest.mark.asyncio
async def test_reject_moc_already_applied(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Already-applied entries cannot be rejected."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [
        _make_suggestion(id="ms-app", status="applied"),
    ])

    update = _make_update_mock(text="/reject_moc ms-app")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_reject_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "is `applied`" in reply
    assert "not pending" in reply


@pytest.mark.asyncio
async def test_reject_moc_no_id_usage_hint(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """``/reject-moc`` with no arg → usage hint."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-x")])

    update = _make_update_mock(text="/reject_moc")
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp)

    await bot.on_reject_moc(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Usage:" in reply
    assert "/reject-moc" in reply


@pytest.mark.asyncio
async def test_reject_moc_unauthorized_silent_drop(
    hypatia_vault: Path, tmp_path: Path,
) -> None:
    """Unknown user → silent drop, queue unchanged."""
    qp = tmp_path / "queue.jsonl"
    _write_queue(qp, [_make_suggestion(id="ms-blk")])

    update = _make_update_mock(text="/reject_moc ms-blk", user_id=999)
    ctx = _make_ctx_mock(hypatia_vault, queue_path=qp, allowed_user_id=42)

    await bot.on_reject_moc(update, ctx)

    update.message.reply_text.assert_not_called()
    from alfred.telegram.moc_suggestion_views import lookup_suggestion
    updated = lookup_suggestion(qp, "ms-blk")
    assert updated is not None
    assert updated.status == "pending", "Queue must be unchanged after silent drop"

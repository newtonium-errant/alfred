"""Unit tests for the talker vision module + photo handler integration.

Coverage:

    * ``select_largest_photo`` picks by area, defends against ordering drift
    * ``download_photo_bytes`` wraps PTB exceptions in :class:`VisionDownloadError`
    * ``build_image_block`` produces Anthropic-shaped base64 vision blocks
    * ``build_user_content`` round-trips text-only AND multimodal cleanly
    * ``storage_path`` / ``save_image_to_inbox`` honour per-instance vault path
    * ``run_turn`` threads ``image_blocks`` onto the user turn correctly
    * Vision-disabled config gate produces user-facing reply, no API call
    * ``Session.from_dict`` + ``to_dict`` round-trip preserves ``images`` field
    * ``_render_content`` collapses image blocks to ``[image]`` (no base64
      bloat in the session-record body)
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from alfred.telegram import vision
from alfred.telegram.config import VisionConfig
from alfred.telegram.session import Session, _render_content


# --- select_largest_photo -------------------------------------------------


class _FakePhoto:
    """Minimal stand-in for telegram.PhotoSize."""
    def __init__(
        self,
        width: int,
        height: int,
        file_id: str = "f",
        file_unique_id: str = "u",
    ) -> None:
        self.width = width
        self.height = height
        self.file_id = file_id
        self.file_unique_id = file_unique_id
        self._file = None  # set by tests that exercise download

    async def get_file(self) -> Any:  # noqa: D401 — match PTB shape
        if self._file is None:
            raise RuntimeError("test setup: get_file not stubbed")
        return self._file


class _FakeFile:
    def __init__(self, content: bytes, *, raises: Exception | None = None) -> None:
        self._content = content
        self._raises = raises

    async def download_as_bytearray(self) -> bytearray:
        if self._raises is not None:
            raise self._raises
        return bytearray(self._content)


def test_select_largest_photo_picks_by_area() -> None:
    """Largest-area photo wins, even when the array is mis-ordered."""
    sizes = [
        _FakePhoto(90, 90),     # 8100 — small
        _FakePhoto(1280, 720),  # 921600 — large
        _FakePhoto(320, 240),   # 76800 — medium
    ]
    chosen = vision.select_largest_photo(sizes)
    assert chosen.width == 1280


def test_select_largest_photo_canonical_telegram_order() -> None:
    """Telegram orders smallest-to-largest; last entry should win."""
    sizes = [
        _FakePhoto(90, 90),
        _FakePhoto(320, 240),
        _FakePhoto(1280, 720),
    ]
    chosen = vision.select_largest_photo(sizes)
    assert chosen.width == 1280


def test_select_largest_photo_empty_raises() -> None:
    with pytest.raises(vision.VisionDownloadError):
        vision.select_largest_photo([])


# --- download_photo_bytes -------------------------------------------------


@pytest.mark.asyncio
async def test_download_photo_bytes_returns_bytes() -> None:
    """Successful download yields plain bytes."""
    photo = _FakePhoto(1280, 720)
    photo._file = _FakeFile(b"\x89PNG\r\n\x1a\n")
    result = await vision.download_photo_bytes(photo)
    assert isinstance(result, bytes)
    assert result == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_download_photo_bytes_wraps_exceptions() -> None:
    """Any download failure is re-raised as :class:`VisionDownloadError`."""
    photo = _FakePhoto(1280, 720)
    photo._file = _FakeFile(b"", raises=RuntimeError("network kaput"))
    with pytest.raises(vision.VisionDownloadError) as exc_info:
        await vision.download_photo_bytes(photo)
    assert "network kaput" in str(exc_info.value)


# --- build_image_block ----------------------------------------------------


def test_build_image_block_anthropic_shape() -> None:
    """Block matches Anthropic's documented vision content-block schema."""
    raw = b"hello world"
    block = vision.build_image_block(raw)
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"
    decoded = base64.standard_b64decode(block["source"]["data"])
    assert decoded == raw


def test_build_image_block_explicit_media_type() -> None:
    """Caller can override media_type for a future PNG / WebP code path."""
    block = vision.build_image_block(b"abc", media_type="image/png")
    assert block["source"]["media_type"] == "image/png"


# --- build_user_content ---------------------------------------------------


def test_build_user_content_text_only_returns_string() -> None:
    """No images → bare string preserved (wk1-shape compatibility)."""
    out = vision.build_user_content("hello")
    assert out == "hello"
    assert isinstance(out, str)


def test_build_user_content_with_image_returns_list() -> None:
    """One image → content-block list with image-then-text ordering."""
    img = vision.build_image_block(b"x")
    out = vision.build_user_content("what is this?", [img])
    assert isinstance(out, list)
    # Image-then-text per Anthropic best-practice ordering.
    assert out[0]["type"] == "image"
    assert out[1]["type"] == "text"
    assert out[1]["text"] == "what is this?"


def test_build_user_content_empty_image_list_falls_through() -> None:
    """Empty list (truthy-falsy edge) falls back to bare-string shape."""
    assert vision.build_user_content("hi", []) == "hi"


# --- storage_path / save_image_to_inbox -----------------------------------


def test_storage_path_pattern(tmp_path: Path) -> None:
    """Filename uses ``screenshot-<UTC>-<short>.<ext>`` under inbox/."""
    when = datetime(2026, 5, 1, 12, 30, 45, tzinfo=timezone.utc)
    p = vision.storage_path(tmp_path, "abcd1234ef", when=when)
    assert p == tmp_path / "inbox" / "screenshot-20260501T123045Z-abcd1234.jpg"


def test_storage_path_strips_unsafe_chars(tmp_path: Path) -> None:
    """Slashes / colons in file_unique_id are dropped."""
    when = datetime(2026, 5, 1, tzinfo=timezone.utc)
    p = vision.storage_path(tmp_path, "ab/cd:ef", when=when)
    # ``/`` and ``:`` are stripped, leaving "abcdef" → trimmed to 8.
    assert "abcdef" in p.name
    assert "/" not in p.name
    assert ":" not in p.name


def test_storage_path_empty_unique_id(tmp_path: Path) -> None:
    """Empty unique_id falls back to ``unknown`` so filename stays well-formed."""
    when = datetime(2026, 5, 1, tzinfo=timezone.utc)
    p = vision.storage_path(tmp_path, "", when=when)
    assert "unknown" in p.name


def test_save_image_to_inbox_creates_inbox(tmp_path: Path) -> None:
    """Inbox dir is created on demand; file content matches input."""
    vault = tmp_path / "salem-vault"
    # Deliberately do NOT pre-create inbox/ — save_image_to_inbox should.
    payload = b"PNG-bytes"
    out = vision.save_image_to_inbox(payload, vault, "uniq", when=datetime(
        2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc,
    ))
    assert out.exists()
    assert out.read_bytes() == payload
    assert (vault / "inbox").is_dir()


def test_save_image_per_instance_vault(tmp_path: Path) -> None:
    """Salem / Hypatia / KAL-LE each write to their own vault root."""
    salem_vault = tmp_path / "alfred-vault"
    hypatia_vault = tmp_path / "library-alexandria"
    when = datetime(2026, 5, 1, tzinfo=timezone.utc)
    salem_path = vision.save_image_to_inbox(
        b"a", salem_vault, "u1", when=when,
    )
    hypatia_path = vision.save_image_to_inbox(
        b"b", hypatia_vault, "u2", when=when,
    )
    assert "alfred-vault" in str(salem_path)
    assert "library-alexandria" in str(hypatia_path)
    assert salem_path.parent != hypatia_path.parent


# --- VisionConfig defaults ------------------------------------------------


def test_vision_config_default_enabled() -> None:
    """Default-on for the 3 live instances; disabled_reply has friendly text."""
    cfg = VisionConfig()
    assert cfg.enabled is True
    assert "describe" in cfg.disabled_reply.lower() or "image" in cfg.disabled_reply.lower()


def test_vision_config_disabled_override() -> None:
    """Explicit disable + custom reply round-trips."""
    cfg = VisionConfig(enabled=False, disabled_reply="No images here")
    assert cfg.enabled is False
    assert cfg.disabled_reply == "No images here"


# --- run_turn integration -------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_threads_image_blocks_onto_user_turn(
    state_mgr, talker_config,
) -> None:
    """``image_blocks`` end up as a content-block list on the user turn."""
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )
    from alfred.telegram import conversation

    sess = Session(
        session_id="abc",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
    )
    state_mgr.set_active(1, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    image_block = vision.build_image_block(b"abc")
    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="what is this screenshot?",
        config=talker_config,
        vault_context_str="",
        system_prompt="sys",
        user_kind="text",
        image_blocks=[image_block],
    )

    user_turns = [t for t in sess.transcript if t["role"] == "user"]
    assert len(user_turns) == 1
    content = user_turns[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image"
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "what is this screenshot?"

    # API call site reached with multimodal content list intact.
    assert len(client.messages.calls) == 1
    sent_messages = client.messages.calls[0]["messages"]
    assert sent_messages[-1]["role"] == "user"
    assert isinstance(sent_messages[-1]["content"], list)
    assert sent_messages[-1]["content"][0]["type"] == "image"


@pytest.mark.asyncio
async def test_run_turn_without_image_blocks_preserves_string_shape(
    state_mgr, talker_config,
) -> None:
    """No image_blocks → user turn stays as a bare string (wk1 compat)."""
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )
    from alfred.telegram import conversation

    sess = Session(
        session_id="abc",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
    )
    state_mgr.set_active(1, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="plain text",
        config=talker_config,
        vault_context_str="",
        system_prompt="sys",
        user_kind="text",
    )

    user_turns = [t for t in sess.transcript if t["role"] == "user"]
    # Bare string preserved; existing render / API paths unchanged.
    assert user_turns[0]["content"] == "plain text"


# --- Session.images field round-trip --------------------------------------


def test_session_to_from_dict_preserves_images() -> None:
    """``images`` field survives the JSON round-trip in state."""
    sess = Session(
        session_id="abc",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
        images=[{
            "path": "/vault/inbox/screenshot.jpg",
            "file_unique_id": "u1",
            "bytes": 1024,
            "turn_index": 0,
            "timestamp": "2026-05-01T12:00:00+00:00",
        }],
    )
    rehydrated = Session.from_dict(sess.to_dict())
    assert rehydrated.images == sess.images


def test_session_from_dict_pre_vision_records_default_empty() -> None:
    """Old state files with no ``images`` key load with an empty list."""
    raw = {
        "session_id": "old",
        "chat_id": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_message_at": datetime.now(timezone.utc).isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [],
        "vault_ops": [],
        # No "images" key — pre-vision state file.
    }
    sess = Session.from_dict(raw)
    assert sess.images == []


# --- _render_content image branch -----------------------------------------


def test_render_content_collapses_image_block_to_marker() -> None:
    """Image content blocks render as ``[image]`` — no base64 in the body."""
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "X" * 100}},
        {"type": "text", "text": "what's this?"},
    ]
    out = _render_content(content)
    assert "[image]" in out
    assert "what's this?" in out
    # No base64 leakage into the rendered transcript body.
    assert "X" * 50 not in out


# --- Vision-disabled gate (unit-level slice of on_photo) ------------------


@pytest.mark.asyncio
async def test_on_photo_gate_when_vision_disabled(talker_config) -> None:
    """``vision.enabled=false`` → user reply, no download, no save, no LLM."""
    from alfred.telegram import bot

    talker_config.vision = VisionConfig(
        enabled=False,
        disabled_reply="Vision is off for this instance.",
    )

    # Build a minimal Update + ctx whose only requirement is reply_text.
    reply = AsyncMock()
    update = type("U", (), {})()
    update.message = type("M", (), {})()
    update.message.photo = [_FakePhoto(1280, 720)]
    update.message.reply_text = reply
    update.message.caption = None
    update.effective_chat = type("C", (), {"id": 1})()
    update.effective_user = type("EU", (), {"id": 1})()

    ctx = type("Ctx", (), {})()
    ctx.application = type("App", (), {"bot_data": {
        "config": talker_config,
        "state_mgr": None,
        "anthropic_client": None,
        "system_prompt": "",
        "vault_context_str": "",
        "chat_locks": {},
    }})()
    ctx.bot = type("B", (), {})()

    await bot.on_photo(update, ctx)
    reply.assert_awaited_once()
    args, _kwargs = reply.call_args
    assert args[0] == "Vision is off for this instance."


@pytest.mark.asyncio
async def test_on_photo_unauthorized_user_silent(talker_config) -> None:
    """Non-allowlisted user gets no reply, no download (matches voice behavior)."""
    from alfred.telegram import bot

    reply = AsyncMock()
    update = type("U", (), {})()
    update.message = type("M", (), {})()
    update.message.photo = [_FakePhoto(1280, 720)]
    update.message.reply_text = reply
    update.message.caption = None
    update.effective_chat = type("C", (), {"id": 1})()
    # Different user_id — not in allowed_users=[1]
    update.effective_user = type("EU", (), {"id": 99999})()

    ctx = type("Ctx", (), {})()
    ctx.application = type("App", (), {"bot_data": {
        "config": talker_config,
        "state_mgr": None,
        "anthropic_client": None,
        "system_prompt": "",
        "vault_context_str": "",
        "chat_locks": {},
    }})()
    ctx.bot = type("B", (), {})()

    await bot.on_photo(update, ctx)
    # Silent — no reply at all (matches the voice / text unauthorized path).
    reply.assert_not_awaited()


# --- Save-failure non-fatal contract --------------------------------------


@pytest.mark.asyncio
async def test_on_photo_save_failure_continues_to_llm(
    talker_config, monkeypatch, capsys,
) -> None:
    """Save-to-inbox failure is non-fatal: image still reaches the LLM.

    Locks the contract claimed in the builder report + verified by the
    code-reviewer pass — without this regression test, a future refactor
    could silently flip the "save failure aborts conversation" decision
    and the only signal would be Andrew not getting replies on
    screenshots when the disk is full.

    Asserts:
        (a) ``handle_message`` is called despite the ``save_image_to_inbox``
            failure (conversation proceeds, not aborted).
        (b) ``image_blocks`` reaches ``handle_message`` populated — the
            in-memory bytes still become a vision content block.
        (c) ``image_metadata`` is empty — no audit-trail row is written
            for an image we couldn't save (avoids dangling-path entries
            on the session record).
        (d) The ``talker.bot.photo_save_failed`` log line carries the
            new ``action=continuing_to_llm_in_memory_only`` field so an
            operator tailing logs can grep the policy decision without
            re-reading source.
    """
    from alfred.telegram import bot, vision as vision_mod

    # 1. Stub vision.save_image_to_inbox to raise — simulates disk full,
    # permission error, vault-path-doesn't-exist, etc. Any exception
    # must hit the except branch in on_photo.
    def _boom(*_args: Any, **_kwargs: Any) -> Path:
        raise OSError("synthetic disk full")
    monkeypatch.setattr(vision_mod, "save_image_to_inbox", _boom)

    # 2. Capture the kwargs handle_message receives. We don't need the
    # downstream Anthropic + state work to actually run — the contract
    # we care about is "on_photo invoked handle_message with the right
    # kwargs after the save failure." Replacing the symbol on the
    # module is the cleanest way to isolate the on_photo slice.
    captured_kwargs: dict[str, Any] = {}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        captured_kwargs["args"] = args
        captured_kwargs["kwargs"] = kwargs

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    # 3. Build a photo update with a fake file the download path can use.
    photo = _FakePhoto(1280, 720, file_unique_id="abc123xy")
    photo._file = _FakeFile(b"\x89PNG\r\n\x1a\n payload bytes")

    reply = AsyncMock()
    update = type("U", (), {})()
    update.message = type("M", (), {})()
    update.message.photo = [photo]
    update.message.reply_text = reply
    update.message.caption = "what's in this screenshot?"
    update.effective_chat = type("C", (), {"id": 1})()
    update.effective_user = type("EU", (), {"id": 1})()

    ctx = type("Ctx", (), {})()
    ctx.application = type("App", (), {"bot_data": {
        "config": talker_config,
        "state_mgr": None,
        "anthropic_client": None,
        "system_prompt": "",
        "vault_context_str": "",
        "chat_locks": {},
    }})()
    ctx.bot = type("B", (), {})()

    await bot.on_photo(update, ctx)

    # (a) handle_message reached — save failure did NOT abort.
    assert captured_kwargs, "handle_message was never invoked — save failure aborted the conversation"

    # (b) image_blocks present and well-formed; the in-memory bytes are
    # intact even though disk persistence failed.
    image_blocks = captured_kwargs["kwargs"].get("image_blocks")
    assert image_blocks is not None
    assert len(image_blocks) == 1
    assert image_blocks[0]["type"] == "image"
    assert image_blocks[0]["source"]["type"] == "base64"
    # Round-trip the base64 to confirm the original bytes survived.
    decoded = base64.standard_b64decode(image_blocks[0]["source"]["data"])
    assert decoded == b"\x89PNG\r\n\x1a\n payload bytes"

    # (c) image_metadata is empty — no audit-trail row written when
    # the file isn't actually on disk. (append_image is called inside
    # handle_message based on this list; an empty list means no
    # append_image call for this turn.)
    image_metadata = captured_kwargs["kwargs"].get("image_metadata")
    assert image_metadata == []

    # Caption forwarded as text.
    assert captured_kwargs["kwargs"].get("text") == "what's in this screenshot?"

    # (d) The "continuing" log line was emitted. structlog is configured
    # to write to stdout in tests so capsys catches it.
    captured = capsys.readouterr()
    log_text = captured.out + captured.err
    assert "talker.bot.photo_save_failed" in log_text
    assert "action=continuing_to_llm_in_memory_only" in log_text
    assert "synthetic disk full" in log_text

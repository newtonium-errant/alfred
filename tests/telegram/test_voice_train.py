"""Tests for the /train + /method-source slash commands (2026-05-07 arc).

Covers:
  * Routing fix: ``vault_create`` for ``essay`` lands at
    ``document/essay/<slug>.md`` (regression for f006c48e).
  * Routing fix: ``voice``, ``voice-cluster``, ``method`` route to
    their own top-level directories.
  * Slash command parsing (``/train`` + ``--cluster`` flag,
    ``/method-source``).
  * Slug + title derivation from raw text.
  * Most-recent-paste classification from conversation transcript.
  * Queue enqueue / drain (JSONL append + read-and-truncate).
  * Async extraction worker — happy path (write structured record,
    flip raw status, DM operator).
  * Async extraction worker — failure path (extraction fails →
    raw record marked failed → operator DM).
  * Cluster-tier rebuild only when ≥2 leaves share a tag.
  * Overall-tier rebuild only when ≥2 cluster summaries exist.
  * Config gate: /train + /method_source NOT registered when block
    absent / disabled; ARE registered when enabled.
  * Config gate: Salem (no voice_train block) doesn't accept the
    commands at the bot layer.
  * Scope: hypatia scope admits writes to all four new types.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import frontmatter
import pytest

from alfred.telegram import bot, voice_train
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
    VoiceTrainConfig,
)
from alfred.vault import ops, schema, scope


# ---------------------------------------------------------------------------
# Routing-fix regression tests
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    """Hypatia-shaped vault root (just the directory; ops.vault_create
    creates type-dirs on demand)."""
    return tmp_path


def test_essay_routes_to_document_essay(vault_path: Path) -> None:
    """REGRESSION: f006c48e essay landed at note/ instead of document/essay/.

    Pin the routing fix — vault_create with type='essay' under hypatia
    scope MUST land at ``document/essay/<slug>.md``.
    """
    result = ops.vault_create(
        vault_path,
        "essay",
        "If You're Not Doing This Then You're Being Left Behind",
        set_fields={"status": "published", "author": "Andrew Errant"},
        body="essay text",
        scope="hypatia",
    )
    assert result["path"].startswith("document/essay/"), (
        f"essay should route to document/essay/, got: {result['path']}"
    )


def test_voice_routes_to_voice(vault_path: Path) -> None:
    result = ops.vault_create(
        vault_path,
        "voice",
        "Some Voice Profile",
        set_fields={"status": "active"},
        body="profile body",
        scope="hypatia",
    )
    assert result["path"].startswith("voice/")
    # Specifically NOT under voice/cluster/.
    assert not result["path"].startswith("voice/cluster/")


def test_voice_cluster_routes_to_voice_cluster(vault_path: Path) -> None:
    result = ops.vault_create(
        vault_path,
        "voice-cluster",
        "veteran",
        set_fields={"status": "active"},
        body="cluster body",
        scope="hypatia",
    )
    assert result["path"].startswith("voice/cluster/")


def test_method_routes_to_method(vault_path: Path) -> None:
    result = ops.vault_create(
        vault_path,
        "method",
        "Getting Things Done",
        set_fields={"status": "active"},
        body="method body",
        scope="hypatia",
    )
    assert result["path"].startswith("method/")


def test_essay_status_validation(vault_path: Path) -> None:
    """``status: published`` is valid for essay (matches f006c48e)."""
    # published is in {draft, published, archived}
    ops.vault_create(
        vault_path, "essay", "Some Essay",
        set_fields={"status": "published"},
        body="x", scope="hypatia",
    )
    # Reject an unknown status.
    with pytest.raises(ops.VaultError):
        ops.vault_create(
            vault_path, "essay", "Other Essay",
            set_fields={"status": "wibble"},
            body="x", scope="hypatia",
        )


def test_hypatia_scope_admits_new_types(vault_path: Path) -> None:
    """All four new types pass HYPATIA_CREATE_TYPES + KNOWN_TYPES_HYPATIA."""
    for record_type in ("essay", "voice", "voice-cluster", "method"):
        assert record_type in scope.HYPATIA_CREATE_TYPES
        assert record_type in schema.KNOWN_TYPES_HYPATIA


def test_talker_scope_does_not_admit_new_types() -> None:
    """Salem (talker scope) MUST NOT create essay/voice/method records.

    Phase 1 is Hypatia-only. A future Salem opt-in would extend
    TALKER_CREATE_TYPES.
    """
    for record_type in ("essay", "voice", "voice-cluster", "method"):
        assert record_type not in scope.TALKER_CREATE_TYPES


def test_kalle_scope_does_not_admit_new_types() -> None:
    """KAL-LE (coding instance) MUST NOT create voice training records."""
    for record_type in ("essay", "voice", "voice-cluster", "method"):
        assert record_type not in scope.KALLE_CREATE_TYPES


# ---------------------------------------------------------------------------
# Slug + title derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hello World", "hello-world"),
        ("# A Heading\n\nbody text", "a-heading"),
        ("## Sub\n\nbody", "sub"),
        ("  multiple   spaces  ", "multiple-spaces"),
        ("café", "cafe"),
        ("über", "uber"),
        ("", "untitled"),
        ("---", "untitled"),
        ("If You're Not Doing This",
         "if-youre-not-doing-this"),
    ],
)
def test_slug_from_text(text: str, expected: str) -> None:
    assert voice_train.slug_from_text(text) == expected


def test_slug_from_text_caps_at_80() -> None:
    long_title = "a-very-long-title-" * 10
    slug = voice_train.slug_from_text(long_title)
    assert len(slug) <= 80
    assert not slug.endswith("-")


def test_title_from_text_strips_markdown_heading() -> None:
    text = "# The Real Title\n\nFirst paragraph body."
    assert voice_train.title_from_text(text) == "The Real Title"


def test_title_from_text_uses_first_nonblank_line() -> None:
    text = "\n\n\nFirst real line\nsecond"
    assert voice_train.title_from_text(text) == "First real line"


def test_title_from_text_caps_long_titles() -> None:
    long = "A" * 200
    title = voice_train.title_from_text(long)
    assert len(title) <= 100


def test_title_from_text_empty_returns_dated_default() -> None:
    title = voice_train.title_from_text("")
    today = date.today().isoformat()
    assert today in title


# ---------------------------------------------------------------------------
# Slash-command argument parsing
# ---------------------------------------------------------------------------


def test_parse_train_args_no_input() -> None:
    assert voice_train.parse_train_args("/train", []) == (None, "")


def test_parse_train_args_body_only() -> None:
    cluster, body = voice_train.parse_train_args(
        "/train this is the body", ["this", "is", "the", "body"],
    )
    assert cluster is None
    assert body == "this is the body"


def test_parse_train_args_cluster_only() -> None:
    cluster, body = voice_train.parse_train_args(
        "/train --cluster veteran", ["--cluster", "veteran"],
    )
    assert cluster == "veteran"
    assert body == ""


def test_parse_train_args_cluster_with_body() -> None:
    cluster, body = voice_train.parse_train_args(
        "/train --cluster veteran some text",
        ["--cluster", "veteran", "some", "text"],
    )
    assert cluster == "veteran"
    assert body == "some text"


def test_parse_train_args_cluster_with_multiline_body() -> None:
    """Newlines preserved when raw_text is multiline."""
    raw = (
        "/train --cluster veteran\n"
        "First paragraph here.\n\n"
        "Second paragraph."
    )
    args = ["--cluster", "veteran", "First", "paragraph", "here.",
            "Second", "paragraph."]
    cluster, body = voice_train.parse_train_args(raw, args)
    assert cluster == "veteran"
    # Newlines preserved.
    assert "\n\n" in body
    assert body.startswith("First paragraph here.")


def test_parse_method_source_args() -> None:
    body = voice_train.parse_method_source_args(
        "/method-source GTD core principles", ["GTD", "core", "principles"],
    )
    assert body == "GTD core principles"


def test_parse_method_source_args_empty() -> None:
    assert voice_train.parse_method_source_args("/method-source", []) == ""


# ---------------------------------------------------------------------------
# Most-recent paste resolver
# ---------------------------------------------------------------------------


def test_find_most_recent_paste_picks_qualifying_message() -> None:
    transcript = [
        {"role": "user", "content": "short hi"},
        {"role": "assistant", "content": "hi back"},
        {"role": "user", "content": "x" * 300},
    ]
    result = voice_train.find_most_recent_user_paste(transcript)
    assert len(result) == 300


def test_find_most_recent_paste_walks_in_reverse() -> None:
    """Most-recent qualifying paste wins (reverse order)."""
    transcript = [
        {"role": "user", "content": "FIRST" * 100},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "SECOND" * 100},
    ]
    result = voice_train.find_most_recent_user_paste(transcript)
    assert result.startswith("SECOND")


def test_find_most_recent_paste_skips_short_below_threshold() -> None:
    transcript = [
        {"role": "user", "content": "x" * 300},  # qualifying
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "short"},  # below threshold
    ]
    result = voice_train.find_most_recent_user_paste(transcript)
    assert len(result) == 300  # picks the qualifying one


def test_find_most_recent_paste_handles_content_block_list() -> None:
    """Anthropic content-block shape (image+text) — text only."""
    transcript = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {}},
                {"type": "text", "text": "y" * 250},
            ],
        },
    ]
    result = voice_train.find_most_recent_user_paste(transcript)
    assert "y" in result
    assert len(result) >= 250


def test_find_most_recent_paste_empty_transcript() -> None:
    assert voice_train.find_most_recent_user_paste([]) == ""


def test_find_most_recent_paste_no_qualifying() -> None:
    transcript = [
        {"role": "user", "content": "short"},
        {"role": "user", "content": "still short"},
    ]
    assert voice_train.find_most_recent_user_paste(transcript) == ""


# ---------------------------------------------------------------------------
# Queue enqueue / drain
# ---------------------------------------------------------------------------


def test_enqueue_and_drain_roundtrip(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    job = voice_train.make_job(
        kind="voice",
        raw_rel_path="document/essay/test.md",
        raw_name="test",
        raw_body="some essay body",
        cluster="veteran",
        chat_id=12345,
        instance="Hypatia",
    )
    voice_train.enqueue_job(queue, job)
    drained = voice_train.drain_queue(queue)
    assert len(drained) == 1
    assert drained[0].kind == "voice"
    assert drained[0].raw_rel_path == "document/essay/test.md"
    assert drained[0].cluster == "veteran"
    assert drained[0].chat_id == 12345
    # File should be empty post-drain.
    assert queue.read_text() == ""


def test_drain_empty_queue(tmp_path: Path) -> None:
    queue = tmp_path / "missing.jsonl"
    assert voice_train.drain_queue(queue) == []


def test_drain_skips_malformed_lines(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        "not valid json\n"
        + json.dumps({
            "job_id": "x", "kind": "voice", "raw_rel_path": "x.md",
            "raw_name": "x", "raw_body": "x",
        }) + "\n"
        + "{}invalid\n"
    )
    drained = voice_train.drain_queue(queue)
    assert len(drained) == 1
    assert drained[0].kind == "voice"


def test_drain_schema_tolerance(tmp_path: Path) -> None:
    """Future schema bumps land alongside in-flight queue entries."""
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({
            "job_id": "x", "kind": "voice", "raw_rel_path": "x.md",
            "raw_name": "x", "raw_body": "x",
            "future_field": "ignored",  # extra field — tolerate
        }) + "\n"
    )
    drained = voice_train.drain_queue(queue)
    assert len(drained) == 1


def test_multiple_enqueues_append(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    for i in range(3):
        job = voice_train.make_job(
            kind="method", raw_rel_path=f"source/x{i}.md",
            raw_name=f"x{i}", raw_body="body",
        )
        voice_train.enqueue_job(queue, job)
    drained = voice_train.drain_queue(queue)
    assert len(drained) == 3


# ---------------------------------------------------------------------------
# Raw-record save
# ---------------------------------------------------------------------------


def test_save_raw_essay_writes_to_document_essay(vault_path: Path) -> None:
    result = voice_train.save_raw_essay(
        vault_path,
        text="# My Essay\n\nBody text here.",
        cluster="veteran",
        scope="hypatia",
    )
    assert result.success
    assert result.rel_path.startswith("document/essay/")
    # Verify the file landed.
    abs_path = vault_path / result.rel_path
    assert abs_path.is_file()
    fm = frontmatter.load(str(abs_path))
    assert fm["type"] == "essay"
    # ``status`` carries the essay-lifecycle state (draft until the
    # operator confirms publication); ``extraction_status`` is the
    # separate worker-driven processing flag the worker flips to
    # ``complete`` post-extraction.
    assert fm["status"] == "draft"
    assert fm["extraction_status"] == "pending"
    assert fm["cluster"] == "veteran"
    assert fm["author"] == "Andrew Errant"


def test_save_raw_essay_no_cluster(vault_path: Path) -> None:
    result = voice_train.save_raw_essay(
        vault_path,
        text="Some essay text without cluster",
        cluster=None,
        scope="hypatia",
    )
    assert result.success
    fm = frontmatter.load(str(vault_path / result.rel_path))
    assert "cluster" not in fm.metadata


def test_save_raw_source_writes_to_source(vault_path: Path) -> None:
    result = voice_train.save_raw_source(
        vault_path,
        text="# GTD\n\nCore principles...",
        scope="hypatia",
    )
    assert result.success
    assert result.rel_path.startswith("source/")
    fm = frontmatter.load(str(vault_path / result.rel_path))
    assert fm["type"] == "source"
    assert fm["extraction_status"] == "pending"


def test_save_raw_source_with_image_metadata(vault_path: Path) -> None:
    result = voice_train.save_raw_source(
        vault_path,
        text="# Method from screenshot\n\nbody",
        scope="hypatia",
        image_metadata=[{
            "path": str(vault_path / "inbox" / "screenshot.jpg"),
            "file_unique_id": "abc",
            "bytes": 1024,
        }],
    )
    assert result.success
    fm = frontmatter.load(str(vault_path / result.rel_path))
    assert fm["source_kind"] == "image"
    assert "source_images" in fm.metadata
    body = (vault_path / result.rel_path).read_text()
    assert "## Images" in body


# ---------------------------------------------------------------------------
# Async extraction worker
# ---------------------------------------------------------------------------


class _FakeMsgs:
    """Stand-in for client.messages with replaceable response."""

    def __init__(self, response_text: str) -> None:
        self._response = response_text
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Block:
            def __init__(self, text: str) -> None:
                self.type = "text"
                self.text = text

        class _Resp:
            def __init__(self, text: str) -> None:
                self.content = [_Block(text)]

        return _Resp(self._response)


class _FakeClient:
    def __init__(self, response_text: str = "") -> None:
        self.messages = _FakeMsgs(response_text)


@pytest.mark.asyncio
async def test_worker_processes_voice_job(vault_path: Path, tmp_path: Path) -> None:
    """End-to-end: queued voice job → structured record + raw status update + DM.

    The worker's main loop pattern is ``await wait_for(shutdown_event.wait(),
    timeout=poll_seconds)`` — so we need shutdown_event to fire AFTER the
    poll interval elapses, not before. Test uses poll_seconds=0.05 +
    shutdown after 0.5s so the worker drains 1-9 ticks (only the first
    has a job; the rest are idle).
    """
    import asyncio

    # Save raw record first.
    raw_result = voice_train.save_raw_essay(
        vault_path,
        text="# Test Essay\n\nBody.",
        cluster="veteran",
        scope="hypatia",
    )
    assert raw_result.success

    # Enqueue.
    queue = tmp_path / "queue.jsonl"
    job = voice_train.make_job(
        kind="voice",
        raw_rel_path=raw_result.rel_path,
        raw_name=raw_result.name,
        raw_body="# Test Essay\n\nBody.",
        cluster="veteran",
        chat_id=99,
    )
    voice_train.enqueue_job(queue, job)

    # Fake LLM output (frontmatter-only structured shape).
    llm_output = (
        "---\n"
        "register: casual-declarative\n"
        "paragraph_rhythm: short-paragraphs\n"
        "voice_signature_one_line: A test voice.\n"
        "---\n\n"
        "Body of the voice profile.\n"
    )
    client = _FakeClient(response_text=llm_output)
    dm_calls: list[tuple[int, str]] = []

    async def _dm(chat_id: int, text: str) -> None:
        dm_calls.append((chat_id, text))

    shutdown = asyncio.Event()

    async def stop_after() -> None:
        # Need 2x poll interval to give the worker time to: tick 1
        # (draining the queue + processing one job), then tick 2 (idle).
        # Without this margin, fast machines occasionally beat the
        # worker out of its first poll.
        await asyncio.sleep(0.4)
        shutdown.set()

    asyncio.create_task(stop_after())
    await voice_train.run_worker(
        queue_path=queue,
        vault_path=vault_path,
        client=client,
        model="claude-opus-4-5",
        scope="hypatia",
        instance="Hypatia",
        poll_seconds=0.05,
        dm_callback=_dm,
        shutdown_event=shutdown,
    )

    # Structured voice profile written.
    voice_files = list((vault_path / "voice").glob("*.md"))
    # Filter cluster/ subdir and any voice profile records.
    leaf_files = [p for p in voice_files if p.is_file()]
    assert len(leaf_files) >= 1, (
        f"expected at least one voice/*.md file; got: {voice_files}"
    )
    # Raw record's extraction_status flipped to complete.
    raw_fm = frontmatter.load(str(vault_path / raw_result.rel_path))
    assert raw_fm["extraction_status"] == "complete"
    # DM fired.
    assert any("voice" in msg.lower() for _, msg in dm_calls)


@pytest.mark.asyncio
async def test_worker_marks_raw_failed_on_llm_empty(
    vault_path: Path, tmp_path: Path,
) -> None:
    """Empty LLM output → raw record's extraction_status flipped to failed."""
    import asyncio

    raw_result = voice_train.save_raw_essay(
        vault_path, text="# Some essay\n\nbody", cluster=None, scope="hypatia",
    )
    queue = tmp_path / "queue.jsonl"
    job = voice_train.make_job(
        kind="voice",
        raw_rel_path=raw_result.rel_path,
        raw_name=raw_result.name,
        raw_body="some essay",
        chat_id=42,
    )
    voice_train.enqueue_job(queue, job)

    client = _FakeClient(response_text="")  # empty
    dm_calls: list[tuple[int, str]] = []

    async def _dm(chat_id: int, text: str) -> None:
        dm_calls.append((chat_id, text))

    shutdown = asyncio.Event()

    async def stop_after() -> None:
        await asyncio.sleep(0.4)
        shutdown.set()

    asyncio.create_task(stop_after())
    await voice_train.run_worker(
        queue_path=queue, vault_path=vault_path, client=client,
        model="claude-opus-4-5", scope="hypatia", instance="Hypatia",
        poll_seconds=0.05, dm_callback=_dm, shutdown_event=shutdown,
    )

    raw_fm = frontmatter.load(str(vault_path / raw_result.rel_path))
    assert raw_fm["extraction_status"] == "failed"
    # Operator DM'd.
    assert dm_calls
    assert "retry" in dm_calls[0][1].lower() or "re-run" in dm_calls[0][1].lower()


# ---------------------------------------------------------------------------
# Cluster / overall builder thresholds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_rebuild_skipped_below_threshold(vault_path: Path) -> None:
    """Single leaf with cluster tag → cluster builder doesn't fire."""
    # Create one voice leaf with cluster=veteran.
    ops.vault_create(
        vault_path, "voice", "single-leaf",
        set_fields={"status": "active", "cluster": "veteran"},
        body="leaf body", scope="hypatia",
    )
    client = _FakeClient(response_text="should-not-be-called")
    await voice_train.maybe_rebuild_cluster(
        vault_path=vault_path, client=client, model="claude-opus-4-5",
        scope="hypatia", cluster_name="veteran",
        dm_callback=None, chat_id=0,
    )
    # No voice/cluster/ files written.
    cluster_dir = vault_path / "voice" / "cluster"
    assert not cluster_dir.exists() or not list(cluster_dir.glob("*.md"))
    # No LLM call made.
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_cluster_rebuild_fires_at_two_leaves(vault_path: Path) -> None:
    """Two leaves with same cluster tag → cluster builder fires."""
    # Two leaves sharing cluster=veteran.
    for name in ("leaf-one", "leaf-two"):
        ops.vault_create(
            vault_path, "voice", name,
            set_fields={"status": "active", "cluster": "veteran"},
            body="body", scope="hypatia",
        )
    cluster_output = (
        "---\n"
        "cluster_name: veteran\n"
        "leaf_count: 2\n"
        "register: casual-declarative\n"
        "voice_signature_one_line: Veteran voice.\n"
        "---\n\n"
        "Cluster summary body.\n"
    )
    client = _FakeClient(response_text=cluster_output)
    await voice_train.maybe_rebuild_cluster(
        vault_path=vault_path, client=client, model="claude-opus-4-5",
        scope="hypatia", cluster_name="veteran",
        dm_callback=None, chat_id=0,
    )
    # Cluster file landed.
    cluster_files = list((vault_path / "voice" / "cluster").glob("*.md"))
    assert len(cluster_files) == 1
    fm = frontmatter.load(str(cluster_files[0]))
    assert fm["type"] == "voice-cluster"


@pytest.mark.asyncio
async def test_overall_rebuild_skipped_below_threshold(vault_path: Path) -> None:
    """One cluster summary → overall builder doesn't fire."""
    cluster_dir = vault_path / "voice" / "cluster"
    cluster_dir.mkdir(parents=True)
    (cluster_dir / "single.md").write_text(
        "---\ntype: voice-cluster\n---\nsingle cluster\n"
    )
    client = _FakeClient(response_text="should-not-be-called")
    await voice_train.maybe_rebuild_overall(
        vault_path=vault_path, client=client, model="claude-opus-4-5",
        scope="hypatia", dm_callback=None, chat_id=0,
    )
    overall_path = vault_path / "voice" / "Andrew Voice Profile.md"
    assert not overall_path.exists()
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_overall_rebuild_fires_at_two_clusters(vault_path: Path) -> None:
    """Two cluster summaries → overall profile builder fires."""
    cluster_dir = vault_path / "voice" / "cluster"
    cluster_dir.mkdir(parents=True)
    for name in ("a", "b"):
        (cluster_dir / f"{name}.md").write_text(
            f"---\ntype: voice-cluster\nname: {name}\n"
            f"created: 2026-05-07\n---\nbody {name}\n"
        )
    overall_output = (
        "---\n"
        "cluster_count: 2\n"
        "postures: [a, b]\n"
        "voice_signature_one_line: Overall.\n"
        "---\n\n"
        "Overall body.\n"
    )
    client = _FakeClient(response_text=overall_output)
    await voice_train.maybe_rebuild_overall(
        vault_path=vault_path, client=client, model="claude-opus-4-5",
        scope="hypatia", dm_callback=None, chat_id=0,
    )
    overall_path = vault_path / "voice" / "Andrew Voice Profile.md"
    assert overall_path.is_file()


# ---------------------------------------------------------------------------
# Config gate — handler registration
# ---------------------------------------------------------------------------


def _make_hypatia_config(
    tmp_path: Path,
    *,
    voice_train_config: VoiceTrainConfig | None = None,
) -> TalkerConfig:
    """Hypatia-shaped config; tests can opt voice_train in/out."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(
            api_key="test-key", model="claude-sonnet-4-6",
        ),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(
            name="Hypatia", canonical="Hypatia", tool_set="hypatia",
        ),
        voice_train=voice_train_config,
    )


def _build_app_and_get_commands(config: TalkerConfig) -> set[str]:
    from alfred.telegram import state as state_mod

    with tempfile.TemporaryDirectory() as tmp:
        mgr = state_mod.StateManager(Path(tmp) / "s.json")
        mgr.load()
        app = bot.build_app(
            config=config,
            state_mgr=mgr,
            anthropic_client=None,
            system_prompt_provider="",
            vault_context_str="",
        )
        commands: set[str] = set()
        for group in app.handlers.values():
            for h in group:
                cmds = getattr(h, "commands", None)
                if cmds:
                    commands.update(cmds)
        return commands


def test_voice_train_commands_not_registered_when_block_absent(
    tmp_path: Path,
) -> None:
    """Default Salem-style config → /train + /method_source NOT registered."""
    config = _make_hypatia_config(tmp_path, voice_train_config=None)
    commands = _build_app_and_get_commands(config)
    assert "train" not in commands
    assert "method_source" not in commands


def test_voice_train_commands_not_registered_when_disabled(
    tmp_path: Path,
) -> None:
    """Block present but command_enabled=False → not registered."""
    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(command_enabled=False),
    )
    commands = _build_app_and_get_commands(config)
    assert "train" not in commands
    assert "method_source" not in commands


def test_voice_train_commands_registered_when_enabled(tmp_path: Path) -> None:
    """Hypatia opts in → both commands show up."""
    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(command_enabled=True),
    )
    commands = _build_app_and_get_commands(config)
    assert "train" in commands
    assert "method_source" in commands


def test_voice_train_config_loaded_from_unified() -> None:
    """Config builder honors the YAML block."""
    from alfred.telegram.config import load_from_unified

    raw = {
        "telegram": {
            "bot_token": "x",
            # instance.name is required (no default per the
            # InstanceConfig contract) — pass it so load_from_unified
            # doesn't trip the "Alfred default would silently misroute"
            # guard.
            "instance": {"name": "Hypatia"},
            "voice_train": {
                "command_enabled": True,
                "worker_poll_seconds": 12,
                "extraction_model": "claude-opus-4-5",
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.voice_train is not None
    assert cfg.voice_train.command_enabled is True
    assert cfg.voice_train.worker_poll_seconds == 12
    assert cfg.voice_train.extraction_model == "claude-opus-4-5"


def test_voice_train_config_block_absent_default_none() -> None:
    """Block missing → field stays None (Salem-shaped)."""
    from alfred.telegram.config import load_from_unified

    raw = {
        "telegram": {
            "bot_token": "x",
            "instance": {"name": "Salem"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.voice_train is None


# ---------------------------------------------------------------------------
# Scope helper (talker config → scope string)
# ---------------------------------------------------------------------------


def test_voice_train_scope_for_hypatia(tmp_path: Path) -> None:
    config = _make_hypatia_config(tmp_path)
    assert bot._voice_train_scope_for(config) == "hypatia"


def test_voice_train_scope_for_kalle(tmp_path: Path) -> None:
    config = _make_hypatia_config(tmp_path)
    config.instance.tool_set = "kalle"
    assert bot._voice_train_scope_for(config) == "kalle"


def test_voice_train_scope_for_salem_falls_through_to_talker(
    tmp_path: Path,
) -> None:
    config = _make_hypatia_config(tmp_path)
    config.instance.tool_set = ""
    assert bot._voice_train_scope_for(config) == "talker"


# ---------------------------------------------------------------------------
# Queue path resolution
# ---------------------------------------------------------------------------


def test_resolve_queue_path_explicit(tmp_path: Path) -> None:
    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            queue_path=str(tmp_path / "custom_queue.jsonl"),
        ),
    )
    resolved = bot._resolve_queue_path(config)
    assert resolved == tmp_path / "custom_queue.jsonl"


def test_resolve_queue_path_per_instance_default(tmp_path: Path) -> None:
    """When queue_path is None, defaults to ./data/<slug>/extraction_queue.jsonl."""
    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(command_enabled=True),
    )
    resolved = bot._resolve_queue_path(config)
    assert resolved == Path("./data") / "hypatia" / "extraction_queue.jsonl"


# ---------------------------------------------------------------------------
# Idempotency: re-extraction body_replace path
# ---------------------------------------------------------------------------


def test_voice_record_re_extraction_uses_body_replace(vault_path: Path) -> None:
    """Re-extracting on the same essay name updates the existing voice record.

    Pins the body_replace allowlist for hypatia scope (added in this
    arc — see scope.py allow_body_replace dict).
    """
    # First write — vault_create.
    ops.vault_create(
        vault_path, "voice", "First Profile",
        set_fields={"status": "active"},
        body="initial body",
        scope="hypatia",
    )
    # Second write — body_replace via vault_edit.
    ops.vault_edit(
        vault_path, "voice/First Profile.md",
        set_fields={"status": "active"},
        body_replace="rewritten body",
        scope="hypatia",
    )
    text = (vault_path / "voice" / "First Profile.md").read_text()
    assert "rewritten body" in text
    assert "initial body" not in text


def test_essay_body_replace_NOT_allowed(vault_path: Path) -> None:
    """Raw essay records are write-once; body_replace MUST be denied."""
    ops.vault_create(
        vault_path, "essay", "Some Essay",
        set_fields={"status": "draft"},
        body="initial body",
        scope="hypatia",
    )
    with pytest.raises(scope.ScopeError):
        ops.vault_edit(
            vault_path, "document/essay/Some Essay.md",
            body_replace="should not be allowed",
            scope="hypatia",
        )


# ---------------------------------------------------------------------------
# P1 #1 regression — _write_structured_record idempotency on real titles
# ---------------------------------------------------------------------------
#
# Bug shape (caught by code-reviewer 2026-05-07): the existence-check
# path used ``slug_from_text(raw_name)`` (snake-cased ASCII) while
# vault_create writes to ``<dir>/<raw_name>.md`` VERBATIM. For any
# title with apostrophes / spaces / capitals (i.e. almost every real
# title), the existence check looked at the wrong file and re-extraction
# crashed on ``VaultError("File already exists")`` instead of routing
# through body_replace. The earlier test
# (``test_voice_record_re_extraction_uses_body_replace``) only
# exercised the low-level vault_create + vault_edit calls directly,
# bypassing the slug-vs-name path computation — which is why this bug
# went uncaught in the original ship.


def test_write_structured_record_idempotent_on_apostrophe_title(
    vault_path: Path,
) -> None:
    """Re-extraction on a title with apostrophes + spaces routes to body_replace.

    Pins the P1 #1 fix: ``_write_structured_record`` MUST use the
    verbatim ``raw_name`` for both the existence check and the
    ``vault_create`` write so re-extraction routes cleanly through
    body_replace instead of crashing on ``File already exists``.
    """
    raw_name = "If You're Not Doing This"
    raw_rel_path = "document/essay/If You're Not Doing This.md"

    # First LLM emission — the structured record doesn't exist yet,
    # so the writer takes the vault_create path.
    first_output = (
        "---\n"
        "register: casual-declarative\n"
        "voice_signature: First profile.\n"
        "---\n\n"
        "Body of the first profile.\n"
    )
    target_rel_first = voice_train._write_structured_record(
        vault_path=vault_path,
        kind="voice",
        raw_name=raw_name,
        raw_rel_path=raw_rel_path,
        cluster=None,
        llm_output=first_output,
        scope="hypatia",
    )
    assert target_rel_first == f"voice/{raw_name}.md"
    assert (vault_path / target_rel_first).is_file()

    # Second LLM emission — the structured record DOES exist, so the
    # writer must take the body_replace path. Pre-fix this raised
    # VaultError("File already exists") because the existence check
    # looked at voice/if-youre-not-doing-this.md (slugged) while the
    # actual file was at voice/If You're Not Doing This.md (verbatim).
    second_output = (
        "---\n"
        "register: declarative-precise\n"
        "voice_signature: Second profile (rewrite).\n"
        "---\n\n"
        "Body of the SECOND profile.\n"
    )
    target_rel_second = voice_train._write_structured_record(
        vault_path=vault_path,
        kind="voice",
        raw_name=raw_name,
        raw_rel_path=raw_rel_path,
        cluster=None,
        llm_output=second_output,
        scope="hypatia",
    )
    assert target_rel_second == target_rel_first  # same path, both writes
    body_after = (vault_path / target_rel_second).read_text(encoding="utf-8")
    assert "SECOND profile" in body_after
    assert "first profile" not in body_after.lower() or "FIRST" not in body_after


def test_write_structured_record_idempotent_method_with_spaces(
    vault_path: Path,
) -> None:
    """Same P1 #1 fix — but for the method writer path."""
    raw_name = "Getting Things Done"
    raw_rel_path = "source/Getting Things Done.md"

    first_output = (
        "---\nmethod_kind: framework\ndomain: productivity\n---\n\nFirst.\n"
    )
    p1 = voice_train._write_structured_record(
        vault_path=vault_path, kind="method", raw_name=raw_name,
        raw_rel_path=raw_rel_path, cluster=None,
        llm_output=first_output, scope="hypatia",
    )
    assert p1 == f"method/{raw_name}.md"

    second_output = (
        "---\nmethod_kind: framework\ndomain: productivity\n---\n\nSecond.\n"
    )
    p2 = voice_train._write_structured_record(
        vault_path=vault_path, kind="method", raw_name=raw_name,
        raw_rel_path=raw_rel_path, cluster=None,
        llm_output=second_output, scope="hypatia",
    )
    assert p2 == p1
    body_after = (vault_path / p2).read_text(encoding="utf-8")
    assert "Second" in body_after
    assert "First" not in body_after


@pytest.mark.asyncio
async def test_cluster_rebuild_idempotent_on_multiword_cluster_name(
    vault_path: Path,
) -> None:
    """P1 #1 fix for ``maybe_rebuild_cluster``.

    When the cluster_name has spaces / apostrophes (e.g. ``Veteran's
    Notes`` vs slug ``veterans-notes``), re-running the cluster
    rebuild MUST route through body_replace, not crash on "file
    already exists." Pre-fix the cluster path used
    ``slug_from_text(cluster_name)`` for the existence check while
    vault_create wrote ``voice/cluster/<cluster_name>.md`` verbatim.
    """
    cluster_name = "Veteran's Notes"
    # Two leaves with the multi-word cluster tag.
    for leaf_name in ("leaf-one", "leaf-two"):
        ops.vault_create(
            vault_path, "voice", leaf_name,
            set_fields={"status": "active", "cluster": cluster_name},
            body=f"body of {leaf_name}", scope="hypatia",
        )
    cluster_output = (
        "---\n"
        f"cluster_name: {cluster_name}\n"
        "leaf_count: 2\n"
        "---\n\nFirst cluster body.\n"
    )
    client = _FakeClient(response_text=cluster_output)

    # First rebuild — vault_create path.
    await voice_train.maybe_rebuild_cluster(
        vault_path=vault_path, client=client, model="claude-opus-4-5",
        scope="hypatia", cluster_name=cluster_name,
        dm_callback=None, chat_id=0,
    )
    target = vault_path / "voice" / "cluster" / f"{cluster_name}.md"
    assert target.is_file(), (
        "cluster file must land at voice/cluster/<verbatim-name>.md"
    )

    # Second rebuild — body_replace path. Pre-fix this would crash
    # because the existence check looked at voice/cluster/veterans-notes.md
    # (slugged) while the actual file was at voice/cluster/Veteran's Notes.md.
    second_output = (
        "---\n"
        f"cluster_name: {cluster_name}\n"
        "leaf_count: 2\n"
        "---\n\nSecond cluster body (rewrite).\n"
    )
    client2 = _FakeClient(response_text=second_output)
    # Should NOT raise.
    await voice_train.maybe_rebuild_cluster(
        vault_path=vault_path, client=client2, model="claude-opus-4-5",
        scope="hypatia", cluster_name=cluster_name,
        dm_callback=None, chat_id=0,
    )
    body_after = target.read_text(encoding="utf-8")
    assert "Second cluster body" in body_after
    assert "First cluster body" not in body_after


# ---------------------------------------------------------------------------
# P1 #2 regression — _strip_command_prefix matches the underscore form
# ---------------------------------------------------------------------------
#
# Bug shape (caught by code-reviewer 2026-05-07): PTB rejects ``-`` in
# CommandHandler names so ``/method-source`` is registered as
# ``CommandHandler("method_source", ...)``. Real Telegram deliveries
# arrive as ``/method_source ...`` (underscore). The earlier
# ``parse_method_source_args`` call passed ``command="method-source"``
# to ``_strip_command_prefix``, whose regex then didn't match the
# underscore form. Multi-line bodies fell back to the whitespace-
# joined ``ctx.args`` and lost paragraph breaks.


def test_parse_method_source_preserves_newlines_with_underscore_form() -> None:
    """Multi-line body delivered via /method_source preserves paragraphs.

    Pins the P1 #2 fix: ``parse_method_source_args`` must pass the
    underscore form (matching PTB's actual delivery) to
    ``_strip_command_prefix`` so the regex matches and the
    newline-preserving rebuild path takes over.
    """
    raw = (
        "/method_source\n"
        "First paragraph of the method.\n\n"
        "Second paragraph here.\n\n"
        "Third paragraph."
    )
    # PTB whitespace-splits args; this is what ctx.args would carry.
    args = [
        "First", "paragraph", "of", "the", "method.",
        "Second", "paragraph", "here.",
        "Third", "paragraph.",
    ]
    body = voice_train.parse_method_source_args(raw, args)
    # Newlines preserved (the bug's signature was their loss).
    assert "\n\n" in body, (
        f"paragraph breaks must survive; got: {body!r}"
    )
    assert body.startswith("First paragraph")
    assert "Second paragraph" in body
    assert "Third paragraph" in body


def test_parse_method_source_handles_botname_suffix() -> None:
    """``/method_source@HypatiaErrantBot ...`` (group-chat shape)."""
    raw = (
        "/method_source@HypatiaErrantBot\n"
        "First paragraph.\n\n"
        "Second paragraph."
    )
    args = ["First", "paragraph.", "Second", "paragraph."]
    body = voice_train.parse_method_source_args(raw, args)
    assert "\n\n" in body
    assert body.startswith("First paragraph")


def test_parse_method_source_hyphen_form_falls_back_gracefully() -> None:
    """If a future code path delivers ``/method-source`` (hyphen)
    rather than the underscore form PTB uses, the function should
    still return something usable — falling back to the whitespace-
    joined args rather than returning empty.

    This is a defensive test for the contract: ``parse_method_source_args``
    NEVER silently drops the body. With the underscore-form fix, the
    hyphen-form raw_text won't trigger the newline-preservation path
    (the regex match fails) — but the whitespace-joined body should
    still be returned, not empty string.
    """
    raw = "/method-source First paragraph.\n\nSecond paragraph."
    args = ["First", "paragraph.", "Second", "paragraph."]
    body = voice_train.parse_method_source_args(raw, args)
    # Body returned (not silently empty) — whitespace-joined is the
    # fallback, paragraphs lost is the cost of the legacy delivery
    # form not matching, but body is NEVER lost.
    assert body == "First paragraph. Second paragraph."


# ---------------------------------------------------------------------------
# Schema verification — intentionally-left-blank status sentinels
# ---------------------------------------------------------------------------
#
# The prompt-tuner's revisions added intentionally-left-blank exits
# emitting LLM ``status:`` sentinels (``insufficient-evidence``,
# ``incoherent-cluster``, ``not-a-method``, ``no-overall-invariants``).
# The vault writer must pass these through end-to-end — silent
# substitution of ``active`` would defeat the whole purpose. These
# tests pin both layers: schema accepts the sentinels (no
# _validate_status rejection) AND the writer passes them through.


@pytest.mark.parametrize(
    "record_type, status",
    [
        ("voice", "insufficient-evidence"),
        ("voice", "no-overall-invariants"),
        ("voice-cluster", "incoherent-cluster"),
        ("method", "not-a-method"),
    ],
)
def test_schema_accepts_intentionally_left_blank_status(
    vault_path: Path, record_type: str, status: str,
) -> None:
    """All four intentionally-left-blank sentinels pass _validate_status."""
    name = f"Test {status}"
    result = ops.vault_create(
        vault_path, record_type, name,
        set_fields={"status": status},
        body=f"intentionally-left-blank body for {status}",
        scope="hypatia",
    )
    # File landed; status preserved.
    fm = frontmatter.load(str(vault_path / result["path"]))
    assert fm["status"] == status


def test_writer_passes_through_insufficient_evidence_status(
    vault_path: Path,
) -> None:
    """``_write_structured_record`` MUST pass through LLM-emitted status.

    Pre-fix the writer set ``status: active`` unconditionally and
    excluded ``status`` from the LLM-fields overlay — silently
    dropping the intentionally-left-blank signal. Post-fix the LLM's
    status flows through to vault.
    """
    raw_name = "Thin Essay"
    raw_rel = "document/essay/Thin Essay.md"
    llm_output = (
        "---\n"
        "status: insufficient-evidence\n"
        "insufficient_reason: under 400 words, no clear voice\n"
        "---\n\n"
        "This input was insufficient to extract a voice profile.\n"
    )
    target = voice_train._write_structured_record(
        vault_path=vault_path, kind="voice", raw_name=raw_name,
        raw_rel_path=raw_rel, cluster=None, llm_output=llm_output,
        scope="hypatia",
    )
    fm = frontmatter.load(str(vault_path / target))
    assert fm["status"] == "insufficient-evidence", (
        f"LLM-emitted intentionally-left-blank sentinel was dropped; "
        f"got status={fm.get('status')!r}"
    )
    assert fm.get("insufficient_reason") == "under 400 words, no clear voice"


def test_writer_defaults_status_to_active_when_llm_omits_it(
    vault_path: Path,
) -> None:
    """When the LLM emits no status (the happy path), default to ``active``."""
    raw_name = "Healthy Essay"
    raw_rel = "document/essay/Healthy Essay.md"
    llm_output = (
        "---\n"
        "register: casual-declarative\n"
        "voice_signature: A real voice.\n"
        "---\n\n"
        "Real profile body.\n"
    )
    target = voice_train._write_structured_record(
        vault_path=vault_path, kind="voice", raw_name=raw_name,
        raw_rel_path=raw_rel, cluster=None, llm_output=llm_output,
        scope="hypatia",
    )
    fm = frontmatter.load(str(vault_path / target))
    assert fm["status"] == "active"


def test_writer_passes_through_method_not_a_method_status(
    vault_path: Path,
) -> None:
    """Same passthrough contract for the method writer path."""
    raw_name = "Just an Essay"
    raw_rel = "source/Just an Essay.md"
    llm_output = (
        "---\n"
        "status: not-a-method\n"
        "not_a_method_reason: opinion essay, no extractable principles\n"
        "---\n\n"
        "This source did not contain an extractable method.\n"
    )
    target = voice_train._write_structured_record(
        vault_path=vault_path, kind="method", raw_name=raw_name,
        raw_rel_path=raw_rel, cluster=None, llm_output=llm_output,
        scope="hypatia",
    )
    fm = frontmatter.load(str(vault_path / target))
    assert fm["status"] == "not-a-method"
    assert fm.get("not_a_method_reason") == (
        "opinion essay, no extractable principles"
    )


# ---------------------------------------------------------------------------
# Bug #57 (2026-05-08) — YAML frontmatter slug derivation
# ---------------------------------------------------------------------------
#
# Substack-export pastes start with a YAML frontmatter block delimited
# by ``---`` lines:
#
#     ---
#     title: "If You're Not Doing This..."
#     subtitle: ...
#     ---
#
#     # If You're Not Doing This...
#     ...body...
#
# Pre-fix the slug derivation took the first non-empty line, which is
# the bare ``---`` marker. Slug ended up as ``---`` (then dropped to
# ``"untitled"`` by the alphanumeric filter) but the FILENAME written
# by ``vault_create`` came from a separate path that produced ``---.md``.
# Log signature was: ``slug=untitled`` + ``rel_path=document/essay/---.md``.

_FRONTMATTER_PASTE = (
    "---\n"
    "title: \"If You're Not Doing This Then You're Being Left Behind\"\n"
    "subtitle: \"No Fate But What We Make...\"\n"
    "author: Andrew Errant\n"
    "date: 2025-07-16\n"
    "canonical: https://example.com/p/if-youre\n"
    "---\n"
    "\n"
    "# If You're Not Doing This Then You're Being Left Behind\n"
    "\n"
    "### No Fate But What We Make...\n"
    "\n"
    "*By Andrew Errant — Jul 16, 2025*\n"
    "\n"
    "---\n"
    "\n"
    "# You've seen the headlines.\n"
    "\n"
    "AI is the FUTURE!! ...\n"
)


def test_slug_from_text_uses_yaml_frontmatter_title() -> None:
    """Bug #57 (2026-05-08): YAML frontmatter title beats first-line ``---``.

    Pre-fix: slug came out as ``---`` (then dropped to ``"untitled"``)
    while the filename written was ``---.md``. Post-fix: parse the
    frontmatter, use the ``title:`` field.
    """
    assert (
        voice_train.slug_from_text(_FRONTMATTER_PASTE)
        == "if-youre-not-doing-this-then-youre-being-left-behind"
    )


def test_title_from_text_uses_yaml_frontmatter_title() -> None:
    """Bug #57: title derivation aligned with slug derivation.

    The bug's surface signature was a divergence between the slug
    variable (``"untitled"``) and the filename written (``---.md``).
    Title MUST share the same resolution path as slug so they can't
    drift.
    """
    assert (
        voice_train.title_from_text(_FRONTMATTER_PASTE)
        == "If You're Not Doing This Then You're Being Left Behind"
    )


def test_slug_from_text_frontmatter_no_title_field_falls_back_to_h1() -> None:
    """Frontmatter present but no ``title:`` field → use first H1 below."""
    text = (
        "---\n"
        "subtitle: \"Something else\"\n"
        "author: Andrew Errant\n"
        "---\n"
        "\n"
        "# The Real H1 Heading\n"
        "\n"
        "Body text.\n"
    )
    assert voice_train.slug_from_text(text) == "the-real-h1-heading"


def test_title_from_text_frontmatter_no_title_field_falls_back_to_h1() -> None:
    text = (
        "---\n"
        "author: Andrew Errant\n"
        "---\n"
        "\n"
        "# The Real H1 Heading\n"
        "\n"
        "Body.\n"
    )
    assert voice_train.title_from_text(text) == "The Real H1 Heading"


def test_slug_from_text_h1_only_no_frontmatter_regression() -> None:
    """REGRESSION: pre-existing H1-only behaviour MUST still work.

    The fix MUST NOT regress the no-frontmatter path — operators who
    paste ``# Title\\n\\nbody`` directly (without a Substack export
    wrapper) still get the H1 as the slug.
    """
    text = "# A Heading\n\nbody text"
    assert voice_train.slug_from_text(text) == "a-heading"


def test_slug_from_text_neither_frontmatter_nor_h1() -> None:
    """No frontmatter, no heading — first non-blank line wins."""
    text = "Just some prose\nthat starts with no markup."
    assert voice_train.slug_from_text(text) == "just-some-prose"


def test_slug_from_text_starts_with_dashes_never_returns_dashes() -> None:
    """Defensive guard: a paste that begins with ``---\\n`` MUST NOT slug to ``---``.

    This is the core regression for Bug #57. Even if the frontmatter
    parser fails (malformed YAML, missing closing ``---``, etc.), the
    slug MUST NOT come back as ``"---"`` or get used as a literal
    filename component.
    """
    # Malformed frontmatter (no closing ``---``). Falls through to the
    # line scanner — which must skip the ``---`` line.
    text = "---\ntitle: not closed properly\nfoo: bar\n\n# Real Title\n"
    slug = voice_train.slug_from_text(text)
    # The bare ``---`` is not a valid slug component; the result must
    # be derived from a real title source or the default.
    assert slug != "---"
    assert "---" not in slug or slug.count("-") < 3 * len(slug)
    # Specifically, never just dashes.
    assert slug.strip("-") != ""


def test_slug_from_text_frontmatter_with_quoted_title() -> None:
    """YAML quoted title (single or double) — outer quotes stripped."""
    text_dquote = (
        '---\ntitle: "Quoted Title"\n---\n\nbody\n'
    )
    text_squote = (
        "---\ntitle: 'Quoted Title'\n---\n\nbody\n"
    )
    assert voice_train.slug_from_text(text_dquote) == "quoted-title"
    assert voice_train.slug_from_text(text_squote) == "quoted-title"


def test_slug_and_title_align_for_frontmatter_paste() -> None:
    """Bug #57's load-bearing invariant: slug and title share one resolver.

    The bug's surface signature was DIVERGENCE between the two — the
    log emitted ``slug=untitled`` while the file landed at ``---.md``.
    Pin: for any frontmatter-shaped input, ``title_from_text`` and
    ``slug_from_text`` must agree on the title source.
    """
    title = voice_train.title_from_text(_FRONTMATTER_PASTE)
    slug = voice_train.slug_from_text(_FRONTMATTER_PASTE)
    # Slug should be the slugified form of the title.
    expected_slug = title.lower()
    expected_slug = expected_slug.replace("'", "")  # apostrophe drops
    expected_slug = expected_slug.replace("  ", " ").replace(" ", "-")
    # The exact slugification rules are tested elsewhere; here we just
    # pin that BOTH derive from the title (not from the ``---`` marker).
    assert "youre-not-doing" in slug
    assert title.startswith("If You're Not")


def test_save_raw_essay_frontmatter_paste_lands_at_real_slug(
    vault_path: Path,
) -> None:
    """End-to-end: save_raw_essay on a Substack-export paste.

    Pre-fix the file landed at ``document/essay/---.md`` (per Andrew's
    2026-05-08 log: ``rel_path=document/essay/---.md slug=untitled``).
    Post-fix the file lands under the real title.
    """
    result = voice_train.save_raw_essay(
        vault_path,
        text=_FRONTMATTER_PASTE,
        cluster="veteran",
        scope="hypatia",
    )
    assert result.success
    # Critical: filename MUST NOT be ``---.md``.
    assert "---.md" not in result.rel_path, (
        f"Bug #57 regression — file landed at {result.rel_path!r}"
    )
    # Slug variable MUST NOT be ``"untitled"`` for a paste that has
    # a real frontmatter title.
    assert result.slug != "untitled", (
        f"Bug #57 slug-divergence regression — slug={result.slug!r} "
        f"despite valid frontmatter title"
    )
    # Real title flows through.
    assert "youre-not-doing" in result.slug.lower()


# ---------------------------------------------------------------------------
# Bug #58 (2026-05-08) — multi-message paste buffer
# ---------------------------------------------------------------------------
#
# Telegram caps each message at ~4096 chars; long Substack pastes get
# split into 2-4 messages by the client. Pre-fix only the first chunk
# (with the ``/train`` prefix) was processed; subsequent chunks fell
# through to Hypatia's natural-language path.
#
# The fix is a per-chat-id paste buffer in bot.bot_data. The slash
# handler opens the buffer + seeds it; on_text appends to the buffer
# (instead of routing to handle_message); a debounce-delayed flush
# task drains the buffer and runs the existing save_raw + enqueue path.


def test_pending_paste_assembled_text_joins_with_paragraph_breaks() -> None:
    """Chunks are joined with ``\\n\\n`` (paragraph break)."""
    pending = voice_train.PendingPaste(
        chat_id=1, kind="voice", cluster=None,
    )
    voice_train.append_paste_chunk(pending, "First chunk text.")
    voice_train.append_paste_chunk(pending, "Second chunk text.")
    voice_train.append_paste_chunk(pending, "Third chunk text.")
    assembled = pending.assembled_text()
    assert assembled == (
        "First chunk text.\n\nSecond chunk text.\n\nThird chunk text."
    )


def test_pending_paste_skips_empty_chunks() -> None:
    """Empty / whitespace-only chunks dropped silently."""
    pending = voice_train.PendingPaste(
        chat_id=1, kind="voice", cluster=None,
    )
    voice_train.append_paste_chunk(pending, "Real text.")
    voice_train.append_paste_chunk(pending, "")
    voice_train.append_paste_chunk(pending, "   \t  \n  ")
    voice_train.append_paste_chunk(pending, "More text.")
    assembled = pending.assembled_text()
    assert assembled == "Real text.\n\nMore text."


def test_pending_paste_assembled_long_substack_essay() -> None:
    """REGRESSION: 3-chunk Substack paste reassembles to the full essay.

    Andrew's 2026-05-08 case: voice profile extracted from the FIRST
    chunk only. Worker output ended at ``closing_style: "Incomplete —
    essay cuts off mid-sentence at 'I was'"``. Post-fix the buffer
    reassembles the chunks into one paste so the worker sees the full
    essay.
    """
    chunk_1 = "First half of an essay. " * 200  # ~5000 chars
    chunk_2 = "Middle section continuing the thought. " * 200
    chunk_3 = "Final paragraphs and the conclusion."
    pending = voice_train.PendingPaste(
        chat_id=999, kind="voice", cluster="veteran",
    )
    voice_train.append_paste_chunk(pending, chunk_1)
    voice_train.append_paste_chunk(pending, chunk_2)
    voice_train.append_paste_chunk(pending, chunk_3)
    assembled = pending.assembled_text()
    # All three chunks present.
    assert "First half" in assembled
    assert "Middle section" in assembled
    assert "Final paragraphs" in assembled
    # Combined length matches sum of chunks (modulo whitespace).
    assert len(assembled) > len(chunk_1)
    assert len(assembled) > len(chunk_2)


@pytest.mark.asyncio
async def test_voice_train_buffer_appends_text_messages(tmp_path: Path) -> None:
    """End-to-end: /train + 2 plain-text continuations → single flushed paste.

    Drives the bot helpers directly (avoids spinning up a real PTB
    application). Confirms:
      * Slash handler opens the buffer with the initial chunk.
      * ``_voice_train_buffer_append`` consumes plain-text chunks
        (returns True), appends them, resets the timer.
      * Flush callback assembles the full paste and runs save_raw +
        enqueue end-to-end.
    """
    import asyncio
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            debounce_seconds=1,
            max_buffer_seconds=10,
            min_paste_chars=50,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    # Stand-in PTB application — only the bits the helpers touch.
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    fake_bot = _FakeBot()
    application = SimpleNamespace(bot_data=bot_data)
    ctx = SimpleNamespace(application=application, bot=fake_bot)

    chat_id = 12345
    initial_chunk = "Chunk one is the slash-arg paste. " * 5
    second_chunk = "Chunk two is a plain text follow-up. " * 5
    third_chunk = "Chunk three closes the paste. " * 5

    # Open buffer + seed with first chunk.
    pending = voice_train.PendingPaste(
        chat_id=chat_id, kind="voice", cluster="veteran",
    )
    voice_train.append_paste_chunk(pending, initial_chunk)
    bot_data["voice_train_pending"][chat_id] = pending

    # Append second chunk (simulates on_text consuming a continuation).
    appended_2 = bot._voice_train_buffer_append(ctx, chat_id, second_chunk)
    assert appended_2 is True
    # Append third chunk.
    appended_3 = bot._voice_train_buffer_append(ctx, chat_id, third_chunk)
    assert appended_3 is True
    # Buffer carries all 3 chunks.
    assert len(pending.chunks) == 3

    # Drive the flush directly.
    await bot._flush_voice_train_buffer(ctx, chat_id, reason="test")

    # Buffer popped from registry.
    assert chat_id not in bot_data["voice_train_pending"]
    # Raw essay record landed.
    essay_files = list((Path(config.vault.path) / "document" / "essay").glob("*.md"))
    assert len(essay_files) == 1
    body = essay_files[0].read_text(encoding="utf-8")
    # All three chunks present in the assembled paste body.
    assert "Chunk one" in body
    assert "Chunk two" in body
    assert "Chunk three" in body
    # Queue has one job.
    queue_jobs = voice_train.drain_queue(Path(config.voice_train.queue_path))
    assert len(queue_jobs) == 1
    assert queue_jobs[0].kind == "voice"
    assert queue_jobs[0].cluster == "veteran"
    # The enqueued raw_body matches the assembled paste, not just the
    # first chunk — this is the load-bearing pin for Bug #58.
    assert "Chunk one" in queue_jobs[0].raw_body
    assert "Chunk two" in queue_jobs[0].raw_body
    assert "Chunk three" in queue_jobs[0].raw_body
    # Operator got at least one ack message.
    assert sent, "no ack messages sent"


@pytest.mark.asyncio
async def test_voice_train_buffer_flush_skips_when_below_min_paste(
    tmp_path: Path,
) -> None:
    """Buffer flushes empty when assembled text is below ``min_paste_chars``.

    Per intentionally-left-blank: emits an explicit "no usable paste"
    reply rather than silently dropping. Caller can re-issue.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=200,  # threshold
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 999

    pending = voice_train.PendingPaste(
        chat_id=chat_id, kind="voice", cluster=None,
    )
    voice_train.append_paste_chunk(pending, "tiny")  # below threshold
    bot_data["voice_train_pending"][chat_id] = pending

    await bot._flush_voice_train_buffer(ctx, chat_id, reason="test")

    # No raw record written (input below min).
    essay_dir = Path(config.vault.path) / "document" / "essay"
    assert not essay_dir.exists() or not list(essay_dir.glob("*.md"))
    # No queue job.
    queue_jobs = voice_train.drain_queue(
        Path(config.voice_train.queue_path)
    )
    assert queue_jobs == []
    # But the operator got a "no usable paste" reply.
    assert sent
    assert "no usable paste" in sent[0][1].lower()


def test_voice_train_buffer_append_returns_false_when_no_buffer() -> None:
    """When no buffer is open for chat_id, append returns False (caller proceeds).

    Pin: the early-return path in on_text only fires when there's
    actually a buffer to append to. Plain conversation text in a chat
    with no /train in flight goes through handle_message normally.
    """
    from types import SimpleNamespace

    bot_data: dict[str, Any] = {
        "config": SimpleNamespace(voice_train=None),
        "voice_train_pending": {},
    }
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))
    assert bot._voice_train_buffer_append(ctx, 12345, "any text") is False


@pytest.mark.asyncio
async def test_voice_train_buffer_method_source_path(tmp_path: Path) -> None:
    """/method-source path: buffer + flush works for method kind too.

    Pin: the symmetric application of the multi-message paste fix
    across both slash commands. Same chunking issue affects
    /method-source for the same reason (Andrew's 2026-05-08 brief
    explicitly covered both).
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 3737

    pending = voice_train.PendingPaste(
        chat_id=chat_id, kind="method", cluster=None,
        image_metadata=[],
    )
    voice_train.append_paste_chunk(pending, "First chunk of method source.")
    voice_train.append_paste_chunk(pending, "Second chunk of method source.")
    bot_data["voice_train_pending"][chat_id] = pending

    await bot._flush_voice_train_buffer(ctx, chat_id, reason="test")

    # Source record landed.
    src_files = list((Path(config.vault.path) / "source").glob("*.md"))
    assert len(src_files) == 1
    body = src_files[0].read_text(encoding="utf-8")
    assert "First chunk" in body
    assert "Second chunk" in body
    # Queue carries the method job with full assembled body.
    queue_jobs = voice_train.drain_queue(Path(config.voice_train.queue_path))
    assert len(queue_jobs) == 1
    assert queue_jobs[0].kind == "method"
    assert "First chunk" in queue_jobs[0].raw_body
    assert "Second chunk" in queue_jobs[0].raw_body


# ---------------------------------------------------------------------------
# Ticket #67 — em-dash / en-dash variant of --cluster flag (iOS auto-correct)
# ---------------------------------------------------------------------------


def test_parse_train_args_emdash_cluster_flag() -> None:
    """REGRESSION: iOS auto-corrects ``--`` → ``—``; em-dash variant works.

    Ticket #67 (2026-05-07). Andrew's observation: ``/train --cluster
    personal`` typed on iOS arrived as ``/train —cluster personal``;
    the strict ``--cluster`` token comparison missed, the em-dash token
    fell into the body, and the slug derivation produced
    ``cluster-personal`` against a broken path.
    """
    cluster, body = voice_train.parse_train_args(
        "/train —cluster personal",
        ["—cluster", "personal"],
    )
    assert cluster == "personal"
    assert body == ""


def test_parse_train_args_endash_cluster_flag() -> None:
    """En-dash (U+2013) variant — covered by the same normalization."""
    cluster, body = voice_train.parse_train_args(
        "/train –cluster veteran",
        ["–cluster", "veteran"],
    )
    assert cluster == "veteran"
    assert body == ""


def test_parse_train_args_double_hyphen_cluster_flag_regression() -> None:
    """REGRESSION: canonical ``--cluster`` still works post-Ticket #67.

    The em/en-dash normalization is a defensive ADDITION; the ASCII
    double-hyphen path must continue to parse identically.
    """
    cluster, body = voice_train.parse_train_args(
        "/train --cluster business",
        ["--cluster", "business"],
    )
    assert cluster == "business"
    assert body == ""


def test_parse_method_source_args_emdash_first_token_unknown_flag_preserved() -> None:
    """Ticket #74 (2026-05-07): em-dash + non-flag-name word stays as em-dash.

    /method-source has no flags today, so ``—notes`` is NOT a known
    flag and must NOT be re-flowed into ``--notes`` — the leading
    em-dash is preserved verbatim so an operator pasting an em-dash-led
    note keeps the original typography.

    Pre-Ticket #74, the leading-dash normalizer fired on any
    dash-run-followed-by-letter, which silently rewrote essay-body em
    dashes to ASCII double-hyphen. The new allowlist (``cluster`` only
    today; `_KNOWN_FLAG_NAMES`) limits the rewrite to actual flags.
    """
    body = voice_train.parse_method_source_args(
        "/method-source —notes some text",
        ["—notes", "some", "text"],
    )
    # Em-dash preserved on the unknown-flag token.
    assert "—notes" in body
    assert "--notes" not in body
    assert "some text" in body


def test_parse_method_source_args_endash_unknown_flag_preserved() -> None:
    """Ticket #74: en-dash variant on a non-flag-name word also preserved."""
    body = voice_train.parse_method_source_args(
        "/method-source –notes some text",
        ["–notes", "some", "text"],
    )
    assert "–notes" in body
    assert "--notes" not in body


def test_parse_method_source_args_canonical_double_hyphen_regression() -> None:
    """Canonical ASCII double-hyphen still works post-Ticket #67."""
    body = voice_train.parse_method_source_args(
        "/method-source --notes some text",
        ["--notes", "some", "text"],
    )
    assert "--notes" in body
    assert "some text" in body


def test_parse_train_args_emdash_in_body_preserved() -> None:
    """Em-dash WITHIN the essay body (after the flag) must NOT be normalized.

    The normalization only applies to the leading flag-prefix token.
    Body tokens — including em-dash-decorated prose — pass through
    intact.
    """
    cluster, body = voice_train.parse_train_args(
        "/train --cluster personal an essay — with em-dashes",
        ["--cluster", "personal", "an", "essay", "—",
         "with", "em-dashes"],
    )
    assert cluster == "personal"
    # The em-dash inside the body survives.
    assert "—" in body


def test_parse_train_args_emdash_with_multiline_body() -> None:
    """REGRESSION: em-dash flag + multiline body strips header cleanly.

    Ticket #67 (2026-05-07). When the operator pastes an iOS-corrected
    flag together with a newline-rich essay, ``_strip_command_prefix``
    re-extracts the body to preserve paragraph breaks. The cluster-
    header regex must match the em/en-dash variants OR the
    ``—cluster personal`` text gets glued to the front of the saved
    essay body.
    """
    raw = (
        "/train —cluster personal\n"
        "First paragraph here.\n\n"
        "Second paragraph."
    )
    cluster, body = voice_train.parse_train_args(
        raw,
        ["—cluster", "personal", "First", "paragraph", "here.",
         "Second", "paragraph."],
    )
    assert cluster == "personal"
    # Header was stripped; body starts at the essay text.
    assert body.startswith("First paragraph here.")
    # Em-dash flag header was NOT smuggled into the body.
    assert "—cluster" not in body
    assert "personal\n" not in body
    # Paragraph break preserved.
    assert "\n\n" in body


# ---------------------------------------------------------------------------
# Ticket #74 — em-dash normalization tightened to flag-pattern allowlist
# ---------------------------------------------------------------------------


def test_parse_train_args_emdash_non_flag_token_preserved() -> None:
    """Ticket #74 (2026-05-07): em-dash + arbitrary word stays as em-dash.

    Pre-Ticket #74 the normalizer rewrote any leading dash-run-then-
    letter token into ``--<word>``, so an operator pasting
    ``/train —Some opening line`` lost the leading em-dash and got
    ``--Some opening line`` saved as the body. The flag-pattern
    allowlist now restricts normalization to ``--<known-flag>``
    shapes only.
    """
    cluster, body = voice_train.parse_train_args(
        "/train —Some opening line",
        ["—Some", "opening", "line"],
    )
    # No flag → no cluster.
    assert cluster is None
    # Em-dash preserved verbatim on the leading token.
    assert body.startswith("—Some")
    assert "--Some" not in body


def test_parse_train_args_emdash_not_a_flag_token_preserved() -> None:
    """Ticket #74: ``—not-a-flag value`` keeps its em-dash.

    Even when the post-em-dash text vaguely looks flag-shaped
    (lowercase letters + hyphen) but doesn't match a known flag
    name, the em-dash is preserved.
    """
    cluster, body = voice_train.parse_train_args(
        "/train —not-a-flag value",
        ["—not-a-flag", "value"],
    )
    assert cluster is None
    assert body.startswith("—not-a-flag")
    assert "--not-a-flag" not in body


def test_parse_method_source_args_emdash_non_flag_token_preserved() -> None:
    """Ticket #74: symmetric coverage on /method-source."""
    body = voice_train.parse_method_source_args(
        "/method-source —Some opening line",
        ["—Some", "opening", "line"],
    )
    assert body.startswith("—Some")
    assert "--Some" not in body


# ---------------------------------------------------------------------------
# Ticket #65 — buffer regression tests (max-buffer ceiling, pre-emption)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_train_buffer_ceiling_flushes_immediately(
    tmp_path: Path,
) -> None:
    """REGRESSION: max-buffer ceiling clamps to immediate flush.

    Ticket #65 (2026-05-07). The clamp logic in
    ``_schedule_voice_train_flush`` (effective_sleep <=0 → flush
    immediately) was verified by inspection but never exercised. Pin
    it: open a buffer with ``opened_at`` mocked far enough in the
    past that ``elapsed > max_buffer_seconds``, then call
    ``_voice_train_buffer_append``. The flush must fire before the
    next event-loop tick.
    """
    import asyncio as _asyncio
    from datetime import datetime as _dt, timedelta, timezone as _tz
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            debounce_seconds=10,
            max_buffer_seconds=30,
            min_paste_chars=10,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 7777

    # Open a buffer with an opened_at FAR in the past — past the
    # max_buffer_seconds ceiling.
    pending = voice_train.PendingPaste(
        chat_id=chat_id, kind="voice", cluster=None,
    )
    pending.opened_at = _dt.now(_tz.utc) - timedelta(seconds=999)
    voice_train.append_paste_chunk(
        pending, "Some essay content. " * 20,
    )
    bot_data["voice_train_pending"][chat_id] = pending

    # Append a chunk — this triggers _schedule_voice_train_flush, which
    # should detect we're past the ceiling and flush immediately.
    appended = bot._voice_train_buffer_append(ctx, chat_id, "more content")
    assert appended is True

    # Yield to allow the flush task to run. Single yield should be
    # enough since the path is "create_task → immediate await of flush".
    await _asyncio.sleep(0.05)

    # Buffer popped (flushed).
    assert chat_id not in bot_data["voice_train_pending"]
    # Either save+enqueue ran OR the buffer flushed empty — but the
    # flush DID happen (no longer in the registry).


@pytest.mark.asyncio
async def test_voice_train_buffer_preempted_on_new_command(
    tmp_path: Path,
) -> None:
    """REGRESSION: a fresh command pre-empts an existing open buffer.

    Ticket #65 (2026-05-07). ``_open_or_extend_voice_train_buffer``
    closes any prior buffer before opening a new one. Pin: open a
    /train buffer, call _open_or_extend with kind=method; assert (a)
    the prior buffer was finalized (popped + flushed=True), (b) the
    new buffer is the active one with the new kind, (c) the prior
    kind/cluster are gone.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            debounce_seconds=10,
            max_buffer_seconds=60,
            min_paste_chars=10,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    # The reply_target.reply_text is awaited by _open_or_extend_voice_train_buffer.
    reply_target = MagicMock()
    reply_target.reply_text = AsyncMock()

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 8888

    # First buffer: /train with cluster=veteran + initial chunk
    # large enough to land a saved record on flush.
    initial_voice = "Voice essay content. " * 30
    await bot._open_or_extend_voice_train_buffer(
        ctx,
        chat_id=chat_id,
        kind="voice",
        cluster="veteran",
        initial_text=initial_voice,
        image_metadata=[],
        reply_target=reply_target,
    )
    first_pending = bot_data["voice_train_pending"][chat_id]
    assert first_pending.kind == "voice"
    assert first_pending.cluster == "veteran"

    # Second command: /method-source — this MUST pre-empt the first.
    await bot._open_or_extend_voice_train_buffer(
        ctx,
        chat_id=chat_id,
        kind="method",
        cluster=None,
        initial_text="Method source content. " * 30,
        image_metadata=[],
        reply_target=reply_target,
    )

    # The first buffer was flushed (it had flushed=True set during
    # the pre-empt path).
    assert first_pending.flushed is True
    # The active buffer is the new one with kind=method.
    new_pending = bot_data["voice_train_pending"][chat_id]
    assert new_pending is not first_pending
    assert new_pending.kind == "method"
    assert new_pending.cluster is None


# ---------------------------------------------------------------------------
# Ticket #70 — debounce tuning + end-marker detection
# ---------------------------------------------------------------------------


def test_voice_train_config_debounce_default_is_10s() -> None:
    """REGRESSION: debounce default bumped from 5s → 10s.

    Ticket #70 (2026-05-07). Telegram client auto-split inter-chunk
    delays observed at 7-12s in real use; 5s caused premature flushes
    that dropped late chunks.
    """
    cfg = VoiceTrainConfig()
    assert cfg.debounce_seconds == 10


def test_voice_train_config_rapid_arrival_default() -> None:
    """Rapid-arrival window default — 3s captures sub-second bursts."""
    cfg = VoiceTrainConfig()
    assert cfg.rapid_arrival_seconds == 3.0


def test_buffer_has_end_marker_substack_footnote() -> None:
    """Substack footnote-tail recognized as end marker."""
    body = "Body content. " * 30 + "\n[^1]: a footnote definition"
    assert voice_train.buffer_has_end_marker(body) is True


def test_buffer_has_end_marker_substack_subscribed() -> None:
    """Substack ``Subscribed`` closing block recognized."""
    body = "Body content. " * 30 + "\nSubscribed\nmore"
    assert voice_train.buffer_has_end_marker(body) is True


def test_buffer_has_end_marker_signoff() -> None:
    """``Would you like to know more?`` sign-off recognized."""
    body = "Body content. " * 30 + " Would you like to know more?"
    assert voice_train.buffer_has_end_marker(body) is True


def test_buffer_has_end_marker_bio_opener() -> None:
    """``I write about `` author-bio opener recognized."""
    body = "Body content. " * 30 + " I write about software."
    assert voice_train.buffer_has_end_marker(body) is True


def test_buffer_has_end_marker_no_marker() -> None:
    """A plain essay body without any marker → False."""
    body = "Just a plain essay body without any closing markers. " * 20
    assert voice_train.buffer_has_end_marker(body) is False


def test_buffer_has_end_marker_marker_too_early_rejected() -> None:
    """Marker appearing in the FIRST 200 chars is treated as false-positive.

    A chat that mentions ``Subscribed`` early in the conversation
    should not trigger end-marker detection — the guard requires the
    marker to come AFTER body content.
    """
    body = "Subscribed early.\n\n" + "More body. " * 100
    # ``\nSubscribed\n`` doesn't appear ≥200 chars in.
    assert voice_train.buffer_has_end_marker(body) is False


def test_buffer_has_end_marker_empty_string() -> None:
    """Empty input → False (degenerate-input guard)."""
    assert voice_train.buffer_has_end_marker("") is False


# --- Ticket #73 — end-marker anchored to last 500 chars / last 25% ---


def test_buffer_has_end_marker_anchored_in_last_500_fires() -> None:
    """Ticket #73 (2026-05-07): marker in last 500 chars → fires (happy path).

    Long body (>500 chars) with the marker placed in the trailing
    window. This is the ordinary end-of-essay case and must continue
    to flush early on the next debounce tick.
    """
    body = "x" * 2000 + " Would you like to know more?"
    # text_len=2030, marker at 2001. anchor_window=max(500, 2030//4)=507,
    # threshold=2030-507=1523. 2001 >= 1523 → fires.
    assert voice_train.buffer_has_end_marker(body) is True


def test_buffer_has_end_marker_mid_body_false_positive_rejected() -> None:
    """Ticket #73: marker BEFORE last 500 / last 25% → no fire.

    The 200-char floor is satisfied but the marker is mid-body, not
    end-of-body. End-anchor gate stops the false-positive flush that
    would otherwise race ahead of the real closing chunk.

    Brief's example shape: 4000-char buffer with marker at position
    1500. text_len=4042 (4000 + 42 for marker tail), anchor_window=
    max(500, 4042//4=1010)=1010, threshold=4042-1010=3032.
    Marker at 1500 < 3032 → no fire.
    """
    body = (
        "x" * 1500
        + " Would you like to know more?"
        + " continuation. " * 200  # ~3000 more chars
    )
    assert len(body) > 4000
    assert voice_train.buffer_has_end_marker(body) is False


def test_buffer_has_end_marker_anchored_in_last_25pct_fires() -> None:
    """Ticket #73: marker in last 25% of a 4000-char buffer → fires.

    Validates the ``max(500, len(text)//4)`` window: on a 4000-char
    buffer the 25% window (1000 chars) wins over the 500-char floor.
    Marker at position 3100 falls in the last 1000 chars → fires.
    """
    body = "x" * 3100 + " Would you like to know more?" + "y" * 100
    # text_len ≈ 3229. Window = max(500, 3229//4=807) = 807.
    # threshold = 3229-807 ≈ 2422. Marker at 3100 ≥ 2422 → fires.
    assert voice_train.buffer_has_end_marker(body) is True


def test_buffer_has_end_marker_marker_at_buffer_start_rejected() -> None:
    """Ticket #73: marker within first 200 chars → no fire (regression).

    The 200-char floor is unchanged by Ticket #73; the end-anchor gate
    is additive. This test pins both gates fire correctly when the
    marker is suspicious at BOTH ends — early in the buffer.
    """
    # Marker at position ~50 (inside 200-char floor). Long tail after.
    body = "head text Would you like to know more? tail " + "y" * 5000
    assert voice_train.buffer_has_end_marker(body) is False


@pytest.mark.asyncio
async def test_voice_train_end_marker_shortens_flush_delay(
    tmp_path: Path,
) -> None:
    """End-marker detection clamps the flush delay to rapid_arrival_seconds.

    Ticket #70 (2026-05-07). When the buffer carries a recognized
    end-marker, the scheduler shortens the effective sleep to
    ``rapid_arrival_seconds`` (default 3s). The shorter delay IS the
    silence guard — a chunk arriving inside the window resets the
    timer. To make the test fast, we set ``rapid_arrival_seconds=0.1``.
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            debounce_seconds=10,
            max_buffer_seconds=60,
            min_paste_chars=10,
            rapid_arrival_seconds=0.1,  # tighten so test runs fast
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 4242

    pending = voice_train.PendingPaste(
        chat_id=chat_id, kind="voice", cluster=None,
    )
    body_with_marker = (
        "Essay content goes here. " * 40
        + "\nSubscribed\nfooter"
    )
    voice_train.append_paste_chunk(pending, body_with_marker)
    bot_data["voice_train_pending"][chat_id] = pending

    # Schedule with the full 10s debounce; end-marker should clamp
    # to ``rapid_arrival_seconds`` (0.1s in this test).
    bot._schedule_voice_train_flush(ctx, chat_id, delay_seconds=10)

    # Wait up to ~1.5s for the flush to land. With end-marker
    # detection working, the flush task ran with ~0.1s sleep.
    deadline = 1.5
    waited = 0.0
    step = 0.05
    while waited < deadline:
        if chat_id not in bot_data["voice_train_pending"]:
            break
        await _asyncio.sleep(step)
        waited += step

    # Buffer popped (flushed) within the early-flush window.
    assert chat_id not in bot_data["voice_train_pending"], (
        f"end-marker fast-path didn't fire within {deadline}s"
    )


@pytest.mark.asyncio
async def test_voice_train_no_end_marker_uses_full_debounce(
    tmp_path: Path,
) -> None:
    """REGRESSION: without an end marker, debounce window is preserved.

    Ticket #70 (2026-05-07). The end-marker fast-path is opt-in by
    content — a buffer without any recognized marker MUST NOT flush
    early. Otherwise we'd cut single-message /train pastes off mid-
    chunk-window. We schedule with debounce=10s and assert the
    buffer is still open ~0.5s later.
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            debounce_seconds=10,
            max_buffer_seconds=60,
            min_paste_chars=10,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 5252

    pending = voice_train.PendingPaste(
        chat_id=chat_id, kind="voice", cluster=None,
    )
    voice_train.append_paste_chunk(
        pending, "Plain essay body without markers. " * 30,
    )
    bot_data["voice_train_pending"][chat_id] = pending

    bot._schedule_voice_train_flush(ctx, chat_id, delay_seconds=10)

    # Wait briefly. With no end-marker, the buffer must NOT flush
    # within the test window.
    await _asyncio.sleep(0.3)
    assert chat_id in bot_data["voice_train_pending"], (
        "buffer flushed without an end marker (false-positive on debounce)"
    )

    # Cancel the pending flush task to keep the test clean.
    pending = bot_data["voice_train_pending"][chat_id]
    if pending.flush_task is not None:
        pending.flush_task.cancel()


@pytest.mark.asyncio
async def test_voice_train_max_ceiling_overrides_end_marker_window(
    tmp_path: Path,
) -> None:
    """REGRESSION: ceiling enforcement still wins over end-marker logic.

    Ticket #70 (2026-05-07). The end-marker fast-path uses
    ``min(1.0, effective_sleep)``; if ``effective_sleep`` was already
    clamped to 0 by the ceiling, the flush still goes immediate. Pin:
    a buffer past the ceiling with end-markers present flushes
    immediately (as if no markers).
    """
    import asyncio as _asyncio
    from datetime import datetime as _dt, timedelta, timezone as _tz
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            debounce_seconds=10,
            max_buffer_seconds=30,
            min_paste_chars=10,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            pass

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 6262

    pending = voice_train.PendingPaste(
        chat_id=chat_id, kind="voice", cluster=None,
    )
    pending.opened_at = _dt.now(_tz.utc) - timedelta(seconds=999)
    body = "Essay. " * 50 + "\nSubscribed\n"
    voice_train.append_paste_chunk(pending, body)
    bot_data["voice_train_pending"][chat_id] = pending

    bot._schedule_voice_train_flush(ctx, chat_id, delay_seconds=10)

    await _asyncio.sleep(0.05)
    # Buffer flushed via the ceiling path (immediate task creation).
    assert chat_id not in bot_data["voice_train_pending"]


# ---------------------------------------------------------------------------
# Ticket #69 — /train without --cluster: bot follow-up question
# ---------------------------------------------------------------------------


def test_looks_like_cluster_reply_single_word() -> None:
    """Plain single token is a valid cluster name."""
    assert bot._looks_like_cluster_reply("personal") is True
    assert bot._looks_like_cluster_reply("veteran") is True
    assert bot._looks_like_cluster_reply("general") is True


def test_looks_like_cluster_reply_with_hyphens() -> None:
    """Hyphen-separated tokens are valid cluster names."""
    assert bot._looks_like_cluster_reply("tech-essays") is True
    assert bot._looks_like_cluster_reply("voice_corpus") is True


def test_looks_like_cluster_reply_rejects_sentences() -> None:
    """Multi-word phrases are NOT cluster replies."""
    assert bot._looks_like_cluster_reply("hello there friend") is False
    assert bot._looks_like_cluster_reply("two words") is False


def test_looks_like_cluster_reply_rejects_command_prefix() -> None:
    """A leading ``/`` means it's a slash command, not a cluster name."""
    assert bot._looks_like_cluster_reply("/end") is False
    assert bot._looks_like_cluster_reply("/train") is False


def test_looks_like_cluster_reply_rejects_too_long() -> None:
    """31-char string exceeds the cluster-name length cap."""
    assert bot._looks_like_cluster_reply("a" * 31) is False
    # Boundary: 30 chars OK.
    assert bot._looks_like_cluster_reply("a" * 30) is True


def test_looks_like_cluster_reply_rejects_special_chars() -> None:
    """Slashes / dots / inline whitespace reject.

    Leading/trailing whitespace is stripped before the shape check
    (operator-friendly — accidental trailing space is forgiven), but
    inline whitespace remains rejected because it implies a multi-
    word phrase.
    """
    assert bot._looks_like_cluster_reply("not/cluster") is False
    assert bot._looks_like_cluster_reply("hello.com") is False
    # Inline whitespace (mid-word) is rejected.
    assert bot._looks_like_cluster_reply("two words") is False


def test_looks_like_cluster_reply_empty() -> None:
    """Empty / whitespace strings reject."""
    assert bot._looks_like_cluster_reply("") is False
    assert bot._looks_like_cluster_reply("   ") is False


@pytest.mark.asyncio
async def test_train_without_cluster_arms_pending_ask(
    tmp_path: Path,
) -> None:
    """``/train`` (no --cluster) queues a pending-cluster ask.

    Ticket #69 (2026-05-07). After the operator pastes /train without
    --cluster, the success reply MUST include the "Which cluster?"
    question AND the bot_data registry MUST carry a pending-cluster
    entry keyed by chat_id.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 1111

    # Run finalize directly (the same path the buffer flush uses).
    text = "Some essay content for training. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster=None,
    )

    # Pending-cluster entry was set.
    pending_asks = bot_data["voice_train_pending_cluster"]
    assert chat_id in pending_asks
    entry = pending_asks[chat_id]
    assert entry["raw_rel_path"].startswith("document/essay/")

    # Reply contains the cluster question.
    assert sent, "no reply was sent"
    last_reply = sent[-1][1]
    assert "Which cluster?" in last_reply
    assert "general" in last_reply  # opt-out sentinel mentioned


@pytest.mark.asyncio
async def test_train_with_cluster_does_not_arm_pending_ask(
    tmp_path: Path,
) -> None:
    """``/train --cluster X`` does NOT trigger the pending-cluster ask.

    Ticket #69 (2026-05-07). The question only fires when the operator
    didn't supply a cluster. With one supplied, the saved record is
    already tagged and no question is needed.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 2222

    text = "Some essay content. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster="veteran",
    )

    # No pending-cluster entry.
    assert chat_id not in bot_data["voice_train_pending_cluster"]
    # Reply does NOT carry the cluster question.
    last_reply = sent[-1][1]
    assert "Which cluster?" not in last_reply


@pytest.mark.asyncio
async def test_cluster_reply_applies_via_vault_edit(
    tmp_path: Path,
) -> None:
    """End-to-end: cluster reply tags the just-saved fixture.

    Ticket #69 (2026-05-07). After /train arms the pending-cluster
    ask, the operator's reply (a single token like "personal") triggers
    ``vault_edit`` on the saved record's frontmatter ``cluster:`` field.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 3333

    # Step 1: /train without --cluster saves + arms the ask.
    text = "An essay paragraph that will be saved. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster=None,
    )
    # Capture the saved path before consumption.
    raw_rel_path = bot_data["voice_train_pending_cluster"][chat_id][
        "raw_rel_path"
    ]
    saved_file = Path(config.vault.path) / raw_rel_path
    assert saved_file.exists()
    # Pre-check: cluster field is null/None.
    pre = frontmatter.loads(saved_file.read_text(encoding="utf-8"))
    assert pre.metadata.get("cluster") in (None, "")

    # Step 2: simulate the operator's "personal" reply.
    entry = bot._consume_pending_cluster_ask(ctx, chat_id)
    assert entry is not None
    await bot._handle_cluster_ask_reply(ctx, chat_id, "personal", entry)

    # The vault_edit applied: cluster field now == "personal".
    post = frontmatter.loads(saved_file.read_text(encoding="utf-8"))
    assert post.metadata.get("cluster") == "personal"
    # Confirmation reply was sent.
    confirms = [s for s in sent if "tagged" in s[1].lower()]
    assert confirms, "no confirmation reply for cluster tag"


@pytest.mark.asyncio
async def test_cluster_reply_general_opts_out(
    tmp_path: Path,
) -> None:
    """``general`` reply is the opt-out sentinel — leaves cluster unset.

    Ticket #69 (2026-05-07). The brief specifies ``general`` as the
    explicit "skip clustering" sentinel; treating it like a normal
    cluster name would tag every uncategorized essay with cluster
    ``general``, which loses the operator's intent.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 4444

    text = "Essay content for opt-out test. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster=None,
    )
    raw_rel_path = bot_data["voice_train_pending_cluster"][chat_id][
        "raw_rel_path"
    ]
    saved_file = Path(config.vault.path) / raw_rel_path

    entry = bot._consume_pending_cluster_ask(ctx, chat_id)
    await bot._handle_cluster_ask_reply(ctx, chat_id, "general", entry)

    # cluster field stays unset — opt-out.
    post = frontmatter.loads(saved_file.read_text(encoding="utf-8"))
    assert post.metadata.get("cluster") in (None, "", "null")


@pytest.mark.asyncio
async def test_cluster_reply_state_expires_after_timeout(
    tmp_path: Path,
) -> None:
    """REGRESSION: cluster-ask state drops after the 5min timeout.

    Ticket #69 (2026-05-07). If the operator doesn't reply within 5
    minutes, the pending-cluster entry is dropped silently. A later
    cluster-shaped message in the same chat does NOT get applied.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            pass

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 5555

    # Manually arm the pending-cluster ask with an expired timestamp
    # (simulates 5+ minutes having passed).
    bot_data["voice_train_pending_cluster"][chat_id] = {
        "raw_rel_path": "document/essay/expired.md",
        "instance": "Hypatia",
        "expires_at": 0.0,  # epoch — long past
    }

    # Consume — must return None (entry expired) and drop the slot.
    entry = bot._consume_pending_cluster_ask(ctx, chat_id)
    assert entry is None
    assert chat_id not in bot_data["voice_train_pending_cluster"]


@pytest.mark.asyncio
async def test_method_source_does_not_arm_cluster_ask(
    tmp_path: Path,
) -> None:
    """REGRESSION: /method-source completion does NOT arm a cluster ask.

    Ticket #69 (2026-05-07). The brief notes that source/ records have
    no ``cluster:`` field — the worker doesn't read or write one. The
    cluster-question UX is /train-only by design.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 6666

    text = "Method source content for the worker. " * 20
    await bot._finalize_method_source_paste(
        ctx, chat_id=chat_id, text=text, image_metadata=[],
    )

    # No pending-cluster entry was armed by the method path.
    assert chat_id not in bot_data["voice_train_pending_cluster"]
    # Reply does NOT carry the cluster question.
    last_reply = sent[-1][1]
    assert "Which cluster?" not in last_reply


# ---------------------------------------------------------------------------
# P1 fix from #69 review (2026-05-08) — cluster-tag race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cluster_reply_reenqueues_extraction_job(
    tmp_path: Path,
) -> None:
    """REGRESSION: cluster reply re-enqueues a fresh extraction job.

    P1 fix (2026-05-08) for the cluster-tag race in #69. The original
    /train job was enqueued with ``cluster=None`` so the worker's
    voice-profile write would land WITHOUT a cluster field; tagging
    only the raw essay leaves the structured voice profile uncluster-
    aware, and ``_list_voice_leaves_with_cluster`` (which reads voice
    profile frontmatter, NOT raw) excludes the leaf from the cluster-
    tier rebuild.

    Fix: ``_handle_cluster_ask_reply`` re-enqueues a fresh job with
    ``cluster=<name>`` after the raw-essay vault_edit succeeds. The
    worker re-runs extraction via the body_replace path, writing the
    voice profile WITH cluster.

    This test pins the queue contents — after the operator's reply,
    the JSONL queue must carry a NEW job with the cluster value baked
    in, addressed at the same raw_rel_path / raw_name as the original.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 7777

    # Step 1: /train without --cluster saves + arms the ask + enqueues
    # the FIRST extraction job (cluster=None).
    text = "An essay for the cluster-race regression. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster=None,
    )
    queue_path = Path(config.voice_train.queue_path)
    first_jobs = voice_train.drain_queue(queue_path)
    assert len(first_jobs) == 1
    first_job = first_jobs[0]
    assert first_job.cluster is None
    assert first_job.kind == "voice"
    raw_rel_path_pinned = first_job.raw_rel_path
    raw_name_pinned = first_job.raw_name

    # Step 2: operator answers "personal".
    entry = bot._consume_pending_cluster_ask(ctx, chat_id)
    assert entry is not None
    # Sanity: the entry now carries raw_name + raw_body for re-enqueue.
    assert entry["raw_name"] == raw_name_pinned
    assert "An essay for the cluster-race" in entry["raw_body"]

    await bot._handle_cluster_ask_reply(ctx, chat_id, "personal", entry)

    # Step 3: a SECOND extraction job is now on the queue — same
    # raw_rel_path, same raw_name, but with cluster="personal".
    second_jobs = voice_train.drain_queue(queue_path)
    assert len(second_jobs) == 1, (
        "expected exactly one re-enqueued job after cluster reply"
    )
    second_job = second_jobs[0]
    assert second_job.cluster == "personal", (
        "re-enqueued job MUST carry the cluster value so the worker's "
        "voice-profile write lands with cluster baked in"
    )
    assert second_job.kind == "voice"
    assert second_job.raw_rel_path == raw_rel_path_pinned
    assert second_job.raw_name == raw_name_pinned
    # Distinct job_id (fresh job, not a duplicate of the original).
    assert second_job.job_id != first_job.job_id
    # Same raw_body (the saved essay text — frozen at /train time).
    assert second_job.raw_body == first_job.raw_body


@pytest.mark.asyncio
async def test_cluster_reply_general_does_not_reenqueue(
    tmp_path: Path,
) -> None:
    """REGRESSION: ``general`` opt-out does NOT re-enqueue extraction.

    P1 fix (2026-05-08). The opt-out short-circuits before either the
    raw-essay tag-write OR the re-enqueue runs. Pin: queue is empty
    after a /train + ``general`` reply (the original /train job was
    drained by the test helper before the reply ran, so the queue
    state we observe here is purely whatever the cluster-reply path
    contributed).
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 8484

    text = "Essay content for opt-out re-enqueue test. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster=None,
    )
    # Drain the original /train job so we can observe what the
    # cluster-reply path contributes.
    queue_path = Path(config.voice_train.queue_path)
    voice_train.drain_queue(queue_path)

    entry = bot._consume_pending_cluster_ask(ctx, chat_id)
    await bot._handle_cluster_ask_reply(ctx, chat_id, "general", entry)

    # Opt-out: NO new job on the queue.
    post_jobs = voice_train.drain_queue(queue_path)
    assert post_jobs == [], (
        "general opt-out must not enqueue a re-extraction job"
    )


@pytest.mark.asyncio
async def test_cluster_reply_reenqueue_failure_falls_back_gracefully(
    tmp_path: Path,
) -> None:
    """REGRESSION: re-enqueue failure does NOT roll back raw-essay tag.

    P1 fix (2026-05-08). If the re-enqueue throws (queue file
    permissions, disk full, etc.), the raw-essay vault_edit tag is
    already applied and stays applied. The reply text surfaces a
    warning so the operator can re-run /train --cluster <name>
    manually to retry.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }
    sent: list[tuple[int, str]] = []

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 9595

    text = "Essay for re-enqueue-failure test. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster=None,
    )
    raw_rel_path = bot_data["voice_train_pending_cluster"][chat_id][
        "raw_rel_path"
    ]
    saved_file = Path(config.vault.path) / raw_rel_path

    entry = bot._consume_pending_cluster_ask(ctx, chat_id)

    # Force enqueue_job to raise.
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated queue failure")

    with patch.object(voice_train, "enqueue_job", _boom):
        await bot._handle_cluster_ask_reply(
            ctx, chat_id, "personal", entry,
        )

    # Raw-essay tag IS applied — first vault_edit ran before the
    # re-enqueue.
    post = frontmatter.loads(saved_file.read_text(encoding="utf-8"))
    assert post.metadata.get("cluster") == "personal"

    # Reply text surfaces the re-enqueue failure.
    confirms = [s for s in sent if "tagged" in s[1].lower()]
    assert confirms, "no confirmation reply"
    last_reply = confirms[-1][1]
    assert "re-extraction" in last_reply.lower()


@pytest.mark.asyncio
async def test_cluster_ask_entry_carries_raw_body_and_name(
    tmp_path: Path,
) -> None:
    """REGRESSION: pending-cluster entry carries raw_name + raw_body.

    P1 fix (2026-05-08). The re-enqueue path needs the raw record's
    name + body to construct a fresh ExtractionJob. Both are stored
    on the entry at finalize time. Pin: after /train without
    --cluster, the entry has these fields populated.
    """
    from types import SimpleNamespace

    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(
            command_enabled=True,
            min_paste_chars=20,
            queue_path=str(tmp_path / "queue.jsonl"),
        ),
    )
    bot_data: dict[str, Any] = {
        "config": config,
        "voice_train_pending": {},
        "voice_train_pending_cluster": {},
    }

    class _FakeBot:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            pass

    ctx = SimpleNamespace(
        application=SimpleNamespace(bot_data=bot_data),
        bot=_FakeBot(),
    )
    chat_id = 6868

    text = "Essay text used for re-enqueue. " * 20
    await bot._finalize_train_paste(
        ctx, chat_id=chat_id, text=text, cluster=None,
    )

    entry = bot_data["voice_train_pending_cluster"][chat_id]
    assert "raw_name" in entry
    assert entry["raw_name"]  # non-empty
    assert "raw_body" in entry
    # The exact assembled body — must include the original text so
    # the worker's extraction operates on the same content.
    assert text.strip() in entry["raw_body"]


# ---------------------------------------------------------------------------
# Ticket #59 (2026-05-08) — backfill helper for un-extracted raw records
# ---------------------------------------------------------------------------


def _write_raw_essay_file(
    vault_path: Path,
    *,
    name: str,
    extraction_status: str = "pending",
    cluster: str | None = None,
    body: str = "Essay body text here.",
) -> Path:
    """Write a raw essay record fixture (frontmatter + body)."""
    essay_dir = vault_path / "document" / "essay"
    essay_dir.mkdir(parents=True, exist_ok=True)
    target = essay_dir / f"{name}.md"
    fm: dict[str, Any] = {
        "type": "essay",
        "name": name,
        "status": "draft",
        "extraction_status": extraction_status,
        "author": "Andrew Errant",
        "source_kind": "paste",
    }
    if cluster:
        fm["cluster"] = cluster
    post = frontmatter.Post(body, **fm)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_voice_companion(vault_path: Path, *, name: str) -> Path:
    """Write a structured voice companion file at ``voice/<name>.md``."""
    voice_dir = vault_path / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    target = voice_dir / f"{name}.md"
    post = frontmatter.Post(
        "Voice profile body.",
        **{"type": "voice", "name": name, "status": "active"},
    )
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_raw_source_file(
    vault_path: Path,
    *,
    name: str,
    extraction_status: str = "pending",
    body: str = "Source body text here.",
) -> Path:
    source_dir = vault_path / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / f"{name}.md"
    fm: dict[str, Any] = {
        "type": "source",
        "name": name,
        "status": "pending",
        "extraction_status": extraction_status,
        "source_kind": "paste",
    }
    post = frontmatter.Post(body, **fm)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_method_companion(vault_path: Path, *, name: str) -> Path:
    method_dir = vault_path / "method"
    method_dir.mkdir(parents=True, exist_ok=True)
    target = method_dir / f"{name}.md"
    post = frontmatter.Post(
        "Method profile body.",
        **{"type": "method", "name": name, "status": "active"},
    )
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def test_backfill_enqueues_pending_essay(vault_path: Path) -> None:
    """Single essay with extraction_status=pending → backfill enqueues 1 job."""
    _write_raw_essay_file(vault_path, name="My Essay")
    jobs, sk_v, sk_m = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    assert len(jobs) == 1
    assert sk_v == 0
    assert sk_m == 0
    assert jobs[0].kind == "voice"
    assert jobs[0].raw_rel_path == "document/essay/My Essay.md"
    assert jobs[0].raw_name == "My Essay"
    assert jobs[0].instance == "hypatia"
    # Body comes from the file content, not the frontmatter.
    assert "Essay body text" in jobs[0].raw_body


def test_backfill_skips_complete_with_companion(vault_path: Path) -> None:
    """Essay with status=complete AND companion exists → skipped."""
    _write_raw_essay_file(
        vault_path, name="Done Essay", extraction_status="complete",
    )
    _write_voice_companion(vault_path, name="Done Essay")
    jobs, sk_v, sk_m = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    assert jobs == []
    assert sk_v == 1
    assert sk_m == 0


def test_backfill_enqueues_when_companion_missing(vault_path: Path) -> None:
    """Essay with status=complete but companion MISSING → backfill enqueues.

    Operator may have manually deleted the structured record to force
    a rebuild; ``extraction_status`` alone isn't trustworthy if the
    companion file is gone.
    """
    _write_raw_essay_file(
        vault_path, name="Half Done", extraction_status="complete",
    )
    # No voice/Half Done.md created.
    jobs, sk_v, sk_m = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    assert len(jobs) == 1
    assert sk_v == 0
    assert jobs[0].raw_name == "Half Done"


def test_backfill_re_enqueues_failed(vault_path: Path) -> None:
    """Essay with extraction_status=failed → re-enqueued (retry path)."""
    _write_raw_essay_file(
        vault_path, name="Broken Essay", extraction_status="failed",
    )
    jobs, _, _ = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    assert len(jobs) == 1
    assert jobs[0].raw_name == "Broken Essay"


def test_backfill_preserves_cluster(vault_path: Path) -> None:
    """Essay with cluster: X → produced job carries cluster=X."""
    _write_raw_essay_file(
        vault_path, name="Veteran Essay", cluster="veteran",
    )
    jobs, _, _ = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    assert len(jobs) == 1
    assert jobs[0].cluster == "veteran"


def test_backfill_handles_source_method_symmetrically(
    vault_path: Path,
) -> None:
    """source/<slug>.md → method job, with same skip semantics."""
    _write_raw_source_file(vault_path, name="Pending Method")
    _write_raw_source_file(
        vault_path, name="Done Method", extraction_status="complete",
    )
    _write_method_companion(vault_path, name="Done Method")
    jobs, sk_v, sk_m = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    assert len(jobs) == 1
    assert jobs[0].kind == "method"
    assert jobs[0].raw_rel_path == "source/Pending Method.md"
    assert jobs[0].raw_name == "Pending Method"
    assert sk_v == 0
    assert sk_m == 1


def test_backfill_returns_empty_on_empty_vault(vault_path: Path) -> None:
    """No essays / no source dir → empty job list, no error.

    Per ``feedback_intentionally_left_blank.md``, the helper should
    silently return ``([], 0, 0)`` and let the caller decide whether
    to print a "0 jobs" line — never crash on a vault that's never
    received a /train.
    """
    jobs, sk_v, sk_m = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    assert jobs == []
    assert sk_v == 0
    assert sk_m == 0


def test_backfill_mixed_essay_and_source(vault_path: Path) -> None:
    """Mixed pending essay + pending source → produces both job kinds."""
    _write_raw_essay_file(vault_path, name="Essay One")
    _write_raw_source_file(vault_path, name="Source One")
    jobs, _, _ = voice_train.collect_backfill_jobs(
        vault_path=vault_path, instance="hypatia",
    )
    kinds = sorted(j.kind for j in jobs)
    assert kinds == ["method", "voice"]


def test_backfill_dry_run_does_not_write_to_queue(
    tmp_path: Path,
) -> None:
    """Dry-run path (collect → don't enqueue) leaves queue file untouched.

    The collector itself never writes to the queue — that's the CLI's
    responsibility — but pin the contract so a future refactor doesn't
    accidentally couple them.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_raw_essay_file(vault, name="X")
    queue_path = tmp_path / "data" / "extraction_queue.jsonl"
    # Don't create the queue file at all — collect should not try to
    # write to it.
    jobs, _, _ = voice_train.collect_backfill_jobs(
        vault_path=vault, instance="hypatia",
    )
    assert len(jobs) == 1
    assert not queue_path.exists()

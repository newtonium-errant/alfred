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

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
  * Config gate: voice_train block absent / disabled → worker not
    started. (The /train + /method_source slash-command registration
    tests died with the Telegram surface, 2026-08-19.)
  * Scope: hypatia scope admits writes to all four new types.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from alfred.telegram import voice_train
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
    assert voice_train.voice_train_scope_for(config) == "hypatia"


def test_voice_train_scope_for_kalle(tmp_path: Path) -> None:
    config = _make_hypatia_config(tmp_path)
    config.instance.tool_set = "kalle"
    assert voice_train.voice_train_scope_for(config) == "kalle"


def test_voice_train_scope_for_salem_falls_through_to_talker(
    tmp_path: Path,
) -> None:
    config = _make_hypatia_config(tmp_path)
    config.instance.tool_set = ""
    assert voice_train.voice_train_scope_for(config) == "talker"


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
    resolved = voice_train.resolve_queue_path(config)
    assert resolved == tmp_path / "custom_queue.jsonl"


def test_resolve_queue_path_per_instance_default(tmp_path: Path) -> None:
    """When queue_path is None, defaults to ./data/<slug>/extraction_queue.jsonl."""
    config = _make_hypatia_config(
        tmp_path,
        voice_train_config=VoiceTrainConfig(command_enabled=True),
    )
    resolved = voice_train.resolve_queue_path(config)
    assert resolved == Path("./data") / "hypatia" / "extraction_queue.jsonl"


# ---------------------------------------------------------------------------
# Threading pins for the T4 relocation
# ---------------------------------------------------------------------------
#
# `resolve_queue_path` / `voice_train_scope_for` moved here from bot.py so they
# would outlive the Telegram bot's deletion. The pins ABOVE prove the functions
# compute correctly; they say nothing about whether PRODUCTION still reaches
# them, and that is the gap worth pinning. Neither production caller is
# reachable from this suite — no test invokes `alfred.cli.cmd_voice`, and the
# daemon's extraction-worker startup needs a live daemon — so if either call
# site still pointed at the deleted `bot` helpers, every test here would stay
# GREEN while `alfred talker voice train backfill` raised ImportError in the
# operator's hands. That is the standing threading trap in its exact shape: the
# tests thread the new API, production keeps the old one, the suite says fine.
# Source inspection is the only altitude available without standing up a CLI or
# a daemon; the idiom follows tests/test_talker_transport_wiring.py, which pins
# the daemon's send-leg wiring the same way for the same reason.


def test_cli_backfill_threads_queue_path_through_voice_train(
    tmp_path: Path,
) -> None:
    """The backfill CLI must resolve its queue path via voice_train, not bot."""
    import inspect

    import alfred.cli

    source = inspect.getsource(alfred.cli.cmd_voice)
    assert (
        "from alfred.telegram.voice_train import resolve_queue_path" in source
    ), (
        "alfred.cli.cmd_voice must import resolve_queue_path from "
        "alfred.telegram.voice_train. It used to come from "
        "alfred.telegram.bot, which T4 deletes — leaving that import in "
        "place breaks `alfred talker voice train backfill` at runtime while "
        "this suite stays green."
    )
    assert "alfred.telegram.bot" not in source, (
        "alfred.cli.cmd_voice must not reach into the Telegram bot module "
        "at all; the bot surface is retired and the CLI backfill path is "
        "web-era code."
    )
    # POSITIVE CONTROL, in the same test: the symbol the call site names is
    # live and computes. Without this the two assertions above would pass
    # identically against a build where resolve_queue_path did not exist —
    # a check that cannot fail on a broken subject is not a check.
    config = _make_hypatia_config(tmp_path)
    assert voice_train.resolve_queue_path(config) == (
        Path("./data") / "hypatia" / "extraction_queue.jsonl"
    )


def test_daemon_worker_threads_queue_and_scope_through_voice_train() -> None:
    """The extraction worker must take queue path + scope from voice_train."""
    import inspect

    from alfred.telegram import daemon as talker_daemon

    source = inspect.getsource(talker_daemon.run)
    assert "_voice_train.resolve_queue_path(" in source, (
        "the daemon's extraction-worker startup must resolve its queue path "
        "via voice_train.resolve_queue_path."
    )
    assert "_voice_train.voice_train_scope_for(" in source, (
        "the daemon's extraction-worker startup must resolve its vault scope "
        "via voice_train.voice_train_scope_for."
    )
    # The bot alias this call site used to go through. Asserted by its OWN
    # name rather than by "bot" — T4 C3 (2026-08-19) deleted bot.py and
    # de-botted daemon.py, so a bare "bot" exclusion would over-match
    # historical-context comments while this stays precise.
    assert "_bot_mod" not in source, (
        "the daemon must not reach voice-train helpers through the bot "
        "module; that import existed only to borrow them and was removed "
        "when they moved to voice_train."
    )
    # POSITIVE CONTROL: both named symbols exist and are callable.
    assert callable(voice_train.resolve_queue_path)
    assert callable(voice_train.voice_train_scope_for)


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
# The fix was a per-chat-id paste buffer in bot.bot_data; that machinery
# and its tests died with the Telegram surface (2026-08-19). What remains
# below is the surface-independent part: the PendingPaste dataclass, the
# debounce/end-marker config, and ``voice_train.buffer_has_end_marker``.


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


    # Either save+enqueue ran OR the buffer flushed empty — but the
    # flush DID happen (no longer in the registry).


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


def test_buffer_has_end_marker_uses_rfind_for_dual_occurrence() -> None:
    """Ticket #78 (2026-05-07): marker mid-body AND at end → fires.

    Regression pin for the find()→rfind() switch. Buffer contains the
    sign-off marker BOTH at a mid-body position (rhetorical question,
    embedded quote, etc. — would be rejected by the end-anchor gate)
    AND at the actual end-of-essay position (the real closing chunk).

    With the old ``text.find(marker)``, the FIRST occurrence wins, the
    end-anchor gate rejects it, and the legitimate late occurrence is
    silently lost — flush waits for the full debounce when it should
    have fired early.

    With ``text.rfind(marker)``, the LAST occurrence wins, the gate
    accepts it, flush fires early — the correct end-of-essay behaviour.
    """
    marker = "Would you like to know more?"
    # Mid-body marker at ~position 300 (past 200-char floor but well
    # before any plausible anchor threshold on a 4000+ char buffer).
    # Then a long bridge of body content. Then the marker again at the
    # very end (the real closing chunk).
    mid_body = "x" * 300 + " " + marker + " "
    bridge = "y " * 1800  # ~3600 chars of bridge body
    body = mid_body + bridge + marker
    # text_len ≈ 3957. anchor_window = max(500, 3957//4=989) = 989.
    # threshold ≈ 3957 - 989 = 2968. find() returns ~301; rfind()
    # returns ~3929. Only rfind() clears the threshold.
    assert len(body) > 3000
    assert voice_train.buffer_has_end_marker(body) is True


# ---------------------------------------------------------------------------
# P1 fix from #69 review (2026-05-08) — cluster-tag race
# (the #69 cluster-ask bot-conversation tests died with the Telegram
# surface, 2026-08-19)
# ---------------------------------------------------------------------------


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

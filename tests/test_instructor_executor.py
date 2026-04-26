"""Tests for ``alfred.instructor.executor``.

Covers the commit 4 plan in full:

  1. Destructive keyword triggers dry-run → no writes occur.
  2. Non-destructive directive executes through tool-use loop.
  3. Retry counter increments on failure; third failure writes to
     ``alfred_instructions_error``.
  4. Audit comment appended; 6th directive prunes the 1st.
  5. Scope enforcement: ``vault_delete`` raises even though the
     instructor scope denies it (belt-and-braces test).
  6. Successful run moves the directive from ``alfred_instructions``
     to ``alfred_instructions_last`` with ``{text, executed_at,
     result}`` shape.

The SDK itself is faked via the ``FakeAnthropicClient`` pattern already
in use in ``tests/telegram/conftest.py``. The SKILL.md file isn't
shipped until commit 5, so tests create a minimal placeholder at
``tmp_path/skills/vault-instructor/SKILL.md`` and point the executor
there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any

import frontmatter
import pytest

import anthropic  # noqa: F401 — the executor imports it; keep the dep live


from alfred.instructor import executor as exec_mod
from alfred.instructor.config import (
    AnthropicConfig,
    InstanceConfig,
    InstructorConfig,
    LoggingConfig,
    StateConfig,
    VaultConfig,
)
from alfred.instructor.executor import (
    ExecutionResult,
    _dispatch_tool,
    execute,
    execute_and_record,
    is_destructive,
)
from alfred.instructor.state import InstructorState


# --- Fake SDK ---------------------------------------------------------------


@dataclass
class FakeBlock:
    """Minimal stand-in for an SDK content block."""

    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.type == "text":
            out["text"] = self.text
        elif self.type == "tool_use":
            out["id"] = self.id
            out["name"] = self.name
            out["input"] = dict(self.input)
        return out


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"


class FakeMessages:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            return FakeResponse(
                content=[FakeBlock(type="text", text='{"status": "done", "summary": "default"}')],
            )
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.messages = FakeMessages(responses or [])


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Minimal skills_dir with a placeholder vault-instructor SKILL.md.

    Commit 5 ships the real SKILL; for executor tests we just need the
    file to exist so ``_load_skill`` doesn't raise. Keep the content
    deterministic so no model-specific SKILL content leaks into tests.
    """
    skills = tmp_path / "skills"
    (skills / "vault-instructor").mkdir(parents=True)
    (skills / "vault-instructor" / "SKILL.md").write_text(
        "# placeholder vault-instructor skill (test fixture)\n",
        encoding="utf-8",
    )
    return skills


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    # Seed the top-level type dirs the tests write into.
    for sub in ("note", "task", "project"):
        (vault / sub).mkdir()
    return vault


@pytest.fixture
def config(tmp_path: Path, vault: Path) -> InstructorConfig:
    return InstructorConfig(
        vault=VaultConfig(path=str(vault)),
        anthropic=AnthropicConfig(
            api_key="DUMMY_ANTHROPIC_TEST_KEY",
            model="claude-sonnet-4-6",
            max_tokens=4096,
        ),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
        state=StateConfig(path=str(tmp_path / "state.json")),
        logging=LoggingConfig(file=str(tmp_path / "instructor.log")),
        poll_interval_seconds=60,
        max_retries=3,
        audit_window_size=5,
    )


@pytest.fixture
def state(tmp_path: Path) -> InstructorState:
    return InstructorState(tmp_path / "state.json")


def _write_record(vault: Path, rel_path: str, content: str) -> Path:
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


# --- is_destructive ---------------------------------------------------------


def test_is_destructive_catches_literal_keyword() -> None:
    kws = ("delete", "remove", "purge")
    assert is_destructive("please delete this record", kws)
    assert is_destructive("REMOVE the tag", kws)  # case-insensitive
    assert is_destructive("purge old entries", kws)


def test_is_destructive_ignores_non_matching() -> None:
    kws = ("delete", "remove", "purge")
    assert not is_destructive("add a backlink to project/X", kws)
    assert not is_destructive("rename this to foo", kws)


# --- Tool dispatch ---------------------------------------------------------


def test_dispatch_vault_read_returns_json(vault: Path) -> None:
    _write_record(
        vault,
        "note/Hello.md",
        dedent(
            """\
            ---
            type: note
            name: Hello
            created: '2026-04-20'
            ---

            Body.
            """
        ),
    )
    mutated: list[str] = []
    out = _dispatch_tool(
        "vault_read",
        {"path": "note/Hello.md"},
        vault,
        dry_run=False,
        session_path=None,
        mutated_paths=mutated,
    )
    # JSON-encoded result with expected keys.
    import json
    payload = json.loads(out)
    assert payload["path"] == "note/Hello.md"
    assert payload["frontmatter"]["type"] == "note"


def test_dispatch_dry_run_blocks_writes_but_allows_reads(vault: Path) -> None:
    """Dry-run returns ``would do X`` for writes, normal results for reads."""
    _write_record(
        vault,
        "note/Existing.md",
        dedent(
            """\
            ---
            type: note
            name: Existing
            created: '2026-04-20'
            ---
            """
        ),
    )
    mutated: list[str] = []
    # vault_edit under dry_run must NOT mutate.
    out = _dispatch_tool(
        "vault_edit",
        {"path": "note/Existing.md", "set_fields": {"tags": ["x"]}},
        vault,
        dry_run=True,
        session_path=None,
        mutated_paths=mutated,
    )
    import json
    payload = json.loads(out)
    assert payload.get("dry_run") is True
    assert payload["would"]["op"] == "edit"
    assert mutated == []
    # Original file is unchanged (no tags field).
    post = frontmatter.load(str(vault / "note/Existing.md"))
    assert "tags" not in post.metadata


def test_dispatch_scope_denies_delete(vault: Path) -> None:
    """vault_delete isn't in the instructor tool surface.

    Even if the model tried to call it by name, the dispatcher returns
    an 'Unknown tool' error — there's no ``vault_delete`` entry in
    ``_TOOL_TO_OP``. That plus the scope check on ``delete`` (denied in
    SCOPE_RULES['instructor']) is belt-and-braces coverage.
    """
    mutated: list[str] = []
    out = _dispatch_tool(
        "vault_delete",
        {"path": "note/Anything.md"},
        vault,
        dry_run=False,
        session_path=None,
        mutated_paths=mutated,
    )
    import json
    payload = json.loads(out)
    assert "error" in payload
    assert "Unknown tool" in payload["error"]


def test_dispatch_create_records_mutation(vault: Path) -> None:
    mutated: list[str] = []
    _dispatch_tool(
        "vault_create",
        {
            "type": "note",
            "name": "FromInstructor",
            "set_fields": {"tags": ["instructor"]},
            "body": "Hello.",
        },
        vault,
        dry_run=False,
        session_path=None,
        mutated_paths=mutated,
    )
    assert mutated == ["note/FromInstructor.md"]
    assert (vault / "note/FromInstructor.md").exists()


# --- End-to-end execute() --------------------------------------------------


async def test_execute_non_destructive_runs_tool_loop(
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    vault: Path,
) -> None:
    """One tool_use turn, one tool_result, one end_turn = successful run."""
    _write_record(
        vault,
        "note/Target.md",
        dedent(
            """\
            ---
            type: note
            name: Target
            created: '2026-04-20'
            ---

            Body.
            """
        ),
    )
    # Scripted responses: first a tool_use, then an end_turn with
    # the SKILL-mandated JSON summary.
    responses = [
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    name="vault_edit",
                    id="tu-1",
                    input={
                        "path": "note/Target.md",
                        "set_fields": {"tags": ["done"]},
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[
                FakeBlock(
                    type="text",
                    text='{"status": "done", "summary": "tagged as done"}',
                )
            ],
            stop_reason="end_turn",
        ),
    ]
    client = FakeClient(responses=responses)

    result = await execute(
        client=client,
        directive="tag this as done",
        record_path="note/Target.md",
        config=config,
        state=state,
        skills_dir=skills_dir,
    )
    assert result.status == "done"
    assert result.summary == "tagged as done"
    assert "note/Target.md" in result.mutated_paths
    assert result.tool_iterations == 2  # one tool_use + one end_turn
    assert result.dry_run is False

    # Tags landed on the record.
    post = frontmatter.load(str(vault / "note/Target.md"))
    assert post.metadata.get("tags") == ["done"]


async def test_execute_destructive_keyword_triggers_dry_run(
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    vault: Path,
) -> None:
    """A directive containing 'delete' runs in dry-run mode; no writes occur."""
    _write_record(
        vault,
        "note/Survivor.md",
        dedent(
            """\
            ---
            type: note
            name: Survivor
            created: '2026-04-20'
            tags: [keep]
            ---

            Body.
            """
        ),
    )
    # Even if the model issues an edit, dry-run short-circuits it to a
    # "would do" result, so the file must remain unchanged.
    responses = [
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    name="vault_edit",
                    id="tu-1",
                    input={
                        "path": "note/Survivor.md",
                        "set_fields": {"tags": []},
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[
                FakeBlock(
                    type="text",
                    text='{"status": "done", "summary": "dry plan"}',
                )
            ],
            stop_reason="end_turn",
        ),
    ]
    client = FakeClient(responses=responses)

    result = await execute(
        client=client,
        directive="delete the tags on this record",
        record_path="note/Survivor.md",
        config=config,
        state=state,
        skills_dir=skills_dir,
    )
    assert result.dry_run is True
    assert result.status == "dry_run"
    # File content preserved — tags still ['keep'].
    post = frontmatter.load(str(vault / "note/Survivor.md"))
    assert post.metadata["tags"] == ["keep"]


async def test_execute_and_record_archives_and_audits(
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    vault: Path,
) -> None:
    """Successful execution moves directive to alfred_instructions_last.

    Checks the full shape the plan calls for:
    - directive removed from alfred_instructions
    - archive entry prepended with {text, executed_at, result}
    - audit comment appended to body
    """
    _write_record(
        vault,
        "note/Arc.md",
        dedent(
            """\
            ---
            type: note
            name: Arc
            created: '2026-04-20'
            alfred_instructions:
              - "tag this as done"
            ---

            Original body.
            """
        ),
    )
    # One tool_use edit then end_turn with the summary JSON.
    responses = [
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    name="vault_edit",
                    id="tu-1",
                    input={
                        "path": "note/Arc.md",
                        "set_fields": {"tags": ["done"]},
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[
                FakeBlock(
                    type="text",
                    text='{"status": "done", "summary": "edited title"}',
                )
            ],
            stop_reason="end_turn",
        ),
    ]
    client = FakeClient(responses=responses)

    result = await execute_and_record(
        client=client,
        directive="tag this as done",
        record_path="note/Arc.md",
        config=config,
        state=state,
        skills_dir=skills_dir,
    )
    assert result.status == "done"

    post = frontmatter.load(str(vault / "note/Arc.md"))
    # Queue cleared.
    assert post.metadata.get("alfred_instructions") == []
    # Archive populated.
    archive = post.metadata.get("alfred_instructions_last")
    assert isinstance(archive, list) and len(archive) == 1
    entry = archive[0]
    assert entry["text"] == "tag this as done"
    assert "executed_at" in entry
    assert "done" in entry["result"]
    # Audit comment in body.
    assert "ALFRED:INSTRUCTION" in post.content
    assert "tag this as done" in post.content or "tag this as done" in str(entry)


async def test_audit_window_prunes_old_blocks(
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    vault: Path,
) -> None:
    """After 6 directives with audit_window_size=5, only the last 5 remain."""
    _write_record(
        vault,
        "note/Aging.md",
        dedent(
            """\
            ---
            type: note
            name: Aging
            created: '2026-04-20'
            alfred_instructions: []
            ---

            Body start.
            """
        ),
    )
    # Run 6 executions. Each one issues one vault_edit + one end_turn
    # summary. We rebuild the fake client responses for each iteration.
    for i in range(6):
        responses = [
            FakeResponse(
                content=[
                    FakeBlock(
                        type="tool_use",
                        name="vault_edit",
                        id=f"tu-{i}",
                        input={
                            "path": "note/Aging.md",
                            "set_fields": {"tags": [f"t{i}"]},
                        },
                    )
                ],
                stop_reason="tool_use",
            ),
            FakeResponse(
                content=[
                    FakeBlock(
                        type="text",
                        text=f'{{"status": "done", "summary": "run-{i}"}}',
                    )
                ],
                stop_reason="end_turn",
            ),
        ]
        client = FakeClient(responses=responses)
        await execute_and_record(
            client=client,
            directive=f"directive #{i}",
            record_path="note/Aging.md",
            config=config,
            state=state,
            skills_dir=skills_dir,
        )

    post = frontmatter.load(str(vault / "note/Aging.md"))
    body = post.content
    # Count audit blocks — must be exactly audit_window_size (5).
    block_count = body.count("<!-- ALFRED:INSTRUCTION")
    assert block_count == 5, body
    # The 1st directive's marker (#0) must be gone; the 5 most recent
    # (1..5) should remain.
    assert "directive #0" not in body
    assert "directive #5" in body


async def test_execute_and_record_retry_then_error_surfaces(
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    vault: Path,
) -> None:
    """After max_retries failures, the directive moves to
    alfred_instructions_error and is cleared from the queue."""
    _write_record(
        vault,
        "note/Flaky.md",
        dedent(
            """\
            ---
            type: note
            name: Flaky
            created: '2026-04-20'
            alfred_instructions:
              - "do it"
            ---

            Body.
            """
        ),
    )

    class FailingClient:
        def __init__(self) -> None:
            self.messages = self

        async def create(self, **kwargs: Any) -> FakeResponse:
            # anthropic.APIError requires message, request, body —
            # build a minimal httpx.Request stand-in.
            import httpx
            req = httpx.Request("POST", "https://example.invalid/messages")
            raise anthropic.APIError("simulated", req, body=None)

    # max_retries defaults to 3. Run 3 times; each call should raise
    # an SDK-level APIError which the executor returns as status=error.
    client = FailingClient()
    for i in range(3):
        result = await execute_and_record(
            client=client,
            directive="do it",
            record_path="note/Flaky.md",
            config=config,
            state=state,
            skills_dir=skills_dir,
        )
        assert result.status == "error"

    post = frontmatter.load(str(vault / "note/Flaky.md"))
    # Directive dropped from the queue.
    assert post.metadata.get("alfred_instructions") == []
    # Error surfaced.
    assert "alfred_instructions_error" in post.metadata
    err = post.metadata["alfred_instructions_error"]
    assert "Failed after max retries" in err
    assert "do it" in err


async def test_scope_denies_via_body_write_on_instructor(
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    vault: Path,
) -> None:
    """Instructor allows body writes (plan). This test asserts the
    allow-path works — a complement to the scope tests shipped in c1
    which covered the deny paths.

    Runs a directive whose tool call uses body_append. The file body
    must grow, the scope check must not raise, and the mutation is
    recorded.
    """
    _write_record(
        vault,
        "note/Appendable.md",
        dedent(
            """\
            ---
            type: note
            name: Appendable
            created: '2026-04-20'
            ---

            Start.
            """
        ),
    )
    responses = [
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    name="vault_edit",
                    id="tu-1",
                    input={
                        "path": "note/Appendable.md",
                        "body_append": "New paragraph from instructor.",
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[
                FakeBlock(
                    type="text",
                    text='{"status": "done", "summary": "appended"}',
                )
            ],
            stop_reason="end_turn",
        ),
    ]
    client = FakeClient(responses=responses)
    result = await execute(
        client=client,
        directive="append a paragraph",
        record_path="note/Appendable.md",
        config=config,
        state=state,
        skills_dir=skills_dir,
    )
    assert result.status == "done"
    post = frontmatter.load(str(vault / "note/Appendable.md"))
    assert "New paragraph from instructor." in post.content

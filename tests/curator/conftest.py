"""Shared fixtures for the curator pipeline test package.

Philosophy: build simple, explicit Python fakes rather than ``MagicMock``
stacks. Tests should read like prose — set up an inbox file, prime the
fake backend with what the LLM would emit, run the pipeline, assert on
the resulting vault state and the ``PipelineResult`` dataclass.

Fixtures:

- :func:`fake_agent_backend`  — a programmable stand-in for the curator's
  ``_call_llm`` coroutine. Each invocation pops the next queued response
  and performs the corresponding side-effects (writing the manifest file,
  appending to the mutation log, creating the note on disk via vault ops).
- :func:`seeded_inbox` — a factory that drops N markdown files into a
  throwaway inbox dir and returns their paths.
- :func:`curator_vault` — a minimal vault layout with the scaffold dirs
  and type templates the pipeline touches.
- :func:`curator_config` — a :class:`CuratorConfig` pointed at the above.
- :func:`pipeline_runner` — bundles vault + config + fake backend + a
  mutation-log session file into a single callable that invokes
  ``run_pipeline`` on a given inbox file. One call, zero ceremony.

Nothing in this module touches the real vault or the real OpenClaw / Claude
subprocesses. The pipeline's ``_call_llm`` is monkeypatched for every test
that uses ``pipeline_runner``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable

import pytest

from alfred.curator.config import (
    AgentConfig,
    CuratorConfig,
    OpenClawBackendConfig,
    StateConfig,
    VaultConfig,
    WatcherConfig,
)
from alfred.vault.mutation_log import create_session_file

from ._fakes import FakeAgentBackend


# ---------------------------------------------------------------------------
# Vault + config fixtures
# ---------------------------------------------------------------------------


# Subset of scaffold dirs the pipeline routinely touches. Keep it narrow —
# every added directory is another potential source of test flakiness.
_CURATOR_VAULT_DIRS = (
    "person", "org", "project", "task", "note", "location",
    "event", "account", "conversation", "decision", "inbox",
    "_templates",
)


@pytest.fixture
def curator_vault(tmp_path: Path) -> Path:
    """Return a minimal vault laid out for the curator pipeline.

    Includes a stub ``project`` record so vault-context builder has
    something to surface, plus ``_templates/`` entries for person/note
    so ``vault_create`` has templates to render from.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in _CURATOR_VAULT_DIRS:
        (vault / sub).mkdir()
    (vault / "inbox" / "processed").mkdir(parents=True, exist_ok=True)

    # One seed project — lets VaultContext's "project (1)" block land in
    # the compact prompt text and gives the sender-context builder a
    # wikilink target.
    (vault / "project" / "Alfred.md").write_text(
        dedent(
            """\
            ---
            type: project
            name: Alfred
            status: active
            created: 2026-04-01
            tags: []
            related: []
            ---

            # Alfred

            Seed project for curator pipeline tests.
            """
        ),
        encoding="utf-8",
    )

    # Minimal templates — vault_create honours these when they exist.
    for rec_type in ("person", "note"):
        (vault / "_templates" / f"{rec_type}.md").write_text(
            dedent(
                f"""\
                ---
                type: {rec_type}
                name: "{{{{title}}}}"
                created: "{{{{date}}}}"
                tags: []
                related: []
                ---

                # {{{{title}}}}
                """
            ),
            encoding="utf-8",
        )

    return vault


@pytest.fixture
def curator_config(curator_vault: Path) -> CuratorConfig:
    """Return a CuratorConfig wired to ``curator_vault`` with OpenClaw
    selected so ``run_pipeline`` is the active path.

    ``skip_entity_enrichment=False`` is the opt-in toggle for Stage-4 tests;
    individual tests flip it to ``True`` when they want to verify the gate.
    """
    return CuratorConfig(
        vault=VaultConfig(path=str(curator_vault)),
        agent=AgentConfig(backend="openclaw", openclaw=OpenClawBackendConfig(
            command="openclaw-not-actually-invoked",
            agent_id="vault-curator-test",
            timeout=5,
        )),
        watcher=WatcherConfig(poll_interval=1, max_concurrent=3),
        state=StateConfig(path=str(curator_vault.parent / "curator_state.json")),
        skip_entity_enrichment=False,
    )


# ---------------------------------------------------------------------------
# Inbox fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_inbox(curator_vault: Path) -> Callable[..., list[Path]]:
    """Factory that drops N markdown files into the vault's inbox.

    Returns a callable so tests can request whatever shape of inbox they
    need without paying for the default::

        files = seeded_inbox(count=3)
        files = seeded_inbox(contents=["email 1 body", "email 2 body"])
    """
    inbox = curator_vault / "inbox"

    def _seed(
        count: int | None = None,
        contents: list[str] | None = None,
        stems: list[str] | None = None,
    ) -> list[Path]:
        if contents is not None:
            items = list(contents)
        elif count is not None:
            items = [f"Seed inbox item {i}\n\nBody text {i}." for i in range(count)]
        else:
            items = ["Default inbox content.\n"]
        created: list[Path] = []
        for idx, body in enumerate(items):
            stem = stems[idx] if (stems and idx < len(stems)) else f"item-{uuid.uuid4().hex[:6]}"
            path = inbox / f"{stem}.md"
            path.write_text(body, encoding="utf-8")
            created.append(path)
        return created

    return _seed


# ---------------------------------------------------------------------------
# Pipeline runner — the one-call test entry point
# ---------------------------------------------------------------------------


@dataclass
class PipelineHarness:
    """Bundle of everything a pipeline test needs to drive one run."""

    config: CuratorConfig
    backend: FakeAgentBackend
    vault_path: Path
    session_paths: list[str] = field(default_factory=list)

    async def run(self, inbox_file: Path) -> Any:
        """Invoke ``run_pipeline`` on one inbox file, allocate a session
        file for mutation logging, stash its path for post-run assertions.
        """
        from alfred.curator.pipeline import run_pipeline

        session_path = create_session_file()
        self.session_paths.append(session_path)
        content = inbox_file.read_text(encoding="utf-8")
        # Empty context_text is fine for most tests — VaultContext's
        # rendering has its own dedicated test module. Tests that care
        # about the prompt can override this by driving _stage1_analyze
        # directly.
        return await run_pipeline(
            inbox_file=inbox_file,
            inbox_content=content,
            vault_context_text="",
            config=self.config,
            session_path=session_path,
        )


@pytest.fixture
def fake_agent_backend(curator_vault: Path) -> FakeAgentBackend:
    """A fresh fake backend per test."""
    return FakeAgentBackend(curator_vault)


@pytest.fixture
def pipeline_runner(
    curator_config: CuratorConfig,
    curator_vault: Path,
    fake_agent_backend: FakeAgentBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> PipelineHarness:
    """Wire ``_call_llm`` → fake backend and hand back a ready harness.

    Every test that uses this fixture gets its own monkeypatched pipeline —
    no state leaks between tests because the fake backend is per-test too.
    """
    from alfred.curator import pipeline as pipeline_module

    async def _fake_call_llm(
        prompt: str,
        config: CuratorConfig,
        session_path: str,
        stage_label: str,
    ) -> str:
        return await fake_agent_backend.call(prompt, config, session_path, stage_label)

    monkeypatch.setattr(pipeline_module, "_call_llm", _fake_call_llm)

    return PipelineHarness(
        config=curator_config,
        backend=fake_agent_backend,
        vault_path=curator_vault,
    )

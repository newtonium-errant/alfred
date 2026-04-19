"""Fakes for the curator pipeline test package.

Kept in its own module (rather than ``conftest.py``) so tests can
``from tests.curator._fakes import FakeLLMResponse, FakeAgentBackend``
without relying on pytest's conftest auto-injection for symbol imports.
The conftest builds fixtures out of these; tests use the classes directly
when they need to construct scripted responses.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alfred.curator.config import CuratorConfig
from alfred.vault.mutation_log import log_mutation
from alfred.vault.ops import vault_create


@dataclass
class FakeLLMResponse:
    """One scripted response from the fake LLM.

    Attributes:
        manifest_entities: List of entity dicts to write to the manifest
            file. Shape matches what Stage 1 consumes — ``{type, name,
            description, body?, fields?}``. If ``None``, no manifest file
            is written (simulates an LLM that failed to produce one).
        create_note_path: If set, a note record is created on-disk and
            logged to the mutation log so ``_find_created_note`` returns
            this path. Passed as a bare stem like ``"Team sync 2026-04-19"``
            which becomes ``note/Team sync 2026-04-19.md``.
        raw_stdout: Literal stdout returned by the fake call. Defaults to
            an empty string; set this to exercise the stdout-parse fallback.
        raise_exception: If set, the fake call raises this exception
            instead of returning (exercises mark-processed-on-failure).
        match_inbox_stem: When set, this response is only consumed by
            calls whose prompt contains ``{stem}.md``. Lets concurrency
            tests script one response per inbox file deterministically
            without relying on asyncio.gather execution order.
    """

    manifest_entities: list[dict] | None = None
    create_note_path: str | None = None
    raw_stdout: str = ""
    raise_exception: BaseException | None = None
    match_inbox_stem: str | None = None


class FakeAgentBackend:
    """Programmable stand-in for :func:`alfred.curator.pipeline._call_llm`.

    Usage::

        backend.queue_response("s1-analyze", FakeLLMResponse(
            create_note_path="Team sync",
            manifest_entities=[{"type": "person", "name": "Jane Doe"}],
        ))

    The pipeline makes one call per stage per attempt. Stage 1 can make up
    to 3 attempts when the manifest is empty, so the test should queue the
    exact number of responses it wants to see consumed.
    """

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path
        # Queue keyed by a *prefix* match against the ``stage_label``
        # passed to ``_call_llm``. Stage 4 labels include the entity
        # type and a sanitised name (e.g. ``s4-person-Jane-Doe``), so
        # matching on ``"s4"`` alone lets a test prime one response for
        # every enrichment call without caring about per-entity names.
        self._responses: dict[str, list[FakeLLMResponse]] = {}
        self.calls: list[dict[str, Any]] = []
        # Concurrency instrumentation — tests flip this to track peak
        # simultaneous in-flight calls.
        self.track_concurrency = False
        self._in_flight = 0
        self.peak_concurrent = 0
        # Optional asyncio sleep injected inside each fake call so tests
        # can force overlap under the semaphore.
        self.hold_seconds = 0.0

    def queue_response(self, stage_prefix: str, response: FakeLLMResponse) -> None:
        self._responses.setdefault(stage_prefix, []).append(response)

    def _pop_response(self, stage_label: str, prompt: str = "") -> FakeLLMResponse:
        # Longest-prefix match wins, so a stage label like
        # ``s4-person-Jane`` falls back to a ``"s4"`` queue if no more
        # specific one exists.
        best = ""
        for prefix in self._responses:
            if stage_label.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        if not best or not self._responses[best]:
            return FakeLLMResponse()

        queue = self._responses[best]
        # First, prefer a response whose ``match_inbox_stem`` appears in
        # the prompt (filename-routed queue). Falls back to the first
        # queue entry with no stem filter when nothing matches — this
        # keeps single-file tests working with no ``match_inbox_stem``
        # boilerplate.
        for i, resp in enumerate(queue):
            if resp.match_inbox_stem and resp.match_inbox_stem in prompt:
                return queue.pop(i)
        for i, resp in enumerate(queue):
            if resp.match_inbox_stem is None:
                return queue.pop(i)
        # Only stem-matched responses remain but none match → default.
        return FakeLLMResponse()

    async def call(
        self,
        prompt: str,
        config: CuratorConfig,
        session_path: str,
        stage_label: str,
    ) -> str:
        """The replacement coroutine for ``_call_llm``.

        Signature matches the real function exactly so it drops in via
        ``monkeypatch.setattr``.
        """
        self.calls.append(
            {"stage": stage_label, "prompt_len": len(prompt), "session": session_path}
        )

        if self.track_concurrency:
            self._in_flight += 1
            self.peak_concurrent = max(self.peak_concurrent, self._in_flight)

        try:
            if self.hold_seconds:
                await asyncio.sleep(self.hold_seconds)

            response = self._pop_response(stage_label, prompt=prompt)

            if response.raise_exception is not None:
                raise response.raise_exception

            # Extract the manifest path from the prompt. Stage 1's prompt
            # contains a ``/tmp/alfred-curator-<id>-manifest.json`` line;
            # we parse it out so we can write the right file even though
            # the real ``stage1_analyze.md`` is what's in the prompt.
            if response.manifest_entities is not None:
                manifest_path = _extract_manifest_path(prompt)
                if manifest_path:
                    Path(manifest_path).write_text(
                        json.dumps({"entities": response.manifest_entities}),
                        encoding="utf-8",
                    )

            # Create a note on-disk and log it to the mutation log so
            # Stage 1's ``_find_created_note`` succeeds.
            if response.create_note_path:
                note_name = response.create_note_path
                note_rel = f"note/{note_name}.md"
                try:
                    vault_create(
                        self.vault_path,
                        "note",
                        note_name,
                        body=f"# {note_name}\n\nNote body.\n",
                    )
                except Exception:
                    # Note already exists — still log so downstream
                    # code resolves to the same path.
                    pass
                log_mutation(session_path, "create", note_rel)

            return response.raw_stdout
        finally:
            if self.track_concurrency:
                self._in_flight -= 1


def _extract_manifest_path(prompt: str) -> str | None:
    """Pull the ``/tmp/alfred-curator-<id>-manifest.json`` out of a prompt.

    The real ``stage1_analyze.md`` template interpolates ``{manifest_path}``
    somewhere in its body; we scrape the formatted prompt so the fake can
    write to the exact file the pipeline then reads.
    """
    m = re.search(r"/tmp/alfred-curator-[a-f0-9]+-manifest\.json", prompt)
    return m.group(0) if m else None

"""Parallel processing + failure-path coverage for the curator.

Upstream 163b7f9 introduced a ``max_concurrent`` semaphore in the daemon
loop so inbox files are processed N-at-a-time rather than one-by-one.
Upstream 7745ea7 added the ``mark_processed`` fallback so a failing file
doesn't block the pipeline forever — it gets moved to ``inbox/processed/``
even when ``run_pipeline`` raises.

These tests exercise both contracts by wrapping ``run_pipeline`` in the
same semaphore + try/except/mark_processed shape that lives in
``daemon._process_startup``. That's the production pattern distilled to
a testable harness — we don't import ``daemon.run()`` because it's a
long-running async loop with watcher / signal handling that doesn't
belong in a unit-test harness.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from alfred.curator.pipeline import run_pipeline
from alfred.curator.writer import mark_processed

from ._fakes import FakeLLMResponse


async def _process_with_fallback(
    inbox_file: Path,
    semaphore: asyncio.Semaphore,
    config,
    session_path: str,
    processed_path: Path,
) -> dict:
    """Production-mirror wrapper: semaphore-bounded run_pipeline call
    with ``mark_processed`` fallback on any exception.

    Returns ``{"file": name, "status": "...", "error": "..."}`` so the
    test can assert on outcomes without depending on state-manager
    internals.
    """
    async with semaphore:
        try:
            content = inbox_file.read_text(encoding="utf-8")
            result = await run_pipeline(
                inbox_file=inbox_file,
                inbox_content=content,
                vault_context_text="",
                config=config,
                session_path=session_path,
            )
            # Happy path: move to processed after success
            if inbox_file.exists():
                mark_processed(inbox_file, processed_path)
            return {"file": inbox_file.name, "status": "ok", "result": result}
        except Exception as exc:
            # Upstream 7745ea7 + Batch B: mark the file processed even
            # on failure so the daemon doesn't retry the same broken file
            # indefinitely.
            if inbox_file.exists():
                try:
                    mark_processed(inbox_file, processed_path)
                except Exception:
                    pass
            return {"file": inbox_file.name, "status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Parallel processing — semaphore gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_pipelines_respect_max_concurrent(
    pipeline_runner, seeded_inbox
) -> None:
    """N > max_concurrent parallel calls never exceed the semaphore limit.

    The fake backend records its peak concurrent call count; gathering
    six inbox items with max_concurrent=2 must keep the peak at ≤2.

    The ``hold_seconds`` knob holds each fake call long enough that they
    overlap — without it, Python's default task scheduling might serialise
    the gather before the counter ever rises above 1 and give a false pass.
    """
    max_concurrent = 2
    inbox_files = seeded_inbox(count=6)
    pipeline_runner.config.watcher.max_concurrent = max_concurrent
    pipeline_runner.config.skip_entity_enrichment = True

    # Prime one response per inbox file (Stage 1 only). Each manifest
    # includes exactly one entity so the pipeline sees a non-empty
    # manifest on the first attempt and doesn't enter the retry loop.
    for i in range(6):
        pipeline_runner.backend.queue_response(
            "s1-analyze",
            FakeLLMResponse(
                create_note_path=f"Parallel note {i}",
                manifest_entities=[{"type": "person", "name": f"Parallel Pat {i}"}],
            ),
        )

    pipeline_runner.backend.track_concurrency = True
    pipeline_runner.backend.hold_seconds = 0.05

    sem = asyncio.Semaphore(max_concurrent)

    from alfred.vault.mutation_log import create_session_file

    async def _run(f: Path) -> None:
        async with sem:
            session_path = create_session_file()
            content = f.read_text(encoding="utf-8")
            await run_pipeline(
                inbox_file=f,
                inbox_content=content,
                vault_context_text="",
                config=pipeline_runner.config,
                session_path=session_path,
            )

    await asyncio.gather(*[_run(f) for f in inbox_files])

    # Every inbox file should have produced exactly one s1-analyze call.
    stage1_calls = [c for c in pipeline_runner.backend.calls if c["stage"] == "s1-analyze"]
    assert len(stage1_calls) == 6
    # Peak concurrent calls must never have exceeded the semaphore limit.
    assert pipeline_runner.backend.peak_concurrent <= max_concurrent
    # And we should have actually seen parallelism (not serialised to 1).
    assert pipeline_runner.backend.peak_concurrent >= 2


@pytest.mark.asyncio
async def test_parallel_pipelines_complete_even_when_one_fails(
    pipeline_runner, seeded_inbox, curator_vault
) -> None:
    """One file raising in Stage 1 must not cancel concurrent peers.

    The daemon wraps each file in its own task with ``return_exceptions=True``
    on the gather; this test replicates the wrapper pattern and asserts
    two successful files still land even when the middle one raises.
    """
    pipeline_runner.config.watcher.max_concurrent = 2
    pipeline_runner.config.skip_entity_enrichment = True
    inbox_files = seeded_inbox(
        contents=["ok 1", "boom", "ok 2"],
        stems=["ok-1", "boom", "ok-2"],
    )
    processed_path = curator_vault / "inbox" / "processed"

    # Route responses by inbox filename (stem) so asyncio.gather
    # scheduling can't mismatch them. The fake scans the prompt for
    # each file's stem and picks the right response regardless of
    # execution order.
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            match_inbox_stem="ok-1.md",
            create_note_path="Note 1",
            manifest_entities=[{"type": "person", "name": "One"}],
        ),
    )
    # The pipeline's Stage 1 loop will try 3 times on any failure; queue
    # three raises (all stem-matched) so all retries blow up (ensures
    # the wrapper sees the exception, not a quietly-empty success).
    for _ in range(3):
        pipeline_runner.backend.queue_response(
            "s1-analyze",
            FakeLLMResponse(
                match_inbox_stem="boom.md",
                raise_exception=RuntimeError("stage1 blew up"),
            ),
        )
    pipeline_runner.backend.queue_response(
        "s1-analyze",
        FakeLLMResponse(
            match_inbox_stem="ok-2.md",
            create_note_path="Note 2",
            manifest_entities=[{"type": "person", "name": "Two"}],
        ),
    )

    sem = asyncio.Semaphore(pipeline_runner.config.watcher.max_concurrent)
    from alfred.vault.mutation_log import create_session_file

    # We want each invocation to have its own session path; simplest
    # approach: wrap in a closure that allocates one per-file.
    outcomes: list[dict] = []

    async def _one(f: Path) -> None:
        session = create_session_file()
        outcome = await _process_with_fallback(
            f, sem, pipeline_runner.config, session, processed_path
        )
        outcomes.append(outcome)

    await asyncio.gather(*[_one(f) for f in inbox_files])

    statuses = {o["file"]: o["status"] for o in outcomes}
    # Both ok files completed, the boom file raised but was caught.
    assert statuses["ok-1.md"] == "ok"
    assert statuses["ok-2.md"] == "ok"
    assert statuses["boom.md"] == "error"


# ---------------------------------------------------------------------------
# mark_processed-on-failure (upstream 7745ea7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_moves_file_to_processed(
    pipeline_runner, seeded_inbox, curator_vault
) -> None:
    """A Stage 1 exception → file still ends up in inbox/processed/.

    Before upstream 7745ea7, a failing file stayed in the inbox and the
    next tick of the watcher would pick it up again. This test pins the
    "move on failure" contract so a stuck file can never become an
    infinite hot loop.
    """
    pipeline_runner.config.skip_entity_enrichment = True
    inbox_files = seeded_inbox(contents=["broken content"], stems=["broken"])
    processed_path = curator_vault / "inbox" / "processed"

    # All three retry attempts raise.
    for _ in range(3):
        pipeline_runner.backend.queue_response(
            "s1-analyze",
            FakeLLMResponse(raise_exception=RuntimeError("LLM unreachable")),
        )

    sem = asyncio.Semaphore(1)
    from alfred.vault.mutation_log import create_session_file

    outcome = await _process_with_fallback(
        inbox_files[0],
        sem,
        pipeline_runner.config,
        create_session_file(),
        processed_path,
    )

    assert outcome["status"] == "error"
    # The inbox is now empty, and the file lives under processed/.
    assert not inbox_files[0].exists()
    assert (processed_path / "broken.md").exists()


@pytest.mark.asyncio
async def test_failure_does_not_retry_same_file_on_next_pass(
    pipeline_runner, seeded_inbox, curator_vault
) -> None:
    """After the fallback move, the file isn't visible to a subsequent
    ``glob('*.md')`` on the inbox — no watcher re-pickup.

    This is the functional equivalent of "marked in state so it isn't
    re-tried forever" when the state is filesystem-based (inbox/processed
    split) rather than a JSON ledger. The guarantee we need: once the
    file has failed and been moved, ``inbox/*.md`` returns zero results
    for that stem.
    """
    pipeline_runner.config.skip_entity_enrichment = True
    inbox_files = seeded_inbox(contents=["fail me"], stems=["fail-me"])
    processed_path = curator_vault / "inbox" / "processed"
    inbox = curator_vault / "inbox"

    # Confirm the file is there before the pipeline runs.
    assert (inbox / "fail-me.md").exists()

    for _ in range(3):
        pipeline_runner.backend.queue_response(
            "s1-analyze",
            FakeLLMResponse(raise_exception=RuntimeError("kaboom")),
        )

    sem = asyncio.Semaphore(1)
    from alfred.vault.mutation_log import create_session_file

    await _process_with_fallback(
        inbox_files[0],
        sem,
        pipeline_runner.config,
        create_session_file(),
        processed_path,
    )

    # Second "scan" — glob for pending inbox files. The failed file must
    # NOT surface, because it lives under processed/ now.
    pending = list(inbox.glob("*.md"))
    assert all("fail-me" not in p.name for p in pending)

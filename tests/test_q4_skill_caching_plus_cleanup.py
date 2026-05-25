"""Regression pins for the Q4 SKILL caching investigation + Q2 NOTE cleanup.

Q4 (ship 2026-05-25):
  - The Claude Code CLI exposes ``--exclude-dynamic-system-prompt-sections``
    documented to "improve cross-user prompt-cache reuse" by moving
    per-machine sections (cwd, env info, memory paths, git status) out of
    the system prompt into the first user message. For daemon use (many
    ``claude -p`` dispatches per day from the SAME machine, against a
    stable system prompt), this also raises the cross-dispatch cache hit
    rate because the volatile sections no longer break the system-prompt
    cache prefix. We bake the flag into the default ``args`` list for all
    three daemon ClaudeBackendConfig dataclasses.

  - cache_control breakpoints on user-prompt content (the SKILL.md text
    we send via stdin) are NOT controllable through the CLI surface —
    deferred until upstream Claude Code SDK exposes the lever. See
    builder report 2026-05-25 for the investigation summary.

Q2 NOTE cleanup (ship 2026-05-25, bundled with Q4):
  - 2.4: ``_cleanup_stale_openclaw_locks`` (37 LOC dead helper in
         ``curator/process.py``) DELETED.
  - 2.4: ``if config.agent.backend == "openclaw":`` branch in
         ``run_batch`` (13 LOC) DELETED — dead post-collapse.
  - 2.5: ``_call_llm`` in ``distiller/pipeline.py`` now RAISES on
         unknown backend instead of returning ``""``. Aligns with the
         daemon-level ``_create_backend`` factory's fail-loud
         contract and ``feedback_intentionally_left_blank.md``.

These pins are intentionally NOT gated behind ``importorskip`` —
they run in every CI lane. Per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import asyncio

import pytest

from alfred.curator import process as curator_process
from alfred.curator.config import ClaudeBackendConfig as CuratorClaudeConfig
from alfred.distiller import pipeline as distiller_pipeline
from alfred.distiller.config import (
    AgentConfig as DistillerAgentConfig,
    ClaudeBackendConfig as DistillerClaudeConfig,
    DistillerConfig,
    VaultConfig as DistillerVaultConfig,
)
from alfred.janitor.config import ClaudeBackendConfig as JanitorClaudeConfig


# ---------------------------------------------------------------------------
# Part 1 — Q4 SKILL caching: --exclude-dynamic-system-prompt-sections in args
# ---------------------------------------------------------------------------

_EXPECTED_FLAG = "--exclude-dynamic-system-prompt-sections"


@pytest.mark.parametrize(
    "config_cls,tool",
    [
        (CuratorClaudeConfig, "curator"),
        (JanitorClaudeConfig, "janitor"),
        (DistillerClaudeConfig, "distiller"),
    ],
)
def test_claude_backend_args_include_cache_reuse_flag(
    config_cls: type, tool: str
) -> None:
    """All three daemon ClaudeBackendConfigs MUST default to passing
    ``--exclude-dynamic-system-prompt-sections`` to ``claude -p``.

    Documented purpose: improve cross-dispatch prompt-cache reuse by
    moving volatile per-machine sections out of the system prompt.
    For the daemons (many dispatches/day against a stable system
    prompt) this is the cache lever available through the CLI today.

    A future change that drops this flag from the default args without
    also updating the regression-pin will fire this test, surfacing the
    cache-cost regression instead of letting it ship silently. Per
    ``feedback_log_emission_test_pattern.md``-shaped reasoning — the
    same shape applies to documented cache-reuse flags, not just log
    events.
    """
    cfg = config_cls()
    assert _EXPECTED_FLAG in cfg.args, (
        f"{tool} ClaudeBackendConfig.args lost the cache-reuse flag "
        f"{_EXPECTED_FLAG!r}. Current args={cfg.args!r}. Q4 ship 2026-05-25 "
        "added this flag for daemon cache hit rate; restore it or update "
        "the pin if the removal is deliberate."
    )


@pytest.mark.parametrize(
    "config_cls",
    [CuratorClaudeConfig, JanitorClaudeConfig, DistillerClaudeConfig],
)
def test_claude_backend_args_still_have_print_flag(config_cls: type) -> None:
    """Adding the cache-reuse flag MUST NOT have displaced ``-p``."""
    cfg = config_cls()
    assert "-p" in cfg.args, (
        f"ClaudeBackendConfig.args dropped ``-p`` — backends rely on "
        f"this for non-interactive mode. Current args={cfg.args!r}."
    )


# ---------------------------------------------------------------------------
# Part 2 — Item 2.4 cleanup: dead OpenClaw lock helper + branch are gone
# ---------------------------------------------------------------------------


def test_cleanup_stale_openclaw_locks_helper_is_deleted() -> None:
    """The 37-LOC ``_cleanup_stale_openclaw_locks`` helper in
    ``curator/process.py`` was dead post backend-abstraction-collapse
    (2026-05-25 commit ``111cf9e``). It was DELETED in the Q4-plus-cleanup
    ship — the daemon's ``_create_backend`` factory raises on
    ``backend == "openclaw"`` before any session lock could form.

    If a future re-introduction wants OpenClaw back, the lock helper
    should be re-introduced ALONGSIDE the backend dataclass + factory
    branch in the same commit, not resurrected as a zombie.
    """
    assert not hasattr(curator_process, "_cleanup_stale_openclaw_locks"), (
        "_cleanup_stale_openclaw_locks resurfaced in curator.process — "
        "the 2026-05-25 cleanup deleted it as dead code. If re-adding "
        "OpenClaw, re-introduce the backend dataclass + factory branch "
        "in the same commit, not just the lock helper."
    )


def test_run_batch_no_longer_branches_on_openclaw_backend() -> None:
    """The ``if config.agent.backend == "openclaw":`` branch in
    ``run_batch`` (13 LOC) was strictly dead post-collapse. Pin via
    source-level scan to keep the cleanup tight without spawning an
    actual run.
    """
    import inspect

    src = inspect.getsource(curator_process.run_batch)
    assert "openclaw" not in src.lower(), (
        "run_batch source still mentions 'openclaw' — the 2026-05-25 "
        "cleanup removed the dead concurrency-forcing branch. If a future "
        "backend genuinely needs serial-only dispatch, design the "
        "constraint at the BaseBackend level (e.g., a `serial_only` "
        "class attribute), not via a `backend == \"<name>\"` literal."
    )


# ---------------------------------------------------------------------------
# Part 2 — Item 2.5: distiller _call_llm raises on unknown backend
# ---------------------------------------------------------------------------


def _make_distiller_cfg(backend_name: str) -> DistillerConfig:
    return DistillerConfig(
        vault=DistillerVaultConfig(path="/tmp/test-vault"),
        agent=DistillerAgentConfig(
            backend=backend_name,
            claude=DistillerClaudeConfig(),
        ),
    )


def test_distiller_call_llm_raises_on_unknown_backend() -> None:
    """``_call_llm`` MUST raise ``ValueError`` on an unknown backend.

    Pre-2026-05-25: returned ``""`` (silent-empty fallback), which let
    extraction failures propagate as missing learn records with only a
    single ``pipeline.unsupported_backend`` log line and no test
    failure signal. Inconsistent with the daemon-level
    ``_create_backend`` factory which already raises.

    Post-2026-05-25 (this commit): raises immediately with the
    daemon-factory-shaped error message. Aligns with
    ``feedback_intentionally_left_blank.md`` — silence is
    indistinguishable from broken.
    """
    cfg = _make_distiller_cfg("openclaw")
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(
            distiller_pipeline._call_llm(
                prompt="anything",
                config=cfg,
                session_path="/tmp/nonexistent-session",
                stage_label="test-stage",
            )
        )
    msg = str(exc_info.value)
    assert "openclaw" in msg, f"error must mention the bad backend name: {msg}"
    assert "claude" in msg.lower(), f"error must hint at supported backends: {msg}"


def test_distiller_call_llm_raises_on_typo_backend() -> None:
    """Same fail-loud behavior for any unknown name (typos, future
    re-introductions not yet wired up). Mirrors the
    ``test_distiller_factory_rejects_typo`` shape in
    ``test_backend_factory_only_known_backends.py``.
    """
    cfg = _make_distiller_cfg("clawd")
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(
            distiller_pipeline._call_llm(
                prompt="anything",
                config=cfg,
                session_path="/tmp/nonexistent-session",
                stage_label="test-stage",
            )
        )
    assert "clawd" in str(exc_info.value)

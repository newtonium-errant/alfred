"""Fake tool runners for orchestrator tests.

The orchestrator spawns tool processes via ``multiprocessing.Process``.
Real tool runners import heavy packages (asyncio daemons, Ollama clients,
Anthropic SDKs) that we do not want running in the test process tree.

Every fake runner is a top-level function so it can be pickled by
``multiprocessing`` on platforms where forking copies state differently
(Windows spawn mode + WSL fork mode both need the target to be importable
by name). Configuration is passed via the ``raw`` dict — the orchestrator
already hands ``raw`` to each runner, so we piggy-back on that channel
instead of using globals.

Fakes exit with a caller-controlled code after a short sleep, so the
orchestrator's restart loop can observe the exit and react.

Usage in tests::

    monkeypatch.setitem(
        orchestrator.TOOL_RUNNERS,
        "curator",
        fake_exit_after_delay,  # 2-arg? 3-arg? depends on the contract
    )

The shape of the fake must match the signature the orchestrator expects
for that tool — tools in ``{"surveyor", "mail", "brief"}`` take
``(raw, suppress_stdout)``; everything else takes
``(raw, skills_dir, suppress_stdout)``.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any


def _shared_exit(raw: dict[str, Any]) -> None:
    """Common exit behavior — read ``_fake_runner`` config from raw."""
    cfg = raw.get("_fake_runner", {})
    tool = cfg.get("tool", "unknown")
    # Default to a short sleep so the orchestrator's main loop has time to
    # observe a live process before the exit fires.
    delay = cfg.get("delay", 0.2)
    exit_code = cfg.get("exit_code", 0)
    # Optional touch-file so tests can verify the runner actually started.
    touch_file = cfg.get("touch_files", {}).get(tool)

    if touch_file:
        try:
            with open(touch_file, "a", encoding="utf-8") as f:
                f.write(f"{os.getpid()}\n")
        except OSError:
            pass

    time.sleep(delay)
    sys.exit(exit_code)


def fake_runner_2arg(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Matches ``_run_surveyor`` / ``_run_mail_webhook`` / ``_run_brief``."""
    _shared_exit(raw)


def fake_runner_3arg(
    raw: dict[str, Any],
    skills_dir: str,
    suppress_stdout: bool = False,
) -> None:
    """Matches ``_run_curator`` / ``_run_janitor`` / ``_run_distiller`` / ``_run_talker``."""
    _shared_exit(raw)


def long_lived_runner_2arg(
    raw: dict[str, Any], suppress_stdout: bool = False,
) -> None:
    """2-arg fake that blocks on signals until terminated.

    Used for SIGTERM / shutdown tests where we want a process that will
    still be alive when the orchestrator's finally block runs ``terminate()``.
    """
    cfg = raw.get("_fake_runner", {})
    touch_file = cfg.get("touch_files", {}).get(cfg.get("tool", "unknown"))
    if touch_file:
        try:
            with open(touch_file, "a", encoding="utf-8") as f:
                f.write(f"{os.getpid()}\n")
        except OSError:
            pass
    # Sleep for a long time — will be interrupted by SIGTERM or SIGKILL.
    time.sleep(60)


def long_lived_runner_3arg(
    raw: dict[str, Any],
    skills_dir: str,
    suppress_stdout: bool = False,
) -> None:
    """3-arg counterpart to ``long_lived_runner_2arg``."""
    long_lived_runner_2arg(raw, suppress_stdout)

"""Sovereign scribe daemon — the STAY-C slot standup (scribe P1-d).

Minimal standup: the slot comes UP boundary-enforced + guard-armed + idle-ready.
NO audio→note pipeline (that is P2). On boot the daemon:

  (a) REQUIRES ``sovereign: {enabled: true}`` — refuses to boot a clinical
      scribe without the no-egress boundary (fail-closed; a scribe block alone
      is a misconfiguration, not a licence to run unguarded).
  (b) RE-VALIDATES the sovereign boundary in its OWN process. The run_all
      parent gate already validated + fork-installs the guard, but a
      spawn-launched child would NOT inherit it — so the daemon self-checks
      (the P1-a r2 belt: parent gate + daemon self-check).
  (c) SELF-INSTALLS the SovereignHttpGuard in its own process — the REAL
      per-process coverage (spawn children don't inherit the parent's httpx
      monkeypatch; this is the load-bearing install, not the fork inheritance).
  (d) boots in synthetic mode and emits ``scribe.daemon.up`` (intentionally-
      left-blank: up + sovereign_ok + synthetic + no-input, so idle-ready is
      distinguishable from broken).
  (e) sits in an idle loop emitting ``scribe.daemon.idle_tick``.

A boundary breach raises :class:`SovereignBoundaryError`; the orchestrator
runner maps it to exit 79 (NON-RESTARTABLE — refuse to re-enter a
cloud-reachable state).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import structlog

from alfred.scribe.config import ScribeConfig, load_from_unified
from alfred.sovereign import (
    SovereignBoundaryError,
    install_sovereign_http_guard,
    is_sovereign_http_guard_installed,
    validate_sovereign_boundary,
)

log = structlog.get_logger(__name__)

# ILB heartbeat cadence — "up, synthetic, no input" so an idle sovereign scribe
# is observably distinct from a dead one. Hourly is ample for a slot with no
# input pipeline yet (P2).
_IDLE_TICK_SECONDS = 3600


def startup(
    raw: dict,
    *,
    env: Mapping[str, str] | None = None,
) -> ScribeConfig:
    """Boot steps (a)-(d) — synchronous, fail-closed. Returns the loaded
    :class:`ScribeConfig`.

    Raises :class:`SovereignBoundaryError` if the sovereign block is absent
    (``scribe_requires_sovereign``) or any of the four barriers is breached
    (``barrier_a`` .. ``barrier_d``). ``env`` defaults to ``os.environ`` (read
    live by the boundary's barrier c); injectable for tests.
    """
    config = load_from_unified(raw)

    # (a) fail-closed: a clinical scribe MUST run behind the boundary.
    sovereign = raw.get("sovereign") or {}
    if not (isinstance(sovereign, dict) and sovereign.get("enabled")):
        raise SovereignBoundaryError(
            "scribe_requires_sovereign",
            "the scribe daemon refuses to boot without sovereign:{enabled:true} "
            "— a clinical scribe must run behind the no-egress boundary. Add "
            "the sovereign block and launch cloud-key-scrubbed (env -u ...).",
        )

    # (b) re-validate the boundary in THIS process (belt for spawn children).
    validate_sovereign_boundary(raw, env=env)  # raises on breach

    # (c) self-install the per-call HTTP guard in THIS process (real coverage).
    install_sovereign_http_guard()

    # (d) ILB up signal.
    log.info(
        "scribe.daemon.up",
        sovereign_ok=True,
        http_guard_installed=is_sovereign_http_guard_installed(),
        mode=config.mode,
        input_dir=config.input_dir,
        has_input=False,
        detail=(
            "sovereign scribe up — boundary validated, http guard armed, "
            "synthetic mode, NO input pipeline yet (P2). Idle-ready."
        ),
    )
    return config


async def run(
    raw: dict,
    *,
    suppress_stdout: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    """Async entry point. Runs :func:`startup`, then the idle heartbeat loop.

    Never returns under normal operation — the orchestrator terminates the
    process on shutdown. A boundary breach in :func:`startup` propagates out
    (the runner maps it to exit 79).
    """
    config = startup(raw, env=env)
    while True:
        await asyncio.sleep(_IDLE_TICK_SECONDS)
        log.info(
            "scribe.daemon.idle_tick",
            mode=config.mode,
            has_input=False,
            detail="sovereign scribe idle — synthetic mode, no input (P2).",
        )

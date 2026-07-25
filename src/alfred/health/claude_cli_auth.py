"""Shared Claude Code CLI auth probe — the ``claude -p`` LOGIN surface (#32).

Distinct from :func:`alfred.health.anthropic_auth.check_anthropic_auth`, which
probes **API-KEY** auth via the Anthropic SDK (``count_tokens``). The curator,
janitor, and distiller structure records by shelling out to ``claude -p``, whose
auth is the Claude Code **login** (OAuth / subscription) — a DIFFERENT surface.
With ``ANTHROPIC_API_KEY`` set, the SDK probe reports OK while ``claude -p`` fails
``Not logged in``: that is exactly the blind spot behind the 2026-07 silent
~6-hour structuring outage (874 curator failures, 0 successes, emails archived
UNSTRUCTURED). The SDK probe stayed green the whole time.

``claude auth status`` prints JSON ``{"loggedIn": true, ...}`` + exit 0 when
logged in and burns ZERO tokens, so this probe is cheap enough to run every BIT
cycle (quick + full).

Classification:
  OK   — exit 0 and ``loggedIn: true``
  FAIL — ``loggedIn: false`` / nonzero exit / "not logged in" text. LOUD: the
         structuring pipeline is DOWN (higher severity than an account going
         quiet). Also FAIL if the ``claude`` CLI isn't found — ``claude -p``
         can't run either.
  WARN — timeout / unparseable-but-exit-0 / other probe error. Transient CLI or
         API variance must NOT hard-FAIL (mirrors the 2026-06-07 haiku-latency
         false-FAIL that drove the anthropic-auth timeout widen).
"""
from __future__ import annotations

import asyncio
import json
import time

from .types import CheckResult, Status

# Generous default — the probe is a fast local status read, but keep headroom
# so a momentarily-slow CLI invocation degrades to WARN, never a false FAIL.
_DEFAULT_TIMEOUT_SECONDS = 10.0


def resolve_claude_command(raw: dict) -> str:
    """The ``claude`` command the daemon actually shells out to.

    Mirrors ``curator/janitor/distiller`` backend config (``agent.claude.command``,
    default ``"claude"``) so the probe tests the same binary the pipeline runs.
    """
    agent = raw.get("agent", {}) or {}
    claude = agent.get("claude", {}) or {}
    cmd = claude.get("command")
    return cmd if isinstance(cmd, str) and cmd else "claude"


def _parse_logged_in(out: str) -> bool | None:
    """Extract ``loggedIn`` from the status JSON. ``None`` if unparseable."""
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("loggedIn"), bool):
        return bool(data["loggedIn"])
    return None


def _safe_fields(out: str) -> dict:
    """Non-PII diagnostic fields from the status JSON.

    Deliberately excludes ``email`` / ``orgId`` / ``orgName`` — the CheckResult
    ``data`` serializes into the vault BIT record + the brief, and there's no
    reason to echo account identity there.
    """
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("authMethod", "subscriptionType") if k in data}


async def check_claude_cli_auth(
    command: str = "claude",
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> CheckResult:
    """Probe ``claude auth status`` and classify the result. Zero tokens."""
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            "auth",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return CheckResult(
            name="claude-cli-auth",
            status=Status.FAIL,
            detail=f"claude CLI not found (command={command!r}) — claude -p structuring cannot run",
        )
    except asyncio.TimeoutError:
        return CheckResult(
            name="claude-cli-auth",
            status=Status.WARN,
            detail=f"claude auth status timed out after {timeout:.0f}s (transient?)",
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="claude-cli-auth",
            status=Status.WARN,
            detail=f"probe error: {exc.__class__.__name__}: {exc}",
        )

    latency = (time.monotonic() - started) * 1000.0
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        tail = (out or err or "(no output)").strip()[:200]
        return CheckResult(
            name="claude-cli-auth",
            status=Status.FAIL,
            detail=f"claude -p not logged in / CLI error (exit {proc.returncode}): {tail}",
            latency_ms=latency,
        )

    logged_in = _parse_logged_in(out)
    if logged_in is True:
        return CheckResult(
            name="claude-cli-auth",
            status=Status.OK,
            detail="claude login active",
            latency_ms=latency,
            data=_safe_fields(out),
        )
    if logged_in is False or "not logged in" in (out + err).lower():
        return CheckResult(
            name="claude-cli-auth",
            status=Status.FAIL,
            detail="claude -p not logged in — structuring pipeline is DOWN (run `claude auth login` on the box)",
            latency_ms=latency,
        )
    # Exit 0 but the shape wasn't what we expected — don't hard-FAIL on it.
    return CheckResult(
        name="claude-cli-auth",
        status=Status.WARN,
        detail="claude auth status exited 0 but loggedIn was unparseable",
        latency_ms=latency,
    )

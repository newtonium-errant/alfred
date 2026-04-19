"""Talker health check.

Probes:
  * telegram section present (SKIP when absent)
  * bot_token set (FAIL when empty or unresolved placeholder)
  * allowed_users populated — WARN on empty list (talker would ignore
    every inbound message otherwise)
  * anthropic auth (shared probe)
  * groq key present (static — STT won't work without it) — WARN on absence

The talker is optional (``[voice]`` extras); the module should still
be importable without the anthropic or telegram SDKs installed.
"""

from __future__ import annotations

from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.anthropic_auth import check_anthropic_auth
from alfred.health.types import CheckResult, Status, ToolHealth


def _is_unresolved(value: str) -> bool:
    """True if the string looks like an unresolved ``${VAR}`` placeholder."""
    return bool(value) and value.startswith("${") and value.endswith("}")


def _check_bot_token(token: str) -> CheckResult:
    if not token:
        return CheckResult(
            name="bot-token",
            status=Status.FAIL,
            detail="telegram.bot_token is empty",
        )
    if _is_unresolved(token):
        return CheckResult(
            name="bot-token",
            status=Status.FAIL,
            detail=f"bot_token placeholder not resolved: {token}",
        )
    return CheckResult(
        name="bot-token",
        status=Status.OK,
        detail=f"token present ({len(token)} chars)",
        data={"length": len(token)},
    )


def _check_allowed_users(allowed: list) -> CheckResult:
    if not allowed:
        return CheckResult(
            name="allowed-users",
            status=Status.WARN,
            detail="allowed_users is empty (talker will reject every inbound message)",
        )
    return CheckResult(
        name="allowed-users",
        status=Status.OK,
        detail=f"{len(allowed)} user(s) allowlisted",
        data={"count": len(allowed)},
    )


def _check_stt_key(stt: dict) -> CheckResult:
    provider = stt.get("provider", "groq")
    api_key = stt.get("api_key", "") or ""
    if not api_key or _is_unresolved(api_key):
        return CheckResult(
            name="stt-key",
            status=Status.WARN,
            detail=f"stt.api_key missing — voice transcription via {provider} will fail",
            data={"provider": provider},
        )
    return CheckResult(
        name="stt-key",
        status=Status.OK,
        detail=f"{provider} key present",
        data={"provider": provider},
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run talker health checks."""
    tel = raw.get("telegram")
    if tel is None:
        return ToolHealth(
            tool="talker",
            status=Status.SKIP,
            detail="no telegram section in config",
        )

    results: list[CheckResult] = [
        _check_bot_token(tel.get("bot_token", "") or ""),
        _check_allowed_users(tel.get("allowed_users", []) or []),
        _check_stt_key(tel.get("stt", {}) or {}),
    ]

    # Anthropic auth — talker uses the SDK directly (not CLI), so the
    # api_key in the telegram section is the authoritative source here.
    # We skip the env-var fallback here on purpose — the talker's own
    # config path is what matters; if the env var supplied the key,
    # `_substitute_env` in telegram/config.py will have already written
    # it into the raw dict by the time this runs in the CLI/daemon.
    anthro_cfg = tel.get("anthropic", {}) or {}
    api_key = anthro_cfg.get("api_key", "") or ""
    if api_key and not _is_unresolved(api_key):
        model = anthro_cfg.get("model", "claude-haiku-4-5")
        results.append(await check_anthropic_auth(api_key, model=model))
    else:
        results.append(CheckResult(
            name="anthropic-auth",
            status=Status.FAIL,
            detail="telegram.anthropic.api_key missing or unresolved",
        ))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="talker", status=status, results=results)


register_check("talker", health_check)

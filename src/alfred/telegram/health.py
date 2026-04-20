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

import time
from typing import Any

import httpx

from alfred.health.aggregator import register_check
from alfred.health.anthropic_auth import check_anthropic_auth
from alfred.health.types import CheckResult, Status, ToolHealth
from alfred.telegram.config import _substitute_env


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


# --- wk2b c6: TTS probes --------------------------------------------------


def _check_tts_key(tts: dict | None) -> CheckResult:
    """Static probe: TTS api_key present + env var resolved.

    SKIPs when the ``tts`` section is absent (opt-in feature — its
    absence shouldn't fail the talker's overall health rollup).
    """
    if tts is None:
        return CheckResult(
            name="tts-key",
            status=Status.SKIP,
            detail="telegram.tts section absent — /brief feature is opt-in",
        )
    provider = tts.get("provider", "elevenlabs")
    api_key = tts.get("api_key", "") or ""
    if not api_key:
        return CheckResult(
            name="tts-key",
            status=Status.FAIL,
            detail=f"tts.api_key missing — {provider} /brief will not work",
            data={"provider": provider},
        )
    if _is_unresolved(api_key):
        return CheckResult(
            name="tts-key",
            status=Status.FAIL,
            detail=f"tts.api_key placeholder not resolved: {api_key}",
            data={"provider": provider},
        )
    return CheckResult(
        name="tts-key",
        status=Status.OK,
        detail=f"{provider} key present ({len(api_key)} chars)",
        data={"provider": provider, "length": len(api_key)},
    )


def _check_capture_handlers() -> CheckResult:
    """Functional probe: capture_batch and capture_extract modules importable."""
    try:
        import alfred.telegram.capture_batch  # noqa: F401
        import alfred.telegram.capture_extract  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="capture-handler-registered",
            status=Status.FAIL,
            detail=f"capture handler import failed: {exc}",
        )
    return CheckResult(
        name="capture-handler-registered",
        status=Status.OK,
        detail="capture_batch + capture_extract modules importable",
    )


async def _check_elevenlabs_auth(tts: dict | None) -> CheckResult:
    """Remote-network probe: GET /v1/user on ElevenLabs with the configured key.

    Runs only in ``full`` mode (caller decides). SKIPs when the tts
    section is absent. The ``/v1/user`` endpoint is the standard
    authenticated health-check endpoint per ElevenLabs docs — returns
    the user's subscription info. Non-200 → FAIL with the status code;
    network error → FAIL with the exception message.
    """
    if tts is None:
        return CheckResult(
            name="elevenlabs-auth",
            status=Status.SKIP,
            detail="telegram.tts section absent — skip remote auth check",
        )
    api_key = tts.get("api_key", "") or ""
    if not api_key or _is_unresolved(api_key):
        return CheckResult(
            name="elevenlabs-auth",
            status=Status.SKIP,
            detail="tts.api_key missing — can't probe remote auth",
        )

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": api_key},
            )
    except Exception as exc:  # noqa: BLE001
        latency = (time.monotonic() - start) * 1000.0
        return CheckResult(
            name="elevenlabs-auth",
            status=Status.FAIL,
            detail=f"network error: {exc}",
            latency_ms=latency,
        )
    latency = (time.monotonic() - start) * 1000.0
    if resp.status_code == 200:
        return CheckResult(
            name="elevenlabs-auth",
            status=Status.OK,
            detail=f"elevenlabs /v1/user 200 OK ({int(latency)}ms)",
            latency_ms=latency,
        )
    return CheckResult(
        name="elevenlabs-auth",
        status=Status.FAIL,
        detail=f"elevenlabs /v1/user returned {resp.status_code}",
        latency_ms=latency,
        data={"status_code": resp.status_code},
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
    # Env-var substitution — the talker daemon runs `_substitute_env` on
    # the raw config at startup via `telegram/config.py::load_from_unified`,
    # so real env-supplied tokens are resolved by the time the SDK is
    # called. The health aggregator hands us the pre-substitution dict,
    # though, so we must mirror the substitution here or we falsely FAIL
    # on `bot_token: "${TELEGRAM_BOT_TOKEN}"`-style configs even when the
    # env var is set.
    raw = _substitute_env(raw or {})

    tel = raw.get("telegram")
    if tel is None:
        return ToolHealth(
            tool="talker",
            status=Status.SKIP,
            detail="no telegram section in config",
        )

    # wk2b c6: ``tts_raw`` stays ``None`` when the section is absent so
    # the TTS probes can SKIP cleanly (vs returning an empty-dict-based
    # FAIL). Distinguishing "not configured" from "misconfigured" matters
    # for an opt-in feature.
    tts_raw = tel.get("tts")
    if not isinstance(tts_raw, dict):
        tts_raw = None

    results: list[CheckResult] = [
        _check_bot_token(tel.get("bot_token", "") or ""),
        _check_allowed_users(tel.get("allowed_users", []) or []),
        _check_stt_key(tel.get("stt", {}) or {}),
        _check_tts_key(tts_raw),
        _check_capture_handlers(),
    ]

    # Remote TTS auth probe — only in ``full`` mode. Skipped in ``quick``
    # mode (pre-brief BIT shouldn't pay a ~2s network call for an opt-in
    # feature) and skipped entirely when tts is absent.
    if mode == "full":
        results.append(await _check_elevenlabs_auth(tts_raw))

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

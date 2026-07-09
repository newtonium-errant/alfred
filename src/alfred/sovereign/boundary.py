"""Fail-closed sovereign no-egress boundary — config/process-load gate.

SECURITY-CRITICAL. This module is the no-egress guarantee for a sovereign
(on-box, local-model-only) instance. A bug here means patient data leaks to
a cloud provider, so every decision fails CLOSED: an ambiguous or
unresolvable input is treated as a breach, never waved through.

``validate_sovereign_boundary(raw)`` runs at CONFIG/PROCESS LOAD (not
prompt-time). It is a no-op unless the config declares an explicit top-level
``sovereign: {enabled: true}`` block — Salem / KAL-LE / Hypatia / VERA-ops
never pay for it. When enforcement IS requested, the boundary raises
:class:`SovereignBoundaryError` unless ALL FOUR independent barriers hold
(any one alone stops egress):

  (a) STT provider on the local allowlist
      {faster-whisper, local-whisper, fake}. Cloud STT (groq / deepgram /
      elevenlabs) refused.
  (b) LLM ``base_url`` host resolves to loopback {127.0.0.1, ::1, localhost}.
      A literal loopback host passes immediately; anything else must resolve
      via ``getaddrinfo`` to ALL-loopback addresses. Resolution failure =>
      refuse (fail-closed on ambiguity).
  (c) NO cloud key PRESENT in the process env AND no ``${CLOUD_KEY}``
      referenced in the config. This barrier runs AFTER the orchestrator's
      config-sibling ``.env`` auto-load, so it catches a key the ``.env``
      re-introduced into ``os.environ`` even when the launch wrapper scrubbed
      the shell env with ``env -u`` (the ``.env`` gap-fill leak).
  (d) No egress transport wired — ``transport`` / peer-push / brief-push /
      ticket-forward / mail / message_bus / pending_items / daily_sync /
      telegram. A sovereign slot has no network surface to push PHI over.

Plus a per-call :class:`SovereignHttpGuard` (see :mod:`alfred.sovereign.http_guard`)
that asserts loopback before connect on every outbound httpx request, to catch
code drift (e.g. a hardcoded cloud STT URL) that the config-time barriers
cannot see.

Local-model-down fails LOUD with NO cloud fallback ("sovereign STT/LLM
unavailable — no cloud fallback by design; audio retained, retry"); that is
the pipeline's responsibility (P2). This module's job is to prove, at load,
that a cloud fallback is not even reachable.

Observability (intentionally-left-blank): every enforced load emits exactly
one structured event — ``sovereign_ok`` when all barriers hold, or
``sovereign_boundary_refused`` (with ``reason=<barrier>``) before raising —
so a sovereign instance that is idle is distinguishable from one that is
broken. A non-sovereign instance emits ``sovereign_not_enforced`` at debug.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Mapping
from urllib.parse import urlsplit

import structlog

from alfred._env import ENV_PLACEHOLDER_RE

log = structlog.get_logger(__name__)


# --- Frozen policy — the contract the barriers enforce ---------------------

# Barrier (a). Local STT providers only. ``faster-whisper`` is the Phase-0
# CPU-box library; ``local-whisper`` is the id the Telegram fallback chain
# reserves for the on-box backstop (telegram/stt_backends.py); ``fake`` is
# the deterministic test provider. Cloud providers (groq / deepgram /
# elevenlabs) are deliberately absent.
SOVEREIGN_STT_ALLOWLIST: frozenset[str] = frozenset(
    {"faster-whisper", "local-whisper", "fake"}
)

# Barrier (b). Loopback host literals that pass without a DNS round-trip.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# Barrier (c). Cloud credential env-var names whose mere PRESENCE (non-empty)
# in the process env breaches the boundary. Kept in lockstep with the launch
# wrapper's ``env -u`` list — a key here that the wrapper forgets to scrub is
# caught at load anyway (that is the point: the boundary does not trust the
# wrapper). Ordered for readable diffs; membership is what matters.
CLOUD_KEY_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "GROQ_API_KEY",
    "DEEPGRAM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ELEVENLABS_API_KEY",
    "TOGETHER_API_KEY",
    "ZO_API_KEY",
)

# Barrier (d). Config sections that wire a network egress path. Presence of
# ANY of these on a sovereign instance is a breach. ``telegram`` is included
# deliberately: a cloud Telegram bot (bot API + the in-process AsyncAnthropic
# reply path) is definitionally non-sovereign. A future sovereign-talker
# backed by a LOCAL model would need an explicit carve-out here — fail-closed
# until then, per the scope-first "design the deny, widen deliberately" rule.
EGRESS_CONFIG_SECTIONS: tuple[str, ...] = (
    "transport",
    "brief_digest_push",
    "ticket_forward",
    "pending_items",
    "message_bus",
    "mail",
    "daily_sync",
    "telegram",
)


class SovereignBoundaryError(Exception):
    """Raised when a sovereign instance's no-egress boundary is breached.

    Non-restartable. The orchestrator maps this to exit 79
    (``_SOVEREIGN_BREACH_EXIT``) and MUST NOT auto-restart — a restart would
    only re-attempt a cloud-reachable start. ``reason`` is the barrier id
    (``barrier_a`` .. ``barrier_d`` / ``http_guard``) for greppable triage;
    ``detail`` is the operator-facing specifics.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"sovereign boundary breached [{reason}]: {detail}")


# --- Shared loopback resolver (barrier b + the HTTP guard) ------------------

def host_is_loopback(host: str) -> bool:
    """Return True iff ``host`` is provably loopback — fail-closed.

    A literal loopback host ({127.0.0.1, ::1, localhost}, IPv6 brackets
    tolerated) passes without a DNS round-trip. Anything else is resolved via
    ``getaddrinfo``; EVERY resolved address must be loopback for the host to
    pass. An empty host, a resolution failure (``gaierror``), an empty result
    set, or an unparseable address all return False — ambiguity is never
    waved through.
    """
    if not host:
        return False
    h = host.strip().lower().strip("[]")
    if h in LOOPBACK_HOSTS:
        return True
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror:
        return False  # unresolvable => not provably loopback => refuse
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0]
        try:
            if not ipaddress.ip_address(ip).is_loopback:
                return False
        except ValueError:
            return False
    return True


# --- Individual barriers ----------------------------------------------------

def _check_stt_local(raw: dict[str, Any]) -> None:
    """Barrier (a) — STT provider on the local allowlist. Fail-closed."""
    scribe = raw.get("scribe") or {}
    stt = scribe.get("stt") or {}
    provider = str(stt.get("provider") or "").strip().lower()
    if provider not in SOVEREIGN_STT_ALLOWLIST:
        allowed = ", ".join(sorted(SOVEREIGN_STT_ALLOWLIST))
        raise SovereignBoundaryError(
            "barrier_a",
            f"scribe.stt.provider must be a local provider ({allowed}); "
            f"got {provider or '(unset)'!r}. Cloud STT (groq / deepgram / "
            f"elevenlabs) is refused on a sovereign instance.",
        )


def _check_llm_loopback(raw: dict[str, Any]) -> None:
    """Barrier (b) — LLM base_url host resolves loopback. Fail-closed."""
    scribe = raw.get("scribe") or {}
    llm = scribe.get("llm") or {}
    base_url = str(llm.get("base_url") or "").strip()
    if not base_url:
        raise SovereignBoundaryError(
            "barrier_b",
            "scribe.llm.base_url is unset — cannot prove the LLM endpoint is "
            "on-box. A sovereign instance must pin a loopback base_url "
            "(e.g. http://127.0.0.1:11434).",
        )
    host = urlsplit(base_url).hostname or ""
    if not host_is_loopback(host):
        raise SovereignBoundaryError(
            "barrier_b",
            f"scribe.llm.base_url host {host or '(unparseable)'!r} is not "
            f"provably loopback ({', '.join(sorted(LOOPBACK_HOSTS))}). "
            f"base_url={base_url!r}. Refusing — a non-loopback (or "
            f"unresolvable) LLM endpoint could reach a cloud model.",
        )


def _check_no_cloud_key(raw: dict[str, Any], env: Mapping[str, str]) -> None:
    """Barrier (c) — no cloud key in env AND no ``${CLOUD_KEY}`` in config.

    Runs AFTER the orchestrator's ``.env`` auto-load, so it catches a key the
    config-sibling ``.env`` re-introduced into ``os.environ`` even when the
    launch wrapper scrubbed the shell env with ``env -u``.
    """
    present = [
        key for key in CLOUD_KEY_ENV_VARS
        if (env.get(key) or "").strip()
    ]
    if present:
        raise SovereignBoundaryError(
            "barrier_c",
            f"cloud credential(s) present in the process env: "
            f"{', '.join(present)}. A sovereign process must launch with a "
            f"scrubbed env (env -u ...) AND a cloud-key-free config-sibling "
            f".env — a key here means the .env re-introduced it after the "
            f"shell scrub.",
        )
    referenced = _cloud_key_placeholders_in_config(raw)
    if referenced:
        raise SovereignBoundaryError(
            "barrier_c",
            f"config references cloud-key placeholder(s): "
            f"{', '.join(sorted(referenced))}. A sovereign config must not "
            f"reference any cloud credential, even by ${{VAR}} placeholder.",
        )


def _cloud_key_placeholders_in_config(value: Any) -> set[str]:
    """Return the set of cloud-key ``${VAR}`` placeholder names anywhere in
    the (recursively walked) config value. Exact-name match against
    :data:`CLOUD_KEY_ENV_VARS` (so ``${ANTHROPIC_API_KEY_DISTILLER_REBUILD}``,
    a distinct var, does not false-match — its presence is instead caught by
    the env check if it is actually set)."""
    found: set[str] = set()
    cloud = set(CLOUD_KEY_ENV_VARS)

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            for name in ENV_PLACEHOLDER_RE.findall(v):
                if name in cloud:
                    found.add(name)
        elif isinstance(v, dict):
            for sub in v.values():
                _walk(sub)
        elif isinstance(v, (list, tuple)):
            for sub in v:
                _walk(sub)

    _walk(value)
    return found


def _check_no_egress(raw: dict[str, Any]) -> None:
    """Barrier (d) — no egress-capable config section wired. Fail-closed."""
    wired = [s for s in EGRESS_CONFIG_SECTIONS if s in raw]
    if wired:
        raise SovereignBoundaryError(
            "barrier_d",
            f"egress-capable config section(s) present: "
            f"{', '.join(wired)}. A sovereign instance wires no transport / "
            f"peer-push / brief-push / ticket-forward / mail / telegram — "
            f"there must be no network surface to push PHI over.",
        )


# --- Public gate ------------------------------------------------------------

def validate_sovereign_boundary(
    raw: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Enforce the four-barrier no-egress boundary. Fail-closed.

    No-op unless ``raw`` declares ``sovereign: {enabled: true}``. When
    enforcement is requested, runs barriers (a)-(d) in order and raises
    :class:`SovereignBoundaryError` on the first breach (after logging
    ``sovereign_boundary_refused reason=<barrier>``). On success emits
    ``sovereign_ok`` and returns.

    Args:
        raw: the unified config dict (as loaded by ``_load_unified_config``;
            env vars NOT yet substituted — that is fine, barrier (c) reads
            ``${VAR}`` placeholders directly).
        env: process environment to inspect for barrier (c). Defaults to
            ``os.environ`` (read live). Injectable for tests.

    Raises:
        SovereignBoundaryError: if any barrier is breached.
    """
    sovereign = raw.get("sovereign") or {}
    if not (isinstance(sovereign, dict) and sovereign.get("enabled")):
        log.debug(
            "sovereign_not_enforced",
            detail="no sovereign:{enabled:true} block — boundary not enforced",
        )
        return

    if env is None:
        import os
        env = os.environ

    try:
        _check_stt_local(raw)
        _check_llm_loopback(raw)
        _check_no_cloud_key(raw, env)
        _check_no_egress(raw)
    except SovereignBoundaryError as e:
        log.error(
            "sovereign_boundary_refused",
            reason=e.reason,
            detail=e.detail,
        )
        raise

    scribe = raw.get("scribe") or {}
    stt = scribe.get("stt") or {}
    llm = scribe.get("llm") or {}
    log.info(
        "sovereign_ok",
        stt_provider=str(stt.get("provider") or ""),
        llm_host=urlsplit(str(llm.get("base_url") or "")).hostname or "",
        egress_clear=True,
        detail="all four no-egress barriers held",
    )

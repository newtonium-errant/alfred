"""Fail-closed sovereign no-egress boundary — config/process-load gate.

SECURITY-CRITICAL. This module is the no-egress guarantee for a sovereign
(on-box, local-model-only) instance. A bug here means patient data leaks to
a cloud provider, so every decision fails CLOSED: an ambiguous or
unresolvable input is treated as a breach, never waved through.

``validate_sovereign_boundary(raw)`` runs at CONFIG/PROCESS LOAD (not
prompt-time). It is a no-op unless the config declares an explicit top-level
``sovereign: {enabled: true}`` block — Salem / KAL-LE / Hypatia / VERA-ops
never pay for it. When enforcement IS requested, the boundary raises
:class:`SovereignBoundaryError` unless ALL of these independent barriers hold
(any one alone stops egress):

  (a) STT provider on the local allowlist
      {faster-whisper, local-whisper, fake}. Cloud STT (groq / deepgram /
      elevenlabs) refused. Its SIBLING (``barrier_a_diarize``) likewise pins the
      diarize provider to {off, fake, pyannote} — no cloud speaker resolution.
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
  (e) The loopback PWA ingest server (#49), when ``scribe.ingest_web.enabled``,
      binds ONLY to a provably-loopback host (0.0.0.0/:: refused at LOAD, before
      any socket binds), carries a bearer token, and has NO egress-shaped field
      in its allowlist-closed sub-tree. A no-op when the server is INERT (the
      default).

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

# Barrier (a) SIBLING. Local diarization providers only (scribe P4).
# ``off`` = no diarization (the fail-closed default); ``fake`` = the
# deterministic CI backend; ``pyannote`` = the real on-box engine (P4-4, loads
# its embedding model offline from the local HF cache). A cloud diarization
# service is deliberately absent — the scribe never sends audio off-box to
# resolve speakers. MUST equal the diarize dispatch set
# (``scribe.diarize.SCRIBE_DIARIZE_PROVIDERS``) — pinned in tests.
SOVEREIGN_DIARIZE_ALLOWLIST: frozenset[str] = frozenset({"off", "fake", "pyannote"})

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
    # RESEND email egress (P1-a review WARN-4) — web/email.py POSTs to
    # api.resend.com/emails; PHI can ride an email body.
    "RESEND_API_KEY",
)

# Known network-egress / agent / tunnel config sections — a DOCUMENTED
# CATALOG, no longer the enforcement mechanism.
#
# As of the P1-a r2 review, barrier (d)'s ENFORCEMENT is the ALLOWLIST
# ``SOVEREIGN_ALLOWED_SECTIONS`` below (fail-closed by default), NOT this
# denylist. The denylist was the wrong shape for a "provable no-egress"
# boundary: it required remembering to deny every egressing tool forever and
# MISSED ``surveyor`` (cloud OpenRouter LLM) twice in one review cycle, plus
# ``brief`` (weather API) and ``cloudflared`` (an outbound tunnel SUBPROCESS
# the httpx guard can never see). This tuple is retained as (1) a readable
# catalog of concrete known-bad sections and (2) explicit negative-test pins;
# it is asserted DISJOINT from the allowlist (nothing here may ever be
# allowlisted). The allowlist already denies every entry here AND every
# future daemon.
#
# Two escape classes that motivated barrier (d) (P1-a review r1) — still the
# reason these sections are catalogued as unsafe:
#
#   1. The ``claude -p`` OAuth SUBPROCESS path (BLOCK-1 — the serious one, a
#      real cloud-LLM egress no other barrier catches).
#      ``subprocess_env.claude_subprocess_env`` DELIBERATELY strips
#      ANTHROPIC_API_KEY/AUTH_TOKEN/BASE_URL so ``claude -p`` falls back to the
#      CACHED OAuth creds in ~/.claude and STILL reaches api.anthropic.com.
#      Stripping the key does NOT neutralise it — it REROUTES it to OAuth. And
#      it is a separate process, so the httpx guard is blind to it.
#      curator / janitor / distiller run ``claude -p`` and auto-start on their
#      OWN block presence, defaulting to ``backend: claude`` (AgentConfig
#      default) even with NO ``agent:`` block — so denying ``agent`` alone is
#      INSUFFICIENT; the agent-backed TOOL blocks are denied too. instructor
#      runs the Anthropic SDK in-process (httpx, guard-caught) but is denied
#      here for defense-in-depth.
#   2. Non-httpx cloud transports the guard cannot wrap (BLOCK-2 / WARN-3):
#      ``web`` (aiohttp STT/TTS in web/stt_deepgram.py + web/tts_elevenlabs.py,
#      plus RESEND email egress in web/email.py), ``gcal`` / ``integrations``
#      (googleapiclient in integrations/gcal.py). The aiohttp guard extension
#      is a hard P2 blocker (task #40) before the scribe web UI may route PHI;
#      until then these are fail-closed here.
#
# ``telegram`` is included deliberately: a cloud Telegram bot (bot API + the
# in-process AsyncAnthropic reply path) is definitionally non-sovereign. A
# future sovereign-talker / sovereign-surveyor backed by a LOCAL model would
# need an explicit carve-out here — fail-closed until then, per the scope-first
# "design the deny, widen deliberately" rule.
EGRESS_CONFIG_SECTIONS: tuple[str, ...] = (
    "transport",
    "brief_digest_push",
    "ticket_forward",
    "pending_items",
    "message_bus",
    "mail",
    "daily_sync",
    "telegram",
    # claude -p OAuth subprocess egress (BLOCK-1) — the backend selector AND
    # the agent-backed tools that default to backend=claude without it.
    "agent",
    "curator",
    "janitor",
    "distiller",
    "instructor",
    # non-httpx cloud transports the httpx guard cannot see (BLOCK-2 / WARN-3).
    "web",
    "gcal",
    "integrations",
    # Denylist misses caught by the r2 review — the reason barrier (d) is now
    # an allowlist. surveyor = cloud OpenRouter LLM (httpx, but its own
    # daemon); brief = weather API HTTP; cloudflared = an outbound TUNNEL
    # subprocess the guard can NEVER see.
    "surveyor",
    "brief",
    "cloudflared",
)


# Barrier (d) ENFORCEMENT — the ALLOWLIST (fail-closed by default).
#
# A sovereign config (``sovereign.enabled``) may contain ONLY these top-level
# sections; ANY other top-level key breaches barrier (d). This is the correct
# shape for a "provable no-egress" boundary: every NEW daemon / block is
# denied here BY DEFAULT until it is explicitly vetted sovereign-safe and
# added, which structurally subsumes surveyor / brief / cloudflared and every
# future tool. Each entry below is filesystem-only, identity-only, or the
# sovereign workload itself — none opens a network surface. Widen ONLY with a
# per-entry safety rationale (the same discipline as the scope allowlists).
SOVEREIGN_ALLOWED_SECTIONS: frozenset[str] = frozenset({
    # Synthetic key stamped onto EVERY raw config by ``_load_unified_config``
    # (cli.py) — a resolved filesystem path string, no network. MUST be
    # allowlisted or every real sovereign config (which always carries it)
    # would refuse at load.
    "_config_path",
    # The enablement gate itself.
    "sovereign",
    # The sovereign workload. ``scribe.stt`` (barrier a) + ``scribe.llm``
    # (barrier b) are independently validated to be local; the rest of the
    # block is pipeline config with no egress.
    "scribe",
    # Filesystem: the PHI vault path + scan-dir lists. No network.
    "vault",
    # Filesystem: log level + log dir. No network.
    "logging",
    # Filesystem: the daemon PID-file path (per-instance collision-avoidance).
    # No network.
    "daemon",
    # Identity-only: instance name / canonical / aliases used for record
    # attribution + templating (``audit/cli.py`` reads ``raw.get("instance")``).
    # No network surface.
    "instance",
})


class SovereignBoundaryError(Exception):
    """Raised when a sovereign instance's no-egress boundary is breached.

    Non-restartable. The orchestrator maps this to exit 79
    (``_SOVEREIGN_BREACH_EXIT``) and MUST NOT auto-restart — a restart would
    only re-attempt a cloud-reachable start. ``reason`` is the barrier id
    (``barrier_a`` / ``barrier_a_diarize`` / ``barrier_b`` .. ``barrier_e`` /
    ``http_guard``) for greppable triage;
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


def _check_diarize_local(raw: dict[str, Any]) -> None:
    """Barrier (a) SIBLING — diarize provider on the local allowlist. Fail-closed.

    Defense-in-depth twin of :func:`_check_stt_local`: the ``scribe`` section is
    already an allowed top-level section (barrier d), so a ``scribe.diarize``
    sub-tree rides in — this positively asserts its ``provider`` is local
    ({off, fake, pyannote}) so no cloud diarization service can be configured to
    ship audio off-box. Absent ``diarize`` block → provider defaults to ``off``
    (unset ⇒ ``off``), which is on the allowlist (a scribe with NO diarize config
    passes)."""
    scribe = raw.get("scribe") or {}
    diarize = scribe.get("diarize") or {}
    provider = str(diarize.get("provider") or "off").strip().lower()
    if provider not in SOVEREIGN_DIARIZE_ALLOWLIST:
        allowed = ", ".join(sorted(SOVEREIGN_DIARIZE_ALLOWLIST))
        raise SovereignBoundaryError(
            "barrier_a_diarize",
            f"scribe.diarize.provider must be a local provider ({allowed}); "
            f"got {provider!r}. Cloud diarization is refused on a sovereign "
            f"instance — speaker resolution stays on-box.",
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
    """Barrier (d) — ALLOWLIST: only sovereign-safe top-level sections.

    Fail-closed by default. A sovereign config may contain ONLY the sections
    in :data:`SOVEREIGN_ALLOWED_SECTIONS`; ANY other top-level key — a tool
    daemon, a transport/egress block, an agent backend, a tunnel, or a
    future-added daemon nobody has vetted yet — is refused. This structurally
    subsumes every known egress section (:data:`EGRESS_CONFIG_SECTIONS`) AND
    every future daemon, so a new tool is denied here until it is explicitly
    vetted sovereign-safe and allowlisted.
    """
    disallowed = sorted(k for k in raw if k not in SOVEREIGN_ALLOWED_SECTIONS)
    if disallowed:
        allowed = ", ".join(sorted(SOVEREIGN_ALLOWED_SECTIONS))
        raise SovereignBoundaryError(
            "barrier_d",
            f"non-allowlisted top-level config section(s): "
            f"{', '.join(disallowed)}. A sovereign config may contain ONLY "
            f"[{allowed}] — every other section is fail-closed by default (a "
            f"tool daemon, transport/egress block, agent backend, or tunnel "
            f"must be vetted sovereign-safe and explicitly allowlisted before "
            f"it can run here). This denies every known egress section AND "
            f"every future daemon by default.",
        )


def _check_ingest_web_loopback(raw: dict[str, Any]) -> None:
    """Barrier (e) — the loopback PWA ingest server (#49) is bind-safe. Fail-closed.

    A NO-OP unless ``scribe.ingest_web.enabled`` is truthy (the server is INERT by
    default). When the server IS enabled, POSITIVELY assert THREE things at
    config-load — BEFORE any socket binds — so a LAN-reachable PHI-ingest hole
    can never reach ``AppRunner``:

      1. ``host`` is provably loopback (REUSES :func:`host_is_loopback` — accepts
         127.0.0.1/::1/localhost, rejects 0.0.0.0/:: /a resolvable LAN name).
         This is the must-have: a ``0.0.0.0`` bind must fail HERE, not merely at
         socket-bind (which would already be listening on the LAN).
      2. a bearer ``token`` is PRESENT (non-empty) — a tokenless loopback ingest
         face is refused (defense-in-depth beyond loopback: an on-box hostile
         process could otherwise POST PHI-adjacent audio).
      3. NO unexpected key in the ``ingest_web`` sub-tree — it is allowlist-closed
         to :data:`~alfred.scribe.config.INGEST_WEB_ALLOWED_KEYS`. A ``base_url`` /
         ``webhook`` / ``forward_to`` / cloud-endpoint field here would be an
         egress surface, so it is fail-closed the same way barrier (d) closes the
         top-level sections.

    All three raise ``barrier_e`` (→ exit 79, non-restartable).

    ENV-SUBSTITUTION SEAM (audit fix — the barrier and the server MUST evaluate
    the IDENTICAL values): ``config.load_from_unified`` substitutes ``${VAR}`` in
    the ingest_web sub-tree BEFORE coercing enabled/host/token and binding the
    socket. This barrier historically read the RAW (un-substituted) values, so an
    ``enabled: "${VAR}"`` (VAR=true) coerced to the LITERAL ``"${VAR}"`` → False →
    barrier returned INERT while the server resolved it → True → BOUND (fail-OPEN,
    to ``host`` which could be 0.0.0.0). Same seam let an UNSET
    ``token: "${SCRIBE_INGEST_TOKEN}"`` stay a non-empty literal that passed the
    old non-empty check yet became the live source-visible bearer. FIX: substitute
    ONLY this sub-tree here — the same values the server binds from. It is a NEW
    copy (``substitute_env_in_value`` never mutates its input), so ``raw`` is
    untouched and barrier-(c)'s RAW-config ``${CLOUD_KEY}`` scan (which runs BEFORE
    this barrier, on ``raw``) is unaffected (Gap D — do NOT substitute the whole
    config before the boundary).
    """
    from alfred._env import ENV_PLACEHOLDER_RE, substitute_env_in_value
    from alfred.scribe.config import INGEST_WEB_ALLOWED_KEYS, coerce_ingest_web_enabled

    scribe = raw.get("scribe") or {}
    if not isinstance(scribe, dict):
        return
    ingest_raw = scribe.get("ingest_web") or {}
    if not isinstance(ingest_raw, dict):
        return
    # Substitute ONLY the ingest_web sub-tree (a fresh copy — no raw mutation), so
    # the barrier evaluates the SAME enabled/host/token the server binds from.
    ingest = substitute_env_in_value(ingest_raw)

    # Use the SHARED enabled-coercion so "does the barrier validate the bind" ==
    # "does the server actually bind" (now on the SUBSTITUTED value — a resolved
    # ``${VAR}=true`` arms both; an unset/quoted false is inert in both).
    if not coerce_ingest_web_enabled(ingest.get("enabled")):
        return  # INERT — no server binds, nothing to validate

    # (3) allowlist-closed sub-tree — refuse any egress-shaped field. (Keys are
    # unchanged by substitution.)
    unexpected = sorted(k for k in ingest if k not in INGEST_WEB_ALLOWED_KEYS)
    if unexpected:
        allowed = ", ".join(sorted(INGEST_WEB_ALLOWED_KEYS))
        raise SovereignBoundaryError(
            "barrier_e",
            f"scribe.ingest_web carries unexpected field(s): "
            f"{', '.join(unexpected)}. The ingest sub-tree is allowlist-closed to "
            f"[{allowed}] — a cloud endpoint / webhook / forward field here would "
            f"be an egress surface and is refused (fail-closed).",
        )

    # (1) loopback host — the must-have. (Substituted: ``${HOST}=127.0.0.1``
    # resolves + passes; an unset ``${HOST}`` stays the literal → host_is_loopback
    # fails → refuse, matching the socket-bind that would also fail.)
    host = str(ingest.get("host") or "").strip()
    if not host_is_loopback(host):
        raise SovereignBoundaryError(
            "barrier_e",
            f"scribe.ingest_web.host {host or '(unset)'!r} is not provably "
            f"loopback ({', '.join(sorted(LOOPBACK_HOSTS))}). A sovereign PWA "
            f"ingest server may bind ONLY to loopback — a 0.0.0.0/:: (or a "
            f"resolvable LAN) bind is a LAN-reachable PHI-ingest hole. Refusing "
            f"at load (before any socket binds).",
        )

    # (2) token present AND a REAL secret — reject empty OR an unresolved ``${..}``
    # placeholder. An unset ``${SCRIBE_INGEST_TOKEN}`` stays the literal after
    # substitution; without this the server's ``compare_digest`` would accept that
    # predictable, source-visible literal as the live bearer (fail-loud on a
    # missing secret, per feedback_hardcoding_and_alfred_naming).
    token = str(ingest.get("token") or "").strip()
    if not token:
        raise SovereignBoundaryError(
            "barrier_e",
            "scribe.ingest_web.token is unset — a sovereign PWA ingest server "
            "requires a bearer token (loopback alone is insufficient: an on-box "
            "process could POST). Set ${SCRIBE_INGEST_TOKEN}.",
        )
    if ENV_PLACEHOLDER_RE.search(token):
        raise SovereignBoundaryError(
            "barrier_e",
            "scribe.ingest_web.token is an UNRESOLVED ${...} placeholder — its env "
            "var is unset (or empty), so the literal placeholder would become the "
            "live bearer secret (source-visible, predictable). Set the env var to a "
            "real secret before enabling the ingest server; refusing at load.",
        )


# --- Public gate ------------------------------------------------------------

def validate_sovereign_boundary(
    raw: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Enforce the layered no-egress boundary. Fail-closed.

    No-op unless ``raw`` declares ``sovereign: {enabled: true}``. When
    enforcement is requested, runs the barrier checks (a / a-diarize / b / c /
    d / e) in order and raises
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
        _check_diarize_local(raw)
        _check_llm_loopback(raw)
        _check_no_cloud_key(raw, env)
        _check_no_egress(raw)
        _check_ingest_web_loopback(raw)
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
        detail="all no-egress barriers held",
    )

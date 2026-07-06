"""Typed config for the Algernon web surface (``web:`` section).

Follows the per-tool config pattern (``load_from_unified`` + ``${VAR}``
substitution), but DELIBERATELY hand-rolls every nested-block construction
instead of routing through a shared ``_build`` / ``_DATACLASS_MAP`` helper.
The dispatch-by-key-name footgun (CLAUDE.md "``_build`` collision footgun")
would bite here: the ``auth`` / ``email`` / ``state`` / ``users`` keys are
exactly the common ones already mapped to OTHER dataclasses in sibling
config modules. Hand-rolling sidesteps the collision class entirely — each
sub-block is constructed explicitly with a schema-tolerance filter
(``__dataclass_fields__``) so an older/newer config with extra keys loads
without crashing (the load-time schema-tolerance contract).

The named-user allowlist (``web.users``) IS the user table — no DB. Each
entry is ``{name, role, email}``. Auth (magic-link / HMAC session token /
Resend sender) is wired in Sub-arc B; this module carries the config those
will read.

Env substitution uses the canonical :func:`alfred._env.substitute_env_in_value`
(NOT a local hand-roll). Its coalesce semantics are load-bearing here: an
env var that is absent OR explicitly empty resolves to the literal
``${VAR}`` placeholder, so :func:`resolve_signing_secret` can fail loud on
BOTH cases (empty + unresolved) rather than silently HMAC-signing tokens
with a placeholder/garbage key. Per ``feedback_substitute_env_consolidation``
+ ``feedback_env_injection_load_bearing``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alfred._env import substitute_env_in_value


def _is_unresolved(value: str | None) -> bool:
    """True when a config string is empty OR an unresolved ``${VAR}``.

    The canonical :func:`alfred._env.substitute_env_in_value` leaves an env
    var that is absent OR set to the empty string as its literal
    ``${VAR}`` placeholder. So "unconfigured" has exactly two surface forms
    — empty string and a leftover ``${...}`` — and this predicate is the
    single place that recognises both. Used by the signing-secret guard
    (fail-loud) and the Resend-creds check (soft-fail → 503).
    """
    return (not value) or value.startswith("${")


# --- Dataclasses -----------------------------------------------------------


@dataclass
class WebUser:
    """One named user in the ``web.users`` allowlist.

    ``name`` is the stable handle (lowercased for lookups + the synthetic
    session id); ``role`` maps to the existing ``run_turn(user_role=...)``
    → ``resolve_scope`` rail (``owner`` / ``ops``); ``email`` is the
    magic-link delivery address (Sub-arc B).
    """

    name: str = ""
    role: str = "owner"
    email: str = ""


@dataclass
class WebAuthConfig:
    """Session + magic-link signing config (consumed in Sub-arc B).

    ``session_secret`` is the instance HMAC signing key (env-substituted).
    Empty is tolerated at load; the auth-use site fails loud when
    ``web.enabled`` and the secret is empty (never sign with an empty key).
    ``base_url`` is the public front-end origin (the cloudflared subdomain)
    used to build magic-link URLs.

    ``mode`` (cross-instance chat, 2026-06-29) selects the identity model
    the ``/chat/*`` handlers use:

    * ``"session"`` (default) — the existing instance-signed
      ``X-Alfred-Session`` token path. The login instance (Salem) mints +
      verifies its own session tokens; ``session_secret`` is REQUIRED.
    * ``"relay"`` — trust-the-relay (mirrors ``/vault/ingest``). The BFF
      authenticates the owner on the login instance, then relays to this
      instance carrying its dedicated ``web`` peer token (Layer 1 authority)
      plus an asserted ``X-Alfred-User`` header (the verified NAME only).
      This instance re-resolves the name against its OWN ``web.users`` to
      derive role + synthetic session id. No session minting / verification
      happens here, so ``session_secret`` is NOT required in relay mode (the
      signing-secret guard is skipped at register + boot).

    An unknown / mistyped value coalesces to the safe ``"session"`` default
    (which then fails loud at the signing-secret guard if no secret is set,
    so a typo'd-relay instance fails closed rather than serving an
    unverified surface).
    """

    session_secret: str = ""
    session_ttl_hours: int = 168
    magic_link_ttl_minutes: int = 15
    base_url: str = ""
    mode: str = "session"


@dataclass
class WebEmailConfig:
    """Outbound email (Resend) config for magic-link delivery (Sub-arc B).

    Missing ``api_key`` / ``from_address`` is a deliberate soft-fail at the
    send site (log + 503), never a crash — so the chat surface proceeds
    without email wired.
    """

    provider: str = "resend"
    api_key: str = ""
    from_address: str = ""


@dataclass
class VoiceIceConfig:
    """ICE knobs for the V0 WebRTC voice surface (``web.voice.ice``).

    * ``advertised_ip`` — when set, the answer SDP's ``a=candidate ... typ
      host`` connection addresses are rewritten to this IP (1:1-NAT deploys
      where the box's on-interface address is private but a public IP
      forwards to it). Empty = no rewrite (the OVH box carries its public IP
      on-interface, so host candidates are already correct — leave unset).
      APPLIED in V0 (pure SDP rewrite, see ``voice_session``).
    * ``stun_servers`` — STUN URLs applied to the server RTCConfiguration AND
      served to the browser via ``GET /voice/config``. Empty = host-candidate
      only (direct UDP; sufficient when the server has a public IP).
    * ``udp_port_range`` — RESERVED (``"min-max"``). aioice has no port-range
      knob (aiortc#487), so this is accepted-and-logged-UNAPPLIED
      (``web.voice.ice_option_unapplied``) — never a silent no-op.
    """

    advertised_ip: str = ""
    stun_servers: list[str] = field(default_factory=list)
    udp_port_range: str = ""


@dataclass
class WebVoiceSttConfig:
    """Streaming-STT config for the V1 assistant pipeline (``web.voice.stt``).

    Required when ``pipeline: assistant`` — the mount gate fails closed (no
    mount, loud log) if ``provider`` is empty/unknown, or if ``provider:
    deepgram`` and ``api_key`` is unresolved. ``provider: fake`` needs no key
    (the keyless dev-box + unit-test path). ``endpointing_ms`` /
    ``utterance_end_ms`` / ``sample_rate`` are clamped at mount by
    ``normalize_stt_settings`` (``web.voice.stt.config_clamped`` on any clamp).
    ``smart_format`` is a config field (contract §1.8) so it can be tuned off
    (→ punctuate=true) without a code change if it delays finals on real audio.
    """

    provider: str = ""            # "" = unconfigured; "deepgram" | "fake"
    api_key: str = ""
    model: str = "nova-3"
    language: str = "en"
    sample_rate: int = 16000      # V1: 16000 only (else clamps)
    endpointing_ms: int = 300     # speech_final silence window; clamp [10,5000]
    utterance_end_ms: int = 1000  # word-gap fallback; 0=off; clamp [1000,5000]
    min_utterance_chars: int = 3  # EOU noise floor (mirrors talker stt)
    smart_format: bool = True


@dataclass
class WebVoiceTtsConfig:
    """Streaming-TTS config for the V2 talk-back plane (``web.voice.tts``).

    ``enabled`` defaults False → the V1 path is byte-identical (stock silence
    outbound source, no TTS worker). Invalid config DEGRADES voice to text-only
    (``web.voice.disabled_tts``) — it NEVER unmounts ``/voice/*`` (TTS is an
    enhancement, unlike STT which is the product; contract §1.4/§1.13).

    ``provider`` is lowercased/stripped but NOT coalesced on unknown values so
    the mount gate can log the raw typo. ``elevenlabs`` needs a resolved
    ``api_key``; ``fake`` needs no key (keyless dev/test). ``output_format`` /
    ``max_tts_chars_per_turn`` / ``max_buffer_seconds`` / ``inactivity_timeout_s``
    are clamped at mount by ``normalize_tts_settings``.

    EGRESS (contract §1.14): enabling this streams every assistant VOICE REPLY
    (only the reply sentence chunks — never system prompt / vault context /
    user text) to the TTS provider. A per-instance decision — Salem accepted;
    VERA / sovereign instances must NOT enable it.
    """

    enabled: bool = False
    provider: str = ""                  # "" = unconfigured; "elevenlabs" | "fake"
    api_key: str = ""                   # ${ELEVENLABS_API_KEY}
    model: str = "eleven_flash_v2_5"
    voice: str = "Rachel"               # friendly name or raw id (telegram.tts.resolve_voice_id)
    output_format: str = "pcm_24000"    # clamp → pcm_16000|22050|24000|44100
    auto_mode: bool = True              # sentence-triggered generation
    max_tts_chars_per_turn: int = 4096  # per-turn spoken cap; clamp [200,20000]
    max_buffer_seconds: int = 30        # playout backpressure ceiling; clamp [5,120]
    inactivity_timeout_s: int = 180     # ElevenLabs idle window; clamp [20,180]
    zero_retention: bool = False        # True → enable_logging=false (plan-gated)


@dataclass
class WebVoiceConfig:
    """Typed config for the WebRTC voice surface (``web.voice``).

    ``enabled`` defaults False — an absent or disabled ``voice:`` block means
    the ``/voice/*`` routes are NOT mounted (opt-in inertness; the route
    table is byte-identical for every instance that doesn't opt in). All
    timeouts live here (NOT as module constants) so a deploy can tune them
    without a code change. ``pipeline`` is an enum — ``"echo"`` (V0 default,
    hear-yourself) or ``"assistant"`` (V1 streaming STT → text reply); any
    other value fails closed (routes NOT mounted, loud log), so a typo can
    never silently serve an unintended pipeline. ``assistant`` additionally
    requires a usable ``stt`` block.
    """

    enabled: bool = False
    max_sessions: int = 2
    pipeline: str = "echo"
    offer_timeout_seconds: int = 10
    connect_deadline_seconds: int = 30
    idle_timeout_seconds: int = 120
    max_session_seconds: int = 1800
    # Assistant pipeline: a connected session with ZERO stt finals for this
    # window closes with reason=no_speech (contract §1.6) — idle_timeout only
    # fires post-disconnect, so without this an abandoned CONNECTED tab
    # streams billable silence for the full max_session_seconds.
    no_speech_close_s: int = 600
    # Reaper sweep cadence. Not in the documented example block (advanced
    # knob) but config-overridable per §4 "REAPER_INTERVAL from config or
    # 15s"; schema-tolerant so an older config without it still loads.
    reaper_interval_seconds: int = 15
    ice: VoiceIceConfig = field(default_factory=VoiceIceConfig)
    stt: WebVoiceSttConfig = field(default_factory=WebVoiceSttConfig)
    tts: WebVoiceTtsConfig = field(default_factory=WebVoiceTtsConfig)


@dataclass
class WebConfig:
    """Typed config for the ``web:`` section.

    ``enabled`` defaults False — an absent or disabled ``web:`` block means
    no web routes are mounted (opt-in inertness; the transport server is
    byte-unchanged for every instance that doesn't opt in).
    """

    enabled: bool = False
    users: list[WebUser] = field(default_factory=list)
    auth: WebAuthConfig = field(default_factory=WebAuthConfig)
    email: WebEmailConfig = field(default_factory=WebEmailConfig)
    voice: WebVoiceConfig = field(default_factory=WebVoiceConfig)
    # Tool-scoped state path for the single-use magic-link nonce store
    # (per the load() schema-tolerance contract's "default state paths must
    # be tool-scoped" rule). Overridable per-instance.
    state_path: str = "./data/web_auth_state.json"


# --- Hand-rolled construction ----------------------------------------------


def _build_users(raw: Any) -> list[WebUser]:
    """Build the ``users`` allowlist, skipping malformed / nameless entries.

    Each entry must be a dict carrying a non-empty ``name``. A nameless or
    non-dict entry is dropped (it can never be matched/authenticated) rather
    than constructing a blank user that would silently shadow nobody.
    """
    out: list[WebUser] = []
    if not isinstance(raw, list):
        return out
    known = WebUser.__dataclass_fields__
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        filtered = {k: v for k, v in entry.items() if k in known}
        name = str(filtered.get("name", "") or "").strip()
        if not name:
            continue
        out.append(
            WebUser(
                name=name,
                role=str(filtered.get("role", "owner") or "owner"),
                email=str(filtered.get("email", "") or ""),
            )
        )
    return out


def _build_auth(raw: Any) -> WebAuthConfig:
    """Hand-roll ``WebAuthConfig`` with a schema-tolerance filter."""
    if not isinstance(raw, dict):
        return WebAuthConfig()
    known = WebAuthConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    defaults = WebAuthConfig()
    # Validate ``mode`` ∈ {session, relay}; an unknown / mistyped value
    # coalesces to the safe ``session`` default (fail-closed — a typo'd
    # relay instance then trips the signing-secret guard instead of serving
    # an unverified surface).
    mode_raw = str(filtered.get("mode", defaults.mode) or defaults.mode).strip().lower()
    mode = mode_raw if mode_raw in {"session", "relay"} else defaults.mode
    return WebAuthConfig(
        session_secret=str(filtered.get("session_secret", "") or ""),
        session_ttl_hours=_int(
            filtered.get("session_ttl_hours"), defaults.session_ttl_hours
        ),
        magic_link_ttl_minutes=_int(
            filtered.get("magic_link_ttl_minutes"),
            defaults.magic_link_ttl_minutes,
        ),
        base_url=str(filtered.get("base_url", "") or ""),
        mode=mode,
    )


def _build_email(raw: Any) -> WebEmailConfig:
    """Hand-roll ``WebEmailConfig`` with a schema-tolerance filter."""
    if not isinstance(raw, dict):
        return WebEmailConfig()
    known = WebEmailConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    defaults = WebEmailConfig()
    return WebEmailConfig(
        provider=str(filtered.get("provider", defaults.provider)
                     or defaults.provider),
        api_key=str(filtered.get("api_key", "") or ""),
        from_address=str(filtered.get("from_address", "") or ""),
    )


def _str_list(raw: Any) -> list[str]:
    """Coerce a config value to a list of non-empty strings.

    Drops non-str / blank entries (a malformed STUN entry can never be a
    usable ICE server URL) rather than constructing a garbage list.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
    return out


def _build_voice_ice(raw: Any) -> VoiceIceConfig:
    """Hand-roll ``VoiceIceConfig`` with a schema-tolerance filter."""
    if not isinstance(raw, dict):
        return VoiceIceConfig()
    known = VoiceIceConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    return VoiceIceConfig(
        advertised_ip=str(filtered.get("advertised_ip", "") or ""),
        stun_servers=_str_list(filtered.get("stun_servers")),
        udp_port_range=str(filtered.get("udp_port_range", "") or ""),
    )


def _build_voice_stt(raw: Any) -> WebVoiceSttConfig:
    """Hand-roll ``WebVoiceSttConfig`` with a schema-tolerance filter.

    Mirrors ``_build_voice_ice`` — dict guard, ``__dataclass_fields__``
    filter, ``_int`` coercions. ``provider`` is lowercased/stripped but NOT
    coalesced on unknown values, so the mount gate can log the raw typo.
    ``api_key`` env substitution rides the module-wide ``substitute_env_in_value``
    already applied at ``load_from_unified``; ``_is_unresolved`` recognises an
    unset/empty ``${DEEPGRAM_API_KEY}`` at the mount gate.
    """
    if not isinstance(raw, dict):
        return WebVoiceSttConfig()
    known = WebVoiceSttConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    defaults = WebVoiceSttConfig()
    return WebVoiceSttConfig(
        provider=str(filtered.get("provider", "") or "").strip().lower(),
        api_key=str(filtered.get("api_key", "") or ""),
        model=str(filtered.get("model", defaults.model) or defaults.model),
        language=str(filtered.get("language", defaults.language)
                     or defaults.language),
        sample_rate=_int(filtered.get("sample_rate"), defaults.sample_rate),
        endpointing_ms=_int(filtered.get("endpointing_ms"), defaults.endpointing_ms),
        utterance_end_ms=_int(
            filtered.get("utterance_end_ms"), defaults.utterance_end_ms,
        ),
        min_utterance_chars=_int(
            filtered.get("min_utterance_chars"), defaults.min_utterance_chars,
        ),
        smart_format=bool(filtered.get("smart_format", defaults.smart_format)),
    )


def _build_voice_tts(raw: Any) -> WebVoiceTtsConfig:
    """Hand-roll ``WebVoiceTtsConfig`` with a schema-tolerance filter.

    Mirrors ``_build_voice_stt`` — dict guard, ``__dataclass_fields__`` filter,
    ``_int`` / ``bool`` coercions. ``provider`` is lowercased/stripped but NOT
    coalesced on unknown values so the mount gate can log the raw typo.
    ``api_key`` env substitution rides the module-wide ``substitute_env_in_value``
    already applied at ``load_from_unified``; ``_is_unresolved`` recognises an
    unset/empty ``${ELEVENLABS_API_KEY}`` at the mount gate.
    """
    if not isinstance(raw, dict):
        return WebVoiceTtsConfig()
    known = WebVoiceTtsConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    defaults = WebVoiceTtsConfig()
    return WebVoiceTtsConfig(
        enabled=bool(filtered.get("enabled", False)),
        provider=str(filtered.get("provider", "") or "").strip().lower(),
        api_key=str(filtered.get("api_key", "") or ""),
        model=str(filtered.get("model", defaults.model) or defaults.model),
        voice=str(filtered.get("voice", defaults.voice) or defaults.voice),
        output_format=str(filtered.get("output_format", defaults.output_format)
                          or defaults.output_format).strip().lower(),
        auto_mode=bool(filtered.get("auto_mode", defaults.auto_mode)),
        max_tts_chars_per_turn=_int(
            filtered.get("max_tts_chars_per_turn"),
            defaults.max_tts_chars_per_turn,
        ),
        max_buffer_seconds=_int(
            filtered.get("max_buffer_seconds"), defaults.max_buffer_seconds,
        ),
        inactivity_timeout_s=_int(
            filtered.get("inactivity_timeout_s"), defaults.inactivity_timeout_s,
        ),
        zero_retention=bool(filtered.get("zero_retention", defaults.zero_retention)),
    )


def _build_voice(raw: Any) -> WebVoiceConfig:
    """Hand-roll ``WebVoiceConfig`` with a schema-tolerance filter.

    Mirrors ``_build_auth`` — isinstance-dict guard, ``__dataclass_fields__``
    filter, ``_int`` coercion for the timeouts / cap, and nested hand-rolled
    ``ice`` + ``stt`` + ``tts`` blocks (NO ``_build`` / ``_DATACLASS_MAP`` —
    this module hand-rolls everything to sidestep the key-name collision
    footgun).
    """
    if not isinstance(raw, dict):
        return WebVoiceConfig()
    known = WebVoiceConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    defaults = WebVoiceConfig()
    return WebVoiceConfig(
        enabled=bool(filtered.get("enabled", False)),
        max_sessions=_int(filtered.get("max_sessions"), defaults.max_sessions),
        pipeline=str(filtered.get("pipeline", defaults.pipeline)
                     or defaults.pipeline),
        offer_timeout_seconds=_int(
            filtered.get("offer_timeout_seconds"),
            defaults.offer_timeout_seconds,
        ),
        connect_deadline_seconds=_int(
            filtered.get("connect_deadline_seconds"),
            defaults.connect_deadline_seconds,
        ),
        idle_timeout_seconds=_int(
            filtered.get("idle_timeout_seconds"),
            defaults.idle_timeout_seconds,
        ),
        max_session_seconds=_int(
            filtered.get("max_session_seconds"),
            defaults.max_session_seconds,
        ),
        no_speech_close_s=_int(
            filtered.get("no_speech_close_s"),
            defaults.no_speech_close_s,
        ),
        reaper_interval_seconds=_int(
            filtered.get("reaper_interval_seconds"),
            defaults.reaper_interval_seconds,
        ),
        ice=_build_voice_ice(filtered.get("ice")),
        stt=_build_voice_stt(filtered.get("stt")),
        tts=_build_voice_tts(filtered.get("tts")),
    )


def _int(value: Any, default: int) -> int:
    """Coerce to int, falling back to ``default`` on None / bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_from_unified(raw: dict[str, Any]) -> WebConfig:
    """Build :class:`WebConfig` from a pre-loaded unified config dict.

    Extracts the ``web`` section. Returns an all-default (disabled) config
    when the section is absent — which the daemon treats as "do not mount
    web routes" (opt-in inertness).
    """
    raw = substitute_env_in_value(raw or {})
    section = raw.get("web", {}) or {}
    if not isinstance(section, dict):
        section = {}
    return WebConfig(
        enabled=bool(section.get("enabled", False)),
        users=_build_users(section.get("users")),
        auth=_build_auth(section.get("auth")),
        email=_build_email(section.get("email")),
        voice=_build_voice(section.get("voice")),
        state_path=str(
            section.get("state_path", "./data/web_auth_state.json")
            or "./data/web_auth_state.json"
        ),
    )


def resolve_signing_secret(auth: WebAuthConfig) -> str:
    """Return the validated HMAC signing secret, or fail loud.

    Raises :class:`ValueError` when the secret is empty OR an unresolved
    ``${VAR}`` placeholder (env var absent or set to empty — both coalesce
    to the literal placeholder via the canonical substituter). Never sign
    web session / magic-link tokens with an empty or placeholder key: a
    silent garbage key would either mint tokens nobody can verify or, worse,
    make forgery trivial depending on the bug. Fail-loud at the use site is
    the only safe behaviour — this is the load-bearing reason the WARN fix
    migrated to the coalesce-to-literal env semantics.

    Actual call sites (kept honest — comment-lies-about-behavior class):
    (1) ``require_web_session`` / the auth token codec, before any
    sign/verify; (2) ``register_web_routes``' startup guard; and (3) the
    talker daemon's web-wiring boot check (``daemon.py``), gated on
    ``web.enabled``. Sites (2)+(3) mean an enabled-but-unconfigured instance
    refuses to mount the web surface at startup (fail-closed — loud
    ``web_secret_unconfigured`` error; the talker daemon continues serving
    Telegram/transport), rather than booting clean and dying at first login.
    Web is opt-in — the misconfig disables only the web surface.
    """
    secret = auth.session_secret or ""
    if _is_unresolved(secret):
        raise ValueError(
            "web.auth.session_secret is unset or unresolved (empty or a "
            "literal ${...} placeholder) — refusing to sign web tokens with "
            "an empty/placeholder key. Set ALFRED_WEB_SESSION_SECRET (or "
            "web.auth.session_secret) to a strong random value."
        )
    return secret

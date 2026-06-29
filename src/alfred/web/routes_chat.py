"""Web chat routes — a second adapter onto the ``run_turn`` engine.

These routes mount on the EXISTING transport aiohttp app (inside the
talker daemon). They build the exact same args the Telegram caller builds
(``bot.py``'s ``run_turn`` call site) and ``await run_turn(...)`` — so the
engine behaviour is byte-identical to Telegram: same scope-enforced vault
bridge, same system blocks, same tool loop.

Route surface (M1, non-streaming):

    POST /chat/open                  → { session_key }
    POST /chat/turn                  → { reply, session_key }
    GET  /chat/history/{session_key} → { turns: [...] }

Auth layering: every non-``/health`` route is already gated by the
transport ``auth_middleware`` (Layer 1, peer token — "this front-end may
talk to me"). Sub-arc A resolves the *user* identity from a client-supplied
name (spoofable — curl/local-only, never public). Sub-arc B replaces
``_resolve_request_identity`` with ``require_web_session`` (Layer 2, an
instance-signed session token) without touching the rest of this module.

Opt-in inertness: :func:`register_web_routes` mounts NOTHING when the
``web`` config is absent / disabled — the transport server stays
byte-unchanged for every instance that doesn't opt in (M1 = Salem only).
"""

from __future__ import annotations

from typing import Any, Callable

from aiohttp import web

from .config import WebConfig
from .identity import (
    WebIdentity,
    check_synthetic_id_collisions,
    resolve_identity_from_name,
)
from .utils import get_logger

log = get_logger(__name__)


# Application storage keys — namespaced so they never collide with the
# transport's own ``transport.*`` keys on the shared Application.
_KEY_WEB_CONFIG = "web.config"
_KEY_WEB_ANTHROPIC = "web.anthropic_client"
_KEY_WEB_STATE_MGR = "web.state_mgr"
_KEY_WEB_TALKER_CONFIG = "web.talker_config"
_KEY_WEB_SYSTEM_PROVIDER = "web.system_prompt_provider"
_KEY_WEB_VAULT_CTX = "web.vault_context_str"

# Sub-arc A identity header (spoofable, peer-gated, curl/local-only). The
# body ``user`` field is preferred for POSTs; this header is the fallback
# that also works for the GET history route. Replaced by an instance-signed
# session token (``X-Alfred-Session``) in Sub-arc B.
_HEADER_WEB_USER = "X-Web-User"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _flatten_transcript_for_web(
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project a session transcript to the web ``history`` shape.

    Keeps only ``user`` / ``assistant`` turns and surfaces their TEXT —
    ``tool_use`` / ``tool_result`` / image blocks are flattened OUT (the
    web view shows the conversation, not the engine's tool plumbing). A
    turn with no surfaced text (a pure tool turn) is dropped entirely.

    Each output turn is ``{role, text, ts}`` where ``ts`` is the turn's
    ``_ts`` stamp (empty string when absent — pre-stamp records).
    """
    out: list[dict[str, Any]] = []
    for turn in transcript:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if role not in ("user", "assistant"):
            continue
        content = turn.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p)
        if not text:
            continue
        out.append({"role": role, "text": text, "ts": turn.get("_ts", "")})
    return out


def _resolve_request_identity(
    request: web.Request,
    web_config: WebConfig,
    body: dict[str, Any] | None,
) -> WebIdentity | None:
    """Resolve the driving user for this request (Sub-arc A path).

    Identity comes from (in order) the request body ``user`` field, the
    ``?user=`` query param, or the ``X-Web-User`` header — whichever is
    present. All three are client-supplied and spoofable; this path is
    peer-gated and curl/local-only until Sub-arc B's signed session token
    replaces it. Returns ``None`` (→ fail-closed 403) when no name resolves
    against the allowlist.
    """
    name: str | None = None
    if isinstance(body, dict):
        raw = body.get("user")
        if isinstance(raw, str):
            name = raw
    if not name:
        name = request.query.get("user") or request.headers.get(_HEADER_WEB_USER)
    return resolve_identity_from_name(web_config, name)


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    """Best-effort JSON body read; returns ``{}`` on empty / invalid body."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → treat as empty
        return {}
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_chat_open(request: web.Request) -> web.StreamResponse:
    """POST /chat/open — close any prior session, open a fresh one.

    Mirrors Telegram's close-then-open so the prior transcript is archived
    as a ``session/`` record before the new session starts. Closing is
    best-effort: a failure to archive is logged and does NOT block opening
    the fresh session (the user must not be wedged out of chat by a stale
    record write).
    """
    web_config: WebConfig = request.app[_KEY_WEB_CONFIG]
    state_mgr = request.app[_KEY_WEB_STATE_MGR]
    talker_config = request.app[_KEY_WEB_TALKER_CONFIG]

    body = await _read_json_body(request)
    identity = _resolve_request_identity(request, web_config, body)
    if identity is None:
        return web.json_response({"error": "unknown_user"}, status=403)

    # Lazy imports — the session module pulls vault ops (heavy) only when a
    # request actually fires, keeping this module import-light for tests.
    from alfred.telegram.session import close_session, open_session

    existing = state_mgr.get_active(identity.synthetic_chat_id)
    if existing:
        try:
            primary_users = getattr(talker_config, "primary_users", None) or []
            close_session(
                state_mgr,
                vault_path_root=talker_config.vault.path,
                chat_id=identity.synthetic_chat_id,
                reason="web_session_reopened",
                user_vault_path=primary_users[0] if primary_users else None,
                stt_model_used="",
                session_type=existing.get("_session_type") or "conversation",
                tool_set=talker_config.instance.tool_set,
            )
        except Exception as exc:  # noqa: BLE001 — archival is best-effort
            log.warning(
                "web.chat.prior_session_close_failed",
                user=identity.user,
                synthetic_chat_id=identity.synthetic_chat_id,
                error=str(exc),
                error_type=type(exc).__name__,
                detail="proceeding to open a fresh session anyway",
            )

    session_obj = open_session(
        state_mgr,
        identity.synthetic_chat_id,
        model=talker_config.anthropic.model,
    )
    log.info(
        "web.chat.session_opened",
        user=identity.user,
        synthetic_chat_id=identity.synthetic_chat_id,
        session_id=session_obj.session_id,
        model=talker_config.anthropic.model,
    )
    return web.json_response({"session_key": session_obj.session_id})


async def _handle_chat_turn(request: web.Request) -> web.StreamResponse:
    """POST /chat/turn — run one user turn through ``run_turn``.

    Assembles the same args ``bot.py`` builds and returns the assistant's
    final text (non-streaming). The engine appends turns + persists vault
    mutations internally, exactly as for Telegram.
    """
    web_config: WebConfig = request.app[_KEY_WEB_CONFIG]
    client = request.app[_KEY_WEB_ANTHROPIC]
    state_mgr = request.app[_KEY_WEB_STATE_MGR]
    talker_config = request.app[_KEY_WEB_TALKER_CONFIG]
    system_prompt_provider: Callable[[], str] = request.app[_KEY_WEB_SYSTEM_PROVIDER]
    vault_context_str: str = request.app[_KEY_WEB_VAULT_CTX]

    body = await _read_json_body(request)
    identity = _resolve_request_identity(request, web_config, body)
    if identity is None:
        return web.json_response({"error": "unknown_user"}, status=403)

    session_key = body.get("session_key")
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message_required"}, status=400)
    # Lenient kind coercion: anything other than "voice" is "text" (kind
    # only tags the user turn's ``_kind`` counter; it never gates behaviour).
    kind = "voice" if body.get("kind") == "voice" else "text"

    active_dict = state_mgr.get_active(identity.synthetic_chat_id)
    if active_dict is None or active_dict.get("session_id") != session_key:
        return web.json_response({"error": "no_such_session"}, status=404)

    from alfred.telegram.conversation import run_turn
    from alfred.telegram.session import Session

    session_obj = Session.from_dict(active_dict)

    # ``user_name`` only when the instance is multi-user — parity with the
    # Telegram ``_name_for`` path. On a single-user instance (the common M1
    # case) it stays None so the sender-identity system block is omitted and
    # the system blocks are byte-identical to Telegram.
    user_name = identity.user if len(web_config.users) > 1 else None

    try:
        reply = await run_turn(
            client=client,
            state=state_mgr,
            session=session_obj,
            user_message=message,
            config=talker_config,
            vault_context_str=vault_context_str,
            system_prompt=system_prompt_provider(),
            user_kind=kind,
            user_role=identity.role,
            user_name=user_name,
        )
    except Exception as exc:  # noqa: BLE001 — surface engine errors as 502
        log.warning(
            "web.chat.engine_error",
            user=identity.user,
            session_key=session_key,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return web.json_response(
            {"error": "engine_error", "detail": str(exc)},
            status=502,
        )

    log.info(
        "web.chat.turn_complete",
        user=identity.user,
        session_key=session_key,
        user_kind=kind,
        reply_chars=len(reply or ""),
    )
    return web.json_response({"reply": reply, "session_key": session_key})


async def _handle_chat_history(request: web.Request) -> web.StreamResponse:
    """GET /chat/history/{session_key} — current active session transcript.

    M1 surfaces the CURRENT active session only (closed-session / vault-
    record history is a later milestone). Tool plumbing is flattened out.
    """
    web_config: WebConfig = request.app[_KEY_WEB_CONFIG]
    state_mgr = request.app[_KEY_WEB_STATE_MGR]

    identity = _resolve_request_identity(request, web_config, None)
    if identity is None:
        return web.json_response({"error": "unknown_user"}, status=403)

    session_key = request.match_info.get("session_key", "")
    active_dict = state_mgr.get_active(identity.synthetic_chat_id)
    if active_dict is None or active_dict.get("session_id") != session_key:
        return web.json_response({"error": "no_such_session"}, status=404)

    transcript = active_dict.get("transcript") or []
    turns = _flatten_transcript_for_web(transcript)
    if not turns:
        # Intentionally-left-blank: an empty history is "ran, nothing to
        # surface", observably distinct from a broken read.
        log.info(
            "web.chat.history_empty",
            user=identity.user,
            session_key=session_key,
        )
    return web.json_response({"turns": turns})


# ---------------------------------------------------------------------------
# Registration / wiring
# ---------------------------------------------------------------------------


def register_web_routes(
    app: web.Application,
    *,
    web_config: WebConfig | None,
    anthropic_client: Any,
    state_mgr: Any,
    talker_config: Any,
    system_prompt_provider: Callable[[], str],
    vault_context_str: str,
    allowed_user_ids: "list[int] | None" = None,
) -> bool:
    """Mount the web chat routes onto ``app`` — IFF web is enabled.

    Returns ``True`` when routes were mounted, ``False`` when the web
    surface is absent / disabled (opt-in inertness: nothing is registered
    and the transport server is byte-unchanged). Must be called BEFORE the
    app is started (aiohttp forbids route additions on a started app);
    the daemon calls it adjacent to ``wire_transport_app``, the same
    pre-start window.

    Runs the synthetic-id collision guard (fail-loud) before stashing any
    runtime deps — a colliding mapping aborts startup rather than silently
    cross-wiring two users' sessions.
    """
    if web_config is None or not web_config.enabled:
        # Intentionally-left-blank: disabled is a deliberate state, logged
        # so "no web routes" is distinguishable from "wiring silently
        # skipped".
        log.info(
            "web.routes.disabled",
            reason="web config absent or web.enabled=false",
        )
        return False

    # Fail-loud collision guard — provable, not probable (see identity.py).
    mapping = check_synthetic_id_collisions(
        web_config.users, allowed_user_ids or []
    )
    log.info(
        "web.routes.collision_check_clean",
        users=len(web_config.users),
        synthetic_ids=sorted(mapping.values()),
    )

    app[_KEY_WEB_CONFIG] = web_config
    app[_KEY_WEB_ANTHROPIC] = anthropic_client
    app[_KEY_WEB_STATE_MGR] = state_mgr
    app[_KEY_WEB_TALKER_CONFIG] = talker_config
    app[_KEY_WEB_SYSTEM_PROVIDER] = system_prompt_provider
    app[_KEY_WEB_VAULT_CTX] = vault_context_str

    app.router.add_post("/chat/open", _handle_chat_open)
    app.router.add_post("/chat/turn", _handle_chat_turn)
    app.router.add_get("/chat/history/{session_key}", _handle_chat_history)

    log.info(
        "web.routes.registered",
        users=len(web_config.users),
        routes=["/chat/open", "/chat/turn", "/chat/history/{session_key}"],
    )
    return True

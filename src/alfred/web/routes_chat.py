"""Web chat routes — a second adapter onto the ``run_turn`` engine.

These routes mount on the EXISTING transport aiohttp app (inside the
talker daemon). They build the exact same args the Telegram caller builds
(``bot.py``'s ``run_turn`` call site) and ``await run_turn(...)`` — so the
engine behaviour is byte-identical to Telegram: same scope-enforced vault
bridge, same system blocks, same tool loop. Non-streaming.

Route surface (M1, non-streaming):

    POST /chat/open                  → { session_key }
    POST /chat/turn                  → { reply, session_key }
    GET  /chat/history/{session_key} → { turns: [...] }

Auth layering: every non-``/health`` route is gated by the transport
``auth_middleware`` (Layer 1, peer token — "this front-end may talk to
me"). Layer 2 resolves the *verified named user* via the mode-aware
:func:`alfred.web.auth.resolve_web_identity`, fail-closed 401:

* ``session`` mode (the login instance, e.g. Salem) — an instance-signed
  ``X-Alfred-Session`` token (``require_web_session``).
* ``relay`` mode (cross-instance targets, e.g. KAL-LE / Hypatia / VERA) —
  an asserted ``X-Alfred-User`` header (verified NAME only, gated by the
  Layer-1 ``web`` peer token), re-resolved against THIS instance's own
  ``web.users``. Mirrors the ``/vault/ingest`` relay-auth model.

M1 deferral (NOTE-1): web turns do NOT inject ``calibration_str`` /
``pushback_level`` — those are populated by the Telegram session-type
router at open (``_calibration_snapshot`` / ``_pushback_level`` on the
active dict), which is out of M1 scope, and calibration is keyed to a
per-user person-record path that ``web.users`` don't carry. Web chat thus
lacks operator voice-calibration + challenge-tuning until a later
milestone — flagged so the capability audit doesn't claim parity it
doesn't have.

Opt-in inertness: :func:`register_web_routes` mounts NOTHING when the
``web`` config is absent / disabled — the transport server stays
byte-unchanged for every instance that doesn't opt in (M1 = Salem only).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from typing import Any, Callable

from aiohttp import web

from alfred.vault.scope import RRTS_INTAKE_ROLE

from .auth import resolve_web_identity
from .config import WebConfig, resolve_signing_secret
from .identity import check_synthetic_id_collisions
from .keys import (
    KEY_WEB_ANTHROPIC,
    KEY_WEB_AUTH_STATE,
    KEY_WEB_CONFIG,
    KEY_WEB_INFLIGHT,
    KEY_WEB_STATE_MGR,
    KEY_WEB_SYSTEM_PROVIDER,
    KEY_WEB_TALKER_CONFIG,
    KEY_WEB_VAULT_CTX,
)
from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Image-carry (2026-06-29, RRTS bug-report → VERA lane) — the §9.6 wire schema
# ---------------------------------------------------------------------------
#
# A chat turn body MAY carry an optional ``images`` field so the Honeydew
# screenshot reaches VERA's vision (text-only today). The wire schema
# (ratified here, published to worksplit §9.6):
#
#     {
#       "session_key": "...", "message": "...", "kind": "text",
#       "images": [
#         { "media_type": "image/png", "data": "<base64>" }
#       ]
#     }
#
# * ``images`` is OPTIONAL — absent / empty → text-only (byte-identical to
#   the pre-feature path).
# * Each entry: ``media_type`` ∈ ALLOWED_IMAGE_MEDIA_TYPES, ``data`` is the
#   base64-encoded image bytes (NO ``data:`` URI prefix).
# * Per-image decoded-size cap MAX_IMAGE_BYTES; per-turn count cap
#   MAX_IMAGES_PER_TURN. Validation returns a 400 ``{"error":"image_invalid"}``
#   (BEFORE the SSE stream opens, on /chat/stream).
# * Intake-only: the image reaches VERA's vision + is persisted to VERA's
#   own inbox (sovereign audit trail). It is NOT egressed (de-PHI/egress is
#   out of scope for this arc).
#
# Anthropic's vision API accepts jpeg / png / gif / webp (per
# telegram/vision.py::DEFAULT_TELEGRAM_PHOTO_MIME context). The Anthropic
# Messages API caps a single base64 image at ~5 MB, so 5 MiB is the per-image
# decoded bound; a Honeydew page screenshot is well under that.
ALLOWED_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})
MAX_IMAGE_BYTES: int = 5 * 1024 * 1024  # 5 MiB decoded, per image
MAX_IMAGES_PER_TURN: int = 4
_IMAGE_EXT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


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


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    """Best-effort JSON body read; returns ``{}`` on empty / invalid body."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → treat as empty
        return {}
    return body if isinstance(body, dict) else {}


def _parse_image_blocks(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, list[tuple[str, bytes]], str | None]:
    """Parse + validate the optional ``images`` field on a chat turn body.

    Returns ``(image_blocks, raws, error)``:

    * ``image_blocks`` — a list of Anthropic vision content blocks (built via
      ``telegram.vision.build_image_block``, reused — not reinvented), or
      ``None`` when no images were carried (text-only turn).
    * ``raws`` — ``[(media_type, raw_bytes), ...]`` for the inbox-persist
      pass (the sovereign audit trail). Empty when no images.
    * ``error`` — a human-readable validation message, or ``None`` on
      success. The handler turns a non-None error into a 400.

    Validation (fail-loud, never silently drop a screenshot): ``images`` must
    be a list; each entry a dict with a known ``media_type`` and a non-empty,
    valid base64 ``data`` string decoding to >0 and <= MAX_IMAGE_BYTES bytes;
    at most MAX_IMAGES_PER_TURN entries.
    """
    from alfred.telegram import vision

    images = body.get("images")
    if images is None:
        return None, [], None
    if not isinstance(images, list):
        return None, [], "images must be a list of {media_type, data} objects"
    if not images:
        return None, [], None
    if len(images) > MAX_IMAGES_PER_TURN:
        return None, [], (
            f"too many images ({len(images)}); max {MAX_IMAGES_PER_TURN} "
            f"per turn"
        )

    blocks: list[dict[str, Any]] = []
    raws: list[tuple[str, bytes]] = []
    for i, item in enumerate(images):
        if not isinstance(item, dict):
            return None, [], f"images[{i}] must be an object"
        media_type = str(item.get("media_type") or "").strip().lower()
        if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
            return None, [], (
                f"images[{i}].media_type must be one of "
                f"{sorted(ALLOWED_IMAGE_MEDIA_TYPES)}; got {media_type!r}"
            )
        data = item.get("data")
        if not isinstance(data, str) or not data.strip():
            return None, [], (
                f"images[{i}].data must be a non-empty base64 string"
            )
        try:
            # ``validate=True`` rejects non-alphabet characters (a bare
            # ``standard_b64decode`` silently DISCARDS them — we want a
            # malformed payload to fail loud as a 400, not decode to junk).
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None, [], f"images[{i}].data is not valid base64"
        if not raw:
            return None, [], f"images[{i}].data decoded to empty bytes"
        if len(raw) > MAX_IMAGE_BYTES:
            return None, [], (
                f"images[{i}] is {len(raw)} bytes; max {MAX_IMAGE_BYTES} "
                f"({MAX_IMAGE_BYTES // (1024 * 1024)} MiB) per image"
            )
        blocks.append(vision.build_image_block(raw, media_type=media_type))
        raws.append((media_type, raw))
    return blocks, raws, None


def _persist_web_images(
    raws: list[tuple[str, bytes]],
    vault_path: str,
    *,
    user: str,
    session_key: str,
) -> None:
    """Persist carried screenshots to VERA's inbox (sovereign audit trail).

    Mirrors the Telegram photo handler's best-effort inbox save (the model
    can still see the image from in-memory bytes even if persistence fails,
    so a save error never blocks the turn). The file_unique_id is a content
    hash so a retransmit dedupes to the same filename. Intake-only — the
    image is stored in VERA's own vault; it is NOT egressed.
    """
    from alfred.telegram import vision

    for media_type, raw in raws:
        ext = _IMAGE_EXT_BY_MEDIA_TYPE.get(media_type, "img")
        unique_id = hashlib.sha256(raw).hexdigest()[:8]
        try:
            vision.save_image_to_inbox(
                raw, vault_path, unique_id, extension=ext,
            )
        except Exception as exc:  # noqa: BLE001 — audit trail is best-effort
            # Intentionally-left-blank: the decision NOT to abort the turn
            # lives in the log (action=...) so an operator tailing the log
            # sees the policy without re-reading source — mirrors the
            # Telegram photo_save_failed contract.
            log.warning(
                "web.chat.image_save_failed",
                user=user,
                session_key=session_key,
                error=str(exc),
                error_type=type(exc).__name__,
                vault_path=vault_path,
                action="continuing_to_llm_in_memory_only",
            )


def _validate_turn_images(
    body: dict[str, Any],
    talker_config: Any,
) -> tuple[
    list[dict[str, Any]] | None,
    list[tuple[str, bytes]],
    tuple[int, dict[str, str]] | None,
]:
    """Vision-gate + parse the optional ``images`` field for a chat turn.

    Shared by ``/chat/turn`` + ``/chat/stream`` so both reject identically.
    Returns ``(image_blocks, raws, error)`` where ``error`` is a
    ``(status, json_payload)`` tuple on failure (the handler returns it
    verbatim) or ``None`` on success. A vision-disabled instance that is
    handed images fails LOUD (400) rather than silently dropping the
    screenshot — mirrors the Telegram vision-disabled gate.
    """
    images_present = (
        isinstance(body.get("images"), list) and bool(body.get("images"))
    )
    vision_enabled = bool(
        getattr(getattr(talker_config, "vision", None), "enabled", True)
    )
    if images_present and not vision_enabled:
        return None, [], (400, {
            "error": "vision_disabled",
            "detail": "this instance has vision disabled; remove the "
                      "images field",
        })
    blocks, raws, err = _parse_image_blocks(body)
    if err is not None:
        return None, [], (400, {"error": "image_invalid", "detail": err})
    return blocks, raws, None


def _build_turn_payload(
    session_obj: Any,
    pre_len: int,
    reply: str,
    session_key: str,
    *,
    deduped: bool = False,
) -> dict[str, Any]:
    """Assemble the post-turn response payload — the SINGLE source of truth.

    Both ``/chat/turn`` (buffered JSON body) and ``/chat/stream``'s terminal
    ``done`` frame build the payload through this helper so the two are
    byte-identical (the frozen contract's final shape arrives either way).

    Reads the per-turn ``_ts`` stamps ``run_turn`` wrote (in place) to
    ``session_obj.transcript`` via ``append_turn``: the assistant turn is
    appended LAST (``transcript[-1]``), the user turn first at ``pre_len``.
    ``pre_len`` MUST be captured BEFORE ``run_turn`` runs. Both stamps
    default to ``""`` so the fields are ALWAYS present (never null/missing),
    mirroring the pre-stamp "" contract ``/chat/history`` already uses.

    ``deduped`` is always present (default ``False``) for shape symmetry
    with the idempotency-dedup fast path.

    RRTS-intake completion signal (2026-06-29, RRTS bug-report → VERA lane):
    when THIS turn filed a ticket under the vouched ``rrts_intake`` scope,
    ``run_turn`` recorded ``{filed, ticket_uid, title}`` on
    ``session_obj.last_filed_ticket`` (cleared at turn start, so it reflects
    this turn only). The extra keys are added ONLY when a ticket was filed —
    a normal turn's payload shape is unchanged. This is the §9.7 synchronous
    completion signal: a LOCAL ticket reference (``ticket_uid``); the GitHub
    issue number does NOT exist at filing time (minted downstream ~15 min
    later) and is intentionally absent here.
    """
    transcript = session_obj.transcript or []
    assistant_ts = transcript[-1].get("_ts", "") if transcript else ""
    user_ts = transcript[pre_len].get("_ts", "") if len(transcript) > pre_len else ""
    payload: dict[str, Any] = {
        "reply": reply,
        "session_key": session_key,
        "ts": assistant_ts,
        "user_ts": user_ts,
        "deduped": deduped,
    }
    filed = getattr(session_obj, "last_filed_ticket", None)
    if isinstance(filed, dict) and filed.get("ticket_uid"):
        payload["filed"] = True
        payload["ticket_uid"] = filed["ticket_uid"]
        payload["title"] = filed.get("title", "")
    return payload


def _user_name_for(identity: Any, web_config: WebConfig) -> str | None:
    """The display name to thread to ``run_turn`` (sender-identity block).

    Threaded when the instance is multi-user (parity with the Telegram
    ``_name_for`` path) OR when the identity is a VOUCHED RRTS reporter
    (``RRTS_INTAKE_ROLE``) — the vouched name IS the ticket ``reporter`` and
    MUST reach ``run_turn`` even though VERA's relay carries an empty
    ``web.users`` roster (vouched = no fixed list, so ``len(users) > 1`` is
    False there). On a single-user session-mode instance it stays ``None``
    so the system blocks are byte-identical to Telegram. (2026-06-29, RRTS
    bug-report → VERA lane.)
    """
    if getattr(identity, "role", "") == RRTS_INTAKE_ROLE:
        return identity.user
    return identity.user if len(web_config.users) > 1 else None


# ---------------------------------------------------------------------------
# Turn idempotency (retry-safe dedup) + concurrent-turn guard
# ---------------------------------------------------------------------------


def _msg_hash(message: str) -> str:
    """Stable hash of a turn's user message (idempotency key-match guard)."""
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _dedup_check(
    session_obj: Any, idempotency_key: str, message: str,
) -> tuple[str | None, dict[str, Any]]:
    """Classify a turn against the session's last-turn idempotency cache.

    Returns one of:
      * ``("hit", cached)`` — same key AND same message → return the cached
        result, do NOT re-run ``run_turn`` (retry-safe; critical for
        vault-writing turns).
      * ``("stale", {})`` — same key but a DIFFERENT message (a client
        reusing a key) → run fresh + warn.
      * ``(None, {})`` — no key / no match → run fresh (normal path).
    """
    if not idempotency_key:
        return None, {}
    if session_obj.last_turn_key != idempotency_key:
        return None, {}
    cached = session_obj.last_turn_result or {}
    if cached.get("msg_hash") == _msg_hash(message):
        return "hit", dict(cached)
    return "stale", {}


def _cached_turn_payload(
    cached: dict[str, Any], session_key: str,
) -> dict[str, Any]:
    """Build the ``deduped: True`` response from a cached turn result.

    Same shape as :func:`_build_turn_payload` (the frozen contract) so a
    deduped reply is indistinguishable on the wire except for the
    ``deduped`` flag.
    """
    return {
        "reply": cached.get("reply", ""),
        "session_key": session_key,
        "ts": cached.get("ts", ""),
        "user_ts": cached.get("user_ts", ""),
        "deduped": True,
    }


# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) — streaming chat turns (Tier-1 keep-alive)
# ---------------------------------------------------------------------------

# Keep-alive heartbeat interval. A long turn (10-23s observed) holds the
# browser↔BFF socket open with no bytes flowing; periodic comment frames
# every KEEPALIVE_SECS keep that leg alive. Module-level so a test can
# patch it to a tiny value without monkeypatching the loop.
KEEPALIVE_SECS = 5.0


async def _sse_write_event(
    resp: web.StreamResponse, event: str, data: dict[str, Any],
) -> None:
    """Write one ``event: <name>\\ndata: <json>\\n\\n`` SSE frame."""
    payload = json.dumps(data, separators=(",", ":"))
    await resp.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))


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
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    state_mgr = request.app[KEY_WEB_STATE_MGR]
    talker_config = request.app[KEY_WEB_TALKER_CONFIG]

    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

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
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    client = request.app[KEY_WEB_ANTHROPIC]
    state_mgr = request.app[KEY_WEB_STATE_MGR]
    talker_config = request.app[KEY_WEB_TALKER_CONFIG]
    system_prompt_provider: Callable[[], str] = request.app[KEY_WEB_SYSTEM_PROVIDER]
    vault_context_str: str = request.app[KEY_WEB_VAULT_CTX]

    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    body = await _read_json_body(request)
    session_key = body.get("session_key")
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message_required"}, status=400)
    # Lenient kind coercion: anything other than "voice" is "text" (kind
    # only tags the user turn's ``_kind`` counter; it never gates behaviour).
    kind = "voice" if body.get("kind") == "voice" else "text"
    idempotency_key = body.get("idempotency_key")
    idempotency_key = idempotency_key if isinstance(idempotency_key, str) else ""

    # Image-carry (optional) — parse + validate BEFORE run_turn so a bad
    # screenshot is a 400, not a half-run turn. Vision-disabled instances
    # fail loud when images are carried (mirrors the Telegram vision gate).
    image_blocks, image_raws, image_err = _validate_turn_images(
        body, talker_config,
    )
    if image_err is not None:
        return web.json_response(image_err[1], status=image_err[0])

    active_dict = state_mgr.get_active(identity.synthetic_chat_id)
    if active_dict is None or active_dict.get("session_id") != session_key:
        return web.json_response({"error": "no_such_session"}, status=404)

    from alfred.telegram.conversation import run_turn
    from alfred.telegram.session import Session, record_turn_idempotency

    session_obj = Session.from_dict(active_dict)

    # --- idempotency dedup (BEFORE run_turn) -------------------------------
    status, cached = _dedup_check(session_obj, idempotency_key, message)
    if status == "hit":
        log.info(
            "web.chat.turn_deduped",
            user=identity.user,
            session_key=session_key,
            idempotency_key_prefix=idempotency_key[:8],
            detail="cached result returned; run_turn NOT re-invoked",
        )
        return web.json_response(_cached_turn_payload(cached, session_key))
    if status == "stale":
        log.warning(
            "web.chat.idempotency_key_reused_new_message",
            user=identity.user,
            session_key=session_key,
            idempotency_key_prefix=idempotency_key[:8],
            detail="same idempotency_key, different message — running fresh",
        )

    # --- concurrent-turn guard (prevents double-append) --------------------
    in_flight = request.app[KEY_WEB_INFLIGHT]
    if session_key in in_flight:
        log.warning(
            "web.chat.turn_in_flight",
            user=identity.user,
            session_key=session_key,
            detail="a turn is already running for this session — rejecting",
        )
        return web.json_response({"error": "turn_in_flight"}, status=409)
    in_flight.add(session_key)
    try:
        # Capture transcript length BEFORE the turn so we can locate the
        # user turn afterwards (appended first, at ``pre_len``); the
        # assistant turn is appended LAST. Both stamps are read off the
        # ``_ts`` clock ``append_turn`` writes — no new clock invented.
        pre_len = len(session_obj.transcript)

        user_name = _user_name_for(identity, web_config)

        # Persist carried screenshots to the inbox (sovereign audit trail,
        # best-effort) BEFORE the turn — mirrors the Telegram photo handler.
        if image_raws:
            _persist_web_images(
                image_raws, talker_config.vault.path,
                user=identity.user, session_key=session_key,
            )

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
                channel="web",
                image_blocks=image_blocks,
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

        # Assemble the response via the shared helper so the buffered body
        # is byte-identical to the stream's terminal ``done`` frame.
        payload = _build_turn_payload(
            session_obj, pre_len, reply, session_key, deduped=False
        )

        # Cache for retry-safe dedup (only when a key was supplied).
        if idempotency_key:
            record_turn_idempotency(
                state_mgr,
                session_obj,
                key=idempotency_key,
                result={
                    "reply": reply,
                    "ts": payload["ts"],
                    "user_ts": payload["user_ts"],
                    "msg_hash": _msg_hash(message),
                },
            )

        log.info(
            "web.chat.turn_complete",
            user=identity.user,
            session_key=session_key,
            user_kind=kind,
            reply_chars=len(reply or ""),
            assistant_ts=payload["ts"],
            user_ts=payload["user_ts"],
            deduped=False,
        )
        return web.json_response(payload)
    finally:
        in_flight.discard(session_key)


async def _handle_chat_stream(request: web.Request) -> web.StreamResponse:
    """POST /chat/stream — one user turn, streamed over Server-Sent Events.

    Tier-1 keep-alive streaming (the safety-critical ``run_turn`` core stays
    BYTE-IDENTICAL — it runs as a detached task; we only emit periodic
    heartbeat frames around it). The terminal ``done`` frame carries the
    EXACT ``/chat/turn`` payload (shared ``_build_turn_payload`` helper).

    Frame protocol:
      * ``event: status\\ndata: {"phase":"tool","tool":...,"iteration":...}``
        — emitted per tool invocation (0+), via ``run_turn(on_event=...)``.
      * ``: keepalive\\n\\n`` — comment frames every ``KEEPALIVE_SECS``.
      * ``event: done\\ndata: <ChatTurnResponse>`` — terminal success.
      * ``event: error\\ndata: {"error","detail"}`` — engine failure.

    ALL validation (auth / body / session-match) returns a JSON 401/400/404
    BEFORE ``resp.prepare()`` — the HTTP status locks once the SSE response
    is prepared, so an error after that point could not set a status.

    Detach-on-disconnect: if the client drops mid-turn we stop the write
    loop and return, but do NOT cancel the ``run_turn`` task — it finishes
    server-side and the reply is persisted by ``append_turn``, so the FE
    reconciles via ``/chat/history`` (never a false "couldn't reach the
    assistant" when the turn actually completed).
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    client = request.app[KEY_WEB_ANTHROPIC]
    state_mgr = request.app[KEY_WEB_STATE_MGR]
    talker_config = request.app[KEY_WEB_TALKER_CONFIG]
    system_prompt_provider: Callable[[], str] = request.app[KEY_WEB_SYSTEM_PROVIDER]
    vault_context_str: str = request.app[KEY_WEB_VAULT_CTX]

    # --- validation (JSON errors BEFORE prepare; status locks after) -------
    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    body = await _read_json_body(request)
    session_key = body.get("session_key")
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message_required"}, status=400)
    kind = "voice" if body.get("kind") == "voice" else "text"
    idempotency_key = body.get("idempotency_key")
    idempotency_key = idempotency_key if isinstance(idempotency_key, str) else ""

    # Image-carry (optional) — validate BEFORE the SSE handshake so a bad
    # screenshot is a JSON 400 (the §9.4 "validation 400/404/409 BEFORE the
    # stream opens" contract), not a mid-stream error frame.
    image_blocks, image_raws, image_err = _validate_turn_images(
        body, talker_config,
    )
    if image_err is not None:
        return web.json_response(image_err[1], status=image_err[0])

    active_dict = state_mgr.get_active(identity.synthetic_chat_id)
    if active_dict is None or active_dict.get("session_id") != session_key:
        return web.json_response({"error": "no_such_session"}, status=404)

    from alfred.telegram.conversation import run_turn
    from alfred.telegram.session import Session, record_turn_idempotency

    session_obj = Session.from_dict(active_dict)

    # --- idempotency decision (sync, pre-prepare) --------------------------
    status, cached = _dedup_check(session_obj, idempotency_key, message)
    if status == "stale":
        log.warning(
            "web.chat.idempotency_key_reused_new_message",
            user=identity.user,
            session_key=session_key,
            idempotency_key_prefix=idempotency_key[:8],
            detail="same idempotency_key, different message — running fresh",
        )

    # --- concurrent-turn guard (JSON 409 BEFORE prepare; reserve atomically
    #     so a second concurrent stream can't slip through the prepare await).
    #     A dedup HIT never runs run_turn, so it skips the guard.
    in_flight = request.app[KEY_WEB_INFLIGHT]
    reserved = False
    if status != "hit":
        if session_key in in_flight:
            log.warning(
                "web.chat.turn_in_flight",
                user=identity.user,
                session_key=session_key,
                detail="a turn is already running for this session — rejecting",
            )
            return web.json_response({"error": "turn_in_flight"}, status=409)
        in_flight.add(session_key)
        reserved = True

    # pre_len captured BEFORE the run_turn task is launched/awaited.
    pre_len = len(session_obj.transcript)
    user_name = _user_name_for(identity, web_config)

    # Persist carried screenshots to the inbox (sovereign audit trail,
    # best-effort) BEFORE launching the turn task — mirrors /chat/turn +
    # the Telegram photo handler. Skipped on a dedup HIT (run_turn never
    # fires there; the original turn already persisted).
    if image_raws and status != "hit":
        _persist_web_images(
            image_raws, talker_config.vault.path,
            user=identity.user, session_key=session_key,
        )

    # --- SSE handshake (HTTP status locks here) ----------------------------
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    try:
        await resp.prepare(request)
    except BaseException:
        # Never leak the in-flight reservation if the handshake fails.
        if reserved:
            in_flight.discard(session_key)
        raise

    # --- dedup HIT → emit the cached result as the terminal frame ----------
    if status == "hit":
        log.info(
            "web.chat.stream_deduped",
            user=identity.user,
            session_key=session_key,
            idempotency_key_prefix=idempotency_key[:8],
            detail="cached result returned; run_turn NOT re-invoked",
        )
        try:
            await _sse_write_event(
                resp, "done", _cached_turn_payload(cached, session_key)
            )
        except (ConnectionResetError, RuntimeError):
            pass
        return resp

    # Status-frame callback. Best-effort: on a dropped client we latch
    # ``client_gone`` so subsequent emits no-op and a write error never
    # raises into run_turn (detach-on-disconnect).
    client_gone = {"v": False}

    async def _on_event(ev: dict[str, Any]) -> None:
        if client_gone["v"]:
            return
        try:
            await _sse_write_event(resp, "status", ev)
        except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
            client_gone["v"] = True

    task = asyncio.create_task(
        run_turn(
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
            channel="web",
            image_blocks=image_blocks,
            on_event=_on_event,
        )
    )

    def _cleanup(done_task: "asyncio.Task[Any]") -> None:
        # Always release the in-flight reservation when the task finishes
        # (normal completion OR a detached client-drop). Also retrieve the
        # result/exception so asyncio never logs "exception never retrieved"
        # on a detached task; log only the detached-failure case (the normal
        # path logs stream_engine_error via the inline task.result()).
        in_flight.discard(session_key)
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None and client_gone["v"]:
            log.warning(
                "web.chat.stream_detached_task_failed",
                user=identity.user,
                session_key=session_key,
                error=str(exc),
                error_type=type(exc).__name__,
                detail="run_turn raised after the SSE client disconnected; "
                       "no reply was persisted for this turn",
            )

    task.add_done_callback(_cleanup)

    # --- keep-alive loop ---------------------------------------------------
    while True:
        done, _pending = await asyncio.wait({task}, timeout=KEEPALIVE_SECS)
        if task in done:
            break
        try:
            await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, RuntimeError):
            # Client dropped mid-turn — DETACH: stop writing, let run_turn
            # finish server-side; the FE reconciles via /chat/history. The
            # ``_cleanup`` done-callback releases the in-flight reservation.
            client_gone["v"] = True
            log.info(
                "web.chat.stream_client_disconnected",
                user=identity.user,
                session_key=session_key,
                detail="client dropped mid-turn — detaching; run_turn "
                       "continues server-side, reply recoverable via history",
            )
            return resp

    # --- terminal frame ----------------------------------------------------
    try:
        reply = task.result()
    except Exception as exc:  # noqa: BLE001 — engine failure → SSE error frame
        log.warning(
            "web.chat.stream_engine_error",
            user=identity.user,
            session_key=session_key,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        if not client_gone["v"]:
            try:
                await _sse_write_event(
                    resp, "error", {"error": "engine_error", "detail": str(exc)}
                )
            except (ConnectionResetError, RuntimeError):
                pass
        return resp

    payload = _build_turn_payload(
        session_obj, pre_len, reply, session_key, deduped=False
    )

    # Cache for retry-safe dedup (only when a key was supplied).
    if idempotency_key:
        record_turn_idempotency(
            state_mgr,
            session_obj,
            key=idempotency_key,
            result={
                "reply": reply,
                "ts": payload["ts"],
                "user_ts": payload["user_ts"],
                "msg_hash": _msg_hash(message),
            },
        )

    log.info(
        "web.chat.stream_complete",
        user=identity.user,
        session_key=session_key,
        user_kind=kind,
        reply_chars=len(reply or ""),
        assistant_ts=payload["ts"],
        user_ts=payload["user_ts"],
        deduped=False,
    )
    if not client_gone["v"]:
        try:
            await _sse_write_event(resp, "done", payload)
        except (ConnectionResetError, RuntimeError):
            pass
    return resp


async def _handle_chat_history(request: web.Request) -> web.StreamResponse:
    """GET /chat/history/{session_key} — current active session transcript.

    M1 surfaces the CURRENT active session only (closed-session / vault-
    record history is a later milestone). Tool plumbing is flattened out.
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    state_mgr = request.app[KEY_WEB_STATE_MGR]

    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

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
    web_auth_state: Any,
    anthropic_client: Any,
    state_mgr: Any,
    talker_config: Any,
    system_prompt_provider: Callable[[], str],
    vault_context_str: str,
    allowed_user_ids: "list[int] | None" = None,
) -> bool:
    """Mount the web chat + auth routes onto ``app`` — IFF web is enabled.

    Returns ``True`` when routes were mounted, ``False`` when the web
    surface is absent / disabled (opt-in inertness: nothing is registered
    and the transport server is byte-unchanged). Must be called BEFORE the
    app is started (aiohttp forbids route additions on a started app); the
    daemon calls it adjacent to ``wire_transport_app``, the same pre-start
    window.

    Two fail-loud startup guards run BEFORE any dep is stashed or route is
    mounted, so a misconfigured instance refuses to mount the web surface
    rather than serving something broken:

    1. synthetic-id collision guard — a colliding name→id mapping aborts
       (provable, not probable; see ``identity.py``); runs in BOTH modes.
    2. signing-secret guard — an enabled-but-unconfigured
       ``web.auth.session_secret`` (empty / unresolved ``${...}``) aborts,
       so we never serve forgeable sessions. **Session mode only** — a
       ``relay``-mode instance never mints / verifies session tokens
       (possession of the Layer-1 ``web`` peer token IS the authority), so
       it has no signing secret to guard and the ``/auth/{login,verify}``
       routes are NOT mounted.
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

    mode = getattr(web_config.auth, "mode", "session") or "session"
    relay_mode = mode == "relay"

    # Guard 1 — synthetic-id collisions (fail-loud). Runs in both modes.
    mapping = check_synthetic_id_collisions(
        web_config.users, allowed_user_ids or []
    )
    # Guard 2 — signing secret must resolve (fail-loud); raises ValueError
    # on empty / unresolved placeholder. SESSION MODE ONLY — a relay
    # instance mints no tokens, so a missing secret is expected and must
    # NOT block mounting.
    if not relay_mode:
        resolve_signing_secret(web_config.auth)

    log.info(
        "web.routes.collision_check_clean",
        users=len(web_config.users),
        mode=mode,
        synthetic_ids=sorted(mapping.values()),
    )

    app[KEY_WEB_CONFIG] = web_config
    app[KEY_WEB_AUTH_STATE] = web_auth_state
    app[KEY_WEB_ANTHROPIC] = anthropic_client
    app[KEY_WEB_STATE_MGR] = state_mgr
    app[KEY_WEB_TALKER_CONFIG] = talker_config
    app[KEY_WEB_SYSTEM_PROVIDER] = system_prompt_provider
    app[KEY_WEB_VAULT_CTX] = vault_context_str
    # Per-app concurrent-turn guard set (NOT module-global — concurrent test
    # apps in one process must not share in-flight state).
    app[KEY_WEB_INFLIGHT] = set()

    app.router.add_post("/chat/open", _handle_chat_open)
    app.router.add_post("/chat/turn", _handle_chat_turn)
    app.router.add_post("/chat/stream", _handle_chat_stream)
    app.router.add_get("/chat/history/{session_key}", _handle_chat_history)

    mounted_routes = [
        "/chat/open",
        "/chat/turn",
        "/chat/stream",
        "/chat/history/{session_key}",
    ]

    # Auth routes (/auth/login, /auth/verify) — SESSION MODE ONLY. A relay
    # instance has no login surface (login/magic-link lives on the
    # session-mode login instance, e.g. Salem). Imported here (not at module
    # top) so routes_auth can import this module's siblings without a cycle.
    if not relay_mode:
        from .routes_auth import register_auth_handlers

        register_auth_handlers(app)
        mounted_routes += ["/auth/login", "/auth/verify"]
    else:
        # Intentionally-left-blank: relay mode deliberately omits the login
        # surface, logged so "no /auth routes" is a deliberate state, not a
        # silent wiring skip.
        log.info(
            "web.routes.relay_mode_no_auth",
            detail="relay auth mode — /auth/login + /auth/verify NOT mounted "
                   "(relay instances never mint / verify session tokens)",
        )

    # STT route (/stt/transcribe) — same lazy-import anti-cycle pattern.
    # Rides the web opt-in; reuses the live STT fallback chain over the
    # talker config already stashed on the app. Mounted in BOTH modes.
    from .routes_stt import register_stt_handlers

    register_stt_handlers(app)
    mounted_routes.append("/stt/transcribe")

    log.info(
        "web.routes.registered",
        users=len(web_config.users),
        mode=mode,
        routes=mounted_routes,
    )
    return True

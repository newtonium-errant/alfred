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
    POST /chat/capture               → { capture_active, spans, closed_span }
    POST /chat/capture/extract       → { ok, record, notes, skipped_reason }

Auth layering: every non-``/health`` route is gated by the transport
``auth_middleware`` (Layer 1, peer token — "this front-end may talk to
me"). Layer 2 resolves the *verified named user* via the mode-aware
:func:`alfred.web.auth.resolve_web_identity`, fail-closed 401:

* ``session`` mode (the login instance, e.g. Salem) — an instance-signed
  ``X-Alfred-Session`` token (``require_web_session``).
* ``relay`` mode (cross-instance targets, e.g. KAL-LE / Hypatia / VERA) —
  an asserted ``X-Alfred-User`` header (verified NAME only, gated at
  Layer 1 by the ``web`` peer token or the vouched ``rrts_relay`` one —
  the two peers ``_resolve_relay_identity`` admits), re-resolved against
  THIS instance's own ``web.users``. Mirrors the ``/vault/ingest``
  relay-auth model.

The two ``/chat/capture*`` routes are the ONE exception to that two-peer
admit: they peer-pin ``web`` alone (:func:`_resolve_capture_identity`,
401 ``wrong_peer``) because span extraction drives vault writes outside
the scope machinery that bounds a vouched reporter. See that helper's
docstring for the derivation.

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
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from alfred.telegram.api_errors import (
    classification_payload,
    classify_engine_error,
)
from alfred.vault.scope import RRTS_INTAKE_ROLE

from .auth import WEB_CHAT_PEER, resolve_web_identity
from .config import WebConfig, resolve_signing_secret
from .identity import WebIdentity, check_synthetic_id_collisions
from .keys import (
    KEY_WEB_ANTHROPIC,
    KEY_WEB_AUTH_STATE,
    KEY_WEB_CAPTURE_EXTRACTING,
    KEY_WEB_CONFIG,
    KEY_WEB_CONTACT_FEED,
    KEY_WEB_CONTACT_STORE,
    KEY_WEB_DATA_DIR,
    KEY_WEB_INFLIGHT,
    KEY_WEB_NOTIFY_STORE,
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


# ---------------------------------------------------------------------------
# Turn-body streamed read — route-scoped 32 MiB cap (image-carry, #29)
# ---------------------------------------------------------------------------
#
# /chat/turn + /chat/stream may now carry base64 image blocks (up to
# MAX_IMAGES_PER_TURN × MAX_IMAGE_BYTES ≈ 4 × 5 MiB decoded, plus base64's
# ~33% inflation + the JSON envelope). That is FAR above the shared transport
# app's ``client_max_size``, which ``build_app`` sets to
# ``DEFAULT_TRANSPORT_CLIENT_MAX_BYTES`` = 14 MiB (NOT aiohttp's 1 MiB default —
# #57 raised it so a base64'd PDF reaches the ingest route's own taxonomy), and
# ``request.json()`` / ``request.read()`` enforce that global cap — they would
# 413 every screenshot turn. So this helper STREAMS ``request.content``
# with its OWN byte cap (:data:`MAX_TURN_BODY_BYTES`), EXACTLY as
# ``routes_stt.py`` streams audio, leaving the 14 MiB guard on every OTHER
# peer/auth/ingest route UNTOUCHED. Do NOT "fix" this by raising
# ``client_max_size`` on the shared app — that weakens DoS protection on every
# peer route (see the ``routes_stt.py`` module docstring). This cap MUST move
# in LOCKSTEP with the two BFF caps (``web/pages/api/chat/turn.ts`` sizeLimit
# and ``stream.ts`` MAX_BODY_BYTES) — a lower tier 413s a real screenshot
# below another and the end-to-end path fails.
_TURN_BODY_CHUNK_BYTES: int = 64 * 1024
MAX_TURN_BODY_BYTES: int = 32 * 1024 * 1024  # 32 MiB


async def _read_json_body(request: web.Request) -> dict[str, Any] | web.Response:
    """Best-effort JSON body read with a route-scoped 32 MiB cap.

    Returns the parsed dict, ``{}`` on an empty / invalid body (the current
    LENIENT contract — a malformed / empty body is treated as ``{}``, NOT a
    400; downstream ``message_required`` validation surfaces the real error),
    or a ``413`` :class:`aiohttp.web.Response` when the streamed body exceeds
    :data:`MAX_TURN_BODY_BYTES`. Callers MUST return a ``web.Response`` result
    VERBATIM (it is the body-too-large error) before touching it as a dict.

    Streams ``request.content`` rather than ``request.json()`` so the body cap
    is OURS (:data:`MAX_TURN_BODY_BYTES`), not the transport app's 14 MiB
    ``client_max_size`` — image turns can be ~28 MiB (4 × 5 MiB base64) and
    ``request.json()`` would 413 them under the global guard. EXACT copy of
    the ``routes_stt.py`` streamed read; do NOT raise ``client_max_size``
    app-wide instead (see that module's docstring).
    """
    buf = bytearray()
    async for chunk in request.content.iter_chunked(_TURN_BODY_CHUNK_BYTES):
        buf.extend(chunk)
        if len(buf) > MAX_TURN_BODY_BYTES:
            log.warning(
                "web.chat.body_too_large",
                bytes_seen=len(buf),
                max_bytes=MAX_TURN_BODY_BYTES,
            )
            return web.json_response(
                {"error": "body_too_large", "max_bytes": MAX_TURN_BODY_BYTES},
                status=413,
            )
    if not buf:
        return {}
    try:
        body = json.loads(buf)
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


#: Model-visible saved-path banner — the builder half of a builder+tuner
#: CONTRACT (XS-BATCH9 item 3; the vault-vera SKILL teaches TO this exact
#: format). One line per persisted screenshot, ``{path}`` filled with the
#: VAULT-RELATIVE inbox path (``inbox/screenshot-<UTCstamp>-<hash8>.<ext>``).
#: The bracketed-system-line family is recovered from the retired bot's
#: attachment banner (``[PDF attached: <filename>]``, bot.py at 34a4dfbf^);
#: vault-relative matches the bug-widget lane's convention
#: (routes_bugreport: "Saved at `<rel>` (relative to the vault root)") and
#: keeps box-absolute paths out of vault records. Change ONLY in lockstep
#: with the SKILL's screenshots teaching — the agent copies these paths
#: verbatim into ticket ``screenshots`` fields.
IMAGE_SAVED_BANNER: str = "[Screenshot attached: {path}]"


def _with_image_banners(message: str, saved_rels: list[str]) -> str:
    """Prepend one saved-path banner per persisted image to the turn text.

    Banner block first, one blank line, then the operator's words — the
    header-then-caption order the retired bot's document banner used. No
    saved paths (no images carried, or every save failed) → the message is
    returned UNCHANGED: a text-only turn stays byte-identical to the
    pre-banner path, and a failed save never yields a path the filesystem
    cannot honour (the invented-path defect this helper exists to close).
    """
    if not saved_rels:
        return message
    banners = "\n".join(
        IMAGE_SAVED_BANNER.format(path=rel) for rel in saved_rels
    )
    return f"{banners}\n\n{message}"


def _persist_web_images(
    raws: list[tuple[str, bytes]],
    vault_path: str,
    *,
    user: str,
    session_key: str,
) -> list[str]:
    """Persist carried screenshots to the inbox; return their vault paths.

    Returns the VAULT-RELATIVE path (``inbox/<filename>``) of every image
    that persisted, in wire order. The caller threads these into the
    model-visible turn text via :func:`_with_image_banners` — before this,
    the saved ``Path`` was DISCARDED here, so the SKILL instructed the
    agent to write a ``screenshots:`` path it was never given (invented
    paths were the live risk). ``inbox/<name>`` is exact by construction:
    ``vision.storage_path`` always writes ``<vault>/inbox/<name>``.

    Persistence stays best-effort, mirroring the retired Telegram photo
    handler (the model can still see the image from in-memory bytes even
    if persistence fails, so a save error never blocks the turn — the
    failed image simply contributes NO path). The file_unique_id is a
    content hash so a retransmit dedupes to the same filename, and hence
    to the same banner. Intake-only — the image is stored in the
    instance's own vault; it is NOT egressed.
    """
    from alfred.telegram import vision

    saved_rels: list[str] = []
    for media_type, raw in raws:
        ext = _IMAGE_EXT_BY_MEDIA_TYPE.get(media_type, "img")
        unique_id = hashlib.sha256(raw).hexdigest()[:8]
        try:
            dest = vision.save_image_to_inbox(
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
            continue
        saved_rels.append(f"inbox/{dest.name}")
    return saved_rels


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


def _capture_voice_correction(
    talker_config: Any,
    *,
    kind: str,
    transcript: Any,
    sent: str,
    user: str,
    session_key: str,
) -> None:
    """Record what the STT heard against what the operator actually sent (#54).

    This is the CAPTURE half of the learned-vocabulary loop. When a message came
    from voice, the composer carries the transcript as it was inserted; the
    difference between that and the sent text is a correction the operator made
    anyway — free supervision nobody had to be asked for.

    Called on the path where the turn REALLY runs, never on the idempotency-dedup
    path: a retried send must not count the same correction twice, because counts
    are what decide whether a term crosses the proposal threshold.

    A ZERO DIFF IS RECORDED, NOT SKIPPED. "He sent the transcript untouched" is
    positive evidence about transcription quality and the denominator for any
    later error rate; dropping it would leave only the failures on file and make
    the STT look worse than it is. It is logged explicitly so the quiet case is
    visibly a decision rather than an absence.

    Best-effort throughout: a capture failure must never cost the operator their
    turn, so everything is inside the try and the turn proceeds regardless.
    """
    if kind != "voice":
        return
    stt = getattr(talker_config, "stt", None)
    if not getattr(stt, "vocab_learning_enabled", False):
        return
    if not isinstance(transcript, str) or not transcript.strip():
        # Voice-kind with no transcript carried: an older client, or the player
        # ask box, which does not seed from a transcript. Not an error.
        return

    try:
        from alfred.telegram.stt_vocab_learning import (
            CorrectionPair,
            append_correction_pair,
            extract_term_corrections,
        )

        corpus_path = getattr(stt, "vocab_corpus_path", "") or ""
        if not corpus_path:
            return
        instance = getattr(getattr(talker_config, "instance", None), "name", "") or ""
        append_correction_pair(
            corpus_path,
            CorrectionPair(transcript=transcript, sent=sent, instance=instance),
        )
        if transcript.strip() == sent.strip():
            log.info(
                "web.chat.voice_transcript_unedited",
                user=user,
                session_key=session_key,
                chars=len(sent),
                detail="operator sent the transcript unchanged — recorded as "
                       "positive evidence about transcription quality",
            )
        else:
            log.info(
                "web.chat.voice_correction_captured",
                user=user,
                session_key=session_key,
                terms=len(extract_term_corrections(transcript, sent)),
                detail="stored the (transcript, sent) pair for the learned-"
                       "vocabulary loop",
            )
    except Exception as exc:  # noqa: BLE001 — capture is never worth a turn
        log.warning(
            "web.chat.voice_correction_capture_failed",
            user=user,
            session_key=session_key,
            error=str(exc),
            error_type=type(exc).__name__,
            action="continuing_the_turn",
        )


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
        # Always-present (capture toggle, R1 2026-08-20): False on every
        # normal turn so the shape never branches; the capture receipt
        # (:func:`_build_capture_receipt`) is the True case.
        "captured": False,
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


def _build_capture_receipt(
    session_obj: Any,
    pre_len: int,
    session_key: str,
    *,
    deduped: bool = False,
) -> dict[str, Any]:
    """The captured-turn response — a RECEIPT, not a reply (R1 capture mode).

    While capture is ON the engine's ``session_type == "capture"``
    short-circuit persists the user turn and returns ``CAPTURE_SENTINEL``
    without any model call — there IS no assistant turn, so
    :func:`_build_turn_payload`'s ``transcript[-1]`` read would misreport
    the user's own turn as the assistant stamp. This builder is the
    captured shape: same always-present fields (``reply`` empty, ``ts``
    empty — no assistant turn exists), ``captured: True``, and the
    ``user_ts`` of the just-persisted turn so the client can stamp its
    received indicator. The sentinel itself NEVER reaches the wire.
    """
    transcript = session_obj.transcript or []
    user_ts = transcript[pre_len].get("_ts", "") if len(transcript) > pre_len else ""
    return {
        "reply": "",
        "captured": True,
        "session_key": session_key,
        "ts": "",
        "user_ts": user_ts,
        "deduped": deduped,
    }


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
        # A retried CAPTURED turn must dedup to a captured receipt, not a
        # blank normal reply — the flag rides the idempotency cache.
        "captured": bool(cached.get("captured", False)),
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
    from alfred.telegram import capture_batch
    from alfred.telegram.session import (
        Session,
        close_session,
        is_capture_candidate,
        open_session,
        stash_close_contract_metadata,
    )

    existing = state_mgr.get_active(identity.synthetic_chat_id)
    # Clinic-capture arc: the incident this fixes is a clinician's dictated
    # capture SILENTLY lost when the PWA reopened the session — closed via
    # web_session_reopened with no structuring and no signal. Web sessions are
    # always session_type=="conversation" (the bot-era /capture and /end
    # openers are gone; the web CAPTURE TOGGLE, R1 2026-08-20, is a MODE
    # with explicit spans, not a session type), so with no spans present we
    # detect capture-worthiness deterministically (``is_capture_candidate``) and
    # (a) close_session UNCONDITIONALLY stamps ``capture_structured: pending``,
    # (b) auto-run structuring when enabled, and (c) surface a ``prior_capture``
    # signal on the response (no server-push channel exists — this is the
    # intentionally-left-blank signal for the web user, read on the next open).
    # When explicit spans DO exist they are the operator's own declaration of
    # the capture material — the heuristic is suppressed and the span
    # finalizer owns the close-time backstop (branch below).
    prior_capture: dict[str, Any] | None = None
    if existing:
        from alfred.telegram import capture_spans

        prior_type = existing.get("_session_type") or "conversation"
        prior_session = Session.from_dict(existing)
        # Explicit capture spans (toggle R1) suppress the whole-session
        # heuristic — same mutual-exclusion rule as close_session: the
        # spans own the capture material, and running both would
        # structure it twice.
        prior_spans = capture_spans.normalized_spans(existing)
        candidate = (not prior_spans) and is_capture_candidate(
            prior_session, prior_type
        )
        rel_path: str | None = None
        try:
            primary_users = getattr(talker_config, "primary_users", None) or []
            rel_path = close_session(
                state_mgr,
                vault_path_root=talker_config.vault.path,
                chat_id=identity.synthetic_chat_id,
                reason="web_session_reopened",
                user_vault_path=primary_users[0] if primary_users else None,
                # Stamp the configured STT model on the record (parity with
                # Telegram, which stashes ``config.stt.model`` at open). Web
                # voice records previously carried ``stt_model: ''`` — the
                # divergence data existed in talker.log but never landed on
                # the record. The daemon idle-timeout close path reads the
                # same value off ``_stt_model_used`` (stashed at open below).
                stt_model_used=talker_config.stt.model,
                session_type=prior_type,
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
        if prior_spans and rel_path:
            # Close-time backstop (toggle R1): any span the operator never
            # accepted the extraction offer for is finalized now, against
            # the just-written parent record. Detached task (retained by
            # the module's own set); the record's ``extracted: false``
            # rows are the fail-safe if it never runs.
            from pathlib import Path as _Path

            from alfred.audit import agent_slug_for

            pending = [
                s for s in prior_spans if not bool(s.get("extracted"))
            ]
            anchor_scope = (
                "hypatia"
                if (talker_config.instance.tool_set or "").lower() == "hypatia"
                else ""
            )
            open_primary = getattr(talker_config, "primary_users", None) or []
            task = None
            if pending:
                task = capture_spans.schedule_span_finalization(
                    client=request.app[KEY_WEB_ANTHROPIC],
                    vault_path=_Path(talker_config.vault.path),
                    active_snapshot=existing,
                    parent_rel_path=rel_path,
                    model=talker_config.anthropic.model or "claude-sonnet-4-6",
                    agent_slug=agent_slug_for(talker_config),
                    anchor_scope=anchor_scope,
                    tool_set=talker_config.instance.tool_set or "",
                    user_vault_path=open_primary[0] if open_primary else "",
                )
            prior_capture = {
                "record": rel_path,
                "status": (
                    "spans_extracting" if (pending and task is not None)
                    else "spans_recorded"
                ),
                "turns": sum(
                    int(s["end"]) - int(s["start"]) for s in prior_spans
                ),
                "spans": len(prior_spans),
                "unextracted": len(pending),
            }
            log.info(
                "web.chat.prior_capture_spans",
                user=identity.user,
                synthetic_chat_id=identity.synthetic_chat_id,
                record=rel_path,
                status=prior_capture["status"],
                spans=len(prior_spans),
                unextracted=len(pending),
            )
        elif candidate and rel_path:
            scheduled = False
            if getattr(talker_config.session, "auto_structure_on_close", False):
                from pathlib import Path as _Path
                from alfred.audit import agent_slug_for
                anchor_scope = (
                    "hypatia"
                    if (talker_config.instance.tool_set or "").lower() == "hypatia"
                    else ""
                )
                task = capture_batch.schedule_capture_structuring(
                    client=request.app[KEY_WEB_ANTHROPIC],
                    vault_path=_Path(talker_config.vault.path),
                    session_rel_path=rel_path,
                    transcript=prior_session.transcript,
                    model=talker_config.anthropic.model or "claude-sonnet-4-6",
                    agent_slug=agent_slug_for(talker_config),
                    anchor_scope=anchor_scope,
                    short_id=(prior_session.session_id or "").split("-")[0],
                    send_follow_up=None,  # no push channel on web
                )
                scheduled = task is not None
            prior_capture = {
                "record": rel_path,
                "status": "structuring" if scheduled else "held_unstructured",
                "turns": len(prior_session.transcript),
            }
            log.info(
                "web.chat.prior_capture_held",
                user=identity.user,
                synthetic_chat_id=identity.synthetic_chat_id,
                record=rel_path,
                status=prior_capture["status"],
                turns=prior_capture["turns"],
            )

    session_obj = open_session(
        state_mgr,
        identity.synthetic_chat_id,
        model=talker_config.anthropic.model,
    )
    # Stash the timeout-close contract metadata (fix: web sessions never
    # timed out). Without ``_vault_path_root`` the daemon idle-timeout
    # sweeper SKIPS the session, so a PWA session stayed open for days until
    # the next reopen closed it — filing the record under a ``started_at``
    # date 1-3 days behind the actual content. With the stash, the sweeper
    # closes an idle web session on the same ``gap_timeout_seconds`` cadence
    # as Telegram, so the record's ``created`` date tracks the content.
    # Web sessions are always ``session_type="conversation"`` (no web
    # /capture, no web /note). ``_stt_model_used`` carries the configured
    # STT model onto the eventual record (web-voice stt_model parity).
    open_primary_users = getattr(talker_config, "primary_users", None) or []
    stash_close_contract_metadata(
        state_mgr,
        identity.synthetic_chat_id,
        vault_path_root=talker_config.vault.path,
        user_vault_path=open_primary_users[0] if open_primary_users else "",
        stt_model_used=talker_config.stt.model,
        session_type="conversation",
        tool_set=talker_config.instance.tool_set or "",
    )
    log.info(
        "web.chat.session_opened",
        user=identity.user,
        synthetic_chat_id=identity.synthetic_chat_id,
        session_id=session_obj.session_id,
        model=talker_config.anthropic.model,
    )
    body_out: dict[str, Any] = {"session_key": session_obj.session_id}
    if prior_capture is not None:
        body_out["prior_capture"] = prior_capture
    return web.json_response(body_out)


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
    if isinstance(body, web.Response):
        return body  # 413 — body exceeded MAX_TURN_BODY_BYTES (image-carry)
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

    from alfred.telegram import capture_spans
    from alfred.telegram.conversation import CAPTURE_SENTINEL, run_turn
    from alfred.telegram.session import Session, record_turn_idempotency

    # Capture mode is SERVER truth (R1): the turn path consults the state
    # the toggle wrote, never a client-asserted flag — a stale client
    # cannot bypass an active capture, and a refreshed one resumes it.
    capture_on = capture_spans.capture_active(active_dict)

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

        # #54 capture — this is the real-send path (a dedup HIT returned above),
        # so a correction is counted exactly once per send.
        _capture_voice_correction(
            talker_config,
            kind=kind,
            transcript=body.get("transcript"),
            sent=message,
            user=identity.user,
            session_key=session_key,
        )

        user_name = _user_name_for(identity, web_config)

        # Persist carried screenshots to the inbox (sovereign audit trail,
        # best-effort) BEFORE the turn — mirrors the retired Telegram photo
        # handler — and keep the saved paths: they become the model-visible
        # banner lines below, which is where the SKILL-taught ticket
        # ``screenshots`` field gets its real values.
        saved_image_rels: list[str] = []
        if image_raws:
            saved_image_rels = _persist_web_images(
                image_raws, talker_config.vault.path,
                user=identity.user, session_key=session_key,
            )

        # C3c player context primer — when the pause→ask turn carries a valid
        # {brief_date, section_id}, prepend the SERVER-composed grounding note so
        # deictic references ("why is that yellow?") resolve against the
        # on-screen slide. The client sends ONLY the structured fields; the note
        # is composed here (never client-authored → no prompt-injection surface).
        # Invalid/absent primer → un-grounded, byte-identical to a normal turn.
        # Injected AFTER the dedup check so the idempotency key stays the raw
        # operator message. ``/chat/turn`` only (v1); the stream handler is
        # untouched.
        from alfred.brief.player_primer import PlayerContextPrimer

        primer = PlayerContextPrimer.from_dict(body.get("primer"))
        # Saved-path banners compose AFTER the dedup check for the same
        # reason the primer does: ``_dedup_check`` keyed on the RAW
        # operator message above, and it must stay keyed on it.
        turn_message = _with_image_banners(message, saved_image_rels)
        if primer.valid:
            turn_message = f"{primer.context_line()}\n\n{turn_message}"
            log.info(
                "web.chat.primer_grounded",
                user=identity.user,
                session_key=session_key,
                brief_date=primer.brief_date,
                section_id=primer.section_id,
            )

        try:
            reply = await run_turn(
                client=client,
                state=state_mgr,
                session=session_obj,
                user_message=turn_message,
                config=talker_config,
                vault_context_str=vault_context_str,
                system_prompt=system_prompt_provider(),
                user_kind=kind,
                user_role=identity.role,
                user_name=user_name,
                channel="web",
                image_blocks=image_blocks,
                # Capture mode (R1): drives the engine's preserved
                # ``session_type == "capture"`` short-circuit — the turn is
                # persisted as span material, NO model call happens, and
                # the sentinel comes back instead of a reply.
                session_type="capture" if capture_on else None,
            )
        except Exception as exc:  # noqa: BLE001 — surface engine errors as 502
            classified = classify_engine_error(exc)
            log.warning(
                "web.chat.engine_error",
                user=identity.user,
                session_key=session_key,
                error=str(exc),
                error_type=type(exc).__name__,
                # Always emitted; the VALUE is the code when recognised and
                # None otherwise. So `classified_as=image_too_large` greps to
                # exactly the known failures, and its presence-with-None on
                # every other engine error is the ILB signal that the
                # classifier ran and abstained rather than never running.
                classified_as=classified.code if classified else None,
            )
            if classified is not None:
                return web.json_response(
                    classification_payload(classified), status=502,
                )
            return web.json_response(
                {"error": "engine_error", "detail": str(exc)},
                status=502,
            )

        # Assemble the response via the shared helpers so the buffered body
        # is byte-identical to the stream's terminal ``done`` frame. A
        # captured turn gets the RECEIPT shape (no assistant turn exists;
        # the sentinel never reaches the wire).
        if reply == CAPTURE_SENTINEL:
            payload = _build_capture_receipt(
                session_obj, pre_len, session_key, deduped=False
            )
        else:
            payload = _build_turn_payload(
                session_obj, pre_len, reply, session_key, deduped=False
            )

        # Cache for retry-safe dedup (only when a key was supplied). A
        # captured turn caches the RECEIPT (blank reply + captured flag),
        # so a retry dedups to the same receipt without re-appending the
        # user turn.
        if idempotency_key:
            record_turn_idempotency(
                state_mgr,
                session_obj,
                key=idempotency_key,
                result={
                    "reply": payload["reply"],
                    "captured": payload["captured"],
                    "ts": payload["ts"],
                    "user_ts": payload["user_ts"],
                    "msg_hash": _msg_hash(message),
                },
            )

        if payload["captured"]:
            log.info(
                "web.chat.turn_captured",
                user=identity.user,
                session_key=session_key,
                user_kind=kind,
                user_ts=payload["user_ts"],
                detail="capture on — turn persisted as span material, no "
                       "model call, receipt returned",
            )
        else:
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
    if isinstance(body, web.Response):
        return body  # 413 — body exceeded MAX_TURN_BODY_BYTES (image-carry)
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

    from alfred.telegram import capture_spans
    from alfred.telegram.conversation import CAPTURE_SENTINEL, run_turn
    from alfred.telegram.session import Session, record_turn_idempotency

    # Capture mode is SERVER truth (R1) — same consult as /chat/turn.
    capture_on = capture_spans.capture_active(active_dict)

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

    # #54 capture — guarded on ``status != "hit"`` for the same reason as
    # /chat/turn: a dedup hit never runs the turn and must not re-count the
    # correction. Placed before the SSE handshake so a client that drops
    # mid-stream still has its correction recorded (it did send the message).
    if status != "hit":
        _capture_voice_correction(
            talker_config,
            kind=kind,
            transcript=body.get("transcript"),
            sent=message,
            user=identity.user,
            session_key=session_key,
        )

    user_name = _user_name_for(identity, web_config)

    # Persist carried screenshots to the inbox (sovereign audit trail,
    # best-effort) BEFORE launching the turn task — mirrors /chat/turn +
    # the retired Telegram photo handler. Skipped on a dedup HIT (run_turn
    # never fires there; the original turn already persisted). The saved
    # paths become the model-visible banner lines on the turn text below,
    # same contract as /chat/turn.
    saved_image_rels: list[str] = []
    if image_raws and status != "hit":
        saved_image_rels = _persist_web_images(
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
            user_message=_with_image_banners(message, saved_image_rels),
            config=talker_config,
            vault_context_str=vault_context_str,
            system_prompt=system_prompt_provider(),
            user_kind=kind,
            user_role=identity.role,
            user_name=user_name,
            channel="web",
            image_blocks=image_blocks,
            on_event=_on_event,
            # Capture mode (R1): same server-truth threading as /chat/turn.
            session_type="capture" if capture_on else None,
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
        classified = classify_engine_error(exc)
        log.warning(
            "web.chat.stream_engine_error",
            user=identity.user,
            session_key=session_key,
            error=str(exc),
            error_type=type(exc).__name__,
            classified_as=classified.code if classified else None,
        )
        if not client_gone["v"]:
            try:
                await _sse_write_event(
                    resp,
                    "error",
                    classification_payload(classified) if classified is not None
                    else {"error": "engine_error", "detail": str(exc)},
                )
            except (ConnectionResetError, RuntimeError):
                pass
        return resp

    # Captured turn → the RECEIPT shape as the terminal ``done`` frame —
    # byte-identical to /chat/turn's captured body (shared builder).
    if reply == CAPTURE_SENTINEL:
        payload = _build_capture_receipt(
            session_obj, pre_len, session_key, deduped=False
        )
    else:
        payload = _build_turn_payload(
            session_obj, pre_len, reply, session_key, deduped=False
        )

    # Cache for retry-safe dedup (only when a key was supplied). Captured
    # turns cache the receipt (blank reply + captured flag), same as
    # /chat/turn.
    if idempotency_key:
        record_turn_idempotency(
            state_mgr,
            session_obj,
            key=idempotency_key,
            result={
                "reply": payload["reply"],
                "captured": payload["captured"],
                "ts": payload["ts"],
                "user_ts": payload["user_ts"],
                "msg_hash": _msg_hash(message),
            },
        )

    if payload["captured"]:
        log.info(
            "web.chat.stream_captured",
            user=identity.user,
            session_key=session_key,
            user_kind=kind,
            user_ts=payload["user_ts"],
            detail="capture on — turn persisted as span material, no "
                   "model call, receipt frame emitted",
        )
    else:
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


async def _handle_chat_active(request: web.Request) -> web.StreamResponse:
    """GET /chat/active — the caller's live session, if they have one (#94).

    READ-ONLY, and that is the entire point. ``/chat/open`` is
    close-prior-then-fresh, so a client that reaches for it to answer "do I
    have a session?" DESTROYS the answer. On 2026-08-11 that showed up as
    open-storms — three opens in 22 seconds — consistent with two devices
    (phone + tablet) holding device-local keys: A's stale key 404s, A opens
    fresh, which closes B's LIVE session, so B 404s and opens fresh, which
    closes A's. Each device is behaving correctly on its own and together they
    tear the conversation apart.

    This endpoint gives the bootstrap path a way to ask without breaking
    anything, so a 404 on a stale key becomes "adopt the live session" instead
    of "start a new one over the top of it".

    Returns ``{"session_key": "..."}`` when a session is active and
    ``{"session_key": null}`` when none is — an explicit null rather than a
    404, because "you have no session" is a normal answer to a normal
    question, not a failure. A 404 here would push the client back toward
    treating absence as an error condition, which is the reflex this whole
    item exists to unwind.

    Auth is the shared web-chat spine (``resolve_web_identity``) on the
    existing mount, so it inherits the ``WEB_CHAT_PEER`` pin from
    ``auth_middleware`` and introduces NO new asserted-identity surface.
    Scoped to the caller's OWN ``synthetic_chat_id``; it can no more see
    another user's session than ``/chat/history`` can.
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    state_mgr = request.app[KEY_WEB_STATE_MGR]

    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    active_dict = state_mgr.get_active(identity.synthetic_chat_id)
    session_key = (active_dict or {}).get("session_id") or None
    turns = len((active_dict or {}).get("transcript") or [])
    log.info(
        "web.chat.active_probed",
        user=identity.user,
        session_key=session_key,
        turns=turns,
        detail=(
            "live session found — the client resumes it instead of opening "
            "over the top of it"
            if session_key else
            "ran, nothing to resume — no active session for this user"
        ),
    )
    return web.json_response({"session_key": session_key, "turns": turns})


# ---------------------------------------------------------------------------
# Capture toggle + span extraction (R1, 2026-08-20)
# ---------------------------------------------------------------------------


def _resolve_capture_identity(
    request: web.Request,
) -> tuple[WebIdentity | None, web.Response | None]:
    """Auth spine for BOTH capture routes — ``(identity, error_response)``.

    Exactly one of the pair is non-``None``. ONE helper rather than two
    copies: the two routes are halves of one act (open a span, extract
    it), and a second spelling of this gate is how one endpoint ends up
    admitting a peer the other refuses.

    Ordering is load-bearing (CLAUDE.md "Relay / asserted-identity routes
    — peer-pin requirement"):

    1. **Peer-pin FIRST** — ``transport_peer`` must be the dedicated chat
       ``web`` peer (:data:`alfred.web.auth.WEB_CHAT_PEER`), checked
       BEFORE identity resolution and before ANY session read or write,
       so a refused request touches nothing.
    2. **Identity** — :func:`resolve_web_identity`, fail-closed 401
       ``invalid_session``, unchanged from the other ``/chat/*`` routes.

    Why the pin is NARROWER here than on ``/chat/turn``. The chat routes
    admit two peers (``web`` and the vouched ``rrts_relay``) and that is
    safe because every chat turn is scope-mediated: ``run_turn`` /
    ``resolve_scope`` map an ``RRTS_INTAKE_ROLE`` identity to the fixed,
    heavily-restricted ``rrts_intake`` scope, so a leaked ``rrts_relay``
    token is bounded. ``/chat/capture/extract`` does NOT go through that
    machinery — it drives ``vault_create`` + structuring (task emission)
    + note extraction DIRECTLY off ``talker_config``, i.e. at the
    instance's own authority. So the bound that makes ``rrts_relay`` safe
    for chat does not exist on this surface, and a leaked reporter token
    could otherwise turn reporter-controlled content into vault notes and
    tasks. The pin is what keeps ``auth.py``'s "bounded — cannot escalate
    scope" property TRUE.

    Peer-pin (401 ``wrong_peer``) rather than the role-refusal (403
    ``forbidden``) shape used by ``routes_notify`` / ``routes_day``: those
    refuse a reporter because the DATA is the operator's (a recipient
    question, and the caller is authenticated-but-not-entitled). Here the
    question is what the TOKEN may authorise — a route performing
    unmediated vault writes requires the chat peer's authority — which is
    the ``routes_brief`` / ``routes_brief_audio`` single-peer shape, and
    it refuses one layer earlier. It is also closed-by-default: a future
    third peer added to ``_resolve_relay_identity`` is refused here until
    somebody widens THIS list on purpose.
    """
    peer = request.get("transport_peer", "")
    if peer != WEB_CHAT_PEER:
        log.warning(
            "web.chat.capture_wrong_peer",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=WEB_CHAT_PEER,
            detail="capture toggle / span extraction drives unmediated "
                   "vault writes — it requires the dedicated chat 'web' "
                   "peer token, not rrts_relay / web_ingest — rejecting "
                   "(401)",
        )
        return None, web.json_response({"error": "wrong_peer"}, status=401)

    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return None, web.json_response({"error": "invalid_session"}, status=401)
    return identity, None


async def _handle_chat_capture(request: web.Request) -> web.StreamResponse:
    """POST /chat/capture — toggle capture mode ON/OFF for the live session.

    R1 (2026-08-20): the unobtrusive capture toggle. Body:
    ``{"session_key": "...", "on": true|false}``.

    SERVER truth: the toggle writes ``_capture_active`` +
    ``_capture_spans`` onto the active-session dict (capture_spans
    module), and the turn handlers consult THAT state — never a
    client-asserted flag. So a refresh mid-capture resumes capturing, and
    span boundaries are decided by request arrival order at the server:
    the turn that arrived one tick before toggle-on is NOT captured; the
    toggle-on turn onward is.

    Refused while a turn is in flight (409 ``turn_in_flight``): a span
    boundary stamped mid-append could land on either side of the turn
    being processed, and an exact boundary is the whole contract. The
    composer disables during a send, so the operator never meets this in
    normal use.

    Toggling OFF returns ``closed_span`` (``{"index", "turns"}``) for the
    just-closed non-empty span — the extraction offer's data — or
    ``null`` when the span was empty (discarded, logged) or capture was
    already off. Both directions are idempotent.

    Auth is :func:`_resolve_capture_identity` — the chat ``web`` peer-pin
    (401 ``wrong_peer``) BEFORE identity, then the usual mode-aware
    identity resolution. NOT the plain ``/chat/*`` spine: this route is
    the door onto span extraction, so it admits one peer, not two.
    """
    state_mgr = request.app[KEY_WEB_STATE_MGR]

    identity, err = _resolve_capture_identity(request)
    if err is not None:
        return err
    assert identity is not None  # exactly one of the pair is non-None

    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    session_key = body.get("session_key")
    on = body.get("on")
    if not isinstance(on, bool):
        return web.json_response(
            {"error": "on_required",
             "detail": "body must carry on: true|false"},
            status=400,
        )

    active_dict = state_mgr.get_active(identity.synthetic_chat_id)
    if active_dict is None or active_dict.get("session_id") != session_key:
        return web.json_response({"error": "no_such_session"}, status=404)

    in_flight = request.app[KEY_WEB_INFLIGHT]
    if session_key in in_flight:
        log.warning(
            "web.chat.capture_toggle_in_flight",
            user=identity.user,
            session_key=session_key,
            on=on,
            detail="a turn is running — span boundary would be ambiguous; "
                   "rejecting the toggle",
        )
        return web.json_response({"error": "turn_in_flight"}, status=409)

    from alfred.telegram import capture_spans

    closed_span: dict[str, Any] | None = None
    if on:
        state = capture_spans.begin_capture(
            state_mgr, identity.synthetic_chat_id
        )
    else:
        state, closed_span = capture_spans.end_capture(
            state_mgr, identity.synthetic_chat_id
        )
    log.info(
        "web.chat.capture_toggled",
        user=identity.user,
        session_key=session_key,
        on=on,
        spans=len(state["spans"]),
        closed_span_turns=(closed_span or {}).get("turns"),
    )
    return web.json_response({
        "session_key": session_key,
        "capture_active": state["capture_active"],
        "spans": state["spans"],
        # Always present — null is the explicit "no span closed" signal
        # (empty span discarded, or an idempotent repeat), so the client
        # never has to distinguish a missing field from an absent offer.
        "closed_span": closed_span,
    })


async def _handle_chat_capture_extract(
    request: web.Request,
) -> web.StreamResponse:
    """POST /chat/capture/extract — run extraction on one closed span.

    R1: the extraction offer's accept path. Body: ``{"session_key",
    "span_index"}``. Drives the preserved capture machinery against the
    span (``capture_spans.extract_capture_span``): span session record,
    structuring (memo branch included), note/zettel extraction per the
    existing per-instance rules, state marked extracted.

    Awaited IN the handler (two LLM calls — seconds, not minutes): the
    client shows a quiet "Extracting…" until the created records come
    back. Refusals are named: 404 ``no_such_session`` /
    ``span_not_found``, 409 ``span_open`` / ``already_extracted`` (the
    latter carrying the existing record + notes) / 409
    ``extraction_in_flight`` on a double-tap.

    Auth is :func:`_resolve_capture_identity` — the chat ``web`` peer-pin
    (401 ``wrong_peer``) BEFORE identity or any state read. This handler
    is the reason that pin is narrower than the chat spine's: the work
    below runs ``vault_create`` + structuring + note extraction off
    ``talker_config`` directly, OUTSIDE the ``run_turn`` / ``resolve_scope``
    machinery that bounds a vouched ``rrts_relay`` identity everywhere
    else in this file.
    """
    state_mgr = request.app[KEY_WEB_STATE_MGR]
    talker_config = request.app[KEY_WEB_TALKER_CONFIG]

    identity, err = _resolve_capture_identity(request)
    if err is not None:
        return err
    assert identity is not None  # exactly one of the pair is non-None

    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    session_key = body.get("session_key")
    span_index = body.get("span_index")
    if not isinstance(span_index, int) or isinstance(span_index, bool):
        return web.json_response(
            {"error": "span_index_required",
             "detail": "body must carry an integer span_index"},
            status=400,
        )

    active_dict = state_mgr.get_active(identity.synthetic_chat_id)
    if active_dict is None or active_dict.get("session_id") != session_key:
        return web.json_response({"error": "no_such_session"}, status=404)

    extracting: set = request.app[KEY_WEB_CAPTURE_EXTRACTING]
    guard_key = (session_key, span_index)
    if guard_key in extracting:
        return web.json_response(
            {"error": "extraction_in_flight"}, status=409
        )
    extracting.add(guard_key)
    try:
        from pathlib import Path as _Path

        from alfred.audit import agent_slug_for
        from alfred.telegram import capture_spans

        anchor_scope = (
            "hypatia"
            if (talker_config.instance.tool_set or "").lower() == "hypatia"
            else ""
        )
        primary = getattr(talker_config, "primary_users", None) or []
        result = await capture_spans.extract_capture_span(
            request.app[KEY_WEB_ANTHROPIC],
            state_mgr,
            _Path(talker_config.vault.path),
            identity.synthetic_chat_id,
            span_index,
            model=talker_config.anthropic.model or "claude-sonnet-4-6",
            agent_slug=agent_slug_for(talker_config),
            anchor_scope=anchor_scope,
            tool_set=talker_config.instance.tool_set or "",
            user_vault_path=primary[0] if primary else "",
        )
    finally:
        extracting.discard(guard_key)

    if result.skipped_reason == "span_not_found":
        return web.json_response({"error": "span_not_found"}, status=404)
    if result.skipped_reason == "span_open":
        return web.json_response(
            {"error": "span_open",
             "detail": "toggle capture off before extracting"},
            status=409,
        )
    if result.skipped_reason == "already_extracted":
        return web.json_response(
            {"error": "already_extracted",
             "record": result.record,
             "notes": result.notes},
            status=409,
        )
    log.info(
        "web.chat.capture_span_extracted",
        user=identity.user,
        session_key=session_key,
        span_index=span_index,
        record=result.record,
        notes=len(result.notes),
        skipped_reason=result.skipped_reason,
    )
    return web.json_response({
        "ok": bool(result.record),
        "session_key": session_key,
        "span_index": span_index,
        "record": result.record,
        "notes": result.notes,
        # "" on clean success, "memo" on the ≤1-message branch, or the
        # extractor's own named degradation — always present.
        "skipped_reason": result.skipped_reason,
    })


async def _handle_chat_history(request: web.Request) -> web.StreamResponse:
    """GET /chat/history/{session_key} — current active session transcript.

    M1 surfaces the CURRENT active session only (closed-session / vault-
    record history is a later milestone). Tool plumbing is flattened out.

    Capture toggle (R1): the response also carries ``capture_active`` +
    ``capture_spans`` (always present) — history is the one endpoint every
    bootstrap path ends on, so this is where a refreshed client learns it
    is still capturing (server truth, not client memory).
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
    from alfred.telegram import capture_spans

    return web.json_response({
        "turns": turns,
        "capture_active": capture_spans.capture_active(active_dict),
        "capture_spans": capture_spans.spans_summary(active_dict),
    })


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
    data_dir: "str | None" = None,
    feed_emit: Any = None,
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
    # The daemon's data dir for the outbound-read spool (#30). Optional —
    # ``None`` (a call site that doesn't thread it) makes the outbound route
    # serve its intentionally-left-blank empty payload rather than crash.
    app[KEY_WEB_DATA_DIR] = data_dir
    # Per-app concurrent-turn guard set (NOT module-global — concurrent test
    # apps in one process must not share in-flight state).
    app[KEY_WEB_INFLIGHT] = set()
    # Per-app in-flight span-extraction guard (capture toggle R1).
    app[KEY_WEB_CAPTURE_EXTRACTING] = set()

    app.router.add_post("/chat/open", _handle_chat_open)
    app.router.add_post("/chat/turn", _handle_chat_turn)
    app.router.add_post("/chat/stream", _handle_chat_stream)
    app.router.add_post("/chat/capture", _handle_chat_capture)
    app.router.add_post(
        "/chat/capture/extract", _handle_chat_capture_extract
    )
    app.router.add_get("/chat/history/{session_key}", _handle_chat_history)
    app.router.add_get("/chat/active", _handle_chat_active)

    mounted_routes = [
        "/chat/open",
        "/chat/active",
        "/chat/turn",
        "/chat/stream",
        "/chat/capture",
        "/chat/capture/extract",
        "/chat/history/{session_key}",
    ]

    # Auth routes (/auth/login, /auth/verify) — SESSION MODE ONLY. A relay
    # instance has no login surface (login/magic-link lives on the
    # session-mode login instance, e.g. Salem). Imported here (not at module
    # top) so routes_auth can import this module's siblings without a cycle.
    if not relay_mode:
        from .routes_auth import register_auth_handlers

        register_auth_handlers(app)
        # /auth/otp/* (#23) are mounted but answer 404 until the operator
        # flips web.auth.otp_enabled (default OFF — per-request gate).
        mounted_routes += [
            "/auth/login",
            "/auth/verify",
            "/auth/otp/request",
            "/auth/otp/verify",
        ]
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

    # Outbound-read route (/web/outbound/{kind}/latest) — #30 brief +
    # daily-sync READ-ON-OPEN. Same lazy-import anti-cycle pattern as STT.
    # Mounted in both modes; the handler peer-pins WEB_CHAT_PEER itself
    # (see routes_brief.py), so an ingest-token read is fail-closed 401.
    from .routes_brief import register_brief_routes

    register_brief_routes(app)
    mounted_routes.append("/web/outbound/{kind}/latest")

    # Brief-audio route (/web/brief/audio) — C3a interruptible player. Same
    # lazy-import anti-cycle pattern; the handler peer-pins WEB_CHAT_PEER itself
    # (routes_brief_audio.py), reads the daemon-spooled narration, synthesizes on
    # demand via the shared telegram.tts primitive, and caches per
    # (brief_date, content-hash, speed) — replay costs zero credits.
    from .routes_brief_audio import register_brief_audio_routes

    register_brief_audio_routes(app)
    mounted_routes.append("/web/brief/audio")
    mounted_routes.append("/web/brief/narration")

    # Notification routes (/chat/notifications*) — parity #22 KAL-LE ticket
    # → PWA notify, POLL slice. Default-ON (web.notifications.enabled; the
    # store only fills when a peer sends a web_notify-tagged notice). The
    # bounded store lives under data_dir; the fan-out sink is registered on
    # the TRANSPORT app (peer_handlers.register_web_notify_sink) so the
    # /peer/send message|notice branch reaches it WITHOUT telegram/daemon.py
    # importing web modules. Same lazy-import anti-cycle pattern as STT.
    if web_config.notifications.enabled:
        from alfred.transport.peer_handlers import register_web_notify_sink

        from .notify_state import WebNotifyStore, build_web_notify_sink
        from .routes_notify import register_notify_routes

        notify_store = None
        if data_dir:
            notify_store = WebNotifyStore.create(
                Path(data_dir) / "web_notify_state.json"
            )
            notify_store.load()
            register_web_notify_sink(
                app, build_web_notify_sink(notify_store, web_config)
            )
            log.info(
                "web.routes.notify_sink_registered",
                state_path=str(notify_store.state_path),
            )
        else:
            # Intentionally-left-blank: no data_dir → nothing to persist
            # into. The routes still mount (serving the explicit empty
            # payload); the peer fan-out logs its own sink-absent skip.
            log.info(
                "web.routes.notify_store_skipped",
                reason="no data_dir threaded — routes serve the empty "
                       "payload; sink not registered",
            )
        app[KEY_WEB_NOTIFY_STORE] = notify_store
        register_notify_routes(app)
        mounted_routes += ["/chat/notifications", "/chat/notifications/ack"]
    else:
        # Intentionally-left-blank: an explicit opt-out is a deliberate
        # state, logged so "no notification routes" is distinguishable
        # from a silent wiring skip.
        app[KEY_WEB_NOTIFY_STORE] = None
        log.info(
            "web.routes.notifications_disabled",
            reason="web.notifications.enabled=false",
        )

    # Contact-surface router routes (/day/*) — C4, the consumer of the
    # operator's ``contact-surface routing`` preference record. Default-ON
    # (web.contact_router.enabled) but INERT until a state path is anchored:
    # with no store the routes serve ``configured: false`` and the PWA stays
    # exactly where it is, so a default-on router does nothing on an instance
    # that has not wired it. The path comes from the config layer, which
    # resolved it through the SAME helper the feed-act dispatcher uses.
    if web_config.contact_router.enabled:
        from .contact_state import WebContactStore
        from .routes_day import register_day_routes

        contact_store = None
        if web_config.contact_router.state_path:
            contact_store = WebContactStore.create(
                web_config.contact_router.state_path
            )
            contact_store.load()
            log.info(
                "web.routes.contact_store_wired",
                state_path=str(contact_store.state_path),
            )
        else:
            # Intentionally-left-blank: nothing anchored the path (no
            # logging.dir, no explicit state_path). The routes still mount and
            # answer honestly; guessing the cwd would be worse than not routing.
            log.info(
                "web.routes.contact_store_skipped",
                reason="no state path anchored (web.contact_router.state_path "
                       "absent and no logging.dir to derive from) — /day/state "
                       "serves configured:false and the PWA will not route",
            )
        app[KEY_WEB_CONTACT_STORE] = contact_store
        # Optional: absent means overrides are still recorded and pattern cards
        # are proposed nowhere (logged at the override site, never silent).
        app[KEY_WEB_CONTACT_FEED] = feed_emit
        if feed_emit is None:
            log.info(
                "web.routes.contact_feed_absent",
                reason="no feed emit handle threaded — override patterns will "
                       "be recorded but no deck card can be dealt",
            )
        register_day_routes(app)
        mounted_routes += ["/day/state", "/day/contact", "/day/override"]
    else:
        # Intentionally-left-blank: an explicit opt-out is a deliberate state.
        app[KEY_WEB_CONTACT_STORE] = None
        app[KEY_WEB_CONTACT_FEED] = None
        log.info(
            "web.routes.contact_router_disabled",
            reason="web.contact_router.enabled=false",
        )

    # Voice routes (/voice/*) — V0 WebRTC echo, default-OFF behind
    # web.voice.enabled. register_voice_handlers is self-gating (returns
    # False + mounts nothing when voice is absent/disabled/relay-mode/
    # mis-piped, so the route table stays byte-identical); when it mounts it
    # also appends its own on_shutdown drain (no daemon.py change). Same
    # lazy-import anti-cycle pattern as STT.
    from .routes_voice import register_voice_handlers

    if register_voice_handlers(app, web_config=web_config):
        mounted_routes += ["/voice/offer", "/voice/close", "/voice/config"]

    log.info(
        "web.routes.registered",
        users=len(web_config.users),
        mode=mode,
        routes=mounted_routes,
    )
    return True

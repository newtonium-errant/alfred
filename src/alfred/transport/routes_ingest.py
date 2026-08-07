"""Cross-instance verbatim document ingest — ``POST /vault/ingest``.

A peer-token-gated transport route (NOT the web-chat mount) that writes a
single VERBATIM record into THIS instance's vault. The operator pastes /
uploads an artifact on the web surface, the Next BFF (the sole holder of
each target instance's ``web_ingest`` peer token) relays the body straight
to the chosen instance's transport, and it lands as a ``{document, note,
source}`` record with provenance frontmatter.

Why this fixes the Telegram large-markdown problem: the write is a
DETERMINISTIC single ``vault_create`` — NO ``run_turn``, NO LLM, NO
chunking — so the body is byte-for-byte what the operator pasted and the
ordering is never rearranged by an agent.

Auth: Layer 1 (peer token + ``X-Alfred-Client: web``) is enforced
automatically by the transport ``auth_middleware`` for every non-``/health``
route — this handler inherits it the moment it mounts. The handler ALSO
peer-pins ``transport_peer == "web_ingest"`` (``INGEST_PEER_NAME``): the
chat ``web`` token and this ``web_ingest`` token share
``allowed_clients: [web]``, so ``allowed_clients`` alone can't distinguish
them — without the pin a full-chat ``web`` token could drive an ingest
write. The asserted user (``X-Alfred-Ingest-User`` header / ``ingested_by``
body field) is PROVENANCE METADATA only, NEVER an authz principal:
possession of the target's ``web_ingest`` token IS the authority to write.
Role gating (owner-only) lives at the BFF, which holds the token.

Two vault gates fire inside ``vault_create``:
  1. ``_validate_type(record_type, scope='web_ingest')`` — gate 1; passes
     because ``document`` / ``source`` are tagged ``available_in_scopes``
     with ``web_ingest`` (``note`` is SCOPE_CANONICAL).
  2. ``check_scope('web_ingest', 'create', ...)`` — gate 2; the
     ``web_ingest_types_only`` policy enforces the {document, note, source}
     create set + denies edit/move/delete.

Opt-in inertness: :func:`register_ingest_routes` mounts NOTHING when
``transport.ingest.enabled`` is false (the default) — every un-opted-in
instance's transport server stays byte-unchanged.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from alfred.documents.pdf import (
    MAX_PDF_BYTES,
    DocumentExtractError,
    extract_pdf_text,
)

from .config import DEFAULT_INGEST_MAX_BODY_CHARS
from .peer_handlers import _get_vault_path
from .utils import get_logger

log = get_logger(__name__)

#: ``body_format`` value that routes an upload through PDF extraction. Absent
#: or ``"text"`` keeps the pre-#57 path byte-for-byte: ``body`` is taken as the
#: record body verbatim, which is what .md / .txt / .csv still do.
BODY_FORMAT_PDF = "pdf"

#: Log-event namespace for the extractor when the INGEST route drives it. The
#: shared module defaults to the talker's prefix; passing this keeps
#: ``talker.attachments.*`` meaning "a Telegram attachment" rather than
#: quietly covering web uploads too.
_PDF_LOG_PREFIX = "transport.ingest.pdf"

#: Extractor reason → (HTTP status, wire error code). Each row is a DISTINCT
#: refusal: six failures that render identically to the operator is exactly the
#: silence the ILB rule exists to prevent, and "it didn't work" is not a
#: sentence anyone can act on. ``pdf_no_text_layer`` is a terminal refusal by
#: operator ruling (2026-08-07) — deliberately NOT a fallback, and the wording
#: promises no future capability.
_PDF_REASON_STATUS: dict[str, tuple[int, str]] = {
    "pdf_no_text_layer": (422, "pdf_no_text_layer"),
    "pdf_encrypted": (422, "pdf_encrypted"),
    "pdf_unreadable": (400, "pdf_unreadable"),
    "pdf_empty_file": (400, "empty_file"),
    "pdf_support_missing": (503, "pdf_support_unavailable"),
}


def fence_extracted_text(text: str, *, language: str = "text") -> str:
    """Wrap extracted text in a fence that its own content cannot terminate.

    Mirrors the CSV half's ``fenceCsv`` (``web/lib/algernon/ingestUpload.ts``)
    on purpose — both surfaces reshape an upload into a record body, and two
    different fencing rules would mean the same document reads differently
    depending on which door it came through.

    The fence grows to one backtick longer than the longest run in the content,
    so a PDF quoting a code block cannot close the block early. Content is
    otherwise untouched; the single departure from byte-for-byte is a newline
    added before the closing fence when the text lacks one, because a closing
    fence must begin a line.
    """
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    content = text if text.endswith("\n") else text + "\n"
    return f"{fence}{language}\n{content}{fence}\n"


# Application-storage keys for the ingest route's config (stashed by
# ``register_ingest_routes`` so the handler reaches them without globals).
_KEY_INGEST_INSTANCE = "transport.ingest_instance"
_KEY_INGEST_MAX_BODY = "transport.ingest_max_body_chars"
_KEY_INGEST_TYPES = "transport.ingest_types"

# Provenance / field length ceilings (defense-in-depth; the BFF zod is the
# primary validator). Title bound mirrors CONTRACT §2 (1..300).
_MAX_TITLE_CHARS = 300
_MAX_SOURCE_CHARS = 500
_MAX_INGESTED_BY_CHARS = 200

# The transport peer NAME (``auth.tokens`` key) whose token authorises a
# verbatim ingest write. PINNED explicitly (not trusting
# ``X-Alfred-Client`` / ``allowed_clients`` alone): the chat ``web`` token
# and this ``web_ingest`` token BOTH carry ``allowed_clients: [web]``, so
# without this pin a request bearing the FULL-chat ``web`` token +
# ``X-Alfred-Client: web`` would clear Layer 1 (resolving ``transport_peer
# = "web"``) and drive a deterministic ingest write under a token meant for
# chat. See CLAUDE.md "Relay / asserted-identity routes — peer-pin
# requirement".
INGEST_PEER_NAME = "web_ingest"


def _json_error(status: int, error: str, **extra: Any) -> web.Response:
    """Consistent error shape — ``{"error": <code>, ...}`` (CONTRACT §2)."""
    payload: dict[str, Any] = {"error": error}
    payload.update(extra)
    return web.json_response(payload, status=status)


def _pdf_body_from_request(
    body: dict[str, Any], *, peer: str,
) -> "web.Response | tuple[str, int]":
    """Decode + extract a base64 PDF, or return the refusal that explains why.

    Returns ``(fenced_text, byte_count)`` on success and a ready
    :class:`web.Response` on every refusal, so the caller stays a straight
    line. Every exit is DISTINCT and logged with its own reason — an operator
    who uploads a password-protected statement must be told it is encrypted,
    not handed the same "couldn't read it" as a truncated download.

    Order matters: the BYTE cap is checked before extraction, so a 40 MiB file
    is refused without spending the memory to parse it.
    """
    raw_b64 = body.get("body_b64")
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        log.warning("transport.ingest.rejected", reason="empty_body", peer=peer,
                    body_format=BODY_FORMAT_PDF)
        return _json_error(400, "empty_body")

    try:
        # ``validate=True`` so stray non-alphabet bytes are an error rather
        # than silently skipped — a truncated upload should not decode into a
        # plausible-looking shorter PDF.
        pdf_bytes = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        log.warning("transport.ingest.rejected", reason="invalid_base64",
                    peer=peer, error=str(exc)[:120])
        return _json_error(400, "invalid_base64")

    if not pdf_bytes:
        log.warning("transport.ingest.rejected", reason="empty_file", peer=peer)
        return _json_error(400, "empty_file")

    if len(pdf_bytes) > MAX_PDF_BYTES:
        log.warning(
            "transport.ingest.rejected", reason="file_too_large", peer=peer,
            file_bytes=len(pdf_bytes), max_bytes=MAX_PDF_BYTES,
        )
        return _json_error(413, "file_too_large", max_bytes=MAX_PDF_BYTES)

    try:
        # ``max_chars=None`` — this route writes a VERBATIM record, so an
        # over-long extraction is REFUSED by the caller's char cap rather than
        # silently truncated the way the talker's context-bound path does.
        text = extract_pdf_text(
            pdf_bytes, max_chars=None, log_event_prefix=_PDF_LOG_PREFIX,
        )
    except DocumentExtractError as exc:
        status, code = _PDF_REASON_STATUS.get(
            exc.reason, (400, "pdf_unreadable"),
        )
        log.warning(
            "transport.ingest.rejected", reason=code, peer=peer,
            file_bytes=len(pdf_bytes), detail=str(exc)[:200],
        )
        return _json_error(status, code, detail=str(exc)[:200])

    log.info(
        "transport.ingest.pdf_extracted",
        peer=peer, file_bytes=len(pdf_bytes), text_chars=len(text),
    )
    return fence_extracted_text(text), len(pdf_bytes)


async def _handle_vault_ingest(request: web.Request) -> web.StreamResponse:
    """POST /vault/ingest — deterministic verbatim ``vault_create``.

    See module docstring for the auth + gate model. Error taxonomy
    (CONTRACT §2, JSON ``{"error": <code>}``):
        vault_not_configured (503), invalid_json (400), invalid_type (400),
        empty_title / title_too_long / empty_body (400),
        body_too_large (413), title_collision (409, + existing ``path``),
        ingest_failed (502).
    """
    # Lazy import — vault.ops pulls schema/scope (heavy) only when a
    # request actually fires, keeping this module import-light for tests.
    from alfred.vault.ops import VaultError, vault_create
    from alfred.vault.scope import WEB_INGEST_CREATE_TYPES, ScopeError

    peer = request.get("transport_peer", "")
    instance = request.app.get(_KEY_INGEST_INSTANCE, "") or ""

    # Peer-pin (defense-in-depth): only the dedicated ``web_ingest`` peer
    # may drive a verbatim ingest write. ``transport_peer`` is the matched
    # peer NAME set by ``auth_middleware``; pinning it closes the shared-
    # ``allowed_clients`` escalation where a full-chat ``web`` token +
    # ``X-Alfred-Client: web`` clears Layer 1 as peer ``web``.
    if peer != INGEST_PEER_NAME:
        log.warning(
            "transport.ingest.rejected",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=INGEST_PEER_NAME,
            detail="ingest requires the dedicated 'web_ingest' peer token — "
                   "refusing a write from another peer (e.g. web)",
        )
        return _json_error(401, "wrong_peer")

    vault_path = _get_vault_path(request)
    if vault_path is None:
        log.warning(
            "transport.ingest.rejected",
            reason="vault_not_configured",
            peer=peer,
        )
        return _json_error(503, "vault_not_configured")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → 400
        return _json_error(400, "invalid_json")
    if not isinstance(body, dict):
        return _json_error(400, "invalid_json")

    # --- record_type (gate-2 ceiling is WEB_INGEST_CREATE_TYPES) ---------
    # The config ``types`` list can only NARROW the universal set; it can
    # never widen it (the scope gate would reject a wider type anyway).
    configured_types = request.app.get(_KEY_INGEST_TYPES) or []
    allowed_types = (
        set(WEB_INGEST_CREATE_TYPES)
        if not configured_types
        else (set(configured_types) & set(WEB_INGEST_CREATE_TYPES))
    )
    record_type = body.get("record_type")
    if not isinstance(record_type, str) or record_type not in allowed_types:
        log.warning(
            "transport.ingest.rejected",
            reason="invalid_type",
            peer=peer,
            record_type=str(record_type),
            allowed=sorted(allowed_types),
        )
        return _json_error(
            400, "invalid_type", allowed=sorted(allowed_types),
        )

    # --- title (1..300) -------------------------------------------------
    title_raw = body.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    if not title:
        log.warning(
            "transport.ingest.rejected", reason="empty_title", peer=peer,
        )
        return _json_error(400, "empty_title")
    if len(title) > _MAX_TITLE_CHARS:
        log.warning(
            "transport.ingest.rejected",
            reason="title_too_long",
            peer=peer,
            title_chars=len(title),
        )
        return _json_error(400, "title_too_long", max_chars=_MAX_TITLE_CHARS)

    # --- body: verbatim text, or a PDF extracted into text (#57) --------
    # ``body_format`` absent / "text" keeps the pre-#57 path byte-for-byte —
    # .md, .txt and .csv still arrive as a ready body string and are written
    # exactly as sent.
    raw_format = body.get("body_format")
    body_format = raw_format.strip().lower() if isinstance(raw_format, str) else ""

    pdf_bytes_len = 0
    if body_format == BODY_FORMAT_PDF:
        extracted = _pdf_body_from_request(body, peer=peer)
        if isinstance(extracted, web.Response):
            return extracted
        body_text, pdf_bytes_len = extracted
    else:
        body_text = body.get("body")
        if not isinstance(body_text, str) or not body_text.strip():
            log.warning(
                "transport.ingest.rejected", reason="empty_body", peer=peer,
            )
            return _json_error(400, "empty_body")

    max_body_chars = int(
        request.app.get(_KEY_INGEST_MAX_BODY, DEFAULT_INGEST_MAX_BODY_CHARS)
    )
    if len(body_text) > max_body_chars:
        # For a PDF this is the EXTRACTED TEXT being too long, which is a
        # different fact from the file being too big — the two have separate
        # limits and separate error codes so the operator is never told to
        # shrink the wrong thing.
        reason = "extracted_text_too_large" if pdf_bytes_len else "body_too_large"
        log.warning(
            "transport.ingest.rejected",
            reason=reason,
            peer=peer,
            body_chars=len(body_text),
            max_chars=max_body_chars,
            pdf_bytes=pdf_bytes_len or None,
        )
        return _json_error(413, reason, max_chars=max_body_chars)

    # --- provenance (metadata only, never authz) ------------------------
    source = ""
    if isinstance(body.get("source"), str):
        source = body["source"].strip()[:_MAX_SOURCE_CHARS]

    # Header assertion wins over the body field (the BFF sets the header
    # from the verified identity cookie); both are provenance-only.
    ingested_by = (
        request.headers.get("X-Alfred-Ingest-User", "")
        or (body.get("ingested_by") if isinstance(body.get("ingested_by"), str) else "")
        or ""
    ).strip()[:_MAX_INGESTED_BY_CHARS]

    ingested_at = ""
    if isinstance(body.get("ingested_at"), str) and body["ingested_at"].strip():
        ingested_at = body["ingested_at"].strip()
    else:
        ingested_at = datetime.now(timezone.utc).isoformat()

    correlation_id = ""
    if isinstance(body.get("correlation_id"), str):
        correlation_id = body["correlation_id"].strip()[:64]

    # set_fields first (BFF-supplied extras like ingested_via /
    # origin_instance), then the canonical provenance overlays it so the
    # caller can't spoof the core provenance via set_fields. vault_create's
    # _filter_reserved_keys strips reserved keys (type/created/...).
    fm: dict[str, Any] = {}
    extra_fields = body.get("set_fields")
    if isinstance(extra_fields, dict):
        for k, v in extra_fields.items():
            if isinstance(k, str):
                fm[k] = v
    fm["ingested_via"] = fm.get("ingested_via", "web")
    if pdf_bytes_len:
        # Provenance the body itself can no longer carry: once extracted and
        # fenced, nothing in the record says it began as a PDF or how big that
        # PDF was. A reader six months on should not have to guess whether the
        # fenced text is the whole document.
        fm["ingested_format"] = BODY_FORMAT_PDF
        fm["ingested_source_bytes"] = pdf_bytes_len
    fm["source"] = source
    fm["ingested_by"] = ingested_by
    fm["ingested_at"] = ingested_at
    if correlation_id:
        fm["ingest_correlation_id"] = correlation_id

    # --- deterministic verbatim write -----------------------------------
    try:
        result = vault_create(
            vault_path,
            record_type,
            title,
            set_fields=fm,
            body=body_text,
            scope="web_ingest",
        )
    except VaultError as exc:
        details = getattr(exc, "details", None) or {}
        # Title collision — near-match OR exact existing file. Surface the
        # existing path so the operator can pick a different title (the
        # idempotency-on-retry property: re-POSTing the same artifact gets
        # a 409 + path the BFF can treat as already-present).
        if details.get("reason") == "near_match":
            existing = details.get("canonical_path", "")
            log.info(
                "transport.ingest.collision",
                reason="near_match",
                peer=peer,
                record_type=record_type,
                path=existing,
                correlation_id=correlation_id,
            )
            return _json_error(409, "title_collision", path=existing)
        msg = str(exc)
        if msg.startswith("File already exists:"):
            existing = msg.split(":", 1)[1].strip()
            log.info(
                "transport.ingest.collision",
                reason="exact",
                peer=peer,
                record_type=record_type,
                path=existing,
                correlation_id=correlation_id,
            )
            return _json_error(409, "title_collision", path=existing)
        # Any other VaultError (required-field / status validation) → 502.
        log.warning(
            "transport.ingest.failed",
            reason="vault_error",
            peer=peer,
            record_type=record_type,
            detail=msg[:200],
            correlation_id=correlation_id,
        )
        return _json_error(502, "ingest_failed", detail=msg[:200])
    except ScopeError as exc:
        # Unexpected: we pre-validated the type against the same set the
        # gate uses + web_ingest allows body writes. A ScopeError here is
        # a server-side policy mismatch — fail loud as 502.
        log.warning(
            "transport.ingest.failed",
            reason="scope_error",
            peer=peer,
            record_type=record_type,
            detail=str(exc)[:200],
            correlation_id=correlation_id,
        )
        return _json_error(502, "ingest_failed", detail=str(exc)[:200])
    except Exception as exc:  # noqa: BLE001 — surface any other failure as 502
        log.warning(
            "transport.ingest.failed",
            reason="unexpected",
            peer=peer,
            record_type=record_type,
            detail=str(exc)[:200],
            error_type=type(exc).__name__,
            correlation_id=correlation_id,
        )
        return _json_error(502, "ingest_failed", detail=str(exc)[:200])

    path = result.get("path", "")
    log.info(
        "transport.ingest.created",
        peer=peer,
        user=ingested_by or "(unset)",
        record_type=record_type,
        path=path,
        instance=instance,
        correlation_id=correlation_id,
        body_chars=len(body_text),
        body_format=body_format or "text",
        pdf_bytes=pdf_bytes_len or None,
    )
    response: dict[str, Any] = {
        "status": "created",
        "path": path,
        "record_type": record_type,
        "instance": instance,
    }
    if correlation_id:
        response["correlation_id"] = correlation_id
    return web.json_response(response)


def register_ingest_routes(
    app: web.Application,
    *,
    enabled: bool,
    instance_name: str,
    max_body_chars: int = DEFAULT_INGEST_MAX_BODY_CHARS,
    types: "list[str] | None" = None,
) -> bool:
    """Mount ``POST /vault/ingest`` onto ``app`` — IFF ingest is enabled.

    Returns ``True`` when the route was mounted, ``False`` when ingest is
    disabled (opt-in inertness: nothing is registered + the transport
    server is byte-unchanged). Must be called BEFORE the app is started
    (aiohttp forbids route additions on a started app); the daemon calls
    it via :func:`alfred.transport.server.wire_transport_app`, the same
    pre-start window as every other ``register_*`` helper.

    The route inherits the transport ``auth_middleware`` peer-gating
    automatically (it is a non-``/health`` route on the shared app).
    """
    if not enabled:
        # Intentionally-left-blank: disabled is a deliberate state, logged
        # so "no ingest route" is distinguishable from "wiring silently
        # skipped" in an operator audit.
        log.info(
            "transport.ingest.disabled",
            reason="transport.ingest.enabled is false / absent",
        )
        return False

    app[_KEY_INGEST_INSTANCE] = instance_name
    app[_KEY_INGEST_MAX_BODY] = int(max_body_chars)
    app[_KEY_INGEST_TYPES] = list(types or [])
    app.router.add_post("/vault/ingest", _handle_vault_ingest)
    log.info(
        "transport.ingest.registered",
        instance=instance_name,
        max_body_chars=int(max_body_chars),
        types=sorted(types) if types else "(universal: document, note, source)",
    )
    return True

"""Jeeves capture intake — ``POST /vault/jeeves/capture`` (task #81, stage 2).

The RECEIVING half of the garage ambient scribe. A capture device (tablet or
Pi) classifies a cued utterance as ROUTE, and posts the TRANSCRIPT here; this
route writes it into this instance's vault as one ``{note, source}`` record
with provenance frontmatter. Deterministic: no ``run_turn``, no LLM, no
chunking — the body is what the operator said, as transcribed.

**NEVER RAW AUDIO.** The whole design rests on ambient audio never leaving
the property except as a cued transcript, and this route is the last place
that fence can be checked. So it is checked HERE, structurally: a body
carrying any audio-shaped key is refused outright with its own error code,
before the gate, before the vault, before anything. A device that started
sending audio would be told to stop rather than quietly filling a vault with
recordings.

**Auth: a dedicated peer, pinned by NAME.** Jeeves is a DEVICE, so it
authenticates as its own peer with its own token rather than via an asserted
header — the stronger of the two models. Layer 1 (peer token +
``X-Alfred-Client``) is enforced by the transport ``auth_middleware`` for
every non-``/health`` route. This handler ALSO pins ``transport_peer ==
"jeeves"``: multiple peer tokens can legitimately share a client name, so
``allowed_clients`` alone cannot distinguish them, and without the pin
another peer's token would drive a capture write. See CLAUDE.md "Relay /
asserted-identity routes — peer-pin requirement".

**Fail-closed mode gate.** Every capture runs through
:func:`alfred.jeeves.gate.guard_capture` before the vault sees it. In
synthetic mode — the default — only a capture tagged ``synthetic: true`` is
processed. An instance that has not deliberately been flipped to live
accepts nothing real, which is the correct posture for a device that has not
been deployed.

**PERSONAL / RRTS ONLY, NEVER CLINICAL.** The ``jeeves`` scope creates
``{note, source}`` and cannot read, edit, move or delete. This module imports
the jeeves gate and nothing from ``alfred.scribe``; the jeeves peer token
must have no route to a clinical instance at all.

Opt-in inertness: :func:`register_jeeves_routes` mounts NOTHING when
``transport.jeeves.enabled`` is false (the default), so every instance
without a garage microphone has a byte-unchanged transport server.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from .peer_handlers import _get_vault_path
from .utils import get_logger

log = get_logger(__name__)

# Application-storage keys (stashed by the registrar so the handler reaches
# them without globals).
_KEY_JEEVES_INSTANCE = "transport.jeeves_instance"
_KEY_JEEVES_MAX_CHARS = "transport.jeeves_max_transcript_chars"
_KEY_JEEVES_TYPES = "transport.jeeves_types"

#: The transport peer NAME (``auth.tokens`` key) whose token authorises a
#: capture write. PINNED explicitly rather than trusting
#: ``X-Alfred-Client`` / ``allowed_clients``: peer tokens can share a client
#: name, and this token's whole purpose is that it grants ONLY this narrow,
#: write-only capability. A capture device sits in a garage; its credential
#: must not be interchangeable with one that can hold a conversation.
JEEVES_PEER_NAME = "jeeves"

#: Default record type when the device does not name one. A garage utterance
#: is a note; ``source`` exists for the case where the operator is capturing
#: something he is reading or listening to.
DEFAULT_RECORD_TYPE = "note"

#: Transcript ceiling (chars). A cued window is at most a few minutes of
#: speech — a few thousand characters. The bound exists so a malfunctioning
#: device cannot fill the vault with one request, not to constrain speech.
DEFAULT_MAX_TRANSCRIPT_CHARS = 32768

_MAX_TITLE_CHARS = 300
_MAX_VERB_CHARS = 32
_MAX_FACT_STRING_CHARS = 64

#: Body keys that would mean the device is sending AUDIO. Refused outright.
#: Named rather than sniffed by value so the refusal is explicable: the
#: device author reads the error and knows exactly which field to remove.
_AUDIO_KEYS: frozenset[str] = frozenset({
    "audio", "audio_b64", "audio_base64", "pcm", "wav", "waveform",
    "window_audio", "raw_audio", "samples", "recording",
})

#: The content-free capture facts a device may attach. Anything else is
#: DROPPED (not refused): a newer device build sending a field this instance
#: does not know must not have its capture rejected, but nor may an unvetted
#: key ride into vault frontmatter where it would be read as provenance.
_ALLOWED_FACT_KEYS: frozenset[str] = frozenset({
    "cue_confidence", "lookback_used_seconds", "requested_lookback_seconds",
    "lookahead_used_seconds", "lookahead_end_reason", "truncated_by_ring",
    "stt_calls", "stt_backend", "stt_confidence_raw",
})

_TITLE_SAFE_RE = re.compile(r"[^\w\s.-]+")


def _json_error(status: int, error: str, **extra: Any) -> web.Response:
    """Consistent error shape — ``{"error": <code>, ...}``."""
    payload: dict[str, Any] = {"error": error}
    payload.update(extra)
    return web.json_response(payload, status=status)


def default_title(captured_at: str) -> str:
    """A title derived from the capture time.

    The device does not invent titles — it has no idea what the utterance is
    about, and a device-generated summary would be a second LLM call for a
    string the operator renames anyway. A timestamp is honest, sorts, and
    makes collisions essentially impossible without a uniqueness scheme
    nobody has to maintain.
    """
    stamp = (captured_at or "").strip() or datetime.now(timezone.utc).isoformat()
    # Colons and the timezone suffix make an awkward filename; keep the
    # information, drop the punctuation the filesystem would rather not see.
    cleaned = _TITLE_SAFE_RE.sub(" ", stamp.replace(":", "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f"Jeeves capture {cleaned}"[:_MAX_TITLE_CHARS]


def _clean_facts(raw: Any) -> dict[str, Any]:
    """Keep only known, scalar, short capture facts.

    Three filters, because this dict lands in vault frontmatter and the
    device is the least-trusted thing in the system: an unknown key is
    dropped, a nested structure is dropped (that is where content hides),
    and an over-long string is dropped (every legitimate value here is a
    number, a boolean, or a short code).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _ALLOWED_FACT_KEYS:
            continue
        if isinstance(value, str):
            if len(value) <= _MAX_FACT_STRING_CHARS:
                out[key] = value
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
    return out


async def _handle_jeeves_capture(request: web.Request) -> web.StreamResponse:
    """POST /vault/jeeves/capture — save-only transcript intake.

    Error taxonomy (JSON ``{"error": <code>}``):
        wrong_peer (401), vault_not_configured (503), invalid_json (400),
        audio_not_accepted (400), invalid_type (400), empty_transcript (400),
        transcript_too_large (413), capture_refused (403),
        title_collision (409), capture_failed (502).
    """
    # Lazy imports — vault.ops pulls schema/scope (heavy), and the jeeves
    # package should not be imported on instances that never receive a
    # capture. Both load only when a request actually fires.
    from alfred.jeeves.config import load_from_unified as load_jeeves_config
    from alfred.jeeves.gate import JeevesCaptureRefused, guard_capture
    from alfred.vault.ops import VaultError, vault_create
    from alfred.vault.scope import JEEVES_CREATE_TYPES, JEEVES_SCOPE, ScopeError

    peer = request.get("transport_peer", "")
    instance = request.app.get(_KEY_JEEVES_INSTANCE, "") or ""

    # --- peer pin (defense in depth) ------------------------------------
    if peer != JEEVES_PEER_NAME:
        log.warning(
            "transport.jeeves.rejected",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=JEEVES_PEER_NAME,
            detail="the capture route requires the dedicated 'jeeves' peer "
                   "token — refusing a write from another peer. A capture "
                   "device's credential is deliberately not interchangeable "
                   "with one that can hold a conversation.",
        )
        return _json_error(401, "wrong_peer")

    vault_path = _get_vault_path(request)
    if vault_path is None:
        log.warning(
            "transport.jeeves.rejected", reason="vault_not_configured", peer=peer,
        )
        return _json_error(503, "vault_not_configured")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → 400
        return _json_error(400, "invalid_json")
    if not isinstance(body, dict):
        return _json_error(400, "invalid_json")

    # --- NEVER RAW AUDIO ------------------------------------------------
    # Checked FIRST, before the gate and before any parsing that could
    # succeed on an audio-bearing body. This is the last place the
    # cued-transcript-only fence can be enforced on the receiving side.
    audio_keys = sorted(set(body) & _AUDIO_KEYS)
    if audio_keys:
        log.error(
            "transport.jeeves.rejected",
            reason="audio_not_accepted",
            peer=peer,
            keys=audio_keys,
            detail="a capture body carried an audio-shaped field. This route "
                   "accepts TRANSCRIPTS ONLY — cued audio is deleted as soon "
                   "as STT returns and is never persisted or transmitted. "
                   "Refusing the whole request.",
        )
        return _json_error(400, "audio_not_accepted", fields=audio_keys)

    # --- record type (gate-2 ceiling is JEEVES_CREATE_TYPES) ------------
    configured_types = request.app.get(_KEY_JEEVES_TYPES) or []
    allowed_types = (
        set(JEEVES_CREATE_TYPES)
        if not configured_types
        else (set(configured_types) & set(JEEVES_CREATE_TYPES))
    )
    raw_type = body.get("record_type")
    record_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() \
        else DEFAULT_RECORD_TYPE
    if record_type not in allowed_types:
        log.warning(
            "transport.jeeves.rejected",
            reason="invalid_type",
            peer=peer,
            record_type=record_type,
            allowed=sorted(allowed_types),
        )
        return _json_error(400, "invalid_type", allowed=sorted(allowed_types))

    # --- transcript ------------------------------------------------------
    raw_transcript = body.get("transcript")
    transcript = raw_transcript.strip() if isinstance(raw_transcript, str) else ""
    if not transcript:
        log.warning(
            "transport.jeeves.rejected", reason="empty_transcript", peer=peer,
            detail="a capture arrived with no words — the device should not "
                   "route an empty transcript, and this instance will not "
                   "create a record the operator has to read and delete.",
        )
        return _json_error(400, "empty_transcript")

    max_chars = int(
        request.app.get(_KEY_JEEVES_MAX_CHARS, DEFAULT_MAX_TRANSCRIPT_CHARS)
    )
    if len(transcript) > max_chars:
        log.warning(
            "transport.jeeves.rejected",
            reason="transcript_too_large",
            peer=peer,
            transcript_chars=len(transcript),
            max_chars=max_chars,
        )
        return _json_error(413, "transcript_too_large", max_chars=max_chars)

    # --- the fail-closed mode gate ---------------------------------------
    # Runs BEFORE the vault write, and its refusal is a 403 rather than a
    # 400: the request was well-formed, the instance is refusing to process
    # it. A device author needs those told apart.
    jeeves_config = load_jeeves_config(request.app.get("jeeves_raw_config") or {})
    correlation_id = ""
    if isinstance(body.get("correlation_id"), str):
        correlation_id = body["correlation_id"].strip()[:64]
    try:
        guard_capture(
            jeeves_config,
            provenance=body.get("provenance"),
            source_id=correlation_id or "(capture)",
        )
    except JeevesCaptureRefused as exc:
        log.warning(
            "transport.jeeves.rejected",
            reason=exc.reason,
            peer=peer,
            mode=exc.mode,
            correlation_id=correlation_id,
            detail=exc.detail[:300],
        )
        return _json_error(403, "capture_refused", reason=exc.reason)

    # --- provenance frontmatter -------------------------------------------
    captured_at = ""
    if isinstance(body.get("captured_at"), str) and body["captured_at"].strip():
        captured_at = body["captured_at"].strip()
    else:
        captured_at = datetime.now(timezone.utc).isoformat()

    raw_title = body.get("title")
    title = raw_title.strip()[:_MAX_TITLE_CHARS] if isinstance(raw_title, str) \
        and raw_title.strip() else default_title(captured_at)

    verb = ""
    if isinstance(body.get("verb"), str):
        verb = body["verb"].strip()[:_MAX_VERB_CHARS]

    fm: dict[str, Any] = {
        "captured_via": "jeeves",
        "captured_at": captured_at,
        "capture_verb": verb,
        # MULTI-SPEAKER BY DESIGN. The garage is a workout/lounge and a
        # second household voice is the room's NORMAL condition (Q6 trial,
        # corrected 2026-08-11). Stamping it on every record means a reader
        # six months on does not have to infer whose words these are, and a
        # downstream consumer is never tempted to attribute the whole
        # transcript to one person.
        "capture_multi_speaker": True,
    }
    fm.update(_clean_facts(body.get("capture_facts")))
    if correlation_id:
        fm["capture_correlation_id"] = correlation_id

    # --- deterministic write ----------------------------------------------
    try:
        result = vault_create(
            vault_path,
            record_type,
            title,
            set_fields=fm,
            body=transcript,
            scope=JEEVES_SCOPE,
        )
    except VaultError as exc:
        details = getattr(exc, "details", None) or {}
        if details.get("reason") == "near_match":
            existing = details.get("canonical_path", "")
            log.info(
                "transport.jeeves.collision", reason="near_match", peer=peer,
                path=existing, correlation_id=correlation_id,
            )
            return _json_error(409, "title_collision", path=existing)
        msg = str(exc)
        if msg.startswith("File already exists:"):
            existing = msg.split(":", 1)[1].strip()
            log.info(
                "transport.jeeves.collision", reason="exact", peer=peer,
                path=existing, correlation_id=correlation_id,
            )
            return _json_error(409, "title_collision", path=existing)
        log.warning(
            "transport.jeeves.failed", reason="vault_error", peer=peer,
            record_type=record_type, detail=msg[:200],
            correlation_id=correlation_id,
        )
        return _json_error(502, "capture_failed", detail=msg[:200])
    except ScopeError as exc:
        # Unexpected: the type was pre-validated against the same set the
        # gate uses, and the jeeves scope allows body writes. A ScopeError
        # here is a server-side policy mismatch — fail loud.
        log.warning(
            "transport.jeeves.failed", reason="scope_error", peer=peer,
            record_type=record_type, detail=str(exc)[:200],
            correlation_id=correlation_id,
        )
        return _json_error(502, "capture_failed", detail=str(exc)[:200])
    except Exception as exc:  # noqa: BLE001 — surface anything else as 502
        log.warning(
            "transport.jeeves.failed", reason="unexpected", peer=peer,
            record_type=record_type, error_type=type(exc).__name__,
            detail=str(exc)[:200], correlation_id=correlation_id,
        )
        return _json_error(502, "capture_failed", detail=str(exc)[:200])

    path = result.get("path", "")
    log.info(
        "transport.jeeves.created",
        peer=peer,
        record_type=record_type,
        path=path,
        instance=instance,
        verb=verb,
        correlation_id=correlation_id,
        # A LENGTH, never the words. The record is the only place the
        # transcript lives.
        transcript_chars=len(transcript),
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


def register_jeeves_routes(
    app: web.Application,
    *,
    enabled: bool,
    instance_name: str = "",
    max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
    types: "list[str] | None" = None,
    jeeves_raw_config: "dict[str, Any] | None" = None,
) -> bool:
    """Mount ``POST /vault/jeeves/capture`` — IFF the route is enabled.

    Returns True when mounted, False when disabled (opt-in inertness:
    nothing is registered and the transport server is byte-unchanged). Must
    be called BEFORE the app is started, like every other ``register_*``
    helper.

    ``jeeves_raw_config`` is the UNIFIED config dict, from which the handler
    builds the typed jeeves config for the mode gate. Passing the raw dict
    rather than a typed object keeps this module free of a load-time
    dependency on the jeeves package — an instance that never receives a
    capture never imports it.
    """
    if not enabled:
        # Intentionally-left-blank: disabled is a deliberate state, logged so
        # "no capture route" is distinguishable from "wiring silently
        # skipped" in an operator audit.
        log.info(
            "transport.jeeves.disabled",
            reason="transport.jeeves.enabled is false / absent",
        )
        return False

    app[_KEY_JEEVES_INSTANCE] = instance_name
    app[_KEY_JEEVES_MAX_CHARS] = int(max_transcript_chars)
    app[_KEY_JEEVES_TYPES] = list(types or [])
    app["jeeves_raw_config"] = dict(jeeves_raw_config or {})
    app.router.add_post("/vault/jeeves/capture", _handle_jeeves_capture)

    # The mode is worth saying at startup: a receiving instance in synthetic
    # mode will refuse every real capture, and the operator should learn
    # that from a boot log rather than from a garage.
    from alfred.jeeves.config import load_from_unified as load_jeeves_config

    mode = load_jeeves_config(jeeves_raw_config or {}).mode
    log.info(
        "transport.jeeves.registered",
        instance=instance_name,
        peer=JEEVES_PEER_NAME,
        mode=mode,
        max_transcript_chars=int(max_transcript_chars),
        types=sorted(types) if types else "(universal: note, source)",
        detail=(
            "mode is 'synthetic' — every capture WITHOUT synthetic:true "
            "provenance will be refused. Set jeeves.mode: live once the "
            "capture device is deployed."
            if mode != "live" else
            "mode is 'live' — real garage captures will be written to the vault."
        ),
    )
    return True

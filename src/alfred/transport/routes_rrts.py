"""RRTS invoices-export receiver — ``POST /rrts/export``.

The invoice side of the Blue Cross payment loop. RRTS's n8n posts a full
JSON snapshot of its invoices nightly; this route writes it, whole, to a
file the reconciler reads. Without it the loop can only ever see what the
provider ANSWERED — never what it ignored, which is the half that makes an
aging watchdog possible.

**Scope: this peer can do exactly ONE thing.** A dedicated ``rrts_export``
peer token, and the only route it reaches is this one. No reads, no other
verbs, nothing else mounted under it. The existing peers are untouched and
un-widened — a token that could already do something else is not a token
this route will accept.

**Peer-pin, per the relay doctrine verbatim.** Layer 1 (the transport's
``auth_middleware``) resolves a token to a peer NAME; this handler then
pins ``transport_peer == "rrts_export"`` and fails closed with 401 +
``reason="wrong_peer"`` otherwise. ``allowed_clients`` alone is not
sufficient and never has been: two peers can legitimately share a client
name, so the name is what distinguishes them. The pin is one line and it
is the difference between "a valid token" and "the right token".

**Capture, do not interpret.** The document's shape is RRTS's, not ours.
Validation is exactly two things — it parses as JSON, and it carries a
top-level ``exported_at`` — because those are what make the file readable
and orderable at all. Everything else is written through untouched. A
receiver that enforced a schema would break the moment their side added a
field, and would be enforcing a contract nobody agreed to.

**The write is the atomic convention** the rest of this codebase already
uses: tmp-write then ``os.replace``. A reader (the reconciler) must never
see a half-written export, and ``os.replace`` on the same filesystem is
the guarantee that it cannot. FULL REPLACE is the contract — the snapshot
is the whole truth of the invoice side, so there is no merge to do.

**A backwards ``exported_at`` is a FINDING, not an error.** If the arriving
export is older than the one on disk, the write still happens (full replace
is what was agreed) but it is logged loudly. A backwards-moving export
means something upstream re-ran an old job or a clock is wrong — the
operator needs to know, and refusing the write would leave him with stale
data AND no signal.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from .utils import get_logger

log = get_logger(__name__)

#: The dedicated peer name this route pins. A token under any other peer —
#: however valid at Layer 1 — is refused.
RRTS_EXPORT_PEER_NAME = "rrts_export"

#: Their document is ~159 invoices. 10 MiB is generous headroom for growth
#: while still refusing anything that could only be a mistake or an attack;
#: the point of a cap is that SOME size is impossible, not that the limit is
#: tight.
DEFAULT_RRTS_MAX_BYTES = 10 * 1024 * 1024

#: The filename under the export directory. Fixed, because the reconciler
#: reads it by name — the DIRECTORY is config-derived, this is not.
EXPORT_FILENAME = "invoices.json"

#: Directory segment under the instance data dir. Lifted here from an inline
#: f-string in the wiring because the landing path is about to have TWO ends:
#: the receiver writes it and the P2 reader reads it. One resolution existed
#: only because one end existed — and a second spelling of a path is how a
#: writer and a reader end up on different files while both look correct.
#: The failure mode is silent and total: the snapshot lands where nothing
#: looks for it, and neither side reports anything wrong.
EXPORT_DIR_NAME = "rrts-export"

_KEY_RRTS_DIR = "_rrts_export_dir"
_KEY_RRTS_MAX = "_rrts_export_max_bytes"


def _json_error(status: int, error: str, **extra: Any) -> web.Response:
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    return web.json_response(payload, status=status)


def export_dir_for(data_dir: str) -> str:
    """``<data_dir>/rrts-export`` — the landing directory, or ``""``.

    THE one derivation. Both ends call it: the transport wiring resolves the
    receiver's write target through it, and the reconcile reader resolves its
    read source through it. Neither spells the segment.

    A STRING join rather than ``pathlib``, matching
    :func:`alfred.common.instance_paths.instance_data_path`: ``Path("./data")
    / "x"`` normalises the leading ``./`` away, and the byte-identity of a
    relative anchor is what lets this be introduced without moving anything
    already on disk.

    Empty ``data_dir`` yields ``""`` — the caller's cue to refuse rather than
    guess. Guessing the process cwd is the defect this whole family of path
    helpers exists to prevent.
    """
    base = (data_dir or "").strip().rstrip("/")
    return f"{base}/{EXPORT_DIR_NAME}" if base else ""


def export_path(export_dir: Path | str) -> Path:
    """``<export_dir>/invoices.json`` — the file the reconciler reads."""
    return Path(export_dir) / EXPORT_FILENAME


def export_path_for(data_dir: str) -> str:
    """``<data_dir>/rrts-export/invoices.json``, or ``""``.

    The whole path in one call, for readers that hold a data dir rather than
    a resolved export directory. Composed from the two helpers above so there
    is still exactly one place that knows either segment.
    """
    directory = export_dir_for(data_dir)
    return str(export_path(directory)) if directory else ""


def read_stored_exported_at(export_dir: Path | str) -> str:
    """The ``exported_at`` of the export currently on disk, or ``""``.

    Never raises. A missing, unreadable or malformed file yields ``""``,
    which reads as "nothing to compare against" — the first export, and any
    recovery from a corrupt one, must not be blocked by the staleness check
    that exists to inform the operator.
    """
    p = export_path(export_dir)
    if not p.is_file():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unreadable prior export is not fatal
        return ""
    if isinstance(data, dict):
        value = data.get("exported_at")
        if isinstance(value, str):
            return value
    return ""


def _write_atomic(export_dir: Path, raw: bytes) -> Path:
    """tmp-write + ``os.replace``. A reader never sees a partial file.

    The temp file is created in the DESTINATION directory, not the system
    temp dir: ``os.replace`` is only atomic within one filesystem, and a
    cross-device rename would silently degrade to copy-then-delete —
    reintroducing exactly the torn read this exists to prevent.
    """
    export_dir.mkdir(parents=True, exist_ok=True)
    final = export_path(export_dir)
    tmp = final.with_suffix(final.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final


async def _handle_rrts_export(request: web.Request) -> web.Response:
    """Receive one full invoices-export snapshot.

    Responses:
        200 ``{ok, bytes, exported_at}``
        400 ``invalid_json`` / ``missing_exported_at``
        401 ``wrong_peer``
        413 ``payload_too_large``
        503 ``export_dir_not_configured``
        500 ``write_failed``
    """
    peer = request.get("transport_peer", "")

    # Peer-pin. Layer 1 has already proven the token is valid; this proves
    # it is the RIGHT one. Two peers can share allowed_clients, so the peer
    # NAME is the only thing that distinguishes them.
    if peer != RRTS_EXPORT_PEER_NAME:
        log.warning(
            "transport.rrts_export.rejected",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=RRTS_EXPORT_PEER_NAME,
            detail="the invoices export requires its own dedicated peer "
                   "token — refusing a write from any other peer",
        )
        return _json_error(401, "wrong_peer")

    export_dir = request.app.get(_KEY_RRTS_DIR)
    if not export_dir:
        log.warning(
            "transport.rrts_export.rejected",
            reason="export_dir_not_configured",
            peer=peer,
            detail="the route is mounted but no export directory resolved; "
                   "nothing was written",
        )
        return _json_error(503, "export_dir_not_configured")

    raw = await request.read()
    size = len(raw)
    max_bytes = int(request.app.get(_KEY_RRTS_MAX) or DEFAULT_RRTS_MAX_BYTES)
    if size > max_bytes:
        log.warning(
            "transport.rrts_export.rejected",
            reason="payload_too_large",
            peer=peer,
            bytes=size,
            max_bytes=max_bytes,
        )
        return _json_error(
            413, "payload_too_large", bytes=size, max_bytes=max_bytes
        )

    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed body → 400
        log.warning(
            "transport.rrts_export.rejected",
            reason="invalid_json",
            peer=peer,
            bytes=size,
        )
        return _json_error(400, "invalid_json")

    if not isinstance(document, dict):
        log.warning(
            "transport.rrts_export.rejected",
            reason="invalid_json",
            peer=peer,
            bytes=size,
            detail="the export must be a JSON object at the top level",
        )
        return _json_error(400, "invalid_json")

    exported_at = document.get("exported_at")
    if not isinstance(exported_at, str) or not exported_at.strip():
        # The ONE field we require. Without it an export cannot be ordered
        # against its predecessor, so the staleness signal below — and any
        # future freshness check — would be structurally impossible.
        log.warning(
            "transport.rrts_export.rejected",
            reason="missing_exported_at",
            peer=peer,
            bytes=size,
            detail="the export must carry a top-level 'exported_at'; it is "
                   "the only field this receiver requires, and it is what "
                   "makes one snapshot orderable against another",
        )
        return _json_error(400, "missing_exported_at")

    exported_at = exported_at.strip()
    previous = read_stored_exported_at(export_dir)
    if previous and exported_at < previous:
        # WRITTEN ANYWAY — full replace is the agreed contract, and refusing
        # would leave the operator with stale data AND no signal. But a
        # backwards-moving export means an old job re-ran or a clock is
        # wrong, and that is his to know about.
        log.warning(
            "transport.rrts_export.stale_export",
            peer=peer,
            exported_at=exported_at,
            previous_exported_at=previous,
            detail="the arriving export is OLDER than the one on disk; "
                   "written anyway (full replace is the contract) but this "
                   "is a finding — an upstream job re-ran or a clock moved",
        )

    try:
        written = _write_atomic(Path(export_dir), raw)
    except Exception as exc:  # noqa: BLE001 — disk failure → 500, loudly
        log.warning(
            "transport.rrts_export.rejected",
            reason="write_failed",
            peer=peer,
            bytes=size,
            error_class=type(exc).__name__,
            error=str(exc)[:200],
        )
        return _json_error(500, "write_failed")

    log.info(
        "transport.rrts_export.received",
        peer=peer,
        bytes=size,
        exported_at=exported_at,
        path=str(written),
        replaced=bool(previous),
        previous_exported_at=previous or "(none — first export)",
    )
    return web.json_response(
        {"ok": True, "bytes": size, "exported_at": exported_at}
    )


def register_rrts_routes(
    app: web.Application,
    *,
    enabled: bool,
    export_dir: str = "",
    max_bytes: int = DEFAULT_RRTS_MAX_BYTES,
) -> bool:
    """Mount ``POST /rrts/export`` — IFF the receiver is enabled.

    Returns ``True`` when mounted, ``False`` when disabled. Opt-in
    inertness: a disabled receiver registers NOTHING, so every instance
    that does not receive an RRTS export has a byte-unchanged transport
    server. Must be called before the app starts, like every other
    ``register_*`` helper.

    The route inherits the transport ``auth_middleware`` automatically (it
    is a non-``/health`` route on the shared app); the handler's peer-pin
    is the second layer on top of that.
    """
    if not enabled:
        # ILB: disabled is a deliberate state and is logged, so "no rrts
        # route" is distinguishable from "the wiring was skipped" in an
        # operator audit.
        log.info(
            "transport.rrts_export.disabled",
            reason="transport.rrts_export.enabled is false / absent",
        )
        return False

    app[_KEY_RRTS_DIR] = export_dir or ""
    app[_KEY_RRTS_MAX] = int(max_bytes)
    app.router.add_post("/rrts/export", _handle_rrts_export)
    log.info(
        "transport.rrts_export.registered",
        export_dir=export_dir or "(unresolved — writes will 503)",
        max_bytes=int(max_bytes),
        peer=RRTS_EXPORT_PEER_NAME,
    )
    return True


__all__ = [
    "DEFAULT_RRTS_MAX_BYTES",
    "EXPORT_DIR_NAME",
    "EXPORT_FILENAME",
    "export_dir_for",
    "export_path_for",
    "RRTS_EXPORT_PEER_NAME",
    "export_path",
    "read_stored_exported_at",
    "register_rrts_routes",
]

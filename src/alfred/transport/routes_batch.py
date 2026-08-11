"""Bulk scan intake — ``POST /vault/batch``, a SAVE-ONLY route (#83).

The operator picks thirty scans and one instruction, and this route puts them
somewhere durable and answers. It does NOT look at a single image: extraction is
the ``batch_image`` drip campaign's job, one scan per item, budgeted and
resumable. Everything here is I/O and refusals.

That split is the whole design. A route that processed inline would hold an HTTP
request open for thirty vision calls, lose the entire batch to one timeout, and
have no way to resume; the drip runner already solves budgeting, retry,
quota-blocking and crash-resume for exactly this shape. So the contract is:
**save what was sent, mint the record that will carry the results, answer with
the batch id.** The first result appears when the campaign runs.

WHY MULTIPART, AND WHY THE CAPS ARE OURS. The obvious alternative — JSON with
base64 images, like ``/chat/turn`` — would buffer roughly 40 MiB in memory for a
30-scan batch before validating anything. This route streams each part to disk
with ``read_chunk`` and never holds more than one chunk.

That choice moves the entire size-refusal burden onto this handler, and the
reason is measured (aiohttp 3.13.5): ``request.multipart()`` does NOT enforce
the app's ``client_max_size``, while ``request.read()`` and ``request.post()``
both do. A 200 KiB multipart body reads clean through a 1 KiB ceiling. So the
app's 14 MiB guard — which refuses an oversize JSON ingest with a bare HTML 413
before any handler runs — will not fire here at all. Nothing below this route
will refuse anything on its behalf.

Hence three SEPARATE caps, each named here rather than borrowed, each enforced
DURING the stream, and each with its own error code and its own units:

  * :data:`DEFAULT_BATCH_MAX_IMAGE_BYTES` — per image (5 MiB). Mirrors the chat
    composer's per-image budget by intent, but is a distinct constant: what one
    scan may weigh is not the same decision as what one chat turn may carry, and
    a shared constant would silently move both.
  * :data:`DEFAULT_BATCH_MAX_IMAGES` — per batch, by COUNT (60).
  * :data:`DEFAULT_BATCH_MAX_TOTAL_BYTES` — per batch, by TOTAL BYTES (128 MiB).

Sixty images each under 5 MiB is 300 MiB, so the total cap binds well before the
count cap on heavy scans and the count cap binds first on light ones. Both are
real; neither is decorative. An operator refused by one must be told WHICH one,
because "make it smaller" and "send fewer" are different actions — that is why
``image_too_large``, ``too_many_images`` and ``batch_too_large`` are three codes
and not one.

Auth: Layer 1 (peer token + ``X-Alfred-Client``) is enforced by the transport
``auth_middleware`` for every non-``/health`` route. This handler ALSO peer-pins
``transport_peer == "web_batch"`` (:data:`BATCH_PEER_NAME`). That pin is not
ceremony: the chat ``web`` token, the ``web_ingest`` token and this one can all
legitimately carry ``allowed_clients: [web]``, so ``allowed_clients`` cannot
tell them apart, and without the pin a chat token would drive vault writes and
queue paid vision work. Same requirement as ``/vault/ingest``; see CLAUDE.md
"Relay / asserted-identity routes — peer-pin requirement". The asserted user
(``X-Alfred-Batch-User``) is PROVENANCE ONLY, never an authz principal —
possession of the ``web_batch`` token IS the authority.

Where the files land is deliberately NOT the vault inbox: see
``alfred.batch.paths``. The curator watches ``inbox/`` and admits every non-
hidden file regardless of extension, so thirty scans dropped there would be
processed a second time in parallel with this campaign. #88 is that defect
already happening to chat images.

Opt-in inertness: :func:`register_batch_routes` mounts NOTHING unless
``transport.batch.enabled`` is true, so every un-opted-in instance's transport
server stays byte-unchanged.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import (
    DEFAULT_BATCH_MAX_IMAGE_BYTES,
    DEFAULT_BATCH_MAX_IMAGES,
    DEFAULT_BATCH_MAX_INSTRUCTION_CHARS,
    DEFAULT_BATCH_MAX_TOTAL_BYTES,
)
from .peer_handlers import _get_vault_path
from .utils import get_logger

log = get_logger(__name__)

#: The transport peer NAME (``auth.tokens`` key) whose token authorises a batch
#: submission. PINNED explicitly rather than trusting ``allowed_clients``: the
#: chat ``web`` token and this one both legitimately carry
#: ``allowed_clients: [web]``, so without the pin a full-chat token would clear
#: Layer 1 and queue paid vision work. See the module docstring.
BATCH_PEER_NAME = "web_batch"

#: Image types accepted for a scan. Deliberately a LOCAL constant rather than an
#: import of ``alfred.web.routes_chat.ALLOWED_IMAGE_MEDIA_TYPES``: this is a
#: transport route and that is a web route, and the import would point the
#: dependency backwards. The two ARE meant to agree — a type the composer accepts
#: and the batch door refuses would be an arbitrary difference to an operator —
#: so a drift pin in ``tests/test_batch_route.py`` asserts they stay equal. That
#: is the house pattern for an intentionally-shared value across two surfaces:
#: name it on both sides, pin the equality, and let the pin fail if either moves.
ALLOWED_SCAN_MEDIA_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})

#: media type → on-disk extension. The stored filename is
#: ``<content-hash>.<ext>``; the extension is cosmetic (the manifest carries the
#: authoritative ``media_type``) but makes the directory readable to a human
#: opening it to see what a batch actually contained.
_EXT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

#: Read granularity for the streamed parts. Matches the chat/STT streamed reads.
_CHUNK_BYTES = 64 * 1024

#: Ceiling on a NON-file form field (instruction, title). Bounded for the same
#: reason the file parts are: ``part.text()`` is unbounded, and this route is the
#: only thing standing between a peer and the box's memory.
_MAX_FIELD_BYTES = 256 * 1024

#: Provenance field length ceilings (defense-in-depth; the BFF validates first).
_MAX_TITLE_CHARS = 200
_MAX_SUBMITTED_BY_CHARS = 200

# Application-storage keys, stashed by ``register_batch_routes`` so the handler
# reaches its config without globals (same shape as the ingest route).
_KEY_BATCH_INSTANCE = "transport.batch_instance"
_KEY_BATCH_DATA_DIR = "transport.batch_data_dir"
_KEY_BATCH_MAX_IMAGES = "transport.batch_max_images"
_KEY_BATCH_MAX_IMAGE_BYTES = "transport.batch_max_image_bytes"
_KEY_BATCH_MAX_TOTAL_BYTES = "transport.batch_max_total_bytes"
_KEY_BATCH_MAX_INSTRUCTION_CHARS = "transport.batch_max_instruction_chars"
_KEY_BATCH_CONFIG_PATH = "transport.batch_config_path"
_KEY_BATCH_KICK_ENABLED = "transport.batch_kick_enabled"

#: The drip campaign that drains submitted batches. Named here because the
#: route KICKS it by name; it must match the campaign key in the operator's
#: ``drip.campaigns`` block.
BATCH_CAMPAIGN_NAME = "batch_image"


class _Refused(Exception):
    """An honest refusal, carrying the response the operator should receive.

    Raised from deep inside the streaming loop and caught once at the top, so
    every early exit runs the SAME cleanup (the half-written batch directory is
    removed) instead of each refusal remembering to tidy up after itself. The
    forgotten-cleanup path is how a refused upload leaves debris behind.
    """

    def __init__(self, response: web.Response) -> None:
        super().__init__("batch submission refused")
        self.response = response


def _json_error(status: int, error: str, **extra: Any) -> web.Response:
    """Consistent error shape — ``{"error": <code>, ...}``, as ingest uses."""
    payload: dict[str, Any] = {"error": error}
    payload.update(extra)
    return web.json_response(payload, status=status)


def mint_batch_id(*, now: datetime | None = None) -> str:
    """A fresh batch id: ``<YYYYMMDD>-<8 hex>``.

    Date-prefixed so a human listing the batch directory can see at a glance
    what is recent, and random-suffixed so two submissions in the same second
    cannot collide. The shape satisfies ``alfred.batch.paths``'s id rule (starts
    alphanumeric, then ``[A-Za-z0-9_-]``), and the caller validates it against
    that rule anyway rather than trusting this function — the id becomes a path
    segment, and a minter that drifted from the validator is a directory
    traversal waiting to be written.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"{stamp}-{secrets.token_hex(4)}"


async def _read_field(part: Any, *, max_bytes: int = _MAX_FIELD_BYTES) -> str:
    """Read a non-file form field with a byte ceiling, decoded as UTF-8.

    ``part.text()`` would read without limit. A field is small by nature, so
    anything past the ceiling is discarded rather than refused: the caller's own
    length check on the decoded value produces the honest, named error, and
    truncating here only bounds the memory it takes to get there.
    """
    buf = bytearray()
    while True:
        chunk = await part.read_chunk(_CHUNK_BYTES)
        if not chunk:
            break
        if len(buf) < max_bytes:
            buf.extend(chunk[: max_bytes - len(buf)])
    return buf.decode("utf-8", errors="replace")


async def _drain_image_part(
    part: Any,
    *,
    dest_dir: Path,
    max_image_bytes: int,
    max_total_bytes: int,
    bytes_so_far: int,
    peer: str,
) -> tuple[str, str, int, str]:
    """Stream ONE image part to disk. Returns ``(item_id, filename, bytes, media_type)``.

    The caps are checked as bytes ARRIVE, not after: a 40 MiB image must be
    refused while it is still arriving, not once it is all in memory or all on
    disk, because refusing after the fact defeats the point of streaming.

    Writes to a temporary name and renames once the content hash is known —
    the file's final name IS its hash, which is what makes a resubmitted image
    land on the same path and dedupe to the same drip item.
    """
    filename_hint = part.filename or "(unnamed)"
    media_type = (part.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if media_type not in ALLOWED_SCAN_MEDIA_TYPES:
        log.warning(
            "transport.batch.rejected",
            reason="unsupported_media_type",
            peer=peer,
            filename=filename_hint[:120],
            media_type=media_type or "(none)",
        )
        raise _Refused(_json_error(
            415, "unsupported_media_type",
            filename=filename_hint[:120],
            media_type=media_type or "(none)",
            allowed=sorted(ALLOWED_SCAN_MEDIA_TYPES),
        ))

    digest = hashlib.sha256()
    size = 0
    tmp_path = dest_dir / f".incoming-{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = await part.read_chunk(_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                # PER-IMAGE cap. Its own code and its own units: the fix is to
                # send a smaller image, which is not the fix for either of the
                # batch-level caps below.
                if size > max_image_bytes:
                    log.warning(
                        "transport.batch.rejected",
                        reason="image_too_large",
                        peer=peer,
                        filename=filename_hint[:120],
                        max_bytes=max_image_bytes,
                    )
                    raise _Refused(_json_error(
                        413, "image_too_large",
                        filename=filename_hint[:120],
                        max_bytes=max_image_bytes,
                    ))
                # WHOLE-BATCH byte cap. The fix here is to send fewer or
                # lighter scans — a different action, hence a different code.
                if bytes_so_far + size > max_total_bytes:
                    log.warning(
                        "transport.batch.rejected",
                        reason="batch_too_large",
                        peer=peer,
                        filename=filename_hint[:120],
                        batch_bytes=bytes_so_far + size,
                        max_bytes=max_total_bytes,
                    )
                    raise _Refused(_json_error(
                        413, "batch_too_large",
                        max_bytes=max_total_bytes,
                        filename=filename_hint[:120],
                    ))
                digest.update(chunk)
                fh.write(chunk)

        if size == 0:
            log.warning(
                "transport.batch.rejected",
                reason="empty_image",
                peer=peer,
                filename=filename_hint[:120],
            )
            raise _Refused(_json_error(
                400, "empty_image", filename=filename_hint[:120],
            ))

        # sha256[:16] — the manifest's item id, the ledger row key and the drip
        # item id are all this same value (``alfred.batch.manifest``).
        item_id = digest.hexdigest()[:16]
        stored_name = f"{item_id}.{_EXT_BY_MEDIA_TYPE[media_type]}"
        os.replace(tmp_path, dest_dir / stored_name)
        return item_id, stored_name, size, media_type
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


async def _handle_vault_batch(request: web.Request) -> web.StreamResponse:
    """POST /vault/batch — save N scans + one instruction, answer with an id.

    Error taxonomy (JSON ``{"error": <code>}``)::

        wrong_peer (401)            batch_not_configured (503)
        vault_not_configured (503)  not_multipart (415)
        empty_instruction (400)     instruction_too_long (400)
        unsupported_media_type (415)
        empty_image (400)           image_too_large (413)   [per image]
        too_many_images (413)       [count]
        batch_too_large (413)       [total bytes]
        no_images (400)             batch_create_failed (502)
    """
    # Lazy imports: vault.ops pulls schema/scope (heavy), and the batch modules
    # are only needed on a real submission. Keeps this module import-light.
    from alfred.batch.manifest import BatchImage, BatchManifest, save_manifest
    from alfred.batch.paths import (
        BatchPathError,
        batch_dir,
        images_dir,
        manifest_path,
        validate_batch_id,
    )
    from alfred.batch.render import render_body
    from alfred.batch.seal import BATCH_STATUS_OPEN
    from alfred.vault.ops import VaultError, vault_create
    from alfred.vault.scope import ScopeError

    peer = request.get("transport_peer", "")

    # Peer-pin. Fail-closed, and FIRST — before any config read or disk touch.
    if peer != BATCH_PEER_NAME:
        log.warning(
            "transport.batch.rejected",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=BATCH_PEER_NAME,
            detail="batch submit requires the dedicated 'web_batch' peer "
                   "token — refusing a submission from another peer",
        )
        return _json_error(401, "wrong_peer")

    vault_path = _get_vault_path(request)
    if vault_path is None:
        log.warning(
            "transport.batch.rejected", reason="vault_not_configured", peer=peer,
        )
        return _json_error(503, "vault_not_configured")

    instance = request.app.get(_KEY_BATCH_INSTANCE, "") or ""
    data_dir = request.app.get(_KEY_BATCH_DATA_DIR, "") or ""
    if not instance or not data_dir:
        # Fail loud rather than defaulting: an unscoped batch directory is
        # shared across every instance on the box (alfred.batch.paths).
        log.warning(
            "transport.batch.rejected",
            reason="batch_not_configured",
            peer=peer,
            instance=instance or "(unset)",
            data_dir=data_dir or "(unset)",
        )
        return _json_error(503, "batch_not_configured")

    if not (request.content_type or "").startswith("multipart/"):
        log.warning(
            "transport.batch.rejected",
            reason="not_multipart",
            peer=peer,
            content_type=request.content_type or "(none)",
        )
        return _json_error(415, "not_multipart")

    max_images = int(request.app.get(_KEY_BATCH_MAX_IMAGES, DEFAULT_BATCH_MAX_IMAGES))
    max_image_bytes = int(request.app.get(
        _KEY_BATCH_MAX_IMAGE_BYTES, DEFAULT_BATCH_MAX_IMAGE_BYTES,
    ))
    max_total_bytes = int(request.app.get(
        _KEY_BATCH_MAX_TOTAL_BYTES, DEFAULT_BATCH_MAX_TOTAL_BYTES,
    ))
    max_instruction_chars = int(request.app.get(
        _KEY_BATCH_MAX_INSTRUCTION_CHARS, DEFAULT_BATCH_MAX_INSTRUCTION_CHARS,
    ))

    batch_id = validate_batch_id(mint_batch_id())
    try:
        root = batch_dir(data_dir, instance, batch_id)
        imgs_dir = images_dir(data_dir, instance, batch_id)
    except BatchPathError as exc:
        log.warning(
            "transport.batch.rejected",
            reason="batch_not_configured", peer=peer, detail=str(exc)[:200],
        )
        return _json_error(503, "batch_not_configured", detail=str(exc)[:200])
    imgs_dir.mkdir(parents=True, exist_ok=True)

    instruction = ""
    title_field = ""
    images: list[BatchImage] = []
    seen: set[str] = set()
    transferred_bytes = 0
    duplicates = 0

    try:
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break

            # A part with no filename is a plain form field.
            if part.filename is None:
                field = (part.name or "").strip()
                if field == "instruction":
                    instruction = (await _read_field(part)).strip()
                elif field == "title":
                    title_field = (await _read_field(part)).strip()
                else:
                    # Drain unknown fields so the reader can advance; ignoring
                    # them without reading would stall the stream.
                    await _read_field(part)
                continue

            # COUNT cap, checked BEFORE reading the 61st image's bytes — there
            # is no reason to transfer a file that is already refused.
            if len(images) >= max_images:
                log.warning(
                    "transport.batch.rejected",
                    reason="too_many_images",
                    peer=peer,
                    max_images=max_images,
                )
                raise _Refused(_json_error(
                    413, "too_many_images", max_images=max_images,
                ))

            item_id, stored, size, media_type = await _drain_image_part(
                part,
                dest_dir=imgs_dir,
                max_image_bytes=max_image_bytes,
                max_total_bytes=max_total_bytes,
                bytes_so_far=transferred_bytes,
                peer=peer,
            )
            transferred_bytes += size
            # Content-addressed dedupe. A retransmitted identical scan lands on
            # the same path and must become ONE manifest entry: the item id IS
            # the content hash, so two entries would be two drip items pointing
            # at one ledger row — the second could never be verified.
            if item_id in seen:
                duplicates += 1
                continue
            seen.add(item_id)
            images.append(BatchImage(
                item_id=item_id,
                filename=stored,
                sha256=item_id,
                bytes=size,
                media_type=media_type,
            ))

        # --- the submission is complete; validate what arrived --------------
        if not instruction:
            log.warning(
                "transport.batch.rejected", reason="empty_instruction", peer=peer,
            )
            raise _Refused(_json_error(400, "empty_instruction"))
        if len(instruction) > max_instruction_chars:
            log.warning(
                "transport.batch.rejected",
                reason="instruction_too_long",
                peer=peer,
                instruction_chars=len(instruction),
                max_chars=max_instruction_chars,
            )
            raise _Refused(_json_error(
                400, "instruction_too_long", max_chars=max_instruction_chars,
            ))
        if not images:
            # ILB: an empty batch is refused with words rather than accepted as
            # a batch of zero, which would mint a record that can never fill in.
            log.warning(
                "transport.batch.rejected",
                reason="no_images",
                peer=peer,
                duplicates=duplicates,
            )
            raise _Refused(_json_error(400, "no_images"))

        submitted_by = (
            request.headers.get("X-Alfred-Batch-User", "") or ""
        ).strip()[:_MAX_SUBMITTED_BY_CHARS]
        created_at = datetime.now(timezone.utc).isoformat()

        # The title always carries the batch id. That is not decoration: a
        # collision here would 409 AFTER the whole upload had been streamed to
        # disk, which is the most expensive possible moment to refuse, and the
        # id is the one component guaranteed unique.
        base = title_field[:_MAX_TITLE_CHARS] or instruction.splitlines()[0][:80]
        title = f"{base} ({batch_id})"

        manifest = BatchManifest(
            batch_id=batch_id,
            instruction=instruction,
            created_at=created_at,
            instance=instance,
            submitted_by=submitted_by,
            images=images,
        )

        # Mint the carried record. A CREATE, never an edit — the regenerate
        # guard (``alfred.batch.seal.assert_regenerable``) governs every
        # subsequent body write, and this route deliberately performs none.
        try:
            result = vault_create(
                vault_path,
                "note",
                title,
                set_fields={
                    "batch_id": batch_id,
                    "batch_status": BATCH_STATUS_OPEN,
                    "batch_items_total": len(images),
                    "batch_items_done": 0,
                    "batch_items_failed": 0,
                    "batch_created_at": created_at,
                    "ingested_via": "web",
                    "ingested_by": submitted_by,
                    "source": f"batch upload — {len(images)} scans",
                },
                body=render_body(manifest, []),
                scope="vera_batch",
            )
        except (VaultError, ScopeError) as exc:
            log.warning(
                "transport.batch.failed",
                reason=type(exc).__name__,
                peer=peer,
                batch_id=batch_id,
                detail=str(exc)[:200],
            )
            raise _Refused(_json_error(
                502, "batch_create_failed", detail=str(exc)[:200],
            )) from exc

        # record_path is written into the manifest AFTER the record exists, so
        # a manifest on disk always points at a record that is really there.
        # The worker refuses outright on an empty record_path rather than
        # guessing, which is what makes this ordering safe to crash inside.
        manifest.record_path = result.get("path", "")
        save_manifest(manifest_path(data_dir, instance, batch_id), manifest)

    except _Refused as refusal:
        _discard_batch(root, batch_id=batch_id, reason="refused")
        return refusal.response
    except Exception as exc:  # noqa: BLE001 — any other failure surfaces as 502
        log.warning(
            "transport.batch.failed",
            reason="unexpected",
            peer=peer,
            batch_id=batch_id,
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        _discard_batch(root, batch_id=batch_id, reason="error")
        return _json_error(502, "batch_create_failed", detail=str(exc)[:200])

    # --- the kick (#83 item 5) ------------------------------------------
    # Everything above is durable. This is the optional head start, and the
    # response tells the truth about whether it happened.
    kick_enabled = bool(request.app.get(_KEY_BATCH_KICK_ENABLED, False))
    kicked_pid: int | None = None
    if kick_enabled:
        from alfred.drip.kick import kick_drip_run

        kicked_pid = kick_drip_run(
            config_path=str(request.app.get(_KEY_BATCH_CONFIG_PATH, "") or ""),
            campaign=BATCH_CAMPAIGN_NAME,
            data_dir=data_dir,
        )
    else:
        # ILB, and the most important signal on this route. Without it the
        # operator uploads thirty scans, gets a cheerful id back, and watches a
        # record sit at 0 of N forever with nothing anywhere saying why. The
        # scans ARE saved; what is missing is anything configured to read them.
        log.warning(
            "transport.batch.no_consumer",
            batch_id=batch_id,
            campaign=BATCH_CAMPAIGN_NAME,
            images=len(images),
            detail="batch saved but NOT queued — the batch_image drip "
                   "campaign is not enabled on this instance, so nothing "
                   "will process these scans until it is",
        )

    log.info(
        "transport.batch.created",
        peer=peer,
        user=submitted_by or "(unset)",
        batch_id=batch_id,
        images=len(images),
        duplicates=duplicates,
        bytes=transferred_bytes,
        path=manifest.record_path,
        instance=instance,
        kicked_pid=kicked_pid,
    )
    response: dict[str, Any] = {
        # "queued" means something is configured to process this batch;
        # "saved" means the files are safe but nothing will read them. They
        # are different facts and the UI says different things about them, so
        # they must not share a word.
        "status": "queued" if kick_enabled else "saved",
        "batch_id": batch_id,
        "images": len(images),
        "bytes": transferred_bytes,
        "path": manifest.record_path,
        "instance": instance,
    }
    if duplicates:
        # Reported, not hidden: the operator picked N files and the record will
        # show fewer sections. Saying so here is the difference between a
        # dedupe and an apparent silent loss.
        response["duplicates"] = duplicates
    return web.json_response(response)


def _discard_batch(root: Path, *, batch_id: str, reason: str) -> None:
    """Remove a batch directory that will never be processed.

    A refused submission must not leave images behind. They would be inert (the
    campaign's ``worklist`` skips any directory without a manifest) but they
    would also be invisible megabytes accumulating on the box with nothing that
    ever reports them.

    Failure to clean up is logged and swallowed: the operator's refusal is the
    answer that matters, and turning a tidy-up error into a different HTTP
    status would be actively misleading about what went wrong.
    """
    try:
        shutil.rmtree(root, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning(
            "transport.batch.cleanup_failed",
            batch_id=batch_id,
            reason=reason,
            path=str(root),
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return
    log.info(
        "transport.batch.discarded",
        batch_id=batch_id,
        reason=reason,
        detail="submission was refused — its partial directory was removed "
               "so nothing is left behind that no campaign will ever read",
    )


def register_batch_routes(
    app: web.Application,
    *,
    enabled: bool,
    instance_name: str,
    data_dir: str = "",
    max_images: int = DEFAULT_BATCH_MAX_IMAGES,
    max_image_bytes: int = DEFAULT_BATCH_MAX_IMAGE_BYTES,
    max_total_bytes: int = DEFAULT_BATCH_MAX_TOTAL_BYTES,
    max_instruction_chars: int = DEFAULT_BATCH_MAX_INSTRUCTION_CHARS,
    config_path: str = "",
    kick_enabled: bool = False,
) -> bool:
    """Mount ``POST /vault/batch`` — IFF batch submit is enabled.

    Returns ``True`` when mounted, ``False`` when disabled (opt-in inertness:
    nothing is registered and the transport server stays byte-unchanged). Must
    be called BEFORE the app is started, like every other ``register_*`` helper.

    ``kick_enabled`` should be TRUE exactly when the ``batch_image`` drip
    campaign is enabled on this instance. It is not a second on/off switch for
    the operator: it is how the route knows whether anything will actually
    process a submission, which is the difference between answering "queued"
    and answering "saved". Guessing True here would promise processing that
    never happens; guessing False would under-report a working setup.

    ``config_path`` is this instance's config file, passed to the kicked run as
    ``--config``. Empty means no kick — see :func:`~alfred.drip.kick.kick_drip_run`
    for why running without it would drain a DIFFERENT instance's campaigns.
    """
    if not enabled:
        # Intentionally-left-blank: disabled is a deliberate state, logged so
        # "no batch route" is distinguishable from "wiring silently skipped".
        log.info(
            "transport.batch.disabled",
            reason="transport.batch.enabled is false / absent",
        )
        return False

    app[_KEY_BATCH_INSTANCE] = instance_name
    app[_KEY_BATCH_DATA_DIR] = str(data_dir or "")
    app[_KEY_BATCH_MAX_IMAGES] = int(max_images)
    app[_KEY_BATCH_MAX_IMAGE_BYTES] = int(max_image_bytes)
    app[_KEY_BATCH_MAX_TOTAL_BYTES] = int(max_total_bytes)
    app[_KEY_BATCH_MAX_INSTRUCTION_CHARS] = int(max_instruction_chars)
    app[_KEY_BATCH_CONFIG_PATH] = str(config_path or "")
    # A kick without a config path would drain the wrong instance, so the two
    # are ANDed here rather than checked separately at request time.
    app[_KEY_BATCH_KICK_ENABLED] = bool(kick_enabled and config_path)
    app.router.add_post("/vault/batch", _handle_vault_batch)
    if kick_enabled and not config_path:
        log.warning(
            "transport.batch.kick_unavailable",
            instance=instance_name,
            detail="the batch_image campaign is enabled but no config path "
                   "reached the route, so submissions will not kick a run. "
                   "They are still saved, and the hourly timer still drains "
                   "them — only the head start is lost.",
        )
    log.info(
        "transport.batch.registered",
        instance=instance_name,
        data_dir=str(data_dir or "(unset)"),
        max_images=int(max_images),
        max_image_bytes=int(max_image_bytes),
        max_total_bytes=int(max_total_bytes),
        kick_enabled=bool(kick_enabled and config_path),
    )
    return True


__all__ = [
    "ALLOWED_SCAN_MEDIA_TYPES",
    "BATCH_PEER_NAME",
    "mint_batch_id",
    "register_batch_routes",
]

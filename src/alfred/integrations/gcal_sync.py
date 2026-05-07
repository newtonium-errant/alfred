"""Pure GCal-sync functions used by both the cross-instance event-propose
handler AND the in-process vault-ops hooks.

Phase A+ shipped the original sync logic inline in
``_handle_canonical_event_propose_create``. This module extracts the
request-independent pieces so they can be reused from:

  * ``alfred.transport.peer_handlers._sync_event_to_gcal`` — thin shim
    that pulls client/config/sentinel out of the aiohttp app and
    delegates here.
  * ``alfred.vault.ops`` event-create / event-update / event-delete
    hooks — the daemon registers closures around these functions so a
    direct ``vault_create("event", ...)`` call from the talker
    conversation, the instructor executor, the daily-sync dispatcher,
    or any future caller mirrors to GCal automatically.
  * ``alfred gcal backfill`` CLI — iterates existing vault events and
    fires the create function on each unsynced record.

All three functions take their dependencies as explicit args (no
hidden aiohttp request lookup, no global state) so they're trivially
testable and reusable.

Return shapes — every function returns a dict so callers can branch on
content without exception-handling for expected non-failure paths:

  * ``{}`` — gcal not configured / disabled. Silent skip.
  * ``{"event_id": "<id>", "calendar_label": "<label>"}`` — success.
    For create: file frontmatter has been written back with
    ``gcal_event_id`` + ``gcal_calendar``. For update: GCal patched,
    no vault writeback (vault is canonical, edit already did its job).
    For delete: the GCal event has been removed.
  * ``{"error": {"code": "<code>", "detail": "<msg>"}}`` — sync
    failed; vault state preserved. ``code`` ∈
    ``calendar_id_missing`` / ``auth_failed`` / ``missing_dependency``
    / ``api_error`` / ``stale_gcal_id`` / ``unknown``.
  * ``{"noop": "<reason>"}`` — for update/delete only: vault record
    has no ``gcal_event_id``, so there's nothing to patch / remove.
    Distinct from ``{}`` (gcal disabled) so callers can log differently.

The sentinel-aware skip logging (``_KEY_GCAL_INTENDED_ON``) lives in
the request-side shim (``peer_handlers._sync_event_to_gcal``) because
it depends on the aiohttp app context. The hook-side caller passes
``intended_on=True`` directly when the operator opted into gcal but
the client is None (setup failure preserved the intent — same
diagnostic value).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Title resolution — gcal_title vs name decoupling
# ---------------------------------------------------------------------------


def resolve_gcal_title(fm: dict) -> tuple[str, str]:
    """Resolve the GCal event title from a vault record's frontmatter.

    Decouples the vault filename / ``name`` field from the title that
    Google Calendar shows on Andrew's phone. The vault frequently needs
    a date-suffixed disambiguator on the filename (e.g.
    ``Novaket — May 13``, ``Fergus Bath 2026-05-12``) so recurring
    events stay findable; GCal renders the date in its own UI, so that
    suffix is redundant noise on the calendar entry.

    Resolution order (operator-set, no auto-derivation):

      1. ``gcal_title`` — explicit override. When set, used verbatim.
      2. ``title`` — existing fallback (peer-handler propose-create
         writes both ``name`` and ``title``; daemon-direct creates
         typically only set ``name``).
      3. ``name`` — final fallback (the vault filename stem).

    Returns ``(title, source)`` where ``source ∈ {"gcal_title",
    "title", "name", ""}``. The source string is logged at the sync
    layer (``title_source`` field on ``gcal.sync_created`` /
    ``gcal.sync_updated``) so operators can grep for "which records
    are using the override" without round-tripping through the
    frontmatter. Empty source means no usable title — the sync layer
    treats that as a degenerate case (returns empty string; GCal will
    refuse to create anyway).

    Per ``vault.schema.EVENT_GCAL_FIELDS`` — this is the canonical
    resolver; future callers MUST use it rather than re-deriving the
    chain (per ``feedback_marker_id_canonical_regex.md`` — once a
    grammar is established, consumers import the canonical resolver
    rather than copy-pasting the precedence rule).
    """
    gcal_title = fm.get("gcal_title")
    if isinstance(gcal_title, str) and gcal_title.strip():
        return gcal_title.strip(), "gcal_title"
    title = fm.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip(), "title"
    name = fm.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip(), "name"
    return "", ""


# ---------------------------------------------------------------------------
# Error classification (formerly in peer_handlers)
# ---------------------------------------------------------------------------


def classify_gcal_error(exc: BaseException) -> str:
    """Map a GCal-side exception to a stable code for downstream renderers.

    Codes are intentionally coarse for v1 — future refinements (e.g.
    splitting ``api_error`` into ``quota_exceeded`` / ``rate_limited``
    / ``server_error``) can add codes without breaking consumers that
    already handle the coarse value.

    Lazy-imports the ``GCalError`` hierarchy because this module is
    imported by ``vault.ops`` (via the hook closures registered at
    daemon startup), and ``vault.ops`` loads on every instance —
    including ones that didn't ``pip install '.[gcal]'``. Eager
    import would crash KAL-LE / Hypatia at startup.
    """
    try:
        from alfred.integrations.gcal import (
            GCalAPIError,
            GCalNotAuthorized,
            GCalNotInstalled,
        )
    except ImportError:
        return "unknown"

    if isinstance(exc, GCalNotAuthorized):
        return "auth_failed"
    if isinstance(exc, GCalNotInstalled):
        return "missing_dependency"
    if isinstance(exc, GCalAPIError):
        return "api_error"
    return "unknown"


# ---------------------------------------------------------------------------
# Internal: shared "is gcal usable?" gate
# ---------------------------------------------------------------------------


def _gcal_skip_check(
    *,
    client: Any,
    config: Any,
    intended_on: bool,
    correlation_id: str,
    op: str,
) -> dict[str, Any] | None:
    """Return the early-return value if gcal is unusable; ``None`` if usable.

    Centralizes the sentinel-aware skip logging so all three hooks
    behave the same way under the same conditions:

      * client is None / config is None / config.enabled is False:
        skip silently if intended_on is False; warn if intended_on is
        True (operator opted in but setup failed at startup).
      * config.alfred_calendar_id is empty: structured error so the
        caller surfaces "fix your config" rather than silently dropping.
    """
    if client is None or config is None or not getattr(config, "enabled", False):
        if intended_on:
            log.warning(
                "gcal.sync_skipped_but_intended_on",
                op=op,
                correlation_id=correlation_id,
                hint=(
                    "gcal.enabled is true in config but client setup failed "
                    "at daemon startup. Run `alfred gcal status` and "
                    "check daemon log for talker.daemon.gcal_setup_failed."
                ),
            )
        else:
            log.debug(
                "gcal.sync_skipped",
                op=op,
                reason="not_configured",
                correlation_id=correlation_id,
            )
        return {}
    if not getattr(config, "alfred_calendar_id", ""):
        log.warning(
            "gcal.sync_skipped",
            op=op,
            reason="alfred_calendar_id_empty",
            correlation_id=correlation_id,
        )
        return {
            "error": {
                "code": "calendar_id_missing",
                "detail": "gcal alfred_calendar_id not configured",
            }
        }
    return None


# ---------------------------------------------------------------------------
# Public: create
# ---------------------------------------------------------------------------


def sync_event_create_to_gcal(
    *,
    client: Any,
    config: Any,
    intended_on: bool = False,
    file_path: Path,
    title: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime,
    correlation_id: str = "",
    title_source: str = "name",
) -> dict[str, Any]:
    """Push a freshly-created vault event to the configured calendar.

    Pushes to ``config.alfred_calendar_id`` (operator-visible as
    Andrew's Calendar (S.A.L.E.M.) per the canonical naming established
    in the SKILL.md sweep at commit ``332b66c``).

    Mirrors the pre-Phase-A+ inline logic exactly, just lifted out of
    the aiohttp request handler. On success: writes ``gcal_event_id``
    + ``gcal_calendar`` (from ``config.alfred_calendar_label``) back
    into the vault record's frontmatter.

    ``title_source`` describes which frontmatter field the caller
    resolved the title from — one of ``"gcal_title"`` / ``"title"`` /
    ``"name"`` (per :func:`resolve_gcal_title`). Logged on
    ``gcal.sync_created`` so operators can grep for records using the
    override. Default ``"name"`` matches the pre-decoupling behavior.
    """
    skip = _gcal_skip_check(
        client=client, config=config, intended_on=intended_on,
        correlation_id=correlation_id, op="create",
    )
    if skip is not None:
        return skip

    from alfred.integrations.gcal import GCalError

    create_kwargs: dict[str, Any] = {
        "start": start_dt,
        "end": end_dt,
        "title": title,
        "description": description,
    }
    if getattr(config, "default_time_zone", ""):
        create_kwargs["time_zone"] = config.default_time_zone

    try:
        event_id = client.create_event(
            config.alfred_calendar_id,
            **create_kwargs,
        )
    except GCalError as exc:
        code = classify_gcal_error(exc)
        log.warning(
            "gcal.sync_create_failed",
            error=str(exc),
            error_code=code,
            correlation_id=correlation_id,
        )
        return {"error": {"code": code, "detail": str(exc)}}

    calendar_label = getattr(config, "alfred_calendar_label", "") or "alfred"

    # Writeback. We re-load the file we just wrote rather than reusing
    # any in-memory frontmatter so any concurrent edits aren't clobbered
    # (defensive; current code paths are single-writer).
    try:
        post = frontmatter.load(str(file_path))
        post["gcal_event_id"] = event_id
        post["gcal_calendar"] = calendar_label
        new_text = frontmatter.dumps(post)
        if not new_text.endswith("\n"):
            new_text += "\n"
        file_path.write_text(new_text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # Soft fail — the GCal event exists, but our dedup loses its
        # anchor. Log + keep the success path so the caller still gets
        # the event_id (and can decide whether to retry the writeback).
        log.warning(
            "gcal.sync_create_writeback_failed",
            error=str(exc),
            event_id=event_id,
            path=str(file_path),
            correlation_id=correlation_id,
        )

    log.info(
        "gcal.sync_created",
        event_id=event_id,
        calendar_id=config.alfred_calendar_id,
        calendar_label=calendar_label,
        title_source=title_source,
        correlation_id=correlation_id,
    )
    return {"event_id": event_id, "calendar_label": calendar_label}


# ---------------------------------------------------------------------------
# Public: update
# ---------------------------------------------------------------------------


def sync_event_update_to_gcal(
    *,
    client: Any,
    config: Any,
    intended_on: bool = False,
    gcal_event_id: str,
    title: str | None = None,
    description: str | None = None,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    correlation_id: str = "",
    title_source: str | None = None,
) -> dict[str, Any]:
    """Patch an existing GCal event to mirror a vault edit.

    Caller is responsible for figuring out which fields actually
    changed (typically by inspecting ``vault_edit``'s
    ``fields_changed`` list). Pass only the fields you want patched —
    others stay as-is. None means "don't touch", "" means "set to
    empty" (only meaningful for description).

    Returns:
      * ``{}`` — gcal not configured (skip).
      * ``{"noop": "no_gcal_event_id"}`` — record has no
        ``gcal_event_id`` (was never synced; nothing to patch). The
        hook layer guards on this BEFORE calling, so this branch is a
        defensive backstop.
      * ``{"event_id": "<id>", "calendar_label": "<label>"}`` — success.
      * ``{"error": {"code": "stale_gcal_id", "detail": "..."}}`` —
        GCal answered 404 (event already deleted). Vault frontmatter
        keeps the stale ID for now; future janitor / repair pass can
        clean up.
      * ``{"error": {"code": "<code>", "detail": "<msg>"}}`` — other
        sync failure.
    """
    skip = _gcal_skip_check(
        client=client, config=config, intended_on=intended_on,
        correlation_id=correlation_id, op="update",
    )
    if skip is not None:
        return skip

    if not gcal_event_id:
        log.debug(
            "gcal.sync_update_noop",
            reason="no_gcal_event_id",
            correlation_id=correlation_id,
        )
        return {"noop": "no_gcal_event_id"}

    from alfred.integrations.gcal import GCalError

    update_kwargs: dict[str, Any] = {}
    if title is not None:
        update_kwargs["title"] = title
    if description is not None:
        update_kwargs["description"] = description
    if start_dt is not None:
        update_kwargs["start"] = start_dt
    if end_dt is not None:
        update_kwargs["end"] = end_dt
    if getattr(config, "default_time_zone", ""):
        update_kwargs["time_zone"] = config.default_time_zone

    try:
        updated = client.update_event(
            config.alfred_calendar_id,
            gcal_event_id,
            **update_kwargs,
        )
    except GCalError as exc:
        code = classify_gcal_error(exc)
        log.warning(
            "gcal.sync_update_failed",
            error=str(exc),
            error_code=code,
            gcal_event_id=gcal_event_id,
            correlation_id=correlation_id,
        )
        return {"error": {"code": code, "detail": str(exc)}}

    if updated is None:
        # GCalClient.update_event returns None on 404/410 — the event
        # was deleted on the calendar side. Don't auto-recreate (could
        # be intentional) and don't crash. Log + return a structured
        # error so a future repair pass can find these.
        log.warning(
            "gcal.sync_update_stale_id",
            gcal_event_id=gcal_event_id,
            calendar_id=config.alfred_calendar_id,
            correlation_id=correlation_id,
        )
        return {
            "error": {
                "code": "stale_gcal_id",
                "detail": (
                    f"GCal event {gcal_event_id} not found (already deleted "
                    "on calendar side). Vault frontmatter keeps the stale "
                    "ID; future repair sweep will surface for cleanup."
                ),
            }
        }

    calendar_label = getattr(config, "alfred_calendar_label", "") or "alfred"
    log.info(
        "gcal.sync_updated",
        event_id=gcal_event_id,
        calendar_id=config.alfred_calendar_id,
        calendar_label=calendar_label,
        patched=sorted(update_kwargs.keys()),
        title_source=title_source if title is not None else None,
        correlation_id=correlation_id,
    )
    return {"event_id": gcal_event_id, "calendar_label": calendar_label}


# ---------------------------------------------------------------------------
# Public: delete
# ---------------------------------------------------------------------------


def sync_event_delete_to_gcal(
    *,
    client: Any,
    config: Any,
    intended_on: bool = False,
    gcal_event_id: str,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Remove a GCal event when its vault record has been deleted.

    Caller (the vault-delete hook) is responsible for reading the
    record's ``gcal_event_id`` BEFORE the file is removed, then
    passing it here. Vault delete is unconditional — even a GCal
    failure here leaves the vault delete intact.

    Returns:
      * ``{}`` — gcal not configured (skip).
      * ``{"noop": "no_gcal_event_id"}`` — record had no
        ``gcal_event_id`` (was never synced; nothing to remove).
      * ``{"deleted": True, "event_id": "<id>"}`` — success or
        GCal returned 404 (already gone — same outcome).
      * ``{"error": {"code": "<code>", "detail": "<msg>"}}`` — sync
        failure other than already-deleted.
    """
    skip = _gcal_skip_check(
        client=client, config=config, intended_on=intended_on,
        correlation_id=correlation_id, op="delete",
    )
    if skip is not None:
        return skip

    if not gcal_event_id:
        log.debug(
            "gcal.sync_delete_noop",
            reason="no_gcal_event_id",
            correlation_id=correlation_id,
        )
        return {"noop": "no_gcal_event_id"}

    from alfred.integrations.gcal import GCalError

    try:
        # GCalClient.delete_event returns True if it was there, False
        # if it was already gone — either way the vault-side deletion
        # is in effect, so we treat both as success.
        client.delete_event(config.alfred_calendar_id, gcal_event_id)
    except GCalError as exc:
        code = classify_gcal_error(exc)
        log.warning(
            "gcal.sync_delete_failed",
            error=str(exc),
            error_code=code,
            gcal_event_id=gcal_event_id,
            correlation_id=correlation_id,
        )
        return {"error": {"code": code, "detail": str(exc)}}

    log.info(
        "gcal.sync_deleted",
        event_id=gcal_event_id,
        calendar_id=config.alfred_calendar_id,
        correlation_id=correlation_id,
    )
    return {"deleted": True, "event_id": gcal_event_id}


# ---------------------------------------------------------------------------
# Public: cancel (vault_edit status: cancelled — soft-cancel path)
# ---------------------------------------------------------------------------


def sync_event_cancellation_to_gcal(
    *,
    client: Any,
    config: Any,
    intended_on: bool = False,
    file_path: Path,
    gcal_event_id: str,
    keep_on_cancel: bool = False,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Mirror a ``vault_edit status: cancelled`` to GCal.

    Two paths, controlled by ``keep_on_cancel``:

      * ``keep_on_cancel=False`` (default) — DELETE the GCal event and
        clear ``gcal_event_id`` from the vault record's frontmatter.
        Subsequent re-creates start fresh; the deleted event is gone
        from the calendar.

      * ``keep_on_cancel=True`` — PATCH the GCal event with
        ``status="cancelled"``. The event stays visible on the calendar
        (struck through per Google's UI) but is marked cancelled.
        ``gcal_event_id`` is RETAINED so a subsequent re-confirmation
        edit could patch the status back. The prompt-tuner teaches
        Salem to set ``gcal_keep_on_cancel: true`` ONLY when Andrew
        explicitly asks "keep it visible / leave on calendar / mark
        cancelled but keep showing".

    This is distinct from :func:`sync_event_delete_to_gcal` which is
    the vault_delete (hard-delete) hook. This function is the
    vault_edit (soft-cancel) hook — same GCal end state on the
    delete path, but the vault record persists and the
    ``gcal_event_id`` clearing is part of THIS function's contract
    (vs `sync_event_delete_to_gcal` where the vault record is gone
    so there's nothing to clear).

    Returns:
      * ``{}`` — gcal not configured (skip).
      * ``{"noop": "no_gcal_event_id"}`` — vault record had no
        ``gcal_event_id`` (was never synced; nothing to remove or
        patch). Common case for vault-only events that never made it
        to GCal in the first place.
      * ``{"cancelled": True, "event_id": "<id>", "path": "delete"}``
        — keep_on_cancel=False path: DELETE succeeded (or event was
        already gone — same outcome) AND ``gcal_event_id`` cleared
        from the vault record.
      * ``{"cancelled": True, "event_id": "<id>", "path":
        "status_cancelled"}`` — keep_on_cancel=True path: PATCH
        succeeded; ``gcal_event_id`` retained on the vault record.
      * ``{"error": {"code": "<code>", "detail": "<msg>"}}`` — sync
        failure other than already-deleted (e.g. 500 from GCal API,
        auth failure). Vault state preserved (the original
        cancellation edit is intact; only the GCal mirror failed).
        ``gcal_event_id`` stays on the vault record so a future
        retry can target the same event.
      * ``{"error": {"code": "stale_gcal_id", "detail": "..."}}`` —
        keep_on_cancel=True path only: GCal answered 404/410 on
        patch. The event was already deleted on the calendar side.
        Vault frontmatter keeps the stale ID for now.
    """
    skip = _gcal_skip_check(
        client=client, config=config, intended_on=intended_on,
        correlation_id=correlation_id, op="cancel",
    )
    if skip is not None:
        return skip

    if not gcal_event_id:
        log.debug(
            "gcal.sync_cancel_noop",
            reason="no_gcal_event_id",
            correlation_id=correlation_id,
        )
        return {"noop": "no_gcal_event_id"}

    from alfred.integrations.gcal import GCalError

    if keep_on_cancel:
        # PATCH path — event stays visible, marked cancelled.
        try:
            updated = client.update_event(
                config.alfred_calendar_id,
                gcal_event_id,
                status="cancelled",
            )
        except GCalError as exc:
            code = classify_gcal_error(exc)
            log.warning(
                "gcal.sync_cancelled_via_status_failed",
                error=str(exc),
                error_code=code,
                gcal_event_id=gcal_event_id,
                path=str(file_path),
                correlation_id=correlation_id,
            )
            return {"error": {"code": code, "detail": str(exc)}}

        if updated is None:
            # 404/410 — event already deleted calendar-side. Same
            # outcome as the delete path would have produced.
            log.warning(
                "gcal.sync_cancel_stale_id",
                gcal_event_id=gcal_event_id,
                calendar_id=config.alfred_calendar_id,
                path=str(file_path),
                correlation_id=correlation_id,
            )
            return {
                "error": {
                    "code": "stale_gcal_id",
                    "detail": (
                        f"GCal event {gcal_event_id} not found on patch "
                        "(already deleted on calendar side). Vault "
                        "frontmatter keeps the stale ID; future repair "
                        "sweep will surface for cleanup."
                    ),
                }
            }

        log.info(
            "gcal.sync_cancelled_via_status",
            event_id=gcal_event_id,
            calendar_id=config.alfred_calendar_id,
            path=str(file_path),
            correlation_id=correlation_id,
        )
        return {
            "cancelled": True,
            "event_id": gcal_event_id,
            "path": "status_cancelled",
        }

    # DELETE path (default) — remove from calendar, clear
    # gcal_event_id from the vault record so subsequent re-creates
    # start fresh.
    try:
        # GCalClient.delete_event returns True if it was there, False
        # if it was already gone — either way the vault-side cancel
        # is in effect, so we treat both as success.
        client.delete_event(config.alfred_calendar_id, gcal_event_id)
    except GCalError as exc:
        code = classify_gcal_error(exc)
        log.warning(
            "gcal.sync_cancelled_via_delete_failed",
            error=str(exc),
            error_code=code,
            gcal_event_id=gcal_event_id,
            path=str(file_path),
            correlation_id=correlation_id,
        )
        return {"error": {"code": code, "detail": str(exc)}}

    # Clear gcal_event_id from the vault record. Direct frontmatter
    # mutation rather than a recursive vault_edit call:
    #   1. vault_edit would re-fire the update hooks → infinite loop /
    #      double-trigger sync.
    #   2. The audit log already captured the cancel (the original
    #      vault_edit that set status: cancelled fired an audit row).
    #   3. Direct mutation matches the existing pattern in
    #      sync_event_create_to_gcal which writes back gcal_event_id
    #      directly without round-tripping through vault_edit.
    try:
        post = frontmatter.load(str(file_path))
        if "gcal_event_id" in post:
            del post["gcal_event_id"]
        # Also drop the calendar label since the mirror is gone — leaving
        # it stale is misleading on a re-confirm cycle (a future re-
        # create might land on a different calendar).
        if "gcal_calendar" in post:
            del post["gcal_calendar"]
        new_text = frontmatter.dumps(post)
        if not new_text.endswith("\n"):
            new_text += "\n"
        file_path.write_text(new_text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # Soft fail — the GCal event is gone, but our cleanup of the
        # vault stale ID failed. Log + keep the success path so the
        # caller still gets the cancelled outcome (the operator can
        # re-run a janitor sweep to clean up the stale ID).
        log.warning(
            "gcal.sync_cancel_writeback_failed",
            error=str(exc),
            event_id=gcal_event_id,
            path=str(file_path),
            correlation_id=correlation_id,
        )

    log.info(
        "gcal.sync_cancelled_via_delete",
        event_id=gcal_event_id,
        calendar_id=config.alfred_calendar_id,
        path=str(file_path),
        correlation_id=correlation_id,
    )
    return {
        "cancelled": True,
        "event_id": gcal_event_id,
        "path": "delete",
    }

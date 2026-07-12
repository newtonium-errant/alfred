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

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter
import structlog

log = structlog.get_logger(__name__)


def _atomic_write_text(file_path: Path, text: str) -> None:
    """Write ``text`` to ``file_path`` atomically (``.tmp`` → ``os.replace``).

    All the direct-frontmatter writebacks below re-serialize an event record
    that a concurrent reader (surveyor / curator / another sync handler) can
    read at any moment. A bare ``write_text`` truncates-then-writes in place,
    exposing a torn-read window; #37 widened it further by moving the create
    sync into an ``asyncio.to_thread`` worker that can land its writeback
    POST-response. ``os.replace`` swaps the file in atomically so a reader
    only ever sees the old or the new full record, never a half-written one.
    Convention mirrors ``msgbus/record.py``, ``contracts/store.py``, and
    ``instructor/state.py``.
    """
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, file_path)


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
# Per-event sync policy — event↔GCal decouple (consolidation Step 4)
# ---------------------------------------------------------------------------

# Canonical policy values (per ``vault.schema.EVENT_GCAL_FIELDS`` →
# ``gcal_sync``). "sync" projects the event to Google Calendar (the
# historical behaviour); "none" never projects it (remind-only — e.g.
# birthdays/anniversaries; the brief's upcoming-events still surfaces them
# because reminders read the vault record directly, not GCal).
SYNC_POLICY_SYNC = "sync"
SYNC_POLICY_NONE = "none"


def resolve_sync_policy(fm: dict) -> str:
    """Resolve a vault event's GCal sync policy from its frontmatter.

    The event's IDENTITY is the record; Google Calendar is ONE optional
    output channel. ``gcal_sync`` declares whether THIS event projects to
    GCal:

      * ``"none"`` → never project (remind-only).
      * anything else, INCLUDING ABSENT → ``"sync"`` (project — the
        historical behaviour).

    The absent-→-``"sync"`` default is load-bearing: it preserves the
    behaviour of every existing event (which carries no ``gcal_sync`` field)
    — they keep syncing exactly as before this field existed. The resolver
    is deliberately FAIL-SAFE toward sync: an unrecognised value resolves to
    ``"sync"`` (we never silently DROP a calendar projection on a typo;
    the worse failure is an accidental never-sync). An unrecognised non-empty
    value emits a debug signal so a typo (e.g. ``gcal_sync: non``) is
    grep-able rather than silently swallowed.

    This is the canonical resolver (mirrors :func:`resolve_gcal_title`);
    all sync entry points (the daemon hooks, the backfill CLI, the peer
    propose-create handler) MUST use it rather than re-deriving the rule.

    NOTE (deferred, §2 review): flipping an ALREADY-synced event to
    ``gcal_sync: none`` does NOT retract its existing GCal entry — the
    update/delete gate just no-ops, so the prior projection lingers on the
    calendar. To retract it, explicitly delete the event (fires the GCal
    delete/cancel hook). New events created with ``none`` never get a
    ``gcal_event_id`` → never reach the calendar (the common path).
    """
    raw = fm.get("gcal_sync")
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized == SYNC_POLICY_NONE:
            return SYNC_POLICY_NONE
        if normalized == SYNC_POLICY_SYNC:
            return SYNC_POLICY_SYNC
        if normalized:
            # Unrecognised non-empty value → fail-safe to sync, but surface
            # the ambiguity so an operator typo is diagnosable.
            log.debug(
                "gcal.sync_policy_unrecognized",
                value=raw[:40],
                resolved=SYNC_POLICY_SYNC,
            )
    return SYNC_POLICY_SYNC


def resolve_collapse_key(fm: dict) -> str:
    """Resolve a vault event's GCal collapse key from its frontmatter (§3).

    ``gcal_collapse_key`` is a CLEAN SERIES LABEL (e.g. ``"rTMS"``), NOT
    date-stamped — the date dimension comes from the event's own ``date``, so
    a collapse group is ``(key, date)`` and the SAME key on every rTMS event
    auto-separates by day. Returns the stripped key, or ``""`` when absent /
    blank / non-string (→ no collapse; the plain per-event sync path applies).

    Canonical resolver (mirrors :func:`resolve_gcal_title` /
    :func:`resolve_sync_policy`); all callers MUST use it.
    """
    raw = fm.get("gcal_collapse_key")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ""


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


def _sync_policy_skip(
    *, sync_policy: str, correlation_id: str, op: str,
) -> dict[str, Any] | None:
    """Return the early-return value if the per-event policy disables sync.

    Consulted by all four sync funcs AFTER :func:`_gcal_skip_check` (the
    global gcal-disabled gate takes precedence — its ``{}`` return is
    unchanged). When the event's ``gcal_sync`` policy is ``"none"`` the func
    returns ``{"noop": "sync_policy_none"}`` — a reason DISTINCT from
    ``{}`` (gcal disabled) and ``{"noop": "no_gcal_event_id"}`` (never
    synced) so callers + logs can tell "operator opted this event out of
    GCal" apart from the other skips. Default ``"sync"`` → ``None`` (proceed),
    so an un-updated caller preserves the historical always-sync behaviour.
    """
    if sync_policy == SYNC_POLICY_NONE:
        log.debug(
            "gcal.sync_skipped",
            op=op,
            reason="sync_policy_none",
            correlation_id=correlation_id,
        )
        return {"noop": "sync_policy_none"}
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
    sync_policy: str = "sync",
) -> dict[str, Any]:
    """Push a freshly-created vault event to the configured calendar.

    Pushes to ``config.alfred_calendar_id`` (operator-visible as
    Andrew's Calendar (S.A.L.E.M.) per the canonical naming established
    in the SKILL.md sweep at commit ``332b66c``).

    Mirrors the pre-Phase-A+ inline logic exactly, just lifted out of
    the aiohttp request handler. On success: writes ``gcal_event_id``
    + ``gcal_calendar`` (from ``config.alfred_calendar_label``) back
    into the vault record's frontmatter.

    ``title_source`` describes which field the caller resolved the
    title from — typically one of ``"gcal_title"`` / ``"title"`` /
    ``"name"`` (per :func:`resolve_gcal_title`). The backfill CLI may
    also pass ``"filename_stem"`` when a record has none of the three
    frontmatter title fields and falls back to the markdown filename
    stem. Logged on ``gcal.sync_created`` so operators can grep for
    records using the override (or the degenerate fallback) without
    round-tripping through the frontmatter. Default ``"name"`` matches
    the pre-decoupling behavior.
    """
    skip = _gcal_skip_check(
        client=client, config=config, intended_on=intended_on,
        correlation_id=correlation_id, op="create",
    )
    if skip is not None:
        return skip
    policy_skip = _sync_policy_skip(
        sync_policy=sync_policy, correlation_id=correlation_id, op="create",
    )
    if policy_skip is not None:
        return policy_skip

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
        _atomic_write_text(file_path, new_text)
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
    sync_policy: str = "sync",
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
    policy_skip = _sync_policy_skip(
        sync_policy=sync_policy, correlation_id=correlation_id, op="update",
    )
    if policy_skip is not None:
        return policy_skip

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
    sync_policy: str = "sync",
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
    policy_skip = _sync_policy_skip(
        sync_policy=sync_policy, correlation_id=correlation_id, op="delete",
    )
    if policy_skip is not None:
        return policy_skip

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
    sync_policy: str = "sync",
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
    policy_skip = _sync_policy_skip(
        sync_policy=sync_policy, correlation_id=correlation_id, op="cancel",
    )
    if policy_skip is not None:
        return policy_skip

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
        _atomic_write_text(file_path, new_text)
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


# ---------------------------------------------------------------------------
# Same-day collapse (§3) — the rTMS umbrella coordinator
# ---------------------------------------------------------------------------


def _coerce_dt(raw: Any) -> datetime | None:
    """Parse a frontmatter start/end value to a ``datetime`` (or None)."""
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    return None


def _coerce_event_date(fm: dict) -> date | None:
    """Resolve an event's group date — ``date`` field first, else
    ``start``.date(). Returns None when neither is parseable."""
    d = fm.get("date")
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str) and d.strip():
        try:
            return date.fromisoformat(d.strip())
        except ValueError:
            start = _coerce_dt(d)
            if start is not None:
                return start.date()
    start = _coerce_dt(fm.get("start"))
    if start is not None:
        return start.date()
    return None


def _normalize_group_date(group_date: Any) -> date | None:
    if isinstance(group_date, datetime):
        return group_date.date()
    if isinstance(group_date, date):
        return group_date
    if isinstance(group_date, str) and group_date.strip():
        try:
            return date.fromisoformat(group_date.strip()[:10])
        except ValueError:
            return None
    return None


def _collapse_title(key: str, n: int, span_start: datetime, span_end: datetime) -> str:
    """Auto-summary title for a collapse group (operator Q1):
    ``"<key> — N sessions (HH:MM–HH:MM)"`` (en-dash separator)."""
    return (
        f"{key} — {n} session{'s' if n != 1 else ''} "
        f"({span_start:%H:%M}–{span_end:%H:%M})"
    )


def _clear_gcal_ids(file_path: Path, correlation_id: str = "") -> None:
    """Direct-frontmatter clear of ``gcal_event_id`` + ``gcal_calendar`` (+ the
    ``gcal_collapse_synced`` skip-unchanged cache) on a secondary member (no
    vault_edit → no hook re-fire; mirrors the cancellation-path writeback). A
    demoted-from-primary member must shed its stale sync-state cache too, else a
    future re-election could read a stale signature."""
    try:
        post = frontmatter.load(str(file_path))
        changed = False
        for k in ("gcal_event_id", "gcal_calendar", "gcal_collapse_synced"):
            if k in post:
                del post[k]
                changed = True
        if changed:
            text = frontmatter.dumps(post)
            if not text.endswith("\n"):
                text += "\n"
            _atomic_write_text(file_path, text)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "gcal.collapse_clear_writeback_failed",
            error=str(exc), path=str(file_path), correlation_id=correlation_id,
        )


def _write_primary_id(
    file_path: Path, event_id: str, calendar_label: str,
    correlation_id: str = "", *, synced_signature: str = "",
) -> None:
    """Direct-frontmatter writeback of the primary's ``gcal_event_id`` +
    ``gcal_calendar`` (no vault_edit → no hook re-fire).

    ``synced_signature`` (when non-empty) persists the last-synced
    ``"<start_iso>|<end_iso>|<title>"`` to ``gcal_collapse_synced`` so the next
    recompute can skip a redundant PATCH when nothing changed (NOTE-A). It's an
    internal sync-state cache (like ``gcal_event_id``), never operator-set; one
    opaque string so YAML never re-types it on reload."""
    try:
        post = frontmatter.load(str(file_path))
        post["gcal_event_id"] = event_id
        post["gcal_calendar"] = calendar_label
        if synced_signature:
            post["gcal_collapse_synced"] = synced_signature
        text = frontmatter.dumps(post)
        if not text.endswith("\n"):
            text += "\n"
        _atomic_write_text(file_path, text)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "gcal.collapse_primary_writeback_failed",
            error=str(exc), event_id=event_id, path=str(file_path),
            correlation_id=correlation_id,
        )


def sync_collapse_group(
    *,
    client: Any,
    config: Any,
    vault_path: Any,
    collapse_key: str,
    group_date: Any,
    intended_on: bool = False,
    correlation_id: str = "",
    orphan_event_id: str = "",
) -> dict[str, Any]:
    """Reconcile one same-day collapse group to ONE GCal entry (§3).

    A collapse group = all ``event`` records sharing ``(gcal_collapse_key,
    date)``. The group projects to a SINGLE GCal entry spanning
    earliest-start → latest-end across sync-eligible members, titled with the
    auto-summary ``"<key> — N sessions (HH:MM–HH:MM)"`` (operator Q1).

    IDEMPOTENT full recompute — safe to call on every member create/update/
    delete (the no-double-create guarantee: the group keeps exactly ONE
    ``gcal_event_id``, on the PRIMARY). Election + reconcile:

      * PRIMARY = an eligible member that already has a ``gcal_event_id``
        (ADOPT its entry — operator Q2, e.g. the manually-created umbrella);
        else the earliest-start eligible member (deterministic, path
        tie-break). ``orphan_event_id`` (passed by the delete hook when the
        deleted member WAS the primary) is adopted onto the elected survivor
        (operator Q3 PROMOTE).
      * RECONCILE: every other GCal id found in the group (duplicates +
        the orphan when not reused) is DELETED so the group converges to one.
      * Eligible = members with ``gcal_sync != "none"`` AND parseable
        start+end (§2 × §3: a ``none`` member never contributes / never
        projects; a member without times can't contribute a span).
      * No eligible members → DELETE any lingering group entry + clear ids
        (last-member-delete → remove the entry, operator Q3).

    Writeback is direct-frontmatter (no ``vault_edit`` → no hook re-fire):
    the primary gets ``gcal_event_id`` + ``gcal_calendar`` + the internal
    ``gcal_collapse_synced`` cache (the last-synced ``"<start>|<end>|<title>"``
    signature, for the skip-unchanged short-circuit); every other member's
    stale ids/cache are cleared.

    SKIP-UNCHANGED (NOTE-A): re-confirming an existing primary whose stored
    signature equals the freshly-computed one — with no duplicates cleaned this
    pass — returns a ``collapse_unchanged`` noop instead of a redundant PATCH.
    A genuine span/title change still PATCHes; first-sync still creates;
    adopt/promote still PATCH (no stored signature → no match).

    Returns (per ``intentionally_left_blank`` — every outcome is explicit):
      * ``{}`` — gcal disabled (skip).
      * ``{"noop": "no_collapse_key"}`` / ``{"noop": "no_eligible_members"}``.
      * ``{"collapsed": True, "action": "noop", "noop": "collapse_unchanged",
        ...}`` — span+title unchanged, PATCH skipped (NOTE-A).
      * ``{"collapsed": True, "action": "created"|"patched"|"deleted",
        "collapse_key", "date", "primary_event_id", "member_count",
        "title", "span": [start_iso, end_iso], "reconciled_deleted": [...]}``.
      * ``{"error": {...}}`` on a GCal failure / bad input.
    """
    skip = _gcal_skip_check(
        client=client, config=config, intended_on=intended_on,
        correlation_id=correlation_id, op="collapse",
    )
    if skip is not None:
        return skip

    key = (collapse_key or "").strip()
    if not key:
        return {"noop": "no_collapse_key"}
    gdate = _normalize_group_date(group_date)
    if gdate is None:
        return {
            "error": {
                "code": "bad_request",
                "detail": f"collapse group_date unparseable: {group_date!r}",
            }
        }

    from alfred.integrations.gcal import GCalError

    calendar_label = getattr(config, "alfred_calendar_label", "") or "alfred"
    calendar_id = config.alfred_calendar_id

    # --- scan the group --------------------------------------------------
    event_dir = Path(vault_path) / "event"
    eligible: list[dict[str, Any]] = []
    all_ids: dict[str, Path] = {}  # event_id -> the file carrying it
    if event_dir.is_dir():
        for md in sorted(event_dir.glob("*.md")):
            try:
                fm = dict(frontmatter.load(str(md)).metadata or {})
            except Exception:  # noqa: BLE001
                continue
            if resolve_collapse_key(fm) != key:
                continue
            if _coerce_event_date(fm) != gdate:
                continue
            eid = str(fm.get("gcal_event_id") or "")
            if eid:
                all_ids[eid] = md
            if resolve_sync_policy(fm) == SYNC_POLICY_NONE:
                continue  # §2: never projects, never contributes a span
            if str(fm.get("status") or "").strip().lower() == "cancelled":
                continue  # a cancelled session leaves the group (excluded from span)
            start = _coerce_dt(fm.get("start"))
            end = _coerce_dt(fm.get("end"))
            if start is None or end is None:
                continue  # can't contribute a span (Q6)
            eligible.append(
                {"path": md, "start": start, "end": end, "gcal_event_id": eid}
            )

    # --- no eligible members → tear down the entry (last-member-delete) ---
    if not eligible:
        ids_to_delete = {
            i for i in (set(all_ids) | ({orphan_event_id} if orphan_event_id else set()))
            if i
        }
        if not ids_to_delete:
            # Nothing matched + nothing to remove → clean no-op (not a
            # spurious "deleted"). Per ILB this is the explicit idle signal.
            log.info(
                "gcal.collapse_no_members",
                collapse_key=key, date=gdate.isoformat(),
                correlation_id=correlation_id,
            )
            return {"noop": "no_eligible_members"}
        deleted: list[str] = []
        for eid in sorted(i for i in ids_to_delete if i):
            try:
                client.delete_event(calendar_id, eid)
                deleted.append(eid)
            except GCalError as exc:
                log.warning(
                    "gcal.collapse_teardown_delete_failed",
                    error=str(exc), gcal_event_id=eid,
                    correlation_id=correlation_id,
                )
        for md in set(all_ids.values()):
            _clear_gcal_ids(md, correlation_id)
        log.info(
            "gcal.collapse_torn_down",
            collapse_key=key, date=gdate.isoformat(),
            deleted=deleted, correlation_id=correlation_id,
        )
        return {
            "collapsed": True, "action": "deleted",
            "collapse_key": key, "date": gdate.isoformat(),
            "primary_event_id": "", "member_count": 0,
            "reconciled_deleted": deleted,
        }

    eligible.sort(key=lambda m: (m["start"], str(m["path"])))

    # --- elect the primary + its id --------------------------------------
    elig_with_id = [m for m in eligible if m["gcal_event_id"]]
    if elig_with_id:
        primary = elig_with_id[0]
        primary_id = primary["gcal_event_id"]
    elif orphan_event_id:
        primary = eligible[0]
        primary_id = orphan_event_id  # PROMOTE the orphaned entry onto a survivor
    else:
        primary = eligible[0]
        primary_id = ""  # → create fresh

    # --- reconcile: delete every OTHER id in the group -------------------
    candidate_ids = set(all_ids)
    if orphan_event_id:
        candidate_ids.add(orphan_event_id)
    reconciled_deleted: list[str] = []
    for eid in sorted(i for i in candidate_ids if i and i != primary_id):
        try:
            client.delete_event(calendar_id, eid)
            reconciled_deleted.append(eid)
        except GCalError as exc:
            log.warning(
                "gcal.collapse_reconcile_delete_failed",
                error=str(exc), gcal_event_id=eid,
                correlation_id=correlation_id,
            )

    # --- compute span + auto-summary title -------------------------------
    span_start = min(m["start"] for m in eligible)
    span_end = max(m["end"] for m in eligible)
    title = _collapse_title(key, len(eligible), span_start, span_end)
    # One opaque signature of everything that determines the projected GCal
    # entry (span + title; the title already encodes member_count via "N
    # sessions"). Compared whole-string, never split — so a "|" inside a key
    # is harmless, and YAML never re-types it on reload.
    signature = f"{span_start.isoformat()}|{span_end.isoformat()}|{title}"

    # --- skip-unchanged PATCH short-circuit (NOTE-A) ---------------------
    # When re-confirming an EXISTING primary (not an orphan-promote, not a
    # fresh create) whose last-synced signature is byte-identical to the
    # freshly-computed one AND this pass cleaned no duplicates, the PATCH would
    # be a no-op round-trip — skip it (and the writeback churn). The common
    # trigger: a member edit to a non-time field fires the hook → recompute →
    # identical span+title. ``elig_with_id`` (not orphan/fresh) + empty
    # ``reconciled_deleted`` (nothing changed structurally) gate it; a genuine
    # span/title change yields a different signature → falls through to PATCH.
    if elig_with_id and not reconciled_deleted:
        try:
            stored_sig = dict(
                frontmatter.load(str(primary["path"])).metadata or {}
            ).get("gcal_collapse_synced")
        except Exception:  # noqa: BLE001
            stored_sig = None
        if stored_sig == signature:
            log.info(
                "gcal.collapse_unchanged",
                collapse_key=key, date=gdate.isoformat(),
                primary_event_id=primary_id, member_count=len(eligible),
                title=title, correlation_id=correlation_id,
            )
            return {
                "collapsed": True, "action": "noop",
                "noop": "collapse_unchanged",
                "collapse_key": key, "date": gdate.isoformat(),
                "primary_event_id": primary_id, "member_count": len(eligible),
                "title": title,
                "span": [span_start.isoformat(), span_end.isoformat()],
                "reconciled_deleted": reconciled_deleted,
            }

    common: dict[str, Any] = {"start": span_start, "end": span_end, "title": title}
    if getattr(config, "default_time_zone", ""):
        common["time_zone"] = config.default_time_zone

    # --- create or patch the single entry --------------------------------
    action = ""
    try:
        if not primary_id:
            primary_id = client.create_event(
                calendar_id, description="", **common,
            )
            action = "created"
        else:
            updated = client.update_event(calendar_id, primary_id, **common)
            if updated is None:
                # Adopted/primary id was stale (404) — create a fresh entry.
                primary_id = client.create_event(
                    calendar_id, description="", **common,
                )
                action = "created"
            else:
                action = "patched"
    except GCalError as exc:
        code = classify_gcal_error(exc)
        log.warning(
            "gcal.collapse_sync_failed",
            error=str(exc), error_code=code, collapse_key=key,
            date=gdate.isoformat(), correlation_id=correlation_id,
        )
        return {"error": {"code": code, "detail": str(exc)}}

    # --- writeback: primary owns the id; everyone else is cleared --------
    _write_primary_id(
        primary["path"], primary_id, calendar_label, correlation_id,
        synced_signature=signature,
    )
    for md in set(all_ids.values()):
        if md != primary["path"]:
            _clear_gcal_ids(md, correlation_id)

    log.info(
        "gcal.collapse_synced",
        collapse_key=key, date=gdate.isoformat(), action=action,
        primary_event_id=primary_id, member_count=len(eligible),
        title=title, reconciled_deleted=reconciled_deleted,
        correlation_id=correlation_id,
    )
    return {
        "collapsed": True, "action": action,
        "collapse_key": key, "date": gdate.isoformat(),
        "primary_event_id": primary_id, "member_count": len(eligible),
        "title": title,
        "span": [span_start.isoformat(), span_end.isoformat()],
        "reconciled_deleted": reconciled_deleted,
    }

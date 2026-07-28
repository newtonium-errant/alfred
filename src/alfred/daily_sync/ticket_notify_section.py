"""Ticket-notifications section provider — #22b (2026-07).

Surfaces the KAL-LE ticket → PWA-notify pipeline (parity #22) in the
Daily Sync as a READ-ONLY, FAIL-LOUD observability section, so the
operator can confirm the pipeline is live in production.

Operator directive (verbatim): *"When it's working well i dont think
notification in the brief is necessary, but to start i want it included
in the daily sync so I can see if it's happening. fail loud."*

## What #22 does (context)

KAL-LE's ticket intake fires exactly one best-effort ``kind=notice`` +
``web_notify`` peer_send when (and only when) a ticket ack is
``created``. Salem's transport fans that notice into the bounded
per-user web notify store (:mod:`alfred.web.notify_state`) BESIDE the
Telegram relay; the PWA polls it back via ``GET /chat/notifications``.
This section adds a THIRD read surface — the Daily Sync — purely for
observability.

## Read-only — the PWA tray owns read/ack

This section NEVER mutates the store: it constructs a
:class:`~alfred.web.notify_state.WebNotifyStore`, ``.load()``s it, and
calls ``.list_for(...)`` (which returns copies) — no ``.enqueue`` / no
``.ack`` / no ``.save``. The PWA notification tray remains the sole
owner of read/ack state. Surfacing here is a passive mirror.

## Cross-process store resolution

The daily_sync daemon and the talker daemon (which HOSTS the web notify
store + sink) are SEPARATE processes. daily_sync cannot introspect the
talker's in-memory ``KEY_WEB_NOTIFY_STORE`` / ``transport.web_notify_sink``
registration — it can only read the on-disk store file. That file lives
at ``<data_dir>/web_notify_state.json`` where ``data_dir`` is
``Path(config.state.path).parent`` — the SAME cross-process convention
the #30 web-outbound spool uses (``write_latest(Path(config.state.path)
.parent, …)``). Atomic writes on the talker side (``.tmp`` → ``os.replace``)
make the read torn-free.

## Three-state fail-loud (ratified design)

* **STATE 1 — notices present.** Render ``### Ticket notifications (N)``
  + one BULLET per notice (text · precedence · source · issue_url).
  Bullets, NOT a numbered list — this is observability, not a
  reply-routable batch; a ``N. …`` line would collide with the reply
  parser's "item N ok" semantics.
* **STATE 2 — genuinely none.** An EXPLICIT
  ``*(No ticket notifications since <last sync>)*`` line under a
  ``(0)`` header — never a blank/omitted section (per
  ``feedback_intentionally_left_blank``: silence must be
  distinguishable from broken).
* **STATE 3 — pipeline breakage, FAIL LOUD.** A ``### Ticket
  notifications ⚠️`` header + a loud ``⚠️`` line, for the LOCALLY
  detectable breakages:
    (a) the notify-store read raised;
    (b) ``web.notifications.enabled`` is false (feature off);
    (c-proxy) ``web.enabled`` is false (web surface / sink never
        mounted) OR no ``web.users`` configured (sink has no operator to
        key to) — the in-process "sink actually registered on the
        transport app" check is CROSS-PROCESS and deferred (see the
        arc's open notes), alongside the deeper cross-instance
        "KAL-LE created N vs Salem received 0" reconciliation.

## Best-effort — never blocks the fire

Every read (web config resolution + store read) is wrapped; a failure
becomes the STATE-3 loud warning, NEVER a crash. Mirrors the brief-spool
swallow discipline in ``daemon.fire_once`` — ``assemble_message`` must
still assemble + the sync must still fire even if this section's read
fails. The assembler's own per-provider ``try/except`` is a backstop;
this module owns the loud rendering.

## Opt-in — Salem-only

Registered unconditionally (like friction / triage / routine_match) but
the provider returns ``None`` (section omitted) unless
``daily_sync.ticket_notify.enabled`` is true. Only Salem — which HOSTS
the notify store — opts in; KAL-LE / Hypatia Daily Syncs stay
byte-unchanged (no false ⚠️).

## Cross-agent contract

:data:`SECTION_HEADER_BASE` is the operator-facing header stem. A future
SKILL update that teaches the talker to recognise the heading should
quote it; a rename here = update the SKILL in lockstep.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from .config import DailySyncConfig
from .confidence import load_state

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Cross-agent contract — operator-facing header stem
# ---------------------------------------------------------------------------

SECTION_HEADER_BASE = "### Ticket notifications"

# Registry slot. 8 places this observability surface near the TOP of the
# Daily Sync (above email calibration at 10, below the pending-items
# queue at 5) so the operator sees "is the pipeline happening" at a
# glance — and a STATE-3 ⚠️ is prominent. Distinct from every existing
# priority (5/10/15/16/22/23/24/25/27/28).
_PRIORITY = 8

# Fallback window when no last-sync timestamp is recoverable from state.
_FALLBACK_WINDOW_HOURS = 24


# ---------------------------------------------------------------------------
# Daemon-set raw config holder — mirrors pending_items_section
# ---------------------------------------------------------------------------
#
# The section provider's ``(config, today)`` signature is fixed by the
# assembler contract, but ``fire_once`` CAN stash the pre-loaded unified
# raw config dict before calling ``assemble_message`` (production path).
# Reused here to read the ``web:`` block (web.enabled / notifications /
# users) WITHOUT a per-fire ``open(config.yaml)`` round-trip + the
# cwd-relative-path fragility. Falls back to ``config.config_path`` for
# direct test callers that didn't stash.
_RAW_CONFIG_HOLDER: dict[str, Any] = {}


def set_raw_config(raw: dict[str, Any]) -> None:
    """Stash the daemon's pre-loaded unified raw config dict. Idempotent."""
    _RAW_CONFIG_HOLDER["raw"] = raw


def clear_raw_config() -> None:
    """Clear the daemon-set raw config holder. Test cleanup helper."""
    _RAW_CONFIG_HOLDER.clear()


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def _header(count: int) -> str:
    return f"{SECTION_HEADER_BASE} ({count})"


def _header_warn() -> str:
    return f"{SECTION_HEADER_BASE} ⚠️"


def _state3(reason: str) -> str:
    """Render the STATE-3 loud warning section."""
    return f"{_header_warn()}\n\n⚠️ ticket-notify pipeline check FAILED — {reason}"


# ---------------------------------------------------------------------------
# Time / window helpers
# ---------------------------------------------------------------------------


def _parse_dt(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string to a tz-aware UTC datetime, else None.

    A naive datetime string is assumed to be UTC. Any parse failure
    returns None (the caller treats an unparseable ts as fail-safe:
    include the notice / fall back to the 24h window — never HIDE).
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_window(
    config: DailySyncConfig, now: datetime,
) -> tuple[datetime, str]:
    """Return ``(window_start, human_label)`` for "since the last sync".

    Prefers the last DAILY-SYNC fire timestamp persisted in state
    (``last_batch.fired_at``, a UTC ISO string stamped by
    ``_build_state_payload``). During ``assemble_message`` this is the
    PREVIOUS fire's timestamp — exactly "since the last sync". Falls
    back to a 24h window when no timestamp is recoverable (first-ever
    fire, or a prior fire that persisted no batch).
    """
    try:
        state = load_state(config.state.path)
        fired_at = (state.get("last_batch") or {}).get("fired_at")
        dt = _parse_dt(fired_at)
        if dt is not None:
            return dt, f"the last sync ({dt.isoformat()})"
    except Exception as exc:  # noqa: BLE001 — window read never blocks the section
        log.debug("daily_sync.ticket_notify.window_state_read_failed", error=str(exc))
    start = now - timedelta(hours=_FALLBACK_WINDOW_HOURS)
    return start, f"the last {_FALLBACK_WINDOW_HOURS}h"


def _within_window(entry_ts_raw: Any, window_start: datetime) -> bool:
    """True when the entry's ts is at/after ``window_start``.

    Fail-safe: an unparseable / missing ts is INCLUDED (never hide a
    notice on a bad timestamp — observability beats precision here).
    """
    ts = _parse_dt(entry_ts_raw)
    if ts is None:
        return True
    return ts >= window_start


# ---------------------------------------------------------------------------
# Web-config + store resolution
# ---------------------------------------------------------------------------


def _load_web_config(config: DailySyncConfig) -> tuple[Any, str]:
    """Resolve the ``web:`` typed config, or ``(None, reason)``.

    Resolution order: daemon-stashed raw config (production) → the
    config file at ``config.config_path`` (direct test / late loaders).
    Every failure is a REASON string (not a raise) so the caller renders
    a STATE-3 ⚠️ rather than crashing the fire.
    """
    raw = _RAW_CONFIG_HOLDER.get("raw")
    if raw is None:
        config_path = getattr(config, "config_path", None)
        if config_path:
            try:
                import yaml

                with open(config_path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception as exc:  # noqa: BLE001
                return None, (
                    f"config file read failed ({exc.__class__.__name__})"
                )
    if raw is None:
        return None, (
            "no unified config available (daemon set_raw_config not called "
            "and config_path unset)"
        )
    try:
        from alfred.web.config import load_from_unified as _load_web

        return _load_web(raw), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"web config parse failed ({exc.__class__.__name__}: {exc})"


def _resolve_store_path(config: DailySyncConfig) -> Path:
    """Resolve the on-disk notify store path.

    Explicit ``ticket_notify.store_path`` wins (split-dir deploys /
    tests); otherwise derive ``<state.path>.parent / web_notify_state.json``
    — the #30 web-outbound ``data_dir`` cross-process contract with the
    talker daemon that writes the store.
    """
    override = getattr(config.ticket_notify, "store_path", "") or ""
    if override:
        return Path(override)
    return Path(config.state.path).parent / "web_notify_state.json"


def _read_notices(config: DailySyncConfig, operator: str) -> list[dict[str, Any]]:
    """Read the operator's notices via the notify store's list API.

    Reuses :class:`~alfred.web.notify_state.WebNotifyStore` verbatim
    (``create`` → ``load`` → ``list_for``) — no duplicated store logic.
    ``load`` tolerates a missing / corrupt file (empty result, no raise);
    ``list_for`` returns NEWEST-FIRST copies and never writes. A missing
    store file therefore reads as "nothing yet" (STATE 2), distinct from
    a broken read (STATE 3, caught by the caller's wrapper).
    """
    from alfred.web.identity import synthetic_chat_id
    from alfred.web.notify_state import WebNotifyStore

    store_path = _resolve_store_path(config)
    store = WebNotifyStore.create(store_path)
    store.load()
    return store.list_for(synthetic_chat_id(operator))


# ---------------------------------------------------------------------------
# Render — STATE 1
# ---------------------------------------------------------------------------


def _render_notices(notices: list[dict[str, Any]], window_label: str) -> str:
    """Render the STATE-1 notices section (bulleted, read-only)."""
    lines = [_header(len(notices)), f"_since {window_label}_", ""]
    for entry in notices:
        text = str(entry.get("text") or "").strip() or "(no text)"
        precedence = str(entry.get("precedence") or "R").strip() or "R"
        source = str(entry.get("source") or "").strip() or "(unknown source)"
        url = str(entry.get("issue_url") or "").strip()
        parts = [
            f"- {text}",
            f"precedence {precedence}",
            f"source {source}",
        ]
        if url:
            parts.append(url)
        lines.append("  ·  ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


def ticket_notify_section(
    config: DailySyncConfig,
    today: date,
) -> str | None:
    """Section provider — read-only, fail-loud ticket-notify observability.

    Returns ``None`` (section omitted) when
    ``daily_sync.ticket_notify.enabled`` is false — instances that don't
    opt in are byte-unchanged. Otherwise renders exactly one of the three
    states (notices / explicit-none / loud-⚠️). Never raises: every read
    is wrapped so a failure degrades to STATE 3, never crashes the fire.
    """
    tn = getattr(config, "ticket_notify", None)
    if tn is None or not tn.enabled:
        log.debug("daily_sync.ticket_notify.disabled")
        return None

    now = datetime.now(timezone.utc)
    window_start, window_label = _resolve_window(config, now)

    # --- Resolve web config (state-3 gates + operator key) ---------------
    web_config, web_err = _load_web_config(config)
    if web_config is None:
        log.warning(
            "daily_sync.ticket_notify.web_config_unresolved",
            date=today.isoformat(),
            reason=web_err,
        )
        return _state3(
            f"could not resolve web config to verify the notify pipeline: "
            f"{web_err}"
        )

    # (b) — notifications feature switched off.
    if not web_config.notifications.enabled:
        log.warning(
            "daily_sync.ticket_notify.state3_notifications_disabled",
            date=today.isoformat(),
        )
        return _state3(
            "web.notifications.enabled is false — the notify store, sink, and "
            "read routes are all skipped, so KAL-LE tickets cannot be notified "
            "on this instance"
        )

    # (c-proxy) — web surface off → the sink is never mounted at all.
    if not web_config.enabled:
        log.warning(
            "daily_sync.ticket_notify.state3_web_disabled",
            date=today.isoformat(),
        )
        return _state3(
            "web.enabled is false — the web surface (and its notify sink) is "
            "never mounted, so KAL-LE ticket notices sent to this instance "
            "have nowhere to land"
        )

    # (c-proxy) — no operator to key notices to (sink_no_operator).
    users = getattr(web_config, "users", []) or []
    operator = users[0].name if users else ""
    if not operator:
        log.warning(
            "daily_sync.ticket_notify.state3_no_operator",
            date=today.isoformat(),
        )
        return _state3(
            "no web.users configured — the notify sink has no operator "
            "identity to key ticket notices to; every notice is dropped"
        )

    # (a) — store read, best-effort. A failure becomes STATE 3, never a crash.
    try:
        notices = _read_notices(config, operator)
    except Exception as exc:  # noqa: BLE001 — read failure → loud, never blocks the fire
        log.warning(
            "daily_sync.ticket_notify.state3_store_read_failed",
            date=today.isoformat(),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return _state3(
            f"notify store read failed: {exc.__class__.__name__}: {exc}"
        )

    windowed = [n for n in notices if _within_window(n.get("ts"), window_start)]

    if not windowed:
        # STATE 2 — explicit intentionally-left-blank line.
        log.info(
            "daily_sync.ticket_notify.none",
            date=today.isoformat(),
            window_start=window_start.isoformat(),
            total_in_store=len(notices),
        )
        return (
            f"{_header(0)}\n\n"
            f"*(No ticket notifications since {window_label})*"
        )

    # STATE 1 — notices present.
    log.info(
        "daily_sync.ticket_notify.rendered",
        date=today.isoformat(),
        count=len(windowed),
        total_in_store=len(notices),
        window_start=window_start.isoformat(),
    )
    return _render_notices(windowed, window_label)


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times."""
    from . import assembler

    if "ticket_notify" in assembler.registered_providers():
        return
    assembler.register_provider(
        "ticket_notify",
        priority=_PRIORITY,
        provider=ticket_notify_section,
    )


__all__ = [
    "SECTION_HEADER_BASE",
    "clear_raw_config",
    "register",
    "set_raw_config",
    "ticket_notify_section",
]

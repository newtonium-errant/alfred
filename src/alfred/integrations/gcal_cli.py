"""``alfred gcal`` subcommand handlers.

Three commands:

  * ``alfred gcal authorize`` — one-time OAuth installed-app flow.
    Opens the user's browser to the Google consent screen, captures
    the redirect, saves the token JSON to ``token_path``. Subsequent
    calls reuse the saved token (refreshed transparently when expired).

  * ``alfred gcal status`` — read-only health snapshot. Prints whether
    a token exists, the configured calendar IDs (redacted to last 8
    chars for shoulder-surf protection), and a quick "events in next
    24h on Alfred + primary" probe so the operator can confirm the
    integration is live.

  * ``alfred gcal test-write`` — creates a throwaway event ~2 hours
    from now on the Alfred calendar to validate the full create path
    end-to-end, then optionally cleans it up. Useful right after
    operator setup to confirm the writes are landing.

All commands return an integer exit code (0 OK, non-zero failure) so
they compose cleanly with shell pipelines and the parent CLI's
``sys.exit(...)`` dispatcher.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def _redact_id(value: str) -> str:
    """Show only the last 8 chars of a calendar ID. Returns ``""`` for empty."""
    if not value:
        return ""
    if len(value) <= 8:
        return value
    return f"...{value[-8:]}"


def _print(line: str = "") -> None:
    """Print to stdout. Wrapper exists so test fixtures can monkeypatch easily."""
    print(line)


# ---------------------------------------------------------------------------
# `alfred gcal authorize`
# ---------------------------------------------------------------------------


def cmd_authorize(raw: dict[str, Any]) -> int:
    """One-time OAuth flow. Saves token to disk, prints success."""
    from .gcal import GCalClient, GCalNotAuthorized, GCalNotInstalled
    from .gcal_config import load_from_unified

    config = load_from_unified(raw)
    if not config.enabled:
        _print(
            "GCal is disabled in config (gcal.enabled: false).\n"
            "Authorization will still proceed — config-disabled means the\n"
            "transport handler skips GCal at runtime, but you may want a\n"
            "valid token cached anyway for `alfred gcal test-write` etc.",
        )

    _print(f"Reading client credentials from: {config.credentials_path}")
    _print(f"Token will be saved to:          {config.token_path}")
    _print("")
    _print("This will open your browser for Google's consent screen.")
    _print("Approve the requested scopes to grant Alfred access.")
    _print("")

    client = GCalClient(
        credentials_path=config.credentials_path,
        token_path=config.token_path,
        scopes=config.scopes,
    )
    try:
        email = client.authorize_interactive()
    except GCalNotInstalled as exc:
        _print(f"ERROR: {exc}")
        return 78  # convention: missing optional dep
    except GCalNotAuthorized as exc:
        _print(f"ERROR: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        _print(f"ERROR: OAuth flow failed: {exc}")
        return 1

    _print("")
    if email:
        _print(f"Authorized as: {email}")
    else:
        _print("Authorized (account email not exposed by token).")
    _print(f"Token saved to: {config.token_path}")
    _print("")
    _print("Next steps:")
    _print("  1. Set ALFRED_GCAL_CALENDAR_ID and ALFRED_GCAL_PRIMARY_ID in .env")
    _print("  2. Run `alfred gcal status` to confirm wiring")
    _print("  3. Run `alfred gcal test-write` to validate end-to-end")
    return 0


# ---------------------------------------------------------------------------
# `alfred gcal status`
# ---------------------------------------------------------------------------


def cmd_status(raw: dict[str, Any], *, wants_json: bool = False) -> int:
    """Read-only health snapshot. Probes both calendars for next-24h events."""
    from .gcal import GCalClient, GCalError, GCalNotAuthorized, GCalNotInstalled
    from .gcal_config import load_from_unified

    config = load_from_unified(raw)
    out: dict[str, Any] = {
        "enabled": config.enabled,
        "credentials_path": str(config.credentials_path),
        "token_path": str(config.token_path),
        "alfred_calendar_id_redacted": _redact_id(config.alfred_calendar_id),
        "primary_calendar_id_redacted": _redact_id(config.primary_calendar_id),
        "scopes": config.scopes,
        "authorized": False,
        "alfred_events_next_24h": None,
        "primary_events_next_24h": None,
        "error": None,
    }

    if not config.enabled:
        out["error"] = "gcal.enabled is false (set true in config.yaml to use)"
    else:
        client = GCalClient(
            credentials_path=config.credentials_path,
            token_path=config.token_path,
            scopes=config.scopes,
        )
        out["authorized"] = client.is_authorized()
        if not out["authorized"]:
            out["error"] = (
                "no usable token on disk — run `alfred gcal authorize`"
            )
        else:
            # Probe both calendars for the next 24h window.
            now = datetime.now(timezone.utc)
            window_end = now + timedelta(hours=24)
            for cal_id, key in (
                (config.alfred_calendar_id, "alfred_events_next_24h"),
                (config.primary_calendar_id, "primary_events_next_24h"),
            ):
                if not cal_id:
                    out[key] = "calendar_id not configured"
                    continue
                try:
                    events = client.list_events(cal_id, now, window_end)
                    out[key] = len(events)
                except GCalNotInstalled as exc:
                    out["error"] = str(exc)
                    out[key] = None
                except GCalNotAuthorized as exc:
                    out["error"] = str(exc)
                    out[key] = None
                except GCalError as exc:
                    out["error"] = f"API error: {exc}"
                    out[key] = None

    if wants_json:
        _print(json.dumps(out, indent=2, sort_keys=True))
    else:
        _print("GCal integration status")
        _print("=======================")
        _print(f"  enabled:              {out['enabled']}")
        _print(f"  authorized:           {out['authorized']}")
        _print(f"  credentials_path:     {out['credentials_path']}")
        _print(f"  token_path:           {out['token_path']}")
        _print(f"  alfred calendar ID:   {out['alfred_calendar_id_redacted'] or '(not set)'}")
        _print(f"  primary calendar ID:  {out['primary_calendar_id_redacted'] or '(not set)'}")
        _print(f"  scopes:               {', '.join(out['scopes'])}")
        _print("")
        if out["error"]:
            _print(f"  ERROR: {out['error']}")
            _print("")
        if out["enabled"] and out["authorized"]:
            _print("  Next 24h:")
            _print(f"    Alfred calendar:  {out['alfred_events_next_24h']}")
            _print(f"    Primary calendar: {out['primary_events_next_24h']}")

    return 0 if not out["error"] else 1


# ---------------------------------------------------------------------------
# `alfred gcal test-write`
# ---------------------------------------------------------------------------


def cmd_test_write(
    raw: dict[str, Any],
    *,
    cleanup: bool = True,
    wants_json: bool = False,
) -> int:
    """Create a throwaway event +2h from now on the Alfred calendar.

    With ``cleanup=True`` (default), deletes the event right after to
    leave the calendar clean. Pass ``--no-cleanup`` to leave it in
    place for visual confirmation on the operator's phone.
    """
    from .gcal import GCalClient, GCalError, GCalNotAuthorized, GCalNotInstalled
    from .gcal_config import load_from_unified

    config = load_from_unified(raw)
    if not config.enabled:
        msg = "GCal is disabled in config (gcal.enabled: false)"
        if wants_json:
            _print(json.dumps({"ok": False, "error": msg}))
        else:
            _print(f"ERROR: {msg}")
        return 1
    if not config.alfred_calendar_id:
        msg = "alfred_calendar_id not configured (set ALFRED_GCAL_CALENDAR_ID)"
        if wants_json:
            _print(json.dumps({"ok": False, "error": msg}))
        else:
            _print(f"ERROR: {msg}")
        return 1

    client = GCalClient(
        credentials_path=config.credentials_path,
        token_path=config.token_path,
        scopes=config.scopes,
    )

    start = datetime.now(timezone.utc) + timedelta(hours=2)
    end = start + timedelta(minutes=15)
    title = f"Alfred test-write {start.isoformat()}"
    description = (
        "This is a test event created by `alfred gcal test-write` to "
        "validate the OAuth + create-event path. Safe to delete."
    )

    try:
        event_id = client.create_event(
            config.alfred_calendar_id,
            start=start,
            end=end,
            title=title,
            description=description,
        )
    except GCalNotInstalled as exc:
        if wants_json:
            _print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            _print(f"ERROR: {exc}")
        return 78
    except GCalNotAuthorized as exc:
        if wants_json:
            _print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            _print(f"ERROR: {exc}")
            _print("Run `alfred gcal authorize` first.")
        return 1
    except GCalError as exc:
        if wants_json:
            _print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            _print(f"ERROR: {exc}")
        return 1

    result: dict[str, Any] = {
        "ok": True,
        "event_id": event_id,
        "calendar_id_redacted": _redact_id(config.alfred_calendar_id),
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cleaned_up": False,
    }

    if cleanup:
        try:
            deleted = client.delete_event(config.alfred_calendar_id, event_id)
            result["cleaned_up"] = deleted
        except GCalError as exc:
            result["cleanup_error"] = str(exc)

    if wants_json:
        _print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print(f"Test event created on Alfred calendar.")
        _print(f"  event_id:  {event_id}")
        _print(f"  title:     {title}")
        _print(f"  start:     {start.isoformat()}")
        _print(f"  end:       {end.isoformat()}")
        if cleanup:
            if result["cleaned_up"]:
                _print(f"  cleanup:   deleted")
            else:
                _print(f"  cleanup:   FAILED — {result.get('cleanup_error', 'unknown')}")
                _print(f"  Run `alfred gcal status` or visit GCal UI to confirm.")
        else:
            _print(f"  cleanup:   skipped (--no-cleanup); visible on your phone")
    return 0

"""``alfred gcal`` subcommand handlers.

Four commands:

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

  * ``alfred gcal backfill`` — iterates existing vault ``event/``
    records, pushes any that haven't been synced (no ``gcal_event_id``
    in frontmatter) to the Alfred calendar, and writes back the ID.
    ``--dry-run`` reports what would happen without making API calls.
    ``--from-date YYYY-MM-DD`` skips events before that date (default:
    today; operator can pass an earlier date to backfill historical
    events).

All commands return an integer exit code (0 OK, non-zero failure) so
they compose cleanly with shell pipelines and the parent CLI's
``sys.exit(...)`` dispatcher.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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


# ---------------------------------------------------------------------------
# `alfred gcal backfill`
# ---------------------------------------------------------------------------


def _resolve_vault_path(raw: dict[str, Any]) -> Path:
    """Pull the vault path out of the unified config; default to ``./vault``."""
    vault_block = raw.get("vault", {}) or {}
    return Path(str(vault_block.get("path", "./vault"))).expanduser()


def _parse_event_window_from_fm(fm: dict) -> tuple[datetime, datetime] | None:
    """Extract (start, end) datetimes from a vault event's frontmatter.

    Returns None when the record lacks parseable times — caller should
    treat as "skip with reason: no_time". We deliberately do NOT
    fabricate times for date-only records (a 1h block at noon would
    silently land on Andrew's calendar at the wrong time of day —
    safer to skip + surface).
    """
    start_raw = fm.get("start")
    end_raw = fm.get("end")
    if not start_raw or not end_raw:
        return None
    try:
        start_dt = datetime.fromisoformat(str(start_raw))
        end_dt = datetime.fromisoformat(str(end_raw))
    except Exception:  # noqa: BLE001
        return None
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        # Naive datetimes — refuse rather than guess at the timezone.
        # Operator can fix the vault record + re-run.
        return None
    return start_dt, end_dt


# ---------------------------------------------------------------------------
# --infer-times helpers (legacy-record promotion)
# ---------------------------------------------------------------------------
#
# 12 vault events landed before Salem's SKILL update for ISO start/end
# (commit a923c1b). They have ``date: 'YYYY-MM-DD'`` + ``time: '4:00 PM'``
# style fields but no ISO start/end, so backfill SKIPs them by default.
# ``--infer-times`` opts in to combining date+time into ISO datetimes
# in America/Halifax timezone, applying duration heuristics on the
# event title, and writing back to vault BEFORE the GCal sync.

import re as _re

_TIME_RE = _re.compile(
    r"""
    ^\s*
    (?P<hour>\d{1,2})
    (?: [:\.](?P<minute>\d{2}) )?
    \s*
    (?P<ampm> a\.?m\.? | p\.?m\.? )?
    \s*$
    """,
    _re.IGNORECASE | _re.VERBOSE,
)


def _parse_time_string(time_str: str) -> tuple[int, int] | None:
    """Parse a wall-clock time string. Returns (hour, minute) 0-23/0-59, or None.

    Handles common shapes:
      * ``"4:00 PM"`` / ``"4 PM"`` / ``"4:00PM"`` / ``"4pm"`` → (16, 0)
      * ``"9 a.m."`` / ``"9:00 AM"`` / ``"9am"`` → (9, 0)
      * ``"14:30"`` / ``"16:00"`` (24-hour) → (14, 30) / (16, 0)
      * ``"noon"`` → (12, 0); ``"midnight"`` → (0, 0)
      * Unparseable garbage → None (caller skips the record)

    The 24-hour vs 12-hour disambiguation: if the AM/PM suffix is
    absent AND the hour is >= 13, treat as 24-hour. Otherwise the
    suffix governs (or assume AM if absent and hour < 13 — which
    matches the "9" → 09:00 convention).
    """
    if time_str is None:
        return None
    s = str(time_str).strip().lower()
    if not s:
        return None
    if s == "noon":
        return (12, 0)
    if s == "midnight":
        return (0, 0)
    m = _TIME_RE.match(s)
    if not m:
        return None
    hour = int(m.group("hour"))
    minute_str = m.group("minute")
    minute = int(minute_str) if minute_str else 0
    ampm = (m.group("ampm") or "").replace(".", "")
    if ampm == "pm":
        if hour < 12:
            hour += 12
        # 12 PM stays 12 (noon).
    elif ampm == "am":
        if hour == 12:
            hour = 0  # 12 AM = midnight
    # else: no suffix — use hour as-is (24-hour interpretation).
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (hour, minute)


# Duration heuristics — match Salem's SKILL guidance. Order matters:
# more specific / longer-duration keywords first so a "concert
# appointment" lands at 2.5h not 1h.
_DURATION_HEURISTICS: list[tuple[str, int, str]] = [
    # (keyword, minutes, label) — matched against title case-insensitively
    ("concert", 150, "concert/show → 2.5h"),
    ("show", 150, "concert/show → 2.5h"),
    ("ticket", 150, "concert/show → 2.5h"),
    ("festival", 150, "concert/show → 2.5h"),
    ("fest", 150, "concert/show → 2.5h"),
    ("appointment", 60, "appointment → 1h"),
    ("consult", 60, "appointment → 1h"),
    ("exam", 60, "appointment → 1h"),
    ("physio", 60, "appointment → 1h"),
    ("dinner", 60, "meal/meeting → 1h"),
    ("lunch", 60, "meal/meeting → 1h"),
    ("meeting", 60, "meal/meeting → 1h"),
]


def _infer_duration_minutes(title: str) -> tuple[int, str]:
    """Return (minutes, label). Default 1h with 'no match → 1h default' label."""
    if not title:
        return (60, "no title → 1h default")
    needle = title.lower()
    for keyword, minutes, label in _DURATION_HEURISTICS:
        if keyword in needle:
            return (minutes, f"matched '{keyword}' → {label}")
    return (60, "no match → 1h default")


def _halifax_offset_for_date(d: date) -> str:
    """Approximate Atlantic timezone offset for a given date.

    DST window: roughly second Sunday of March through first Sunday of
    November (US/Canadian rule). Approximation: months April–October
    inclusive get ADT (``-03:00``); months November–March inclusive
    get AST (``-04:00``). Mid-March + early-November transition weeks
    may end up on the wrong side of the boundary by a few days, but
    GCal normalizes on display since the dateTime carries the offset
    explicitly. Per spec call-out, "rule-of-thumb is fine".
    """
    return "-03:00" if 4 <= d.month <= 10 else "-04:00"


def _infer_times_for_record(fm: dict) -> dict[str, Any] | None:
    """Try to construct ISO start/end from frontmatter ``date`` + ``time`` fields.

    Returns one of:
      * ``None`` — no inference attempted (record is already
        well-shaped or has no date-only state to promote)
      * ``{"reason": "<code>"}`` — inference rejected; ``code`` is
        ``no_time_string`` / ``no_date`` / ``unparseable_time``
      * ``{"start": "<iso>", "end": "<iso>", "duration_min": int,
         "heuristic": "<label>"}`` — inference succeeded

    Caller is responsible for writing the returned start/end back to
    frontmatter + then running the normal sync.
    """
    # Already has start/end → no inference needed (handled by the
    # normal path).
    if fm.get("start") or fm.get("end"):
        return None

    date_raw = fm.get("date")
    time_raw = fm.get("time")

    if not date_raw:
        # Without a date we can't ground the event. Don't try.
        return {"reason": "no_date"}
    if not time_raw:
        # Date-only record. Salem's SKILL (post-update) shouldn't
        # produce these for events with a known time; legacy records
        # whose raw input had no time mention shouldn't get a wall-
        # clock guessed for them.
        return {"reason": "no_time_string"}

    parsed = _parse_time_string(str(time_raw))
    if parsed is None:
        return {"reason": "unparseable_time", "raw_time": str(time_raw)}
    hour, minute = parsed

    try:
        d = date.fromisoformat(str(date_raw)[:10])
    except ValueError:
        return {"reason": "unparseable_date", "raw_date": str(date_raw)}

    title = str(fm.get("title") or fm.get("name") or "")
    duration_min, heuristic = _infer_duration_minutes(title)

    offset = _halifax_offset_for_date(d)
    start_iso = (
        f"{d.isoformat()}T{hour:02d}:{minute:02d}:00{offset}"
    )
    end_dt = datetime.fromisoformat(start_iso) + timedelta(minutes=duration_min)
    end_iso = end_dt.isoformat()

    return {
        "start": start_iso,
        "end": end_iso,
        "duration_min": duration_min,
        "heuristic": heuristic,
        "raw_date": str(date_raw),
        "raw_time": str(time_raw),
    }


def cmd_backfill(
    raw: dict[str, Any],
    *,
    dry_run: bool = False,
    from_date: str | None = None,
    infer_times: bool = False,
    wants_json: bool = False,
) -> int:
    """Iterate vault event records; push unsynced ones to GCal.

    Per record decision tree:
      * Already has ``gcal_event_id`` in frontmatter → SKIP (already synced)
      * Missing or unparseable ``start``/``end`` → SKIP (no_time)
        UNLESS ``infer_times`` is True AND the record has parseable
        ``date`` + ``time`` fields → INFER + WRITE BACK + sync
      * ``start`` date < ``from_date`` cutoff → SKIP (before_cutoff)
      * Otherwise → push to GCal via ``sync_event_create_to_gcal``,
        which writes back ``gcal_event_id`` + ``gcal_calendar`` on
        success

    ``from_date`` (ISO YYYY-MM-DD): default = today. Operator can
    pass an earlier date to include historical events.

    ``infer_times`` (default False): opt-in flag for legacy-record
    rescue. When True, records with ``date`` + ``time`` (but no ISO
    start/end) get times constructed from those fields + a duration
    heuristic on the title, written back to vault frontmatter, then
    pushed normally. Default-off preserves the "refuse to fabricate
    timestamps" safety. See :func:`_infer_times_for_record` for the
    exact rules.

    Dry-run: makes no API calls, no vault writes; just reports what
    would happen. With ``infer_times=True`` + ``dry_run=True``, the
    inferred (start, end) are reported but not written to vault.
    """
    import frontmatter

    from .gcal import GCalClient
    from .gcal_config import load_from_unified
    from .gcal_sync import sync_event_create_to_gcal

    config = load_from_unified(raw)
    if not config.enabled:
        msg = "GCal is disabled in config (gcal.enabled: false)"
        if wants_json:
            _print(json.dumps({"ok": False, "error": msg}))
        else:
            _print(f"ERROR: {msg}")
        return 1
    if not config.alfred_calendar_id and not dry_run:
        msg = "alfred_calendar_id not configured (set ALFRED_GCAL_CALENDAR_ID)"
        if wants_json:
            _print(json.dumps({"ok": False, "error": msg}))
        else:
            _print(f"ERROR: {msg}")
        return 1

    vault_path = _resolve_vault_path(raw)
    event_dir = vault_path / "event"
    if not event_dir.is_dir():
        msg = f"No event/ directory under vault: {vault_path}"
        if wants_json:
            _print(json.dumps({"ok": False, "error": msg}))
        else:
            _print(f"ERROR: {msg}")
        return 1

    # Resolve cutoff date (default: today in local time).
    if from_date:
        try:
            cutoff = date.fromisoformat(from_date)
        except ValueError:
            msg = f"--from-date must be YYYY-MM-DD, got: {from_date}"
            if wants_json:
                _print(json.dumps({"ok": False, "error": msg}))
            else:
                _print(f"ERROR: {msg}")
            return 1
    else:
        cutoff = date.today()

    # Construct the client only when we'll actually call it (dry-run
    # skips this so an operator can rehearse without a token).
    client = None
    if not dry_run:
        client = GCalClient(
            credentials_path=config.credentials_path,
            token_path=config.token_path,
            scopes=config.scopes,
        )

    synced: list[dict[str, str]] = []
    inferred: list[dict[str, Any]] = []
    skipped_already_synced: list[str] = []
    skipped_no_time: list[str] = []
    skipped_no_time_string: list[str] = []
    skipped_unparseable_time: list[dict[str, str]] = []
    skipped_before_cutoff: list[str] = []
    failed: list[dict[str, str]] = []

    for md_file in sorted(event_dir.glob("*.md")):
        rel_path = f"event/{md_file.name}"
        try:
            post = frontmatter.load(str(md_file))
            fm = dict(post.metadata or {})
        except Exception as exc:  # noqa: BLE001
            failed.append({
                "path": rel_path,
                "error": f"frontmatter parse failed: {exc}",
            })
            continue

        if fm.get("gcal_event_id"):
            skipped_already_synced.append(rel_path)
            continue

        window = _parse_event_window_from_fm(fm)

        # Inference path — only when --infer-times is set AND the record
        # is missing ISO start/end. Mutates ``fm`` in-place + writes back
        # to vault (unless dry-run). The window result is then re-derived
        # from the new fm before falling through to the normal sync path.
        if window is None and infer_times:
            inference = _infer_times_for_record(fm)
            if inference is None:
                # Should never happen given window is None — defensive.
                skipped_no_time.append(rel_path)
                continue
            if "reason" in inference:
                # Inference rejected — bucket by sub-reason.
                reason = inference["reason"]
                if reason == "no_time_string" or reason == "no_date":
                    skipped_no_time_string.append(rel_path)
                elif reason in ("unparseable_time", "unparseable_date"):
                    skipped_unparseable_time.append({
                        "path": rel_path,
                        "reason": reason,
                        "raw": inference.get("raw_time")
                            or inference.get("raw_date", ""),
                    })
                else:
                    skipped_no_time.append(rel_path)
                log.debug(
                    "gcal.backfill_inference_rejected",
                    path=rel_path,
                    reason=reason,
                )
                continue
            # Inference succeeded — log + (unless dry-run) write back.
            log.info(
                "gcal.backfill_inferred_times",
                path=rel_path,
                raw_date=inference["raw_date"],
                raw_time=inference["raw_time"],
                inferred_start=inference["start"],
                inferred_end=inference["end"],
                duration_min=inference["duration_min"],
                heuristic=inference["heuristic"],
            )
            inferred.append({
                "path": rel_path,
                "title": str(fm.get("title") or fm.get("name") or md_file.stem),
                "start": inference["start"],
                "end": inference["end"],
                "duration_min": inference["duration_min"],
                "heuristic": inference["heuristic"],
                "raw_date": inference["raw_date"],
                "raw_time": inference["raw_time"],
            })
            fm["start"] = inference["start"]
            fm["end"] = inference["end"]
            if not dry_run:
                try:
                    post["start"] = inference["start"]
                    post["end"] = inference["end"]
                    new_text = frontmatter.dumps(post)
                    if not new_text.endswith("\n"):
                        new_text += "\n"
                    md_file.write_text(new_text, encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "gcal.backfill_infer_writeback_failed",
                        path=rel_path,
                        error=str(exc),
                    )
                    failed.append({
                        "path": rel_path,
                        "code": "infer_writeback_failed",
                        "detail": str(exc),
                    })
                    continue
            window = _parse_event_window_from_fm(fm)
            if window is None:
                # Inferred ISO strings should always re-parse — if not,
                # something's deeply wrong. Bucket as failure.
                failed.append({
                    "path": rel_path,
                    "code": "inferred_unparseable",
                    "detail": (
                        f"inferred start/end did not re-parse: "
                        f"{inference.get('start')!r} / "
                        f"{inference.get('end')!r}"
                    ),
                })
                continue

        if window is None:
            skipped_no_time.append(rel_path)
            continue
        start_dt, end_dt = window

        # Date filter — start.date() in local tz vs cutoff.
        if start_dt.astimezone().date() < cutoff:
            skipped_before_cutoff.append(rel_path)
            continue

        title = str(fm.get("title") or fm.get("name") or md_file.stem)
        description = str(fm.get("summary") or "")
        correlation_id = f"backfill-{md_file.stem[:32]}"

        if dry_run:
            synced.append({
                "path": rel_path,
                "title": title,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "would_sync": True,
            })
            continue

        # Real sync.
        result = sync_event_create_to_gcal(
            client=client,
            config=config,
            intended_on=True,  # backfill explicitly intends gcal on
            file_path=md_file,
            title=title,
            description=description,
            start_dt=start_dt,
            end_dt=end_dt,
            correlation_id=correlation_id,
        )
        if result.get("event_id"):
            synced.append({
                "path": rel_path,
                "title": title,
                "gcal_event_id": result["event_id"],
            })
        elif result.get("error"):
            err = result["error"]
            failed.append({
                "path": rel_path,
                "title": title,
                "code": err.get("code", "unknown"),
                "detail": err.get("detail", ""),
            })
        else:
            # Empty result = gcal disabled mid-run (shouldn't happen
            # since we gate at the top, but defensive).
            failed.append({
                "path": rel_path,
                "title": title,
                "code": "unknown",
                "detail": "sync returned empty result",
            })

    summary: dict[str, Any] = {
        "ok": len(failed) == 0,
        "dry_run": dry_run,
        "infer_times": infer_times,
        "from_date": cutoff.isoformat(),
        "vault_path": str(vault_path),
        "synced_count": len(synced),
        "inferred_count": len(inferred),
        "skipped_already_synced": len(skipped_already_synced),
        "skipped_no_time": len(skipped_no_time),
        "skipped_no_time_string": len(skipped_no_time_string),
        "skipped_unparseable_time": len(skipped_unparseable_time),
        "skipped_before_cutoff": len(skipped_before_cutoff),
        "failed_count": len(failed),
        "synced": synced,
        "inferred": inferred,
        "failed": failed,
        # Verbose lists kept under separate keys so a JSON consumer can
        # tell "skipped because already synced" from "skipped because
        # the operator's --from-date filtered them out".
        "skipped_no_time_paths": skipped_no_time,
        "skipped_no_time_string_paths": skipped_no_time_string,
        "skipped_unparseable_time_records": skipped_unparseable_time,
        "skipped_before_cutoff_paths": skipped_before_cutoff,
    }

    total_records = (
        len(synced) + len(failed) + len(skipped_already_synced)
        + len(skipped_no_time) + len(skipped_no_time_string)
        + len(skipped_unparseable_time) + len(skipped_before_cutoff)
    )

    if wants_json:
        _print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        verb = "Would sync" if dry_run else "Synced"
        _print(f"GCal backfill — {'DRY RUN' if dry_run else 'LIVE'}"
               + (" (--infer-times)" if infer_times else ""))
        _print(f"  Vault:      {vault_path}")
        _print(f"  Cutoff:     {cutoff.isoformat()} (events before are skipped)")
        _print(f"  {verb}:     {len(synced)}")
        if infer_times:
            inferred_verb = "Would infer" if dry_run else "Inferred"
            _print(f"  {inferred_verb}:   {len(inferred)} "
                   f"(--infer-times wrote start/end before sync)")
        skip_parts = [
            f"{len(skipped_already_synced)} already synced",
            f"{len(skipped_no_time)} no time",
        ]
        if infer_times:
            skip_parts.append(
                f"{len(skipped_no_time_string)} no time-string"
            )
            skip_parts.append(
                f"{len(skipped_unparseable_time)} unparseable time"
            )
        skip_parts.append(f"{len(skipped_before_cutoff)} before cutoff")
        _print(f"  Skipped:    {', '.join(skip_parts)}")
        _print(f"  Failed:     {len(failed)}")
        if synced:
            _print("")
            _print("  Records:")
            for s in synced:
                if dry_run:
                    _print(f"    [would sync]  {s['path']}  ({s['title']})")
                else:
                    _print(f"    [synced]      {s['path']}  → {s['gcal_event_id']}")
        if inferred:
            _print("")
            _print("  Inferred (--infer-times):")
            for i in inferred:
                _print(
                    f"    [inferred]    {i['path']}  "
                    f"({i['raw_date']} {i['raw_time']} → "
                    f"{i['start']} → {i['end']}; {i['heuristic']})"
                )
        if skipped_unparseable_time:
            _print("")
            _print("  Skipped (unparseable time):")
            for u in skipped_unparseable_time:
                _print(
                    f"    [skip]        {u['path']}  "
                    f"(raw {u.get('raw', '')!r} could not be parsed)"
                )
        if failed:
            _print("")
            _print("  Failures:")
            for f in failed:
                _print(f"    [FAILED]      {f['path']}  "
                       f"({f.get('code', 'unknown')}: {f.get('detail', '')})")
        if total_records == 0:
            # Per ``feedback_intentionally_left_blank.md`` — explicit
            # "ran, nothing to do" so silence is distinguishable from
            # broken.
            _print("")
            _print("  No event records found in vault.")

    return 0 if not failed else 1

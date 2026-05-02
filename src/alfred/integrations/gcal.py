"""Google Calendar adapter — OAuth Installed-App flow + Calendar v3.

Used by Salem's Phase A+ inter-instance comms to:

  1. **Conflict-check** — query Andrew's Alfred Calendar (R/W) and his
     primary calendar (read-only by application policy) so an event
     proposed by Hypatia / KAL-LE doesn't slot on top of a real meeting
     Salem can't see in the vault.

  2. **Push-to-phone** — after a vault ``event/`` record is created,
     create the matching event on the Alfred Calendar so Andrew sees it
     on his phone calendar app. The vault is canonical; the GCal entry
     is a projection.

Architecture:
  * **Calendar-as-provenance** — Salem only ever writes to the configured
    Alfred Calendar ID. Andrew's manual writes go to his primary. The
    calendar holding the event encodes its origin; no metadata field
    needed.
  * **Read-only on primary** — enforced at this layer: the only methods
    that take a calendar_id are list_events (no side effects) and
    create_event (which we never call against primary in any code path).
  * **Token refresh handled by google-auth** — load saved token JSON,
    if expired call ``Credentials.refresh(Request())``, re-write to
    disk on success.

SDK quirks centralized here (per ``feedback_sdk_quirk_centralization.md``):
  * ``InstalledAppFlow.run_local_server(port=0)`` for the one-time
    interactive consent flow — picks a free localhost port itself.
  * ``Credentials.from_authorized_user_file`` reads our cached token.
  * ``service.events().list().execute()`` returns a dict whose ``items``
    key holds raw event dicts; ``insert().execute()`` returns the
    created event dict.
  * Datetime serialization: GCal expects RFC 3339 strings for dateTime.
    Datetime objects must be timezone-aware; we serialize via
    ``isoformat()`` and pass ``timeZone`` separately for safety.
  * Datetime parse: GCal returns either ``dateTime`` (timed event) or
    ``date`` (all-day event). Helper ``_parse_event_window`` handles
    both, returning timezone-aware datetimes.

Optional dependency: ``google-auth``, ``google-auth-oauthlib``,
``google-api-python-client`` are imported lazily inside the methods
that need them so the module loads even when the libs aren't installed.
Callers that touch the Google API path should be wrapped in
``try/except GCalNotInstalled`` (or be gated by config-enabled checks
upstream).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public exception types
# ---------------------------------------------------------------------------


class GCalError(Exception):
    """Base class for all GCal adapter errors."""


class GCalNotInstalled(GCalError):
    """Raised when the google-* libraries aren't importable.

    Caller should treat this as "GCal integration unavailable" — log
    + skip the GCal-dependent code path, don't crash the request.
    """


class GCalNotAuthorized(GCalError):
    """Raised when no usable token is on disk and refresh failed.

    Operator must run ``alfred gcal authorize`` to mint a fresh token.
    """


class GCalAPIError(GCalError):
    """Raised on any non-recoverable Google API failure (HTTP error, quota)."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GCalEvent:
    """One Google Calendar event in normalized form.

    ``raw`` holds the full Google API response dict for debugging /
    forward-compat — callers that need a field we haven't lifted into
    a typed attribute can dig in there.
    """

    id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Conflict-check helper (consumed by transport.peer_handlers)
# ---------------------------------------------------------------------------


def event_to_conflict_dict(
    event: GCalEvent,
    *,
    source: str,
) -> dict[str, Any]:
    """Render a GCalEvent into the conflict-response shape.

    Mirrors the vault-conflict shape produced by
    ``transport.peer_handlers._scan_event_conflicts``, with two GCal-
    specific fields:

      * ``source`` — caller-supplied string ("gcal_alfred" or
        "gcal_primary") so the proposing instance / Andrew can read
        "you have a primary-calendar meeting" rather than just "you
        have a meeting".
      * ``gcal_event_id`` — opaque GCal ID, usable to delete or fetch
        the event via this adapter later.
    """
    return {
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "source": source,
        "gcal_event_id": event.id,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# OAuth scope sufficient for read+write on calendar events. Narrower
# than ``calendar`` (which would also let us mutate calendars themselves —
# create / delete entire calendars). Application-layer policy further
# restricts writes to the configured Alfred Calendar ID.
DEFAULT_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/calendar.events",
]


def _import_google() -> tuple[Any, Any, Any, Any]:
    """Import google-auth + google-api-python-client; raise on missing.

    Returned tuple: ``(Credentials, Request, InstalledAppFlow, build)``.
    Centralizes the lazy import so each entry point doesn't repeat the
    try/except.
    """
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:
        raise GCalNotInstalled(
            "Google Calendar integration requires google-auth, "
            "google-auth-oauthlib, and google-api-python-client. "
            "Install with: pip install -e '.[gcal]'"
        ) from exc
    return Credentials, Request, InstalledAppFlow, build


def _parse_event_window(raw: dict) -> tuple[datetime, datetime]:
    """Pull (start, end) datetimes out of a Google API event dict.

    Google returns one of two shapes for each end:

      * ``{"dateTime": "2026-05-04T14:00:00-03:00", "timeZone": "..."}``
        (timed event)
      * ``{"date": "2026-05-04"}``
        (all-day event — symmetric expansion to ±12h around UTC midnight
        applied here so the conflict-check overlap logic still finds
        timed-event collisions inside the day, regardless of operator
        timezone)

    Returns timezone-aware datetimes.
    """
    def _coerce(end_dict: dict, *, is_end: bool) -> datetime:
        if "dateTime" in end_dict:
            # GCal sends ISO 8601 with offset; fromisoformat handles it
            # since 3.11. For older Python, swap to a more permissive
            # parser — the project pins >=3.11 so we're safe.
            dt = datetime.fromisoformat(end_dict["dateTime"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        if "date" in end_dict:
            d = datetime.fromisoformat(end_dict["date"]).date()
            # Symmetric ±12h around UTC midnight — same trick the
            # vault-side scanner uses for date-only records. Brackets
            # any plausible local timezone the all-day record meant.
            base = datetime.combine(d, time(12, 0), tzinfo=timezone.utc)
            return base + timedelta(days=1) if is_end else base - timedelta(days=1)
        raise GCalAPIError(f"event end has neither dateTime nor date: {end_dict!r}")

    start_raw = raw.get("start") or {}
    end_raw = raw.get("end") or {}
    return _coerce(start_raw, is_end=False), _coerce(end_raw, is_end=True)


def _normalize_event(raw: dict, calendar_id: str) -> GCalEvent:
    """Convert one raw Google event dict → GCalEvent."""
    start_dt, end_dt = _parse_event_window(raw)
    return GCalEvent(
        id=str(raw.get("id", "")),
        calendar_id=calendar_id,
        title=str(raw.get("summary") or "(no title)"),
        start=start_dt,
        end=end_dt,
        description=str(raw.get("description") or ""),
        raw=raw,
    )


def _expand_user_path(path: str | Path) -> Path:
    """Expand ``~`` and resolve to absolute Path. Idempotent on absolute paths."""
    return Path(path).expanduser()


# ---------------------------------------------------------------------------
# GCalClient
# ---------------------------------------------------------------------------


class GCalClient:
    """Thin wrapper around Google Calendar v3 with cached credentials.

    Construction is cheap (no network, no import). The Google service
    object is built lazily inside :meth:`_service` and cached on the
    instance. Token refresh happens transparently on every service
    fetch — google-auth's ``Credentials`` knows how to refresh itself
    given a ``Request`` and a refresh token.

    NOT thread-safe by design — instantiate one per caller (or pass
    around inside a single asyncio task). The Google API client is
    synchronous; if you need to call from async code, run via
    ``asyncio.to_thread``.
    """

    def __init__(
        self,
        credentials_path: str | Path,
        token_path: str | Path,
        scopes: list[str] | None = None,
    ) -> None:
        self.credentials_path = _expand_user_path(credentials_path)
        self.token_path = _expand_user_path(token_path)
        self.scopes = list(scopes) if scopes else list(DEFAULT_SCOPES)
        self._service: Any | None = None
        self._creds: Any | None = None

    # -- Auth lifecycle ------------------------------------------------

    def authorize_interactive(self) -> str:
        """Run the one-time OAuth installed-app flow. Saves token to disk.

        Returns the email address of the authorized account (extracted
        from the saved token's ``id_token`` claim if available, else
        empty string). The CLI prints this to confirm which account
        was authorized.

        Caller is responsible for ``credentials_path`` already pointing
        at a Google Cloud client-credentials JSON (the ``Desktop app``
        OAuth client type — Web is incompatible because it requires
        a registered redirect URI).
        """
        Credentials, _Request, InstalledAppFlow, _build = _import_google()  # noqa: N806

        if not self.credentials_path.exists():
            raise GCalNotAuthorized(
                f"OAuth client credentials file not found: {self.credentials_path}. "
                f"Download credentials.json from Google Cloud Console "
                f"(OAuth 2.0 Client IDs → Desktop app) and save to that path."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path), self.scopes,
        )
        # port=0 tells the library to pick an available localhost port
        # at runtime. The OAuth consent screen redirects the browser
        # back to that port when the user approves.
        creds = flow.run_local_server(port=0)
        self._save_credentials(creds)
        self._creds = creds

        # id_token is optional — best-effort email extraction so the
        # CLI can confirm "authorized as andrew@gmail.com".
        try:
            id_token = getattr(creds, "id_token", None)
            if isinstance(id_token, dict):
                return str(id_token.get("email", "") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def is_authorized(self) -> bool:
        """Cheap pre-flight — does a usable token exist on disk?

        Doesn't refresh; just checks the file is parseable. Used by
        ``alfred gcal status`` so we can report "not authorized" without
        triggering a refresh side-effect.
        """
        if not self.token_path.exists():
            return False
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        # Minimum viable token: must have refresh_token (which is what
        # makes it usable across restarts).
        return bool(data.get("refresh_token"))

    def _load_credentials(self) -> Any:
        """Load credentials from disk + refresh if expired. Cached on self."""
        if self._creds is not None and self._creds.valid:
            return self._creds

        Credentials, Request, _Flow, _build = _import_google()  # noqa: N806

        if not self.token_path.exists():
            raise GCalNotAuthorized(
                f"No GCal token found at {self.token_path}. "
                f"Run `alfred gcal authorize` first."
            )

        try:
            creds = Credentials.from_authorized_user_file(
                str(self.token_path), self.scopes,
            )
        except Exception as exc:  # noqa: BLE001
            raise GCalNotAuthorized(
                f"GCal token at {self.token_path} unreadable: {exc}. "
                f"Run `alfred gcal authorize` to mint a fresh one."
            ) from exc

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:  # noqa: BLE001
                    # The same generic Exception catches both terminal
                    # auth failure (refresh token revoked / expired) AND
                    # transient transport errors (DNS hiccup, TLS handshake,
                    # 5xx from Google). Telling the operator to re-auth on
                    # a transient failure burns an OAuth flow they don't
                    # need. The message acknowledges both — operators
                    # should re-try once before re-authorizing. A tighter
                    # fix would distinguish ``google.auth.exceptions.
                    # RefreshError`` from generic transport failures, but
                    # the import-coupling cost isn't justified for v1.
                    raise GCalNotAuthorized(
                        f"GCal token refresh failed: {exc}. "
                        f"If this persists, run `alfred gcal authorize` to "
                        f"mint a fresh token. (Transient network errors "
                        f"during refresh also surface here — re-try before "
                        f"re-authorizing.)"
                    ) from exc
                # Persist the refreshed access token + new expiry.
                self._save_credentials(creds)
            else:
                raise GCalNotAuthorized(
                    "GCal token is invalid and has no refresh_token. "
                    "Run `alfred gcal authorize`."
                )

        self._creds = creds
        return creds

    def _save_credentials(self, creds: Any) -> None:
        """Write credentials JSON to disk. Creates parent dir if needed."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        # ``Credentials.to_json()`` returns a JSON string we can round-
        # trip via ``from_authorized_user_file``. Write atomic-ish via
        # a temp + rename so a half-written file can never be loaded.
        tmp = self.token_path.with_suffix(self.token_path.suffix + ".tmp")
        tmp.write_text(creds.to_json(), encoding="utf-8")
        tmp.replace(self.token_path)
        log.info(
            "gcal.token_saved",
            path=str(self.token_path),
        )

    def _service_obj(self) -> Any:
        """Build (or return cached) googleapiclient ``service`` instance."""
        if self._service is not None:
            return self._service
        _Creds, _Req, _Flow, build = _import_google()  # noqa: N806
        creds = self._load_credentials()
        # cache_discovery=False suppresses the file-cache deprecation
        # warning; the discovery doc is fetched fresh per process which
        # is fine for a long-running daemon (one fetch per restart).
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    # -- API surface ---------------------------------------------------

    def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        max_results: int = 250,
    ) -> list[GCalEvent]:
        """Return events on ``calendar_id`` overlapping [time_min, time_max].

        ``time_min`` / ``time_max`` must be timezone-aware. Google's
        events.list ``timeMin`` / ``timeMax`` parameters are inclusive/
        exclusive respectively — same half-open semantics as the
        vault-side overlap check, so a back-to-back event at exactly
        ``time_max`` won't be returned (and shouldn't conflict).

        ``max_results`` is the per-page cap (Google's max is 2500;
        250 is plenty for a 30-day window even on a busy calendar).
        v1 doesn't paginate — if a real query trips this cap we'll
        wire pagination, but the conflict-check window is hours not
        weeks so it's not a near-term concern.
        """
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise GCalAPIError(
                "list_events: time_min and time_max must be timezone-aware",
            )

        service = self._service_obj()
        try:
            resp = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,  # expand recurring events
                    orderBy="startTime",
                    maxResults=max_results,
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise GCalAPIError(
                f"events.list failed for calendar {calendar_id}: {exc}"
            ) from exc

        items = resp.get("items", []) or []
        events: list[GCalEvent] = []
        for raw in items:
            # Defensively skip events we can't parse rather than raise —
            # one weird recurrence shouldn't kill the whole query.
            try:
                events.append(_normalize_event(raw, calendar_id))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "gcal.event_parse_skipped",
                    calendar_id=calendar_id,
                    event_id=raw.get("id"),
                    error=str(exc),
                )
                continue
        return events

    def create_event(
        self,
        calendar_id: str,
        *,
        start: datetime,
        end: datetime,
        title: str,
        description: str = "",
        time_zone: str | None = None,
    ) -> str:
        """Create a timed event on ``calendar_id``. Returns the GCal event ID.

        ``start`` / ``end`` must be timezone-aware. ``time_zone`` is
        optional — if omitted, GCal uses the calendar's default timezone
        for display (the dateTime string still carries an offset, so
        the actual time is unambiguous). Pass an explicit IANA name
        (e.g. ``"America/Halifax"``) to force display semantics.

        Per architecture: this method should ONLY be called against the
        Alfred Calendar ID. The handler enforces this; this function
        does not (it's a thin SDK wrapper).
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise GCalAPIError(
                "create_event: start and end must be timezone-aware",
            )
        if end <= start:
            raise GCalAPIError("create_event: end must be strictly after start")

        body: dict[str, Any] = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if time_zone:
            body["start"]["timeZone"] = time_zone
            body["end"]["timeZone"] = time_zone

        service = self._service_obj()
        try:
            created = (
                service.events()
                .insert(calendarId=calendar_id, body=body)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise GCalAPIError(
                f"events.insert failed on calendar {calendar_id}: {exc}"
            ) from exc

        event_id = str(created.get("id", ""))
        log.info(
            "gcal.event_created",
            calendar_id=calendar_id,
            event_id=event_id,
            title=title[:80],
        )
        return event_id

    def get_event(self, calendar_id: str, event_id: str) -> GCalEvent | None:
        """Fetch one event by ID. Returns None if the event was deleted."""
        service = self._service_obj()
        try:
            raw = (
                service.events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            # Google's API returns 404 / 410 for missing/cancelled events.
            # Best-effort string match — the SDK exception type is
            # googleapiclient.errors.HttpError but importing it here
            # would force the optional dep to load eagerly.
            msg = str(exc)
            if "404" in msg or "410" in msg or "Not Found" in msg:
                return None
            raise GCalAPIError(
                f"events.get failed for {calendar_id}/{event_id}: {exc}"
            ) from exc
        return _normalize_event(raw, calendar_id)

    def delete_event(self, calendar_id: str, event_id: str) -> bool:
        """Delete one event. Returns True on success, False if already gone.

        Useful for the ``alfred gcal test-write --cleanup`` flow.
        """
        service = self._service_obj()
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=event_id,
            ).execute()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "404" in msg or "410" in msg or "Not Found" in msg:
                return False
            raise GCalAPIError(
                f"events.delete failed for {calendar_id}/{event_id}: {exc}"
            ) from exc
        log.info(
            "gcal.event_deleted",
            calendar_id=calendar_id,
            event_id=event_id,
        )
        return True

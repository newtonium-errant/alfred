"""Contact-surface router routes — ``/day/*`` (C4).

Three routes, the consumer side of ``preference/Algernon — contact-surface
routing.md``:

    GET  /day/state     → the day-state the PWA evaluates the rule set against
    POST /day/contact   → record one app-open (the rule that fired + the surface)
    POST /day/override  → record a one-tap override of that open

Handler ordering is load-bearing (CLAUDE.md "Relay / asserted-identity routes —
peer-pin requirement"), and is the SAME spine as :mod:`alfred.web.routes_notify`
— deliberately, because this surface serves the same operator-private facts the
notification tray does (an unresolved-notice count, when the operator was last
here, which surfaces they use):

1. **Peer-pin FIRST** — ``transport_peer`` must be the dedicated chat ``web``
   peer or the vouched ``rrts_relay`` peer, checked BEFORE identity resolution
   or any store read. Note this is NOT the ``web_feed`` peer that
   ``transport/routes_feed.py`` pins: those routes carry no user identity at
   all, and every fact here is per-identity. Fail-closed 401 + a logged
   ``web.day.wrong_peer``.
2. **Identity** — :func:`resolve_web_identity`, fail-closed 401.
3. **Recipient-pin** — a vouched ``RRTS_INTAKE_ROLE`` reporter is REFUSED 403.
   A bug reporter is authenticated enough to chat and must never read the
   operator's day, nor write contacts into the operator's own log.
4. **Read/write** — keyed to the identity's OWN ``synthetic_chat_id``.

WHERE THE DAY STATE ACTUALLY COMES FROM — because two of its four inputs did
not exist before this lane, and a reader who assumes otherwise will look for a
source that isn't there:

* ``unresolved_flagged_notifications`` — PRE-EXISTING, read from
  :mod:`alfred.web.notify_state` (the #86 tray, dismissed entries excluded).
* ``last_session_ended`` — PRE-EXISTING, read from
  :mod:`alfred.telegram.state` (active ``last_message_at`` + closed
  ``ended_at``), widened here to also count an app-open.
* ``brief_read_today`` — **DERIVED FROM THIS SURFACE'S OWN CONTACT LOG.**
  Nothing in the system recorded that the brief had been read; ``GET
  /web/outbound/brief/latest`` still records nothing. It is computed from the
  most recent contact whose LANDED surface was the brief, decayed by the
  operator's own ``brief_read_decay_hours``.
* ``last_active_surface`` — **DERIVED THE SAME WAY.** Nothing tracked which
  surface the operator used either.

So this endpoint is not a read-only aggregation of state that already existed;
it is a reader of a log this surface also writes. That is not a workaround —
the spec already required overrides to be logged with their triggering state,
so the write path existed regardless, and these two fall out of it rather than
needing two more stores.

THE CLIENT NEVER SENDS STATE. ``POST /day/contact`` carries a rule id and a
surface — nothing else. The triggering state stored alongside is recomputed HERE
from the same sources ``GET /day/state`` reads. The spec requires overrides to be
logged with their triggering state; taking the client's word for that state would
put an unvalidated blob into the store AND make the pattern evidence say whatever
a buggy (or replayed) client said it was. The server already knows the answer, so
it writes its own.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from alfred.vault.scope import RRTS_INTAKE_ROLE

from .auth import RRTS_RELAY_PEER, WEB_CHAT_PEER, resolve_web_identity
from .config import WebConfig
from .contact_state import ARMED_RULES, SURFACES, WebContactStore
from .day_state import compute_day_state, pattern_config_from_args, read_router_preference
from .identity import WebIdentity
from .keys import (
    KEY_WEB_CONFIG,
    KEY_WEB_CONTACT_FEED,
    KEY_WEB_CONTACT_STORE,
    KEY_WEB_NOTIFY_STORE,
    KEY_WEB_STATE_MGR,
)
from .utils import get_logger

log = get_logger(__name__)

# The subset of the stored contact that the triggering-state snapshot keeps.
# Bounded on purpose: the contact log is a rolling window kept in one JSON file,
# and a snapshot that grew with the payload would grow the file without bound.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "time_since_last_session_hours",
    "brief_read_today",
    "unresolved_flagged_notifications",
    "last_active_surface",
    "levers",
)


def _get_vault_path(request: web.Request) -> Any:
    """The transport's registered vault path, or ``None``.

    Lazy import: :mod:`alfred.transport.peer_handlers` is heavy and sits on the
    other side of a documented import cycle with the transport server, so the
    web modules reach it inside handlers rather than at module scope (the same
    pattern ``routes_chat`` uses for every transport-side import).
    """
    from alfred.transport.peer_handlers import _get_vault_path as _resolve

    return _resolve(request)


def _resolve_day_identity(
    request: web.Request,
) -> tuple[WebIdentity | None, web.Response | None]:
    """Shared auth spine for all three routes — ``(identity, error_response)``.

    Exactly one of the pair is non-``None``. See the module docstring for the
    load-bearing ordering (peer-pin → identity → recipient-pin).
    """
    peer = request.get("transport_peer", "")
    if peer not in (WEB_CHAT_PEER, RRTS_RELAY_PEER):
        log.warning(
            "web.day.wrong_peer",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=f"{WEB_CHAT_PEER} | {RRTS_RELAY_PEER}",
            detail="the contact-surface router reads and writes the operator's "
                   "own day — it requires the dedicated chat 'web' (or vouched "
                   "'rrts_relay') peer token, not web_feed / web_ingest — "
                   "rejecting (401)",
        )
        return None, web.json_response({"error": "wrong_peer"}, status=401)

    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return None, web.json_response({"error": "invalid_session"}, status=401)

    if identity.role == RRTS_INTAKE_ROLE:
        log.warning(
            "web.day.reporter_refused",
            reporter=identity.user[:64],
            detail="rrts_intake (vouched reporter) identity may not read or "
                   "write the operator's contact-router state — rejecting (403)",
        )
        return None, web.json_response({"error": "forbidden"}, status=403)

    return identity, None


def _compute(request: web.Request, identity: WebIdentity) -> dict[str, Any]:
    """Assemble the day-state payload for ``identity``."""
    return compute_day_state(
        user_key=str(identity.synthetic_chat_id),
        contact_store=request.app.get(KEY_WEB_CONTACT_STORE),
        notify_store=request.app.get(KEY_WEB_NOTIFY_STORE),
        state_mgr=request.app.get(KEY_WEB_STATE_MGR),
        vault_path=_get_vault_path(request),
    )


async def _read_json_dict(
    request: web.Request,
) -> tuple[dict[str, Any], web.Response | None]:
    """Parse a JSON object body — ``(body, error_response)``."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → 400
        return {}, web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return {}, web.json_response({"error": "invalid_json"}, status=400)
    return body, None


async def _handle_day_state(request: web.Request) -> web.StreamResponse:
    """GET /day/state — the router's only input."""
    identity, err = _resolve_day_identity(request)
    if err is not None:
        return err
    assert identity is not None  # narrowed by the shared spine

    return web.json_response(_compute(request, identity))


async def _handle_day_contact(request: web.Request) -> web.StreamResponse:
    """POST /day/contact ``{rule, surface}`` — record one app-open.

    Returns ``{"contact_id": str, "recorded": bool}``. An unwired store answers
    ``{"contact_id": "", "recorded": false}`` with a 200: the PWA already opened
    the surface, and failing the log write is not a reason to tell it the open
    did not happen. The honest signal is the flag, which the client logs.
    """
    identity, err = _resolve_day_identity(request)
    if err is not None:
        return err
    assert identity is not None

    body, bad = await _read_json_dict(request)
    if bad is not None:
        return bad

    rule = body.get("rule")
    surface = body.get("surface")
    # Validated against the ARMED set, not merely the vocabulary: a client
    # reporting a rule this build cannot evaluate means the two sides disagree
    # about what is live, and storing that contact would poison the pattern
    # evidence with a context the detector cannot interpret.
    if not isinstance(rule, str) or rule not in ARMED_RULES:
        return web.json_response(
            {"error": "invalid_rule", "armed": list(ARMED_RULES)}, status=400
        )
    if not isinstance(surface, str) or surface not in SURFACES:
        return web.json_response(
            {"error": "invalid_surface", "surfaces": list(SURFACES)}, status=400
        )

    store: WebContactStore | None = request.app.get(KEY_WEB_CONTACT_STORE)
    if store is None:
        # ILB: an unwired store is a deployment state, not an error. Logged so
        # "no contacts are being recorded" is answerable.
        log.info(
            "web.day.contact_not_recorded",
            user=identity.user,
            rule=rule,
            surface=surface,
            reason="no contact store wired (no state path anchored)",
        )
        return web.json_response({"contact_id": "", "recorded": False})

    # The triggering state, computed HERE (see the module docstring) and BEFORE
    # this contact is appended — the state the rule actually fired on, not the
    # state after recording that it fired.
    state = _compute(request, identity)
    snapshot = {k: state[k] for k in SNAPSHOT_FIELDS if k in state}

    entry = store.record_contact(
        str(identity.synthetic_chat_id),
        rule=rule,
        surface=surface,
        state=snapshot,
    )
    log.info(
        "web.day.contact",
        user=identity.user,
        contact_id=entry["id"],
        rule=rule,
        surface=surface,
    )
    return web.json_response({"contact_id": entry["id"], "recorded": True})


async def _handle_day_override(request: web.Request) -> web.StreamResponse:
    """POST /day/override ``{contact_id, surface}`` — the one-tap override.

    The correction signal. Recording it is the ONLY thing that must succeed;
    pattern detection rides behind a belt so a feed fault cannot cost the
    operator the evidence that the router got it wrong.
    """
    identity, err = _resolve_day_identity(request)
    if err is not None:
        return err
    assert identity is not None

    body, bad = await _read_json_dict(request)
    if bad is not None:
        return bad

    contact_id = body.get("contact_id")
    surface = body.get("surface")
    if not isinstance(contact_id, str) or not contact_id.strip():
        return web.json_response({"error": "missing_contact_id"}, status=400)
    if not isinstance(surface, str) or surface not in SURFACES:
        return web.json_response(
            {"error": "invalid_surface", "surfaces": list(SURFACES)}, status=400
        )

    store: WebContactStore | None = request.app.get(KEY_WEB_CONTACT_STORE)
    if store is None:
        log.info(
            "web.day.override_not_recorded",
            user=identity.user,
            surface=surface,
            reason="no contact store wired (no state path anchored)",
        )
        return web.json_response({"recorded": False, "patterns_surfaced": 0})

    user_key = str(identity.synthetic_chat_id)
    entry = store.record_override(user_key, contact_id.strip(), surface=surface)
    if entry is None:
        # 404, not a minted contact: an override with no contact has no
        # triggering state, and a pattern observation without its context is
        # exactly what ``same_context_required`` exists to exclude.
        log.warning(
            "web.day.override_unknown_contact",
            user=identity.user,
            contact_id=contact_id[:64],
            detail="no contact with that id for this identity — refusing to "
                   "mint one, an override needs the state its contact carries",
        )
        return web.json_response({"error": "unknown_contact"}, status=404)

    log.info(
        "web.day.override",
        user=identity.user,
        contact_id=entry["id"],
        rule=entry.get("rule", ""),
        opened=entry.get("surface", ""),
        landed=surface,
    )

    # Propose, never apply. Behind a belt: the override above is already
    # persisted, and a feed fault must not turn a recorded correction into a
    # 500 the client reads as "your override was not saved".
    surfaced = 0
    feed = request.app.get(KEY_WEB_CONTACT_FEED)
    if feed is not None and getattr(feed, "enabled", False):
        from alfred.feed.store import FeedStore

        from .contact_patterns import emit_pattern_cards

        args, _source = read_router_preference(_get_vault_path(request))
        surfaced = emit_pattern_cards(
            contact_store=store,
            user_key=user_key,
            feed_store=FeedStore(feed.store_path),
            instance=feed.instance,
            config=pattern_config_from_args(args),
        )
    else:
        # ILB: no feed wired (or feed disabled) means patterns are detected
        # nowhere and proposed nowhere — a real degraded state, said out loud.
        log.info(
            "web.day.pattern_surfacing_skipped",
            user=identity.user,
            reason="no feed emit handle wired / feed disabled — overrides are "
                   "still recorded, but no pattern card can be dealt",
        )

    return web.json_response({"recorded": True, "patterns_surfaced": surfaced})


def register_day_routes(app: web.Application) -> None:
    """Mount the contact-router routes. Called by ``register_web_routes``
    (web config + stores already stashed)."""
    app.router.add_get("/day/state", _handle_day_state)
    app.router.add_post("/day/contact", _handle_day_contact)
    app.router.add_post("/day/override", _handle_day_override)

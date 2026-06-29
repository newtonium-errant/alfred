"""Web-user identity → synthetic session id, plus the wire-time guard.

``Session.chat_id`` is an ``int`` — a Telegram chat id in the talker's
original world. The web surface keeps the entire session machinery
(``open_session`` / ``StateManager.get_active`` / ``append_turn`` / the
session-record writer) unchanged by mapping each named web user to a
**stable synthetic int** in a reserved band that no Telegram chat id can
ever occupy.

Collision-proof against Telegram:

* The Telegram Bot API guarantees chat ids fit in <= 52 significant bits
  (max magnitude < ``2**52`` ~= 4.5e15); group / supergroup ids are
  negative. The reserved band ``[9e15, 9e15 + 2**32)`` sits entirely
  ABOVE ``2**52`` (so no positive Telegram id reaches it) and is positive
  (so no negative group id collides). It also stays below ``2**53`` (the
  IEEE-754 safe-integer ceiling) as a courtesy, though the synthetic id
  never leaves the server — the wire carries the opaque ``session_key``
  (a uuid), never the int.

The mapping is HASH-keyed off the stable lowercased ``name`` (not the
config list order) so a user's active session survives ``web.users``
reorder / add / remove. A wire-time guard
(:func:`check_synthetic_id_collisions`) makes the (astronomically
unlikely) hash-collision case PROVABLE rather than probable: it fails the
daemon loud if any two web users collide, or if a web user collides with a
Telegram ``allowed_users`` id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from .config import WebConfig, WebUser


# Reserved synthetic-id band. See module docstring for the collision proof.
WEB_USER_ID_BASE = 9_000_000_000_000_000  # 9e15, above Telegram's 2**52 ceiling
WEB_USER_ID_SPAN = 2 ** 32


@dataclass(frozen=True)
class WebIdentity:
    """A resolved, authenticated web user.

    ``user`` is the configured display name; ``role`` feeds
    ``run_turn(user_role=...)`` → ``resolve_scope``; ``synthetic_chat_id``
    keys the session store. Carries ``role`` so a future scope-gated
    ``/vault/*`` write surface can reuse the same identity object.
    """

    user: str
    role: str
    synthetic_chat_id: int


def synthetic_chat_id(name: str) -> int:
    """Map a web-user name to its stable synthetic session id.

    Deterministic: same (normalised) name → same id, across process
    restarts and config edits. Normalisation is ``strip().lower()`` so a
    casing change in config doesn't orphan an active session.
    """
    digest = hashlib.sha256(name.strip().lower().encode("utf-8")).digest()
    return WEB_USER_ID_BASE + (int.from_bytes(digest[:6], "big") % WEB_USER_ID_SPAN)


def resolve_identity_from_name(
    web_config: "WebConfig", name: str | None,
) -> WebIdentity | None:
    """Resolve a name to a :class:`WebIdentity` via the allowlist.

    Case-insensitive match against ``web_config.users``. Returns ``None``
    when ``name`` is empty or not in the allowlist — the caller turns that
    into a fail-closed 403.

    NOTE (Sub-arc A): this is the *unauthenticated* identity path — the
    name is taken from a client-supplied field, which is spoofable. It is
    gated behind the existing peer token (only the registered front-end can
    reach the route) and is curl-/local-only; the public route does NOT go
    live until Sub-arc B replaces this with ``require_web_session`` (an
    instance-signed session token). The function stays as the single
    identity seam so the B swap is localised.
    """
    if not name:
        return None
    target = name.strip().lower()
    if not target:
        return None
    for user in web_config.users:
        if user.name.strip().lower() == target:
            return WebIdentity(
                user=user.name,
                role=user.role,
                synthetic_chat_id=synthetic_chat_id(user.name),
            )
    return None


def check_synthetic_id_collisions(
    users: "Sequence[WebUser]",
    allowed_user_ids: "Iterable[int]" = (),
) -> dict[str, int]:
    """Fail loud if any synthetic id collides — pairwise or with Telegram.

    Computes ``synthetic_chat_id`` for every web user and asserts:

    1. **pairwise-distinct** — no two web users hash to the same id;
    2. **disjoint from Telegram** — no web user's id equals a configured
       ``allowed_users`` id (defensive even though the reserved band sits
       above Telegram's range).

    Raises :class:`ValueError` on any collision so the daemon refuses to
    start with an ambiguous session-id mapping (provable, not probable).
    Returns the ``{name: synthetic_id}`` map on success so the caller can
    log / inspect it.

    Per ``feedback_intentionally_left_blank.md``: the caller logs an
    explicit "checked, clean" signal — a no-collision result is observably
    distinct from the guard not having run.
    """
    allowed = {int(uid) for uid in allowed_user_ids}
    seen: dict[int, str] = {}
    mapping: dict[str, int] = {}
    for user in users:
        sid = synthetic_chat_id(user.name)
        if sid in seen:
            raise ValueError(
                "web synthetic-id collision: "
                f"users {seen[sid]!r} and {user.name!r} both map to {sid}"
            )
        if sid in allowed:
            raise ValueError(
                f"web synthetic-id collision: user {user.name!r} maps to "
                f"{sid}, which is also a Telegram allowed_users id"
            )
        seen[sid] = user.name
        mapping[user.name] = sid
    return mapping

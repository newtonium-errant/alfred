"""Telegram-package compatibility shims — cross-module helpers that
multiple call sites need but should not duplicate.

Currently houses the instance-name normaliser shared between
:mod:`alfred.telegram.bot` (peer-route self-target check) and
:mod:`alfred.telegram.speed_pref` (per-instance TTS speed lookup). The
two modules used to carry independent copies; the bug surface that
created (any divergence between the two normalisations would break the
``(instance, user)`` key match across the dispatch path) is exactly the
sort of drift the canonical-helper pattern exists to prevent.

Distinct from the root Anthropic-SDK helper
(:mod:`alfred._anthropic_compat`), which targets SDK-level model-family
quirks. This module is for *internal* shared helpers.
"""

from __future__ import annotations

from typing import Iterable


def _normalize_instance_name(s: str) -> str:
    """Return the canonical peer-key form of an instance name.

    Lowercases, strips dots, and maps spaces to dashes. The legacy
    ``alfred`` → ``salem`` mapping is preserved so a default-configured
    install (``InstanceConfig(name="Alfred")``) still matches the
    ``salem`` peer key in ``config.transport.peers`` and the
    canonical-person-record preference tables.

    The legacy mapping is intentionally retained — removing it would
    break edge-case migration paths (default-name installs, older
    fixtures) that the multi-instance roster never explicitly retired.
    See ``project_hardcoding_followups.md`` item 5: extraction is safe,
    deletion is deferred.
    """
    normalized = (s or "").lower().replace(".", "").replace(" ", "-")
    if normalized == "alfred":
        return "salem"
    return normalized


def collapse_peer_name(s: str) -> str:
    """Collapse an instance/peer name to its IDENTITY-comparison form.

    Strips every separator that instances are spelled with — dots, spaces,
    dashes, underscores — and casefolds. ``KAL-LE``, ``K.A.L.L.E.``,
    ``kal_le`` and ``kalle`` all collapse to ``kalle``.

    This is deliberately LOSSIER than :func:`_normalize_instance_name`,
    and the two are not interchangeable. ``_normalize_instance_name``
    produces a *storage key* (it preserves dashes, so ``KAL-LE`` →
    ``kal-le`` but ``K.A.L.L.E.`` → ``kalle`` — two spellings of one
    instance yielding two different keys). This collapses spellings to a
    single identity so the two converge. Same shape as
    ``transport.config.is_stayc_peer_name``, which has always compared
    identities this way.

    Not a peer key itself — the collapsed form is a comparison token
    only. Use :func:`resolve_peer_key` to get back the CONFIGURED key.
    """
    collapsed = (s or "").strip().casefold()
    for sep in (".", " ", "-", "_"):
        collapsed = collapsed.replace(sep, "")
    # Legacy default-name install: ``InstanceConfig(name="Alfred")`` must
    # still resolve to the ``salem`` peer key. Mirrors the same mapping in
    # ``_normalize_instance_name``; see that docstring for why it stays.
    if collapsed == "alfred":
        return "salem"
    return collapsed


def resolve_peer_key(
    name: str, known_peer_keys: Iterable[str],
) -> str | None:
    """Resolve any spelling of an instance name to its CONFIGURED peer key.

    ``known_peer_keys`` is this instance's own configured key set — the
    ``transport.peers`` / ``auth.tokens`` keys. The returned value is the
    key AS CONFIGURED (not the collapsed form), so callers can use it
    directly for the peers-dict lookup.

    FAIL-CLOSED: returns ``None`` when nothing matches. This never guesses
    a key that isn't configured — an unroutable name is refused, not
    invented. Callers are expected to log the miss (the
    ``talker.peer_key_unresolved`` event) so a config typo is greppable
    rather than a silent no-route.

    Why this exists (#30): peer keys are the WIRE namespace and are NOT
    the same namespace as vault scopes (Salem's peer key is ``salem`` but
    her scope is ``talker``) — so they can't be unified by renaming. The
    live defect this closes is a *sender-local alias* mismatch: VERA's
    ``transport.peers`` names KAL-LE as ``kalle`` while every other
    instance writes ``kal-le``. Matching on the collapsed identity lets a
    classifier that emits the canonical ``kal-le`` still resolve against
    VERA's ``kalle`` key, and vice versa.

    Ambiguity (two configured keys collapsing to the same identity, e.g.
    both ``kalle`` and ``kal-le`` present) can only mean one instance
    spelled two ways, so either is a correct route. Resolution is the
    sorted-first key purely for determinism.
    """
    target = collapse_peer_name(name)
    if not target:
        return None
    matches = [
        str(key) for key in known_peer_keys
        if collapse_peer_name(str(key)) == target
    ]
    if not matches:
        return None
    return sorted(matches)[0]


__all__ = [
    "_normalize_instance_name",
    "collapse_peer_name",
    "resolve_peer_key",
]

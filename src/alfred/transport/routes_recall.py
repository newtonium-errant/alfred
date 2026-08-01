"""Cross-instance recall — the ANSWER side (#20 S1, 2026-08-01).

A peer-token-gated ``POST /peer/recall`` route: an allowed peer sends a
free-text ``{query, types?}`` and THIS instance searches ITS OWN vault,
returning bounded matches (a capped snippet + a record pointer) or an
honest empty match set (no-match is NOT an error). The talker's ask side
(S2) drives this on a local-miss; this module is only the answerer.

Security shape (the centerpiece — every rule is enforced answerer-side):

  * **The answerer enforces its own allowlist.** The (asking peer, record
    type) check runs HERE against THIS instance's ``transport.recall``
    config — the asker's claim of entitlement is never trusted. Types
    outside the asking peer's allowlist are filtered BEFORE the search, so
    a disallowed type is never globbed, read, or returned.
  * **Identity is the authenticated transport peer.** ``transport_peer``
    is set by the transport ``auth_middleware`` (bearer token match), not
    an asserted header. The peer-pin: a caller whose authenticated peer is
    not a KEY in ``recall.peers`` is refused fail-closed (401
    ``wrong_peer`` + a logged reason) — only a configured recall-asking
    peer may drive an answer.
  * **Bounded projection is answerer-side.** ``max_matches`` caps the
    record count; ``snippet_max_chars`` caps each snippet (oversize →
    truncated + a ``truncated`` flag). The asker cannot request "full".
  * **Every answer is audited.** One ``kind: "recall_read"`` line lands in
    the answerer's existing cross-instance-read trail
    (``canonical.audit_log_path``) — who asked, which types were
    requested / searched / denied, which records left, when. One trail,
    op-differentiated, exactly as ``/peer/search`` reuses it with
    ``kind: "search"``.

STAY-C fence: STAY-C is CATEGORICALLY excluded from recall in both
directions. The primary guard is the config-load fence
(:func:`alfred.transport.config._build_recall` fails loud). This module's
:func:`register_recall_routes` re-checks at mount time (belt-and-braces):
it never mounts ``/peer/recall`` on a STAY-C instance, and raises if
participation was somehow requested on one.

Opt-in inertness: :func:`register_recall_routes` mounts NOTHING when
recall is disabled (the default) — a non-participant never even exposes
the route (``/peer/recall`` → 404), which is the strongest reading of
"an instance with no recall: section answers nothing."

The pure helpers (:func:`resolve_search_types`, :func:`build_snippet`)
carry the allowlist + projection logic with no I/O, so the semantics are
unit-testable without spinning up an aiohttp app — same split as
``peer_search`` vs the ``/peer/search`` handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter
from aiohttp import web

from .canonical_audit import append_audit
from .config import (
    RECALL_QUERY_MAX_CHARS,
    RecallConfigError,
    is_stayc_peer_name,
)
from .nl_broker import truncate_answer
from .peer_handlers import (
    _ensure_correlation_id,
    _get_config,
    _get_vault_path,
    _json_error,
)
from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pure engine — allowlist resolution + snippet projection (no I/O)
# ---------------------------------------------------------------------------


def resolve_search_types(
    allowed_types: list[str],
    requested: Any,
) -> tuple[list[str], list[str]]:
    """Intersect the asker's allowlist with any requested types.

    THE allowlist gate — runs BEFORE the search so a disallowed type is
    never globbed. Returns ``(search_types, denied_types)``:

      * ``requested is None`` → search the FULL allowlist, deny nothing.
      * ``requested`` a list → search only the requested types that are IN
        the allowlist (order-preserved, de-duplicated); any requested type
        OUTSIDE the allowlist lands in ``denied_types`` and is NEVER
        searched or returned.

    The asker's request can only ever NARROW the allowlist, never widen it
    — a type the peer isn't entitled to cannot be reached by naming it.
    """
    allowed = list(dict.fromkeys(allowed_types))  # de-dup, preserve order
    allowed_set = set(allowed)
    if requested is None:
        return allowed, []
    requested_clean = [
        t for t in requested if isinstance(t, str) and t.strip()
    ]
    search = list(dict.fromkeys(
        t for t in requested_clean if t in allowed_set
    ))
    denied = sorted({t for t in requested_clean if t not in allowed_set})
    return search, denied


def build_snippet(body: str, query: str, max_chars: int) -> tuple[str, bool]:
    """Return a bounded excerpt of ``body`` around the first ``query`` hit.

    Deterministic (case-insensitive substring locate, no fuzzy match — the
    house server-side rule). The window starts a little before the match
    (or at the body start when the match is in frontmatter, not the body)
    and is hard-capped to ``max_chars`` via the house
    :func:`~alfred.transport.nl_broker.truncate_answer` primitive.

    Returns ``(snippet, truncated)``. ``truncated`` is True whenever the
    excerpt does not cover the whole body — either the tail was clipped by
    the cap OR the window started past the body's beginning — so the asker
    always knows the snippet is a fragment.
    """
    text = (body or "").strip()
    if not text:
        return "", False
    ql = (query or "").casefold().strip()
    idx = text.casefold().find(ql) if ql else -1
    if idx > 0:
        # Center-ish: start up to a quarter-window before the match so the
        # hit isn't flush against the snippet's leading edge.
        lead = max(0, idx - max(0, max_chars // 4))
    else:
        lead = 0
    window = text[lead:]
    snippet, hard_trunc = truncate_answer(window, max_chars)
    truncated = hard_trunc or lead > 0
    return snippet, truncated


# ---------------------------------------------------------------------------
# I/O engine — search this instance's vault (reuses vault_search)
# ---------------------------------------------------------------------------


def _snippet_for_path(
    vault_path: Path, rel_path: str, query: str, max_chars: int,
) -> tuple[str, bool]:
    """Load a record's body and build its bounded snippet. Never raises."""
    try:
        post = frontmatter.load(str(vault_path / rel_path))
        body = post.content or ""
    except Exception as exc:  # noqa: BLE001 — one bad record never fails recall
        log.warning(
            "transport.recall.snippet_read_failed",
            path=rel_path, error=str(exc),
        )
        return "", False
    return build_snippet(body, query, max_chars)


def search_recall(
    vault_path: Path,
    search_types: list[str],
    query: str,
    *,
    max_matches: int,
    snippet_max_chars: int,
    answerer_instance: str,
) -> list[dict[str, Any]]:
    """Search the allowed type dirs for ``query`` → bounded match list.

    Reuses the house search surface (:func:`alfred.vault.ops.vault_search`)
    per allowed type with a ``glob_pattern`` scoped to that type's
    directory — so ONLY allowlisted types are ever globbed (filter-before-
    search), and the match is a deterministic case-insensitive substring
    (``vault_search``'s ``re.escape`` grep, no fuzzy). Each hit gets a
    bounded snippet + a record pointer ``{instance, path}``. Stops at
    ``max_matches`` (the answerer-side projection cap).
    """
    # Lazy import — vault.ops pulls schema/scope (heavy) only when a
    # request actually fires, keeping this module import-light for tests
    # that exercise the pure helpers.
    from alfred.vault.ops import vault_search

    matches: list[dict[str, Any]] = []
    for rt in search_types:
        if len(matches) >= max_matches:
            break
        # Glob-site path-traversal backstop (defense-in-depth). The recall
        # allowlist is already type-validated at config load (`_build_recall`
        # drops any non-KNOWN type incl. "../"), and `resolve_search_types`
        # only ever emits types drawn from that validated allowlist — so this
        # is belt-and-braces. But this is THE function where a config/request-
        # derived string composes into a filesystem glob, so it refuses any
        # type carrying a path separator or parent-ref rather than trusting
        # the upstream wall (the #18 "every glob-reaching path" pattern).
        if "/" in rt or "\\" in rt or ".." in rt:
            log.warning(
                "transport.recall.unsafe_type_skipped",
                record_type=rt,
                detail="type contains a path separator / parent-ref — "
                       "refusing to glob (traversal backstop)",
            )
            continue
        try:
            hits = vault_search(
                vault_path,
                glob_pattern=f"{rt}/*.md",
                grep_pattern=query,
            )
        except Exception as exc:  # noqa: BLE001 — one bad type never fails recall
            log.warning(
                "transport.recall.type_search_failed",
                record_type=rt, error=str(exc),
            )
            continue
        for hit in hits:
            if len(matches) >= max_matches:
                break
            rel_path = hit.get("path", "")
            snippet, truncated = _snippet_for_path(
                vault_path, rel_path, query, snippet_max_chars,
            )
            matches.append({
                "type": hit.get("type") or rt,
                "name": hit.get("name") or "",
                "snippet": snippet,
                "truncated": truncated,
                "record_pointer": {
                    "instance": answerer_instance,
                    "path": rel_path,
                },
            })
    return matches


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _handle_peer_recall(request: web.Request) -> web.StreamResponse:
    """POST /peer/recall — answer a peer's recall query from our vault.

    Body:
        {
          "query":  "<free-text, required, 1..RECALL_QUERY_MAX_CHARS>",
          "types":  ["person", "project", ...],   # optional; narrows only
          "correlation_id": "<optional>"
        }

    Returns 200 ``{status, instance, count, matches[], correlation_id}``
    where each match is ``{type, name, snippet, truncated,
    record_pointer{instance, path}}``. An empty ``matches`` is a 200 (an
    honest no-match, NOT an error). Error taxonomy (``{reason,
    correlation_id, ...}``): invalid_json (400), empty_query (400),
    query_too_long (400), schema_error (400), wrong_peer (401),
    recall_not_enabled (403), vault_not_configured (503).
    """
    config = _get_config(request)
    recall_cfg = config.recall
    peer = request.get("transport_peer", "")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → 400
        cid = _ensure_correlation_id(request, None)
        return _json_error(400, "invalid_json", correlation_id=cid)
    if not isinstance(body, dict):
        cid = _ensure_correlation_id(request, None)
        return _json_error(400, "invalid_json", correlation_id=cid)
    cid = _ensure_correlation_id(request, body)

    # Fail-closed on participation. The route is only mounted when recall
    # is enabled, so this is belt-and-braces — but never answer with the
    # feature off, whatever the mount path.
    if not recall_cfg.enabled:
        log.warning(
            "transport.recall.rejected",
            reason="recall_not_enabled",
            peer=peer or "(none)",
            correlation_id=cid,
        )
        return _json_error(403, "recall_not_enabled", correlation_id=cid)

    # Peer-pin: identity is the authenticated transport_peer (never the
    # asker's claim). Only a peer that is a configured recall asker (a KEY
    # in recall.peers) may drive an answer — anyone else is fail-closed.
    peer_rules = recall_cfg.peers.get(peer)
    if peer_rules is None:
        log.warning(
            "transport.recall.rejected",
            reason="wrong_peer",
            peer=peer or "(none)",
            correlation_id=cid,
            detail="authenticated peer is not a configured recall asker",
        )
        return _json_error(401, "wrong_peer", correlation_id=cid)

    # --- query (required, bounded) --------------------------------------
    query_raw = body.get("query")
    if not isinstance(query_raw, str) or not query_raw.strip():
        log.warning(
            "transport.recall.rejected",
            reason="empty_query", peer=peer, correlation_id=cid,
        )
        return _json_error(400, "empty_query", correlation_id=cid)
    query = query_raw.strip()
    if len(query) > RECALL_QUERY_MAX_CHARS:
        log.warning(
            "transport.recall.rejected",
            reason="query_too_long", peer=peer,
            query_chars=len(query), correlation_id=cid,
        )
        return _json_error(
            400, "query_too_long",
            correlation_id=cid, max_chars=RECALL_QUERY_MAX_CHARS,
        )

    # --- types (optional; can only narrow the allowlist) ----------------
    requested = body.get("types")
    if requested is not None and not isinstance(requested, list):
        return _json_error(
            400, "schema_error",
            correlation_id=cid, detail="types must be a list of strings",
        )

    # The allowlist gate (the centerpiece): resolve BEFORE any search so a
    # disallowed type is never globbed.
    search_types, denied_types = resolve_search_types(peer_rules.types, requested)
    requested_types = (
        [t for t in requested if isinstance(t, str) and t.strip()]
        if isinstance(requested, list)
        else list(peer_rules.types)
    )

    vault_path = _get_vault_path(request)
    answerer = str(request.app.get("transport.instance_name") or "")
    audit_path = config.canonical.audit_log_path

    if vault_path is None:
        log.warning(
            "transport.recall.rejected",
            reason="vault_not_configured", peer=peer, correlation_id=cid,
        )
        return _json_error(503, "vault_not_configured", correlation_id=cid)

    matches = search_recall(
        vault_path,
        search_types,
        query,
        max_matches=recall_cfg.max_matches,
        snippet_max_chars=recall_cfg.snippet_max_chars,
        answerer_instance=answerer,
    )

    # Audit EVERY answer (including an empty match set) — the answerer's
    # cross-instance-read trail, op kind ``recall_read``. The core columns
    # carry the type-level disclosure decision (requested / granted=searched
    # / denied); ``extra`` carries the recall specifics.
    returned_paths = [m["record_pointer"]["path"] for m in matches]
    append_audit(
        audit_path,
        peer=peer,
        record_type="",  # multi-type op — types live in the columns below
        name="",
        requested=requested_types,
        granted=list(search_types),
        denied=denied_types,
        correlation_id=cid,
        extra={
            "kind": "recall_read",
            "query": query[:200],
            "types_searched": list(search_types),
            "match_count": len(matches),
            "returned_paths": returned_paths,
        },
    )

    # Intentionally-left-blank: a zero-match answer is a legitimate,
    # non-error outcome — log it explicitly so "answered, nothing matched"
    # is distinguishable from "handler never ran".
    log.info(
        "transport.recall.answered",
        peer=peer,
        instance=answerer,
        correlation_id=cid,
        match_count=len(matches),
        types_searched=list(search_types),
        denied_types=denied_types,
    )

    return web.json_response({
        "status": "ok",
        "instance": answerer,
        "count": len(matches),
        "matches": matches,
        "correlation_id": cid,
    })


# ---------------------------------------------------------------------------
# Registrar — opt-in mount (mirrors routes_ingest / routes_feed)
# ---------------------------------------------------------------------------


def register_recall_routes(
    app: web.Application,
    *,
    enabled: bool,
    instance_name: str,
) -> bool:
    """Mount ``POST /peer/recall`` onto ``app`` — IFF recall is enabled.

    Returns ``True`` when the route was mounted, ``False`` otherwise
    (opt-in inertness: nothing is registered + the transport server is
    byte-unchanged; ``/peer/recall`` → 404). Must be called BEFORE the app
    is started (aiohttp forbids route additions on a started app); the
    daemon calls it via :func:`alfred.transport.server.wire_transport_app`.

    STAY-C fence (belt-and-braces — the config-load fence in
    ``_build_recall`` is the primary guard): a STAY-C instance NEVER mounts
    the route, and a request to enable participation on one is a fail-loud
    :class:`RecallConfigError`. The route inherits the transport
    ``auth_middleware`` peer-gating automatically (non-``/health`` route).
    """
    if is_stayc_peer_name(instance_name):
        if enabled:
            raise RecallConfigError(
                f"instance '{instance_name}' is STAY-C — cross-instance "
                "recall participation is categorically forbidden (both "
                "directions); refusing to mount /peer/recall"
            )
        log.info(
            "transport.recall.disabled",
            reason="stay-c categorically excluded from recall",
            instance=instance_name,
        )
        return False

    if not enabled:
        # Intentionally-left-blank: disabled is a deliberate state, logged
        # so "no recall route" is distinguishable from "wiring silently
        # skipped" in an operator audit.
        log.info(
            "transport.recall.disabled",
            reason="transport.recall.enabled is false / absent",
            instance=instance_name,
        )
        return False

    app.router.add_post("/peer/recall", _handle_peer_recall)
    log.info("transport.recall.registered", instance=instance_name)
    return True

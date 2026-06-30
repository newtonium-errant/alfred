"""Typed config for the Alfred outbound-push transport.

Same pattern as every other tool: ``load_from_unified(raw)`` takes the
pre-parsed unified config dict and returns a typed
``TransportConfig``. Environment variables are substituted via
``${VAR}`` syntax before the dataclasses are built.

The transport does NOT run as its own daemon — the server lives inside
the talker's event loop. This config is nevertheless loaded via the
same unified-config pattern so CLI commands, BIT probes, and the
talker's daemon can build it from ``config.yaml`` without a bespoke
loader.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Backward-compat aliases — ``ENV_RE`` and ``_substitute_env`` are
# importable from this module (legacy callers reference them) but
# the canonical home is now ``alfred._env``. New callers should
# import from there directly. See alfred/_env.py for the empty-
# string coalesce semantics + rationale. Module-level imports are
# usable from outside as ``from alfred.transport.config import ENV_RE``
# without any explicit ``__all__`` gate.
from alfred._env import (
    ENV_PLACEHOLDER_RE as ENV_RE,
    substitute_env_in_value as _substitute_env,
)

from .utils import get_logger

log = get_logger(__name__)


# --- Dataclasses ------------------------------------------------------------


# The loopback address the local health probe and the orchestrator's
# ALFRED_TRANSPORT_HOST injection prefer when ``host`` is a multi-bind
# list. A co-located caller must reach the transport over loopback, not
# an overlay/peer IP that also appears in the bind list.
LOOPBACK_HOST: str = "127.0.0.1"


def host_is_loopback(host: str) -> bool:
    """True if ``host`` names a loopback target — the WHOLE loopback set, not
    just ``127.0.0.1``: any ``127.0.0.0/8`` address, ``::1``, or ``"localhost"``.

    A co-located caller (health probe, orchestrator env-inject) must reach the
    transport over loopback; recognising the full set means ``["::1", ...]`` or
    ``["localhost", ...]`` still resolves to a loopback target rather than the
    overlay IP.
    """
    s = (host or "").strip()
    if not s:
        return False
    if s.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(s).is_loopback
    except ValueError:
        return False


def _classify_bind_host(host: str) -> str:
    """Classify a bind-host candidate: ``"ok"`` | ``"wildcard"`` | ``"invalid"``.

    Fail-CLOSED guard for ``host_list`` — on a PHI box with no TLS / rate-limit
    on the transport, an all-interfaces bind is a policy breach, so we enforce
    "never 0.0.0.0" in CODE, not a comment:

      * ``"wildcard"`` — resolves to an unspecified/all-interfaces address
        (``0.0.0.0`` / ``::``). Caught for IP literals via
        ``ipaddress(...).is_unspecified`` AND for the numeric-string forms
        (``"0"`` / ``"00"`` → ``getaddrinfo`` → ``0.0.0.0``) by resolving.
      * ``"invalid"`` — empty, ``"*"``, the ``str``-coerced ``"None"`` / a
        bogus port int, or anything that doesn't resolve to a real address.
      * ``"ok"`` — a concrete, bindable, non-wildcard address.

    ``getaddrinfo`` here is bounded: a bind address is an IP literal or
    ``localhost`` (resolved locally), never a remote hostname, so there's no
    meaningful DNS-hang risk at startup.
    """
    s = (host or "").strip()
    if not s or s == "*":
        return "invalid"
    # Fast path: a clean IP literal.
    try:
        ip = ipaddress.ip_address(s)
        return "wildcard" if ip.is_unspecified else "ok"
    except ValueError:
        pass
    # Not an IP literal — resolve (catches "0"/"00" → 0.0.0.0, "localhost" → ok,
    # rejects un-resolvable garbage like the str-coerced "None").
    try:
        infos = socket.getaddrinfo(s, None)
    except (socket.gaierror, UnicodeError, OSError, ValueError):
        return "invalid"
    if not infos:
        return "invalid"
    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            if ipaddress.ip_address(sockaddr[0]).is_unspecified:
                return "wildcard"
        except (ValueError, IndexError):
            continue
    return "ok"


def normalize_host_list(value: Any) -> list[str]:
    """De-duplicated, order-preserved bind list from a ``host`` value.

    Accepts the back-compat string form (single bind → one-element list)
    or the Stage 3.5 list form (``["127.0.0.1", "10.99.0.1"]`` → both,
    de-duplicated, order-preserved). ``None`` / empty-string / empty-list
    → ``[]`` (callers decide the fail-safe). Non-string scalars are
    ``str``-coerced so a stray YAML value doesn't crash the loader.
    """
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (list, tuple)):
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            s = str(item).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out
    s = str(value).strip()
    return [s] if s else []


def resolve_local_host(value: Any, default: str = LOOPBACK_HOST) -> str:
    """Single loopback-preferred address from a ``host`` value (str|list).

    Used where a co-located caller must reach the transport over a
    loopback interface — the local health probe and the orchestrator's
    ``ALFRED_TRANSPORT_HOST`` injection. A single host (string, or a
    one-element list) passes through unchanged (full back-compat for the
    pre-Stage-3.5 string case). For a multi-bind list, prefer an explicit
    ``127.0.0.1`` if present, else the first entry. Empty / missing →
    ``default`` (callers that must distinguish "unset" pass ``""``).
    """
    hosts = normalize_host_list(value)
    if not hosts:
        return default
    if len(hosts) == 1:
        return hosts[0]
    # Prefer ANY loopback target (127.0.0.0/8, ::1, "localhost"), not just the
    # literal 127.0.0.1 — so ["::1", "10.99.0.1"] resolves to the loopback.
    for h in hosts:
        if host_is_loopback(h):
            return h
    return hosts[0]


@dataclass
class ServerConfig:
    """HTTP server bind address.

    Localhost-only by default — v1 assumes all callers are co-located
    with the talker daemon. Stage 3.5 widens this to support peer
    connections from other hosts; until then, exposing the port to
    non-loopback interfaces is a policy mistake (no TLS, shared-secret
    auth, no rate-limiting outside per-chat Telegram floor).

    ``host`` accepts either a single address string (the back-compat
    default — one bind, byte-identical behavior) OR a list of addresses
    (Stage 3.5 — bind every address in the list against the one shared
    port). Use :meth:`host_list` to read the normalized bind list and
    :meth:`host_display` for a human-readable render; never read ``host``
    raw at a bind/probe site (a list value would break a ``str``-shaped
    consumer). The list form is deliberately an explicit allowlist — we
    bind exactly the named addresses and nothing else, never ``0.0.0.0``.
    """

    host: str | list[str] = LOOPBACK_HOST
    port: int = 8891

    def host_list(self) -> list[str]:
        """Normalized, de-duplicated, validated bind list — the SINGLE choke
        point that enforces the bind allowlist.

        A string ``host`` yields a single-element list (one ``TCPSite``); a
        list yields each address once, order-preserved. Each entry is then
        fail-CLOSED validated (:func:`_classify_bind_host`): any wildcard /
        all-interfaces address (``0.0.0.0`` / ``::`` / ``"0"`` / ``"00"``) or
        un-resolvable garbage (``"*"``, the ``str``-coerced ``"None"`` / a bogus
        int) is DROPPED with a loud WARN — NEVER bound. This is why a PHI box
        with no TLS on the transport can't be tricked into an all-interfaces
        bind by config.

        Fail-safe: if validation empties the list, fall back to
        ``[LOOPBACK_HOST]`` — the server NEVER binds nothing (silent offline)
        and NEVER falls back to a wildcard.
        """
        kept: list[str] = []
        for h in normalize_host_list(self.host):
            verdict = _classify_bind_host(h)
            if verdict == "ok":
                kept.append(h)
            else:
                log.warning(
                    "transport.server.host_dropped",
                    host=h,
                    reason=verdict,  # "wildcard" | "invalid"
                )
        return kept or [LOOPBACK_HOST]

    def host_display(self) -> str:
        """Human-readable bind list, comma-joined.

        A single host renders byte-identically to the old ``host`` string
        (e.g. ``"127.0.0.1"``); a list renders ``"127.0.0.1, 10.99.0.1"``.
        """
        return ", ".join(self.host_list())


@dataclass
class SchedulerConfig:
    """In-process scheduler knobs.

    The scheduler runs inside the talker daemon as a sibling asyncio
    task. It polls the vault for due ``remind_at`` values on
    task records and dispatches scheduled entries out of the pending
    queue. See ``src/alfred/transport/scheduler.py`` (commit 4).
    """

    # How often the scheduler wakes up to look for due reminders.
    poll_interval_seconds: int = 30

    # Reminders whose ``remind_at`` is older than this window by the
    # time the scheduler sees them get routed to the dead-letter queue
    # instead of firing. Catches the "daemon was down for hours" case —
    # spamming a week's worth of stale reminders in one burst would be
    # worse than dropping them and telling the operator.
    stale_reminder_max_minutes: int = 180


@dataclass
class AuthTokenEntry:
    """One entry in the ``auth.tokens`` dict.

    The dict key is the peer name (``local``, plus Stage 3.5 peers like
    ``kal-le`` / ``stay-c``). Each peer gets its own shared secret and
    its own allowlist of ``X-Alfred-Client`` values.
    """

    token: str = ""
    allowed_clients: list[str] = field(default_factory=list)


@dataclass
class AuthConfig:
    """Bearer-token auth config.

    The ``tokens`` dict is keyed by peer name. v1 has one entry
    (``local``) whose token is injected into every co-located tool's
    subprocess env by the orchestrator. Stage 3.5 adds per-peer entries
    using the same schema — no rewrite needed.
    """

    tokens: dict[str, AuthTokenEntry] = field(default_factory=dict)


# Allowed filter operators for a filtered peer query (P1, 2026-06-09).
# Fixed enum — the deterministic broker (``/peer/search``) implements
# exactly these; a predicate naming any other operator is rejected
# fail-closed. ``eq`` scalar equality; ``contains`` substring OR
# list-membership (with wikilink-unwrap for ``[[type/Name]]`` list
# elements); ``gte`` / ``lte`` / ``between`` ISO-date or numeric
# comparison.
FILTER_OPERATORS: frozenset[str] = frozenset(
    {"eq", "contains", "gte", "lte", "between"}
)

# Hard ceiling on a filtered query's result cap, regardless of what a
# per-type ``max_limit`` is set to. Defense against a misconfigured
# policy disclosing an unbounded record set.
FILTER_LIMIT_CEILING: int = 50


@dataclass
class FilterDimRule:
    """Allowed operators for one filterable dimension (P1, 2026-06-09).

    ``op`` is the allowlist of operators (from :data:`FILTER_OPERATORS`)
    a peer may use when filtering on this dimension. A predicate naming
    an operator NOT in this list is denied fail-closed. Unknown operators
    in the config are dropped at load time (validated against
    ``FILTER_OPERATORS``).
    """

    op: list[str] = field(default_factory=list)


@dataclass
class PeerQueryRules:
    """Filtered-query permissions for one peer × record-type (P1).

    The OPTIONAL ``query`` sub-block on :class:`PeerFieldRules`. When
    ABSENT (the default for every existing config entry — ``None`` on
    ``PeerFieldRules.query``), filtered queries are denied entirely and
    only the by-exact-name ``/peer/query`` path works (byte-identical to
    pre-P1). When present, it governs the new ``/peer/search`` endpoint.

    Three fail-closed gates flow from this:
      1. Type-queryable — ``query`` absent → all ``/peer/search`` denied.
      2. Filter-dimension — a predicate dim not in ``filter_dims`` (or an
         operator not in that dim's :class:`FilterDimRule.op`) → denied.
      3. Return-field — the existing ``fields`` allowlist still decides
         what comes back per matched record (via ``apply_field_permissions``).

    ``filter_dims`` maps a frontmatter field name (the dimension the peer
    may filter on) → its :class:`FilterDimRule`. ``sort`` is the allowlist
    of fields usable as a sort key. ``max_limit`` caps the result count
    (clamped further by :data:`FILTER_LIMIT_CEILING`); ``default_limit``
    applies when the request omits a limit.
    """

    filter_dims: dict[str, FilterDimRule] = field(default_factory=dict)
    sort: list[str] = field(default_factory=list)
    max_limit: int = 10
    default_limit: int = 5


@dataclass
class NLQueryRules:
    """NL-lane (LLM-mediated) opt-in for one peer × record-type.

    The OPTIONAL ``nl_query`` sub-block on :class:`PeerFieldRules`
    (LLM lane, 2026-06-10). When ABSENT (``None`` — the default for
    every existing config entry), the NL lane is DENIED for this
    peer × type. Presence requires the deterministic ``query`` block
    on the same entry — the NL lane retrieves THROUGH the deterministic
    engine, so a deterministic policy must exist (``nl_query`` without
    ``query`` is a config inconsistency: warned at load + treated as
    absent, fail-closed).

    ``compose_fields`` is the COMPOSITION-GRANT tier: frontmatter
    fields (dotted notation supported) the holder's composer LLM may
    read as INPUT when answering this peer's NL question, but which the
    deterministic lane continues to deny raw. GOVERNANCE SEMANTIC: a
    compose-grant is a disclosure decision with friction — the peer can
    learn what the field says in PARAPHRASE (bounded by the answer
    length cap + verbatim-run guard), but never receives the raw value.
    Only compose-grant a field whose content is acceptable for that
    peer to learn in prose. Ships EMPTY by default everywhere.

    ``max_records`` caps how many matched records are fed to the
    composer (clamped to the deterministic ``query.max_limit`` and
    :data:`FILTER_LIMIT_CEILING` at execution time).
    """

    compose_fields: list[str] = field(default_factory=list)
    max_records: int = 5


@dataclass
class PeerFieldRules:
    """Per-record-type permissions for one peer.

    ``fields`` is the allowlist of frontmatter field names (supporting
    dotted notation like ``preferences.coding``) the peer may read.
    ``bodies`` is a belt-and-braces flag — never True in v1, never
    honoured by the handler. Parked here so future code that wants to
    grow body-level access doesn't require a schema change.

    ``query`` (P1, 2026-06-09) is the OPTIONAL filtered-query policy. When
    ``None`` (the default + every existing config), filtered queries via
    ``/peer/search`` are denied — only by-exact-name ``/peer/query`` works,
    exactly as before. When present, it opts this peer × type into the
    deterministic filtered-query broker. See :class:`PeerQueryRules`.

    ``nl_query`` (LLM lane, 2026-06-10) is the OPTIONAL NL-lane opt-in.
    When ``None`` (the default + every existing config), NL queries via
    ``kind=query_nl`` are denied for this peer × type — both existing
    lanes byte-identical. See :class:`NLQueryRules`.
    """

    fields: list[str] = field(default_factory=list)
    bodies: bool = False
    query: "PeerQueryRules | None" = None
    nl_query: "NLQueryRules | None" = None


@dataclass
class PeerEntry:
    """One entry in ``transport.peers`` — where to reach a peer instance.

    ``base_url`` is the peer's transport endpoint (e.g.
    ``http://127.0.0.1:8892`` for KAL-LE). ``token`` is the bearer
    secret the local client sends when reaching out. Auth direction
    is symmetric in v1 — the same token appears in the peer's own
    ``transport.auth.tokens`` dict keyed by our instance name, and
    vice versa.
    """

    base_url: str = ""
    token: str = ""


@dataclass
class NLBrokerConfig:
    """Holder-side NL-lane mechanics + answer-shape limits (LLM lane).

    The MASTER SWITCH for the LLM-mediated opt-in lane. ``enabled``
    defaults False — an instance without this block never runs an NL
    broker turn regardless of per-peer ``nl_query`` grants (fail-closed
    at both levels).

    ``model``: Anthropic model id for the interpret + compose calls.
    Empty string (the default) = inherit the talker's
    ``telegram.anthropic.model`` — avoids a per-instance model literal
    in code; operators may override per-instance (e.g. a haiku-class
    model once volume justifies it).

    Answer-shape limits (enforced in CODE, post-compose — the composer
    prompt's rules are the second layer, never the only one):
      * ``max_answer_chars`` — composed answer hard cap; overflow is
        truncated with a marker + audit flag (ratified Decision F).
      * ``verbatim_run_limit`` — no contiguous run of this many
        normalized chars from any compose-tier field value may appear
        in the answer; violation = answer NOT delivered (Decision H).
      * ``compose_field_max_chars`` — per-value input truncation before
        a compose-tier value enters the composer prompt.

    ``max_subqueries`` clamps how many structured sub-queries one NL
    question may derive (Decision E: MVP 1; the interpreter output is a
    list from day one so raising this is config-only).

    ``question_max_chars`` bounds the inbound NL question (token-cost +
    injection-surface gate, checked at handler entry).

    ``interpret_max_tokens`` / ``compose_max_tokens`` cap each LLM
    call's output; ``llm_timeout_seconds`` is the per-call client
    timeout.
    """

    enabled: bool = False
    model: str = ""
    max_subqueries: int = 1
    question_max_chars: int = 2000
    max_answer_chars: int = 1200
    verbatim_run_limit: int = 80
    compose_field_max_chars: int = 1500
    interpret_max_tokens: int = 1024
    compose_max_tokens: int = 1024
    llm_timeout_seconds: float = 30.0


@dataclass
class CanonicalConfig:
    """Per-peer canonical record permissions + audit settings.

    ``owner`` is True on the instance that holds canonical records
    (SALEM). Peers set it to False so their handler returns 404
    ``canonical_not_owned`` for ``/canonical/*`` requests.

    ``audit_log_path`` is where every canonical read (even denied
    ones) gets logged as a JSONL line. Default matches the other
    data/ siblings.

    ``peer_permissions`` is a nested dict keyed by peer name → record
    type → :class:`PeerFieldRules`. Default-deny: if a peer isn't
    listed, or the type isn't listed, or fields is empty, the canonical
    handler returns 403.

    ``proposals_path`` is where ``POST /canonical/<type>/propose``
    queues creation requests from subordinate instances. The Daily Sync
    section provider reads from this file; the dispatcher writes state
    transitions back to it. Default sits alongside the audit log so
    operators can grep both in one ``ls data/``.

    ``nl_broker`` (LLM lane, 2026-06-10) holds the holder-side NL-lane
    mechanics. Default-constructed = disabled; the lane additionally
    requires per-(peer, type) ``nl_query`` grants. See
    :class:`NLBrokerConfig`.
    """

    owner: bool = False
    audit_log_path: str = "./data/canonical_audit.jsonl"
    proposals_path: str = "./data/canonical_proposals.jsonl"
    peer_permissions: dict[str, dict[str, PeerFieldRules]] = field(
        default_factory=dict,
    )
    nl_broker: NLBrokerConfig = field(default_factory=NLBrokerConfig)


@dataclass
class StateConfig:
    """Where the transport state JSON lives.

    ``pending_queue`` holds scheduled ``/outbound/send`` entries whose
    ``scheduled_at`` is in the future. ``send_log`` is a short rolling
    record of recent sends for idempotency (the 24h dedupe window).
    ``dead_letter`` captures terminally-failed entries; the CLI exposes
    inspect/retry/drop commands against it.
    """

    path: str = "./data/transport_state.json"

    # Dead-letter entries older than this are eligible for eviction by
    # the CLI's maintenance command. The daemon does not auto-evict —
    # operators keep the visibility.
    dead_letter_max_age_days: int = 30


# Default body-size cap for the cross-instance ingest route (256 KiB).
# Bounds the DoS surface the 8000-char chat cap doesn't cover; the BFF
# enforces the same ceiling (CONTRACT §5 ``MAX_INGEST_CHARS``).
DEFAULT_INGEST_MAX_BODY_CHARS: int = 262144


@dataclass
class IngestConfig:
    """Cross-instance verbatim document ingest route config (2026-06-29).

    The opt-in ``POST /vault/ingest`` route on the transport app. Default
    ``enabled=False`` — an un-opted-in instance never mounts the route, so
    every non-ingest instance's transport server stays byte-unchanged
    (same opt-in inertness posture as the ``web:`` chat surface).

    ``max_body_chars`` is the verbatim-body size ceiling (chars), enforced
    in the handler with a 413 ``body_too_large``. The BFF caps the same
    value (defense-in-depth across the trust boundary).

    ``types`` is an OPTIONAL per-instance NARROWING of the universal create
    set (``WEB_INGEST_CREATE_TYPES`` = {document, note, source}). When set,
    the handler validates the request type against the INTERSECTION of this
    list and the code-level ceiling — it can only narrow, never widen
    (``check_scope``'s ``web_ingest_types_only`` gate is the hard ceiling).
    Empty / absent (the default) → the full universal set.
    """

    enabled: bool = False
    max_body_chars: int = DEFAULT_INGEST_MAX_BODY_CHARS
    types: list[str] = field(default_factory=list)


@dataclass
class TransportConfig:
    """Typed config for the transport module."""

    server: ServerConfig = field(default_factory=ServerConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    state: StateConfig = field(default_factory=StateConfig)
    canonical: CanonicalConfig = field(default_factory=CanonicalConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    peers: dict[str, PeerEntry] = field(default_factory=dict)


# --- Recursive builder ------------------------------------------------------


def _build_auth_token_entry(data: dict[str, Any]) -> AuthTokenEntry:
    """Build one ``AuthTokenEntry`` from a dict.

    The dict shape is ``{"token": str, "allowed_clients": [str, ...]}``.
    Unknown keys are tolerated but dropped — this keeps forward-compat
    room for Stage 3.5 additions without breaking the v1 loader.
    """
    return AuthTokenEntry(
        token=str(data.get("token", "") or ""),
        allowed_clients=list(data.get("allowed_clients", []) or []),
    )


def _build_auth(data: dict[str, Any]) -> AuthConfig:
    """Build ``AuthConfig`` with per-peer token entries."""
    tokens_raw = data.get("tokens", {}) or {}
    tokens: dict[str, AuthTokenEntry] = {}
    for peer, entry_raw in tokens_raw.items():
        if isinstance(entry_raw, dict):
            tokens[str(peer)] = _build_auth_token_entry(entry_raw)
    return AuthConfig(tokens=tokens)


def _build_peer_query_rules(raw: Any) -> "PeerQueryRules | None":
    """Build the optional ``query`` sub-block, or ``None`` when absent (P1).

    Returns ``None`` when ``raw`` is missing / not a dict — the
    back-compat default that keeps filtered queries denied (only by-name
    works). When present, parses ``filter_dims`` (each dim's ``op`` list
    intersected against :data:`FILTER_OPERATORS` so an unknown operator
    in config is dropped rather than silently granting it), ``sort``,
    ``max_limit`` (clamped to :data:`FILTER_LIMIT_CEILING`), and
    ``default_limit``.
    """
    if not isinstance(raw, dict):
        return None

    filter_dims: dict[str, FilterDimRule] = {}
    dims_raw = raw.get("filter_dims", {}) or {}
    if isinstance(dims_raw, dict):
        for dim_name, dim_rule_raw in dims_raw.items():
            if not isinstance(dim_rule_raw, dict):
                continue
            ops_raw = dim_rule_raw.get("op", []) or []
            # Intersect against the fixed operator enum — an unknown
            # operator in config is dropped (never silently granted).
            ops = [
                str(o) for o in ops_raw
                if isinstance(o, str) and o in FILTER_OPERATORS
            ]
            filter_dims[str(dim_name)] = FilterDimRule(op=ops)

    sort_raw = raw.get("sort", []) or []
    sort = [str(s) for s in sort_raw if isinstance(s, str)]

    try:
        max_limit = int(raw.get("max_limit", 10))
    except (TypeError, ValueError):
        max_limit = 10
    max_limit = max(1, min(max_limit, FILTER_LIMIT_CEILING))

    try:
        default_limit = int(raw.get("default_limit", 5))
    except (TypeError, ValueError):
        default_limit = 5
    default_limit = max(1, min(default_limit, max_limit))

    return PeerQueryRules(
        filter_dims=filter_dims,
        sort=sort,
        max_limit=max_limit,
        default_limit=default_limit,
    )


def _build_nl_query_rules(
    raw: Any,
    *,
    has_query: bool,
    peer_name: str = "",
    type_name: str = "",
) -> "NLQueryRules | None":
    """Build the optional ``nl_query`` sub-block, or ``None`` when absent.

    Returns ``None`` when ``raw`` is missing / not a dict — the
    back-compat default that keeps the NL lane denied for this
    peer × type. A present ``nl_query`` WITHOUT a sibling ``query``
    block is a config inconsistency (the NL lane retrieves through the
    deterministic engine, which gate 1 would deny anyway): warn at load
    time + treat as absent so the misconfiguration is visible, not
    silent (fail-closed either way).
    """
    if not isinstance(raw, dict):
        return None
    if not has_query:
        log.warning(
            "transport.config.nl_query_without_query",
            peer=peer_name,
            type=type_name,
            detail=(
                "nl_query requires a sibling deterministic `query` block; "
                "NL lane stays DENIED for this peer×type until one is added"
            ),
        )
        return None

    compose_raw = raw.get("compose_fields", []) or []
    compose_fields = [f for f in compose_raw if isinstance(f, str) and f]

    try:
        max_records = int(raw.get("max_records", 5))
    except (TypeError, ValueError):
        max_records = 5
    max_records = max(1, min(max_records, FILTER_LIMIT_CEILING))

    return NLQueryRules(
        compose_fields=compose_fields,
        max_records=max_records,
    )


def _build_nl_broker(raw: Any) -> NLBrokerConfig:
    """Build the holder-side ``nl_broker`` block (defaults = disabled).

    Numeric knobs are int/float-coerced with the dataclass defaults as
    fallback; every count is floored at 1 so a zero/negative config
    value can't wedge the lane into an unusable-but-enabled state.
    """
    if not isinstance(raw, dict):
        return NLBrokerConfig()

    defaults = NLBrokerConfig()

    def _int(key: str, fallback: int) -> int:
        try:
            return max(1, int(raw.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback

    try:
        timeout = float(raw.get("llm_timeout_seconds", defaults.llm_timeout_seconds))
    except (TypeError, ValueError):
        timeout = defaults.llm_timeout_seconds

    return NLBrokerConfig(
        enabled=bool(raw.get("enabled", False)),
        model=str(raw.get("model", "") or ""),
        max_subqueries=_int("max_subqueries", defaults.max_subqueries),
        question_max_chars=_int("question_max_chars", defaults.question_max_chars),
        max_answer_chars=_int("max_answer_chars", defaults.max_answer_chars),
        verbatim_run_limit=_int("verbatim_run_limit", defaults.verbatim_run_limit),
        compose_field_max_chars=_int(
            "compose_field_max_chars", defaults.compose_field_max_chars,
        ),
        interpret_max_tokens=_int(
            "interpret_max_tokens", defaults.interpret_max_tokens,
        ),
        compose_max_tokens=_int("compose_max_tokens", defaults.compose_max_tokens),
        llm_timeout_seconds=max(1.0, timeout),
    )


def _build_canonical(data: dict[str, Any]) -> CanonicalConfig:
    """Build ``CanonicalConfig`` + nested per-peer-type rules."""
    peer_perms_raw = data.get("peer_permissions", {}) or {}
    peer_perms: dict[str, dict[str, PeerFieldRules]] = {}
    if isinstance(peer_perms_raw, dict):
        for peer_name, types_raw in peer_perms_raw.items():
            if not isinstance(types_raw, dict):
                continue
            type_map: dict[str, PeerFieldRules] = {}
            for type_name, rules_raw in types_raw.items():
                if not isinstance(rules_raw, dict):
                    continue
                query = _build_peer_query_rules(rules_raw.get("query"))
                type_map[str(type_name)] = PeerFieldRules(
                    fields=list(rules_raw.get("fields", []) or []),
                    bodies=bool(rules_raw.get("bodies", False)),
                    query=query,
                    nl_query=_build_nl_query_rules(
                        rules_raw.get("nl_query"),
                        has_query=query is not None,
                        peer_name=str(peer_name),
                        type_name=str(type_name),
                    ),
                )
            peer_perms[str(peer_name)] = type_map
    return CanonicalConfig(
        owner=bool(data.get("owner", False)),
        audit_log_path=str(
            data.get("audit_log_path", "./data/canonical_audit.jsonl")
        ),
        proposals_path=str(
            data.get("proposals_path", "./data/canonical_proposals.jsonl")
        ),
        peer_permissions=peer_perms,
        nl_broker=_build_nl_broker(data.get("nl_broker")),
    )


def _build_ingest(data: dict[str, Any]) -> IngestConfig:
    """Build the optional ``ingest`` block (defaults = disabled).

    ``max_body_chars`` is int-coerced with the dataclass default as a
    fallback and floored at 1 so a zero/negative config value can't wedge
    the route into an always-413 state. ``types`` keeps only non-empty
    string entries (a per-instance narrowing list).
    """
    if not isinstance(data, dict):
        return IngestConfig()

    try:
        max_body_chars = max(1, int(data.get("max_body_chars", DEFAULT_INGEST_MAX_BODY_CHARS)))
    except (TypeError, ValueError):
        max_body_chars = DEFAULT_INGEST_MAX_BODY_CHARS

    types_raw = data.get("types", []) or []
    types = [str(t) for t in types_raw if isinstance(t, str) and t.strip()]

    return IngestConfig(
        enabled=bool(data.get("enabled", False)),
        max_body_chars=max_body_chars,
        types=types,
    )


def _build_peers(data: dict[str, Any]) -> dict[str, PeerEntry]:
    """Build the ``peers`` dict: peer_name → :class:`PeerEntry`."""
    out: dict[str, PeerEntry] = {}
    for peer_name, entry_raw in (data or {}).items():
        if not isinstance(entry_raw, dict):
            continue
        out[str(peer_name)] = PeerEntry(
            base_url=str(entry_raw.get("base_url", "") or ""),
            token=str(entry_raw.get("token", "") or ""),
        )
    return out


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    The ``auth`` section has a non-trivial nested structure so we
    handle it explicitly; the others map cleanly onto simple nested
    dataclasses.
    """
    if cls is TransportConfig:
        kwargs: dict[str, Any] = {}
        if "server" in data and isinstance(data["server"], dict):
            kwargs["server"] = ServerConfig(**{
                k: v for k, v in data["server"].items()
                if k in {"host", "port"}
            })
        if "scheduler" in data and isinstance(data["scheduler"], dict):
            kwargs["scheduler"] = SchedulerConfig(**{
                k: v for k, v in data["scheduler"].items()
                if k in {"poll_interval_seconds", "stale_reminder_max_minutes"}
            })
        if "auth" in data and isinstance(data["auth"], dict):
            kwargs["auth"] = _build_auth(data["auth"])
        if "state" in data and isinstance(data["state"], dict):
            kwargs["state"] = StateConfig(**{
                k: v for k, v in data["state"].items()
                if k in {"path", "dead_letter_max_age_days"}
            })
        if "canonical" in data and isinstance(data["canonical"], dict):
            kwargs["canonical"] = _build_canonical(data["canonical"])
        if "ingest" in data and isinstance(data["ingest"], dict):
            kwargs["ingest"] = _build_ingest(data["ingest"])
        if "peers" in data and isinstance(data["peers"], dict):
            kwargs["peers"] = _build_peers(data["peers"])
        return TransportConfig(**kwargs)
    # Fallback — not currently reached, but keeps the shape extensible.
    return cls(**data)


def load_config(path: str | Path = "config.yaml") -> TransportConfig:
    """Load and parse config.yaml into a fully-built TransportConfig."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    return load_from_unified(raw)


def load_from_unified(raw: dict[str, Any]) -> TransportConfig:
    """Build TransportConfig from a pre-loaded unified config dict.

    Extracts the ``transport`` section. Returns all-default config when
    the section is absent — callers that need a token-configured
    transport must detect the all-empty ``auth.tokens`` dict and fail
    closed, which the server's auth middleware does on every request.
    """
    raw = _substitute_env(raw)
    tool = raw.get("transport", {}) or {}
    return _build(TransportConfig, tool)

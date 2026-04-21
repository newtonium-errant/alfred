"""Per-peer field-level permissions for canonical record access.

Stage 3.5 D3 implementation — SALEM owns canonical person/org/location
identity records; peers (KAL-LE, STAY-C, RRTS) fetch them via
``GET /canonical/<type>/<name>`` but only receive the frontmatter
fields their peer entry explicitly allows. Bodies are never exposed;
default is deny-all.

The canonical-permissions config lives under
``transport.canonical.peer_permissions`` in ``config.yaml``:

.. code-block:: yaml

    transport:
      canonical:
        owner: true             # SALEM only — peers set this to false
        audit_log_path: "./data/canonical_audit.jsonl"
        peer_permissions:
          kal-le:
            person:
              fields: [name, email, timezone, "preferences.coding"]
              bodies: false
          stay-c:
            person:
              fields: [name]    # PHI firewall: nothing else
              bodies: false

This module holds the pure function — :func:`apply_field_permissions` —
that takes a frontmatter dict plus the peer's field list and returns
the filtered view. The HTTP handler in :mod:`peer_handlers` composes
it with :func:`canonical_audit.append_audit` to produce the final
response + audit trail.
"""

from __future__ import annotations

from typing import Any


def apply_field_permissions(
    peer: str,
    record_type: str,
    frontmatter: dict[str, Any],
    perms: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return ``(filtered_frontmatter, granted_fields, denied_fields)``.

    Args:
        peer: Peer name (e.g. ``"kal-le"``).
        record_type: Record type being requested (e.g. ``"person"``).
        frontmatter: The full frontmatter dict from the canonical record.
        perms: The ``peer_permissions`` dict from
            :class:`CanonicalConfig` — keyed by peer name. Shape:
            ``{peer: {record_type: {"fields": [...], "bodies": bool}}}``.
            ``None`` or empty → default-deny.

    Returns:
        A tuple:
          - ``filtered_frontmatter`` — only the fields the peer may read.
            Never contains the body (callers must not add it back).
          - ``granted_fields`` — list of fields present in the output.
          - ``denied_fields`` — list of fields present in ``frontmatter``
            but withheld by permissions. Useful for the audit log.

    Semantics:
      - **Default deny**: missing peer entry, missing record-type entry,
        or empty fields list all return ``({}, [], all_fields)``. A
        peer with no explicit grant gets nothing.
      - **Dotted-field access**: ``"preferences.coding"`` reads
        ``frontmatter["preferences"]["coding"]``. The output nests the
        value at the same dotted path so the peer sees the shape it
        asked for.
      - **Field intersection**: if the allowlist names a field that
        isn't in the frontmatter, it's simply absent from the result —
        not an error.
      - **Bodies**: always excluded. The ``bodies: false`` flag is a
        belt-and-braces config echo; enforcing it here would require a
        `body` kwarg we don't accept.
    """
    if not frontmatter:
        return {}, [], []

    source_keys = list(frontmatter.keys())

    if not perms:
        return {}, [], source_keys

    peer_rules = perms.get(peer)
    if not isinstance(peer_rules, dict):
        return {}, [], source_keys

    type_rules = peer_rules.get(record_type)
    if type_rules is None:
        return {}, [], source_keys

    # ``type_rules`` may be a dict (raw config shape used by handler
    # tests) or a PeerFieldRules dataclass (shape produced by
    # ``load_from_unified``). Normalise the ``fields`` lookup.
    if isinstance(type_rules, dict):
        allowed = type_rules.get("fields") or []
    else:
        allowed = list(getattr(type_rules, "fields", None) or [])
    if not isinstance(allowed, list) or not allowed:
        return {}, [], source_keys

    out: dict[str, Any] = {}
    granted: list[str] = []
    # We copy the fields intersection-style — anything named by the
    # peer that actually exists in the source frontmatter makes it
    # through. Missing fields are silently dropped; the peer sees the
    # absence, not an error.
    for field_spec in allowed:
        if not isinstance(field_spec, str):
            continue
        if "." in field_spec:
            value = _read_dotted(frontmatter, field_spec)
            if value is _SENTINEL:
                continue
            _write_dotted(out, field_spec, value)
            granted.append(field_spec)
        else:
            if field_spec not in frontmatter:
                continue
            out[field_spec] = frontmatter[field_spec]
            granted.append(field_spec)

    denied = [k for k in source_keys if k not in granted and _not_nested_covered(k, granted)]
    return out, granted, denied


# Sentinel for "field absent" vs. "field present but None".
_SENTINEL: Any = object()


def _read_dotted(source: dict[str, Any], dotted: str) -> Any:
    """Walk ``source`` by dotted key segments. Returns ``_SENTINEL`` if absent."""
    cur: Any = source
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return _SENTINEL
        cur = cur[seg]
    return cur


def _write_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    """Write ``value`` into ``target`` at the dotted path, creating dicts."""
    segs = dotted.split(".")
    cur: dict[str, Any] = target
    for seg in segs[:-1]:
        existing = cur.get(seg)
        if not isinstance(existing, dict):
            existing = {}
            cur[seg] = existing
        cur = existing
    cur[segs[-1]] = value


def _not_nested_covered(top_key: str, granted: list[str]) -> bool:
    """True iff ``top_key`` isn't covered by any dotted grant in ``granted``.

    E.g. if the peer has ``preferences.coding`` granted, the top-level
    ``preferences`` key shouldn't be reported as fully denied — partial
    access was granted. Purely cosmetic for audit-log clarity.
    """
    for g in granted:
        if g == top_key:
            return False
        if g.startswith(top_key + "."):
            return False
    return True

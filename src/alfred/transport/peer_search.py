"""Deterministic filtered-query engine for /peer/search (P1, 2026-06-09).

The inter-instance peer-messaging P1 deterministic lane. A requester
(Hypatia/KAL-LE) sends a filtered query — record type + filter predicates
+ sort + limit — and the holder (Salem) executes it DETERMINISTICALLY
against the disclosure policy: no LLM, no NL interpretation, code-only
glob → parse → predicate → sort → limit. The field gate
(``apply_field_permissions``) decides what comes back per matched record.

This module holds the pure, I/O-light engine: clause validation against
the policy (fail-closed), predicate evaluation (with wikilink-unwrap for
list dimensions like ``participants``), sort, and limit. The aiohttp
handler in :mod:`peer_handlers` wires it to the vault + audit + field gate.

Kept separate from ``peer_handlers`` so the predicate semantics are unit-
testable without spinning up an aiohttp app.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any

from .config import FILTER_LIMIT_CEILING, PeerQueryRules


def json_sanitize(value: Any) -> Any:
    """Recursively coerce a value into a JSON-serializable form.

    ``frontmatter.load`` returns YAML scalars as native Python objects —
    ``date`` / ``datetime`` for ISO-date frontmatter (``date``, ``start``,
    ``end`` on event records), which ``aiohttp.web.json_response`` (stdlib
    ``json``) cannot serialize → ``TypeError: Object of type date is not
    JSON serializable``. This walks dicts / lists and converts every
    ``date`` / ``datetime`` to its ``.isoformat()`` string, leaving
    JSON-native scalars (str/int/float/bool/None) untouched. Any other
    exotic scalar falls back to ``str()`` so a permitted field carrying
    an unexpected YAML type can never 500 the search response.

    Used on the field-gated record dicts before ``json_response``. Pure +
    reusable — the same date-serialization hazard the VERA digest handled
    inline with ``str(created)``, generalized here because ``/peer/search``
    returns arbitrary permitted fields that may include several dates.
    """
    if isinstance(value, dict):
        return {k: json_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_sanitize(v) for v in value]
    # ``datetime`` is a subclass of ``date`` — check it first is harmless
    # since both route to ``.isoformat()``.
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Unknown exotic scalar — never 500 the response over it.
    return str(value)


class FilterPolicyError(Exception):
    """Raised when a filter clause is denied by policy (fail-closed).

    Carries a machine-readable ``code`` + the rejected dimension/operator
    so the handler can map it to the right HTTP error + audit it.
    """

    def __init__(self, code: str, detail: str, *, dim: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.dim = dim


@dataclass
class FilterClause:
    """One validated filter predicate: ``<dim> <op> <value>``."""

    dim: str
    op: str
    value: Any


@dataclass
class SortSpec:
    """Validated sort directive."""

    by: str
    dir: str  # "asc" | "desc"


# Wikilink unwrap: ``[[person/Andrew Newton]]`` → ``Andrew Newton`` and
# also matches the bare ``person/Andrew Newton`` / ``Andrew Newton``
# forms. Decision D (2026-06-09): the ``participants`` dimension is a
# wikilink list, so ``contains "Andrew Newton"`` must match the list
# element ``[[person/Andrew Newton]]``.
_WIKILINK_RE = re.compile(r"^\s*\[\[(?:[^/\]]+/)?([^\]]+)\]\]\s*$")


def _unwrap_wikilink(value: str) -> str:
    """Return the display name inside a ``[[type/Name]]`` wikilink.

    Falls back to the trimmed input when it isn't a wikilink — so a bare
    string list element ("Andrew Newton") matches too. The optional
    ``type/`` prefix is stripped so ``[[person/Andrew Newton]]`` →
    ``Andrew Newton``.
    """
    m = _WIKILINK_RE.match(value)
    if m:
        return m.group(1).strip()
    return value.strip()


def validate_clauses(
    raw_filter: Any, query_rules: PeerQueryRules,
) -> list[FilterClause]:
    """Validate request filter clauses against the policy. Fail-closed.

    Each clause must be ``{"dim": str, "op": str, "value": ...}``. The
    dimension must be in ``query_rules.filter_dims`` AND the operator must
    be in that dimension's allowed ``op`` list — otherwise
    :class:`FilterPolicyError` (``filter_dim_denied``) is raised naming the
    rejected dim. A malformed clause raises ``schema_error``.

    Returns the validated clauses (empty list when ``raw_filter`` is empty
    — a no-predicate search returns all records of the type, capped by
    limit, which is a legitimate "most recent N" query).
    """
    if raw_filter is None:
        return []
    if not isinstance(raw_filter, list):
        raise FilterPolicyError(
            "schema_error", "filter must be a list of clauses",
        )

    out: list[FilterClause] = []
    for clause in raw_filter:
        if not isinstance(clause, dict):
            raise FilterPolicyError(
                "schema_error", "each filter clause must be an object",
            )
        dim = clause.get("dim")
        op = clause.get("op")
        if not isinstance(dim, str) or not dim:
            raise FilterPolicyError(
                "schema_error", "clause.dim must be a non-empty string",
            )
        if not isinstance(op, str) or not op:
            raise FilterPolicyError(
                "schema_error", "clause.op must be a non-empty string",
            )
        dim_rule = query_rules.filter_dims.get(dim)
        if dim_rule is None:
            raise FilterPolicyError(
                "filter_dim_denied",
                f"filtering on dimension '{dim}' is not permitted",
                dim=dim,
            )
        if op not in dim_rule.op:
            raise FilterPolicyError(
                "filter_dim_denied",
                f"operator '{op}' is not permitted on dimension '{dim}'",
                dim=dim,
            )
        out.append(FilterClause(dim=dim, op=op, value=clause.get("value")))
    return out


def validate_sort(raw_sort: Any, query_rules: PeerQueryRules) -> SortSpec | None:
    """Validate the sort directive against the policy allowlist.

    ``raw_sort`` is ``{"by": str, "dir": "asc"|"desc"}`` or absent.
    Returns ``None`` when absent. Raises ``filter_dim_denied`` when the
    sort field isn't in ``query_rules.sort``; ``schema_error`` on a
    malformed shape. ``dir`` defaults to ``"desc"`` (the common
    "most-recent-first" case) and any non-``asc`` value is treated as
    ``desc``.
    """
    if raw_sort is None:
        return None
    if not isinstance(raw_sort, dict):
        raise FilterPolicyError("schema_error", "sort must be an object")
    by = raw_sort.get("by")
    if not isinstance(by, str) or not by:
        raise FilterPolicyError(
            "schema_error", "sort.by must be a non-empty string",
        )
    if by not in query_rules.sort:
        raise FilterPolicyError(
            "filter_dim_denied",
            f"sorting on field '{by}' is not permitted",
            dim=by,
        )
    direction = raw_sort.get("dir", "desc")
    direction = "asc" if direction == "asc" else "desc"
    return SortSpec(by=by, dir=direction)


def resolve_limit(raw_limit: Any, query_rules: PeerQueryRules) -> int:
    """Clamp the request limit to the policy ceiling.

    Absent / invalid → ``query_rules.default_limit``. Present → clamped to
    ``min(requested, query_rules.max_limit, FILTER_LIMIT_CEILING)`` and
    floored at 1.
    """
    if raw_limit is None:
        limit = query_rules.default_limit
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = query_rules.default_limit
    ceiling = min(query_rules.max_limit, FILTER_LIMIT_CEILING)
    return max(1, min(limit, ceiling))


def _as_list(value: Any) -> list[Any]:
    """Coerce a frontmatter value into a list for membership testing."""
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _clause_matches(clause: FilterClause, fm: dict[str, Any]) -> bool:
    """Evaluate one predicate against a record's frontmatter. Deterministic.

    Operator semantics (P1 fixed enum):
      * ``eq``       — scalar equality (string compare, case-sensitive).
      * ``contains`` — substring OR list-membership. For list-shaped
        dimensions (e.g. ``participants``) each element is wikilink-
        unwrapped (``[[person/Andrew Newton]]`` → ``Andrew Newton``) and
        compared to the (also-unwrapped) query value. For a scalar
        dimension it's a plain substring test.
      * ``gte`` / ``lte`` — string comparison (ISO dates sort
        lexicographically, so ``"2026-01-01" <= "2026-05-30"`` works).
      * ``between`` — value must be ``[lo, hi]``; ``lo <= field <= hi``.

    A field absent from the frontmatter never matches (fail-closed at the
    record level — an absent dimension can't satisfy a predicate).
    """
    field_value = fm.get(clause.dim)
    if field_value is None and clause.dim not in fm:
        return False

    if clause.op == "eq":
        return str(field_value) == str(clause.value)

    if clause.op == "contains":
        target = _unwrap_wikilink(str(clause.value))
        items = _as_list(field_value)
        if items and isinstance(field_value, list):
            # List dimension — membership after wikilink-unwrap.
            for item in items:
                if _unwrap_wikilink(str(item)) == target:
                    return True
            return False
        # Scalar dimension — substring test.
        return target in str(field_value)

    if clause.op == "gte":
        return str(field_value) >= str(clause.value)

    if clause.op == "lte":
        return str(field_value) <= str(clause.value)

    if clause.op == "between":
        bounds = clause.value
        if not isinstance(bounds, list) or len(bounds) != 2:
            return False
        lo, hi = str(bounds[0]), str(bounds[1])
        fv = str(field_value)
        return lo <= fv <= hi

    # Unknown operator should never reach here (validate_clauses gates
    # against the policy op list); fail-closed if it somehow does.
    return False


def filter_sort_limit(
    records: list[dict[str, Any]],
    clauses: list[FilterClause],
    sort_spec: SortSpec | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Apply predicates (AND), sort, and limit to a list of frontmatter dicts.

    Pure function — the handler passes parsed frontmatter dicts; this
    returns the surviving subset in final order. All clauses must match
    (logical AND). Sort is stable; ``None`` sort preserves filesystem
    glob order (already sorted by the caller for determinism).
    """
    matched = [
        fm for fm in records
        if all(_clause_matches(c, fm) for c in clauses)
    ]
    if sort_spec is not None:
        matched.sort(
            key=lambda fm: str(fm.get(sort_spec.by, "")),
            reverse=(sort_spec.dir == "desc"),
        )
    return matched[:limit]

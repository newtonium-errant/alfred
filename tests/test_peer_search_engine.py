"""Unit tests for the deterministic peer-search engine (P1, 2026-06-09).

Pure-function coverage of ``transport.peer_search`` — clause validation
(fail-closed against policy), predicate semantics (incl. wikilink-unwrap
for the ``participants`` dimension per Decision D), sort, and limit.
The aiohttp end-to-end path lives in ``test_peer_search_handler.py``.
"""

from __future__ import annotations

import pytest

from alfred.transport.config import (
    FILTER_LIMIT_CEILING,
    FilterDimRule,
    PeerQueryRules,
)
from alfred.transport.peer_search import (
    FilterPolicyError,
    SortSpec,
    _clause_matches,
    _unwrap_wikilink,
    filter_sort_limit,
    json_sanitize,
    resolve_limit,
    validate_clauses,
    validate_sort,
    FilterClause,
)


def _event_query_rules() -> PeerQueryRules:
    """A policy: filter on participants (eq/contains) + date (gte/lte/between)."""
    return PeerQueryRules(
        filter_dims={
            "participants": FilterDimRule(op=["eq", "contains"]),
            "date": FilterDimRule(op=["gte", "lte", "between"]),
        },
        sort=["date"],
        max_limit=10,
        default_limit=5,
    )


# ---------------------------------------------------------------------------
# Wikilink unwrap (Decision D)
# ---------------------------------------------------------------------------


def test_unwrap_wikilink_strips_type_prefix():
    assert _unwrap_wikilink("[[person/Andrew Newton]]") == "Andrew Newton"


def test_unwrap_wikilink_no_type_prefix():
    assert _unwrap_wikilink("[[Andrew Newton]]") == "Andrew Newton"


def test_unwrap_wikilink_bare_string_passthrough():
    assert _unwrap_wikilink("Andrew Newton") == "Andrew Newton"


# ---------------------------------------------------------------------------
# validate_clauses — fail-closed
# ---------------------------------------------------------------------------


def test_validate_clauses_allowed_dim_and_op():
    rules = _event_query_rules()
    clauses = validate_clauses(
        [{"dim": "participants", "op": "contains", "value": "Andrew Newton"}],
        rules,
    )
    assert len(clauses) == 1
    assert clauses[0].dim == "participants"


def test_validate_clauses_denies_unlisted_dim():
    rules = _event_query_rules()
    with pytest.raises(FilterPolicyError) as exc:
        validate_clauses(
            [{"dim": "secret_notes", "op": "contains", "value": "x"}], rules,
        )
    assert exc.value.code == "filter_dim_denied"
    assert exc.value.dim == "secret_notes"


def test_validate_clauses_denies_op_not_in_dim_allowlist():
    rules = _event_query_rules()
    # participants allows eq/contains but NOT gte.
    with pytest.raises(FilterPolicyError) as exc:
        validate_clauses(
            [{"dim": "participants", "op": "gte", "value": "x"}], rules,
        )
    assert exc.value.code == "filter_dim_denied"


def test_validate_clauses_empty_filter_is_allowed():
    """No predicate = 'all records of type, capped by limit' (most-recent N)."""
    assert validate_clauses(None, _event_query_rules()) == []
    assert validate_clauses([], _event_query_rules()) == []


def test_validate_clauses_malformed_raises_schema_error():
    rules = _event_query_rules()
    with pytest.raises(FilterPolicyError) as exc:
        validate_clauses("not a list", rules)
    assert exc.value.code == "schema_error"
    with pytest.raises(FilterPolicyError):
        validate_clauses([{"op": "eq"}], rules)  # missing dim


# ---------------------------------------------------------------------------
# validate_sort + resolve_limit
# ---------------------------------------------------------------------------


def test_validate_sort_allowed_field():
    spec = validate_sort({"by": "date", "dir": "desc"}, _event_query_rules())
    assert spec == SortSpec(by="date", dir="desc")


def test_validate_sort_denies_unlisted_field():
    with pytest.raises(FilterPolicyError) as exc:
        validate_sort({"by": "participants", "dir": "asc"}, _event_query_rules())
    assert exc.value.code == "filter_dim_denied"


def test_validate_sort_defaults_dir_to_desc():
    spec = validate_sort({"by": "date"}, _event_query_rules())
    assert spec.dir == "desc"


def test_validate_sort_none_returns_none():
    assert validate_sort(None, _event_query_rules()) is None


def test_resolve_limit_clamps_to_max():
    assert resolve_limit(99, _event_query_rules()) == 10  # max_limit


def test_resolve_limit_default_when_absent():
    assert resolve_limit(None, _event_query_rules()) == 5  # default_limit


def test_resolve_limit_floors_at_one():
    assert resolve_limit(0, _event_query_rules()) == 1


def test_resolve_limit_respects_global_ceiling():
    rules = PeerQueryRules(max_limit=FILTER_LIMIT_CEILING + 100, default_limit=5)
    # max_limit can't exceed the global ceiling at the resolve layer.
    assert resolve_limit(FILTER_LIMIT_CEILING + 100, rules) == FILTER_LIMIT_CEILING


# ---------------------------------------------------------------------------
# _clause_matches — predicate semantics
# ---------------------------------------------------------------------------


def test_contains_matches_wikilink_list_participants():
    """Decision D: contains 'Andrew Newton' matches [[person/Andrew Newton]]."""
    fm = {"participants": ["[[person/Andrew Newton]]", "[[person/Jamie Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value="Andrew Newton")
    assert _clause_matches(clause, fm) is True


def test_contains_no_match_absent_person():
    fm = {"participants": ["[[person/Jamie Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value="Andrew Newton")
    assert _clause_matches(clause, fm) is False


# ---------------------------------------------------------------------------
# _clause_matches — LIST `contains` whole-token-subset (2026-06-13 fix)
# ---------------------------------------------------------------------------
#
# Root cause (session ae87ec92): the LIST `contains` branch did EXACT
# post-unwrap equality, so Salem's NL broker deriving `participants contains
# "Andrew"` (vault owner stored as [[person/Andrew Newton]] → "Andrew Newton")
# matched nothing → every NL query naming Andrew by single name zeroed out.
# Fix: whole-token-subset — the query value's words must all be WHOLE WORDS
# in the entry's display name (order-independent), still fail-closed on
# fragments / empty / absent-token (anti-fishing & anti-fabrication guard).


def test_contains_single_first_name_token_matches():
    """'Andrew' matches [[person/Andrew Newton]] (the bug this fix closes)."""
    fm = {"participants": ["[[person/Andrew Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value="Andrew")
    assert _clause_matches(clause, fm) is True


def test_contains_single_surname_token_matches():
    """'Newton' matches [[person/Andrew Newton]] — surname token."""
    fm = {"participants": ["[[person/Andrew Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value="Newton")
    assert _clause_matches(clause, fm) is True


def test_contains_other_full_name_first_token_matches():
    """'Stephanie' matches [[person/Stephanie Pearce]]."""
    fm = {"participants": ["[[person/Stephanie Pearce]]"]}
    clause = FilterClause(dim="participants", op="contains", value="Stephanie")
    assert _clause_matches(clause, fm) is True


def test_contains_ambiguous_token_matches_both_forms():
    """'Ben' matches both the bare [[person/Ben]] and [[person/Ben McMillan]]."""
    clause = FilterClause(dim="participants", op="contains", value="Ben")
    assert _clause_matches(clause, {"participants": ["[[person/Ben]]"]}) is True
    assert _clause_matches(
        clause, {"participants": ["[[person/Ben McMillan]]"]},
    ) is True


def test_contains_full_name_still_matches_exact():
    """'Andrew Newton' still matches [[person/Andrew Newton]] (natural subset)."""
    fm = {"participants": ["[[person/Andrew Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value="Andrew Newton")
    assert _clause_matches(clause, fm) is True


# --- Anti-fishing: sub-word fragments MUST NOT match (whole-word boundary) ---


@pytest.mark.parametrize("fragment", ["a", "And", "ndrew"])
def test_contains_subword_fragment_never_matches(fragment):
    """Fragments of a name are not whole words → fail-closed (anti-fishing)."""
    fm = {"participants": ["[[person/Andrew Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value=fragment)
    assert _clause_matches(clause, fm) is False


def test_contains_empty_value_never_matches():
    """Empty value has no tokens → fail-closed (would otherwise over-match)."""
    fm = {"participants": ["[[person/Andrew Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value="")
    assert _clause_matches(clause, fm) is False


# --- Anti-fabrication: a value with a token the entry lacks MUST NOT match ---
# Regression guard for the original hallucinated-surname bug: "Ben Carver"
# names a person who is not "Ben McMillan" — the "carver" token is absent.


def test_contains_absent_token_never_matches_anti_fabrication():
    """'Ben Carver' does NOT match [[person/Ben McMillan]] — 'carver' absent."""
    fm = {"participants": ["[[person/Ben McMillan]]"]}
    clause = FilterClause(dim="participants", op="contains", value="Ben Carver")
    assert _clause_matches(clause, fm) is False


def test_contains_case_insensitive_token_match():
    """Token comparison casefolds both sides ('andrew' matches 'Andrew')."""
    fm = {"participants": ["[[person/Andrew Newton]]"]}
    clause = FilterClause(dim="participants", op="contains", value="andrew")
    assert _clause_matches(clause, fm) is True


def test_contains_scalar_branch_unchanged_substring():
    """SCALAR contains is unchanged — substring (powers `name` title search)."""
    fm = {"name": "rTMS appointment 2026-06-20"}
    assert _clause_matches(
        FilterClause("name", "contains", "rTMS"), fm,
    ) is True
    # Substring (not whole-word) still applies on the scalar branch.
    assert _clause_matches(
        FilterClause("name", "contains", "appoint"), fm,
    ) is True


def test_contains_matches_wikilink_value_form():
    """The query value may itself be a wikilink — unwrapped on both sides."""
    fm = {"participants": ["[[person/Andrew Newton]]"]}
    clause = FilterClause(
        dim="participants", op="contains", value="[[person/Andrew Newton]]",
    )
    assert _clause_matches(clause, fm) is True


def test_contains_scalar_substring():
    fm = {"title": "Quarterly planning with Andrew"}
    clause = FilterClause(dim="title", op="contains", value="Andrew")
    assert _clause_matches(clause, fm) is True


def test_eq_scalar():
    fm = {"status": "confirmed"}
    assert _clause_matches(FilterClause("status", "eq", "confirmed"), fm) is True
    assert _clause_matches(FilterClause("status", "eq", "cancelled"), fm) is False


def test_gte_lte_iso_date():
    fm = {"date": "2026-05-30"}
    assert _clause_matches(FilterClause("date", "gte", "2026-01-01"), fm) is True
    assert _clause_matches(FilterClause("date", "lte", "2026-01-01"), fm) is False


def test_between_iso_date():
    fm = {"date": "2026-05-30"}
    inside = FilterClause("date", "between", ["2026-01-01", "2026-12-31"])
    outside = FilterClause("date", "between", ["2025-01-01", "2025-12-31"])
    assert _clause_matches(inside, fm) is True
    assert _clause_matches(outside, fm) is False


def test_absent_field_never_matches():
    """Fail-closed at the record level — an absent dim can't satisfy."""
    fm = {"title": "x"}
    assert _clause_matches(FilterClause("date", "gte", "2026-01-01"), fm) is False


# ---------------------------------------------------------------------------
# filter_sort_limit — AND + sort + limit
# ---------------------------------------------------------------------------


def test_filter_sort_limit_and_combines_clauses():
    records = [
        {"name": "A", "participants": ["[[person/Andrew Newton]]"], "date": "2026-05-30"},
        {"name": "B", "participants": ["[[person/Andrew Newton]]"], "date": "2025-01-01"},
        {"name": "C", "participants": ["[[person/Jamie Newton]]"], "date": "2026-06-01"},
    ]
    clauses = [
        FilterClause("participants", "contains", "Andrew Newton"),
        FilterClause("date", "gte", "2026-01-01"),
    ]
    out = filter_sort_limit(records, clauses, SortSpec("date", "desc"), 10)
    # Only A matches both predicates (B is too old, C is Jamie).
    assert [r["name"] for r in out] == ["A"]


def test_filter_sort_limit_sort_desc_then_limit():
    records = [
        {"name": "old", "date": "2026-01-01"},
        {"name": "new", "date": "2026-06-01"},
        {"name": "mid", "date": "2026-03-01"},
    ]
    out = filter_sort_limit(records, [], SortSpec("date", "desc"), 1)
    # Newest-first, limit 1 → the single most-recent record.
    assert [r["name"] for r in out] == ["new"]


def test_filter_sort_limit_no_sort_preserves_order():
    records = [{"name": "x"}, {"name": "y"}, {"name": "z"}]
    out = filter_sort_limit(records, [], None, 2)
    assert [r["name"] for r in out] == ["x", "y"]


# ---------------------------------------------------------------------------
# json_sanitize — date/datetime coercion (prod-bug regression pin)
# ---------------------------------------------------------------------------
#
# Bug: frontmatter.load returns ISO-date frontmatter as native date/
# datetime objects, which json_response (stdlib json) cannot serialize →
# 500 on every event /peer/search. json_sanitize coerces them to
# isoformat strings before the response.


def test_json_sanitize_coerces_date():
    import datetime as dt

    out = json_sanitize({"date": dt.date(2026, 5, 30)})
    assert out == {"date": "2026-05-30"}
    # The result must be JSON-serializable (the original failure mode).
    import json
    json.dumps(out)  # must not raise


def test_json_sanitize_coerces_datetime():
    import datetime as dt

    out = json_sanitize({"start": dt.datetime(2026, 5, 30, 14, 0, 0)})
    assert out["start"].startswith("2026-05-30T14:00:00")


def test_json_sanitize_recurses_lists_and_nested():
    import datetime as dt

    out = json_sanitize({
        "participants": ["[[person/Andrew Newton]]"],
        "dates": [dt.date(2026, 1, 1), dt.date(2026, 2, 2)],
        "nested": {"when": dt.date(2026, 3, 3)},
    })
    assert out["dates"] == ["2026-01-01", "2026-02-02"]
    assert out["nested"]["when"] == "2026-03-03"
    assert out["participants"] == ["[[person/Andrew Newton]]"]


def test_json_sanitize_passes_native_scalars_through():
    out = json_sanitize({"s": "x", "i": 3, "f": 1.5, "b": True, "n": None})
    assert out == {"s": "x", "i": 3, "f": 1.5, "b": True, "n": None}


def test_json_sanitize_whole_result_is_json_serializable():
    """End-to-end: a realistic event field dict round-trips through json."""
    import datetime as dt
    import json

    record = {
        "name": "Coffee with Andrew",
        "title": "Coffee chat",
        "date": dt.date(2026, 5, 30),
        "participants": ["[[person/Andrew Newton]]"],
    }
    json.dumps(json_sanitize(record))  # must not raise

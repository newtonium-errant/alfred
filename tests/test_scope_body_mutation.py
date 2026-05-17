"""Per-instance × per-type matrix tests for body_insert_at + body_replace.

These mutation tools land in ``vault_edit`` as new kwargs (c2 of this
arc). The scope rules (this commit, c1) gate them per-instance and
per-type. The matrix is the principal artifact — every cell below
maps to one expectation:

| Caller        | append    | insert_at                        | replace                       |
|---------------|-----------|----------------------------------|-------------------------------|
| hypatia       | universal | note,concept,document,           | same set                      |
|               |           | template,fiction-* (per spec)    |                               |
| talker(Salem) | universal | note,task,event(no-gcal-id)      | same — but refuses if         |
|               |           |                                  | event has gcal_event_id       |
| kalle         | universal | note,principle,pattern           | same set                      |
| janitor       | universal | * (stub-flesh-out workflows)     | DENIED (autofix-loop risk)    |
| janitor_enrich| universal | DENIED                           | DENIED                        |
| distiller     | universal | DENIED                           | DENIED                        |
| curator       | universal | DENIED                           | DENIED                        |
| surveyor      | universal | DENIED                           | DENIED                        |
| instructor    | universal | * (operator-driven, trusted)     | * (operator-driven, trusted)  |

Plus the universally-denied set: session, conversation, capture,
run, input, assumption, decision, constraint, contradiction,
synthesis. These refuse body_insert_at AND body_replace under EVERY
scope, even ones whose allowlist would otherwise pass.

Per ``CLAUDE.md`` "Validation Gate Ordering": the universal-deny
fires BEFORE the per-instance allowlist check. So an instructor
calling body_insert_at on a ``session`` record gets the universal-
deny error, not the wildcard pass.
"""

from __future__ import annotations

import pytest

from alfred.vault.scope import (
    SCOPE_RULES,
    ScopeError,
    check_scope,
    _BODY_MUTATE_DENIED_TYPES,
)


# ---------------------------------------------------------------------------
# Universally-denied types — refuse under EVERY scope
# ---------------------------------------------------------------------------


_DENIED_TYPES_SAMPLE = [
    "session", "conversation", "capture", "run", "input",
    "assumption", "decision", "constraint", "contradiction", "synthesis",
]


@pytest.mark.parametrize("scope", [
    "talker", "kalle", "hypatia", "instructor", "janitor",
    "curator", "distiller", "surveyor", "janitor_enrich",
])
@pytest.mark.parametrize("denied_type", _DENIED_TYPES_SAMPLE)
def test_body_insert_at_denied_universally_for_atomic_types(
    scope, denied_type,
):
    """body_insert_at refuses for every (scope × denied_type) cell —
    the universal-deny set takes precedence over per-instance allowlists.
    """
    with pytest.raises(ScopeError, match="universally denied"):
        check_scope(scope, "body_insert_at", record_type=denied_type)


@pytest.mark.parametrize("scope", [
    "talker", "kalle", "hypatia", "instructor", "janitor",
    "curator", "distiller", "surveyor", "janitor_enrich",
])
@pytest.mark.parametrize("denied_type", _DENIED_TYPES_SAMPLE)
def test_body_replace_denied_universally_for_atomic_types(
    scope, denied_type,
):
    with pytest.raises(ScopeError, match="universally denied"):
        check_scope(scope, "body_replace", record_type=denied_type)


def test_universal_deny_set_pinned_to_spec():
    """Pin: the universal-deny set matches the spec's matrix verbatim.
    Adding/removing a type from this set is a deliberate decision; the
    pin catches accidental drift."""
    expected = {
        "session", "conversation", "capture", "run", "input",
        "assumption", "decision", "constraint", "contradiction", "synthesis",
    }
    assert _BODY_MUTATE_DENIED_TYPES == expected


# ---------------------------------------------------------------------------
# Hypatia — note/concept/document/template/fiction-*
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed_type", [
    "note", "concept", "document", "template",
    "fiction-continuity", "fiction-story", "fiction-structure",
    "fiction-world", "fiction-voice", "fiction-character",
    # 2026-05-06 — practice-session allows anchored mid-doc updates
    # (operator adds an observation against a specific exercise heading
    # mid-session). See ``allow_body_replace`` test below for the
    # asymmetry — replace is DENIED to preserve practice-session as a
    # historical record.
    "practice-session",
    # 2026-05-17 — article co-write scope extension. Hypatia is a true
    # co-writer on articles: operator-on-request mid-doc inserts
    # ("add a paragraph between graf 3 and 4 of Part 2") allowed.
    "article",
])
def test_hypatia_body_insert_at_allows_per_spec_types(allowed_type):
    check_scope("hypatia", "body_insert_at", record_type=allowed_type)


@pytest.mark.parametrize("allowed_type", [
    "note", "concept", "document", "template",
    "fiction-continuity",
    # ``practice-session`` deliberately OMITTED here — the matrix
    # asymmetry is: insert_at allowed, replace DENIED. See the
    # body-mutation matrix table comment in scope.py for the rationale.
    #
    # 2026-05-17 — article co-write scope extension. Full-Part rewrites
    # on operator request ("rewrite Part 3, keep the rest") allowed.
    "article",
])
def test_hypatia_body_replace_allows_per_spec_types(allowed_type):
    check_scope("hypatia", "body_replace", record_type=allowed_type)


def test_hypatia_body_replace_denies_practice_session() -> None:
    """Practice-session is OMITTED from Hypatia's body_replace
    allowlist (added 2026-05-06). The matrix asymmetry is intentional:
    insert_at is allowed (operator adds an observation against a
    specific exercise heading); replace would erase the in-session
    progression the record is meant to capture."""
    with pytest.raises(ScopeError, match="may not 'body_replace'"):
        check_scope(
            "hypatia", "body_replace", record_type="practice-session",
        )


def test_hypatia_body_insert_at_denies_outside_spec_set():
    """``project`` is not in Hypatia's allowlist — denied even though
    it's not in the universal-deny set."""
    with pytest.raises(ScopeError, match="may not 'body_insert_at'"):
        check_scope("hypatia", "body_insert_at", record_type="project")


def test_hypatia_body_replace_denies_outside_spec_set():
    with pytest.raises(ScopeError, match="may not 'body_replace'"):
        check_scope("hypatia", "body_replace", record_type="project")


# ---------------------------------------------------------------------------
# Salem (talker) — note/task/event with gcal carve-out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed_type", ["note", "task", "event"])
def test_talker_body_insert_at_allows_per_spec_types(allowed_type):
    check_scope("talker", "body_insert_at", record_type=allowed_type)


def test_talker_body_replace_allows_event_without_gcal_event_id():
    """An event with NO synced GCal mirror (no gcal_event_id) IS
    eligible for body_replace under the talker allowlist."""
    check_scope(
        "talker", "body_replace", record_type="event",
        existing_frontmatter={
            "type": "event", "name": "Local-only event",
        },
    )


def test_talker_body_replace_refuses_event_with_gcal_event_id():
    """The headline carve-out: body_replace on a Salem event with a
    synced GCal mirror refuses with operator-actionable guidance."""
    with pytest.raises(ScopeError, match="synced GCal mirror"):
        check_scope(
            "talker", "body_replace", record_type="event",
            existing_frontmatter={
                "type": "event",
                "name": "Has mirror",
                "gcal_event_id": "alfred-cal-event-123",
            },
        )


def test_talker_body_replace_carve_out_does_not_apply_to_body_insert_at():
    """body_insert_at on an event WITH gcal_event_id IS allowed —
    inserting a new section doesn't risk dropping the gcal id from
    frontmatter, only full rewrites do."""
    check_scope(
        "talker", "body_insert_at", record_type="event",
        existing_frontmatter={
            "type": "event", "gcal_event_id": "alfred-cal-event-456",
        },
    )


def test_talker_body_replace_event_no_existing_frontmatter_passes():
    """Defensive: caller didn't pass existing_frontmatter (e.g. legacy
    test fixture or a path where the file hasn't been read yet). The
    gate must NOT crash on the missing kwarg — it just skips the
    gcal carve-out check, which is acceptable because the production
    caller (vault_edit) DOES pass existing_frontmatter."""
    check_scope("talker", "body_replace", record_type="event")


# ---------------------------------------------------------------------------
# KAL-LE — note/principle/pattern (decisions universally denied)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed_type", ["note", "principle", "pattern"])
def test_kalle_body_insert_at_allows_per_spec_types(allowed_type):
    check_scope("kalle", "body_insert_at", record_type=allowed_type)


@pytest.mark.parametrize("allowed_type", ["note", "principle", "pattern"])
def test_kalle_body_replace_allows_per_spec_types(allowed_type):
    check_scope("kalle", "body_replace", record_type=allowed_type)


def test_kalle_body_insert_at_denies_outside_spec_set():
    with pytest.raises(ScopeError, match="may not 'body_insert_at'"):
        check_scope("kalle", "body_insert_at", record_type="org")


# ---------------------------------------------------------------------------
# Janitor — body_insert_at wildcard (allow_body_writes still gates),
# body_replace DENIED
# ---------------------------------------------------------------------------


def test_janitor_body_insert_at_wildcard_allowlist():
    """Janitor's allow_body_insert_at is ``"*": True`` (wildcard) so
    a non-denied type passes the per-type gate. The
    ``allow_body_writes: False`` gate at the scope-level body-write
    check is a SEPARATE rule (catches body_write=True from
    body_append) and isn't tested here — that's the existing
    behaviour."""
    check_scope("janitor", "body_insert_at", record_type="note")
    check_scope("janitor", "body_insert_at", record_type="task")
    # Universal-deny still wins.
    with pytest.raises(ScopeError, match="universally denied"):
        check_scope("janitor", "body_insert_at", record_type="session")


def test_janitor_body_replace_denied_for_all_types():
    """body_replace is universally DENIED for janitor (autofix-loop
    risk per spec). Even an allowed type like ``note`` refuses.

    Two gates can fire here in priority order:
      1. ``allow_body_writes: False`` (existing rule) — fires FIRST
         in ``check_scope`` because body-write rejection precedes the
         operation-permission check. Message: "may not write record
         body content".
      2. ``allow_body_replace: {}`` (new rule, this commit) — would
         fire IF allow_body_writes were True. Message: "no allowlist".

    Today (2026-05-04) gate #1 is the operative one. The forgiving
    ``match="janitor"`` substring matches both gate paths so the test
    survives a future widen of allow_body_writes (the natural
    extension path documented in c1's commit body) without needing
    a wording chase. The test_janitor_new_allowlist_gate_message_
    when_body_writes_widened test below pins the new gate's wording
    via monkeypatch for that future migration."""
    with pytest.raises(ScopeError, match="janitor"):
        check_scope("janitor", "body_replace", record_type="note")


def test_janitor_new_allowlist_gate_message_when_body_writes_widened(
    monkeypatch,
):
    """Belt-and-suspenders: the NEW allow_body_replace allowlist gate
    fires (with its "no allowlist" message) when allow_body_writes is
    flipped to True. Pinned now so a future widen of allow_body_writes
    (the natural extension path documented in c1's commit body) doesn't
    silently lose the body_replace deny — the new gate must keep firing
    independently of the body_write gate.

    Monkeypatch the janitor scope's ``allow_body_writes`` to True so
    the existing body-write gate stops firing FIRST; then assert the
    new allowlist gate's specific "no allowlist" message takes over."""
    from alfred.vault.scope import SCOPE_RULES
    monkeypatch.setitem(
        SCOPE_RULES["janitor"], "allow_body_writes", True,
    )
    with pytest.raises(ScopeError, match="no allowlist"):
        check_scope("janitor", "body_replace", record_type="note")


# ---------------------------------------------------------------------------
# Distiller / Curator / Surveyor / janitor_enrich — fully denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", [
    "distiller", "curator", "surveyor", "janitor_enrich",
])
def test_fully_denied_scopes_refuse_body_insert_at(scope):
    """Per the spec matrix: distiller / curator / surveyor /
    janitor_enrich are DENIED for both body mutation tools.
    ``note`` (a non-denied type) still refuses because the
    per-instance allowlist is empty."""
    with pytest.raises(ScopeError, match="no allowlist"):
        check_scope(scope, "body_insert_at", record_type="note")


@pytest.mark.parametrize("scope", [
    "distiller", "curator", "surveyor", "janitor_enrich",
])
def test_fully_denied_scopes_refuse_body_replace(scope):
    with pytest.raises(ScopeError, match="no allowlist"):
        check_scope(scope, "body_replace", record_type="note")


# ---------------------------------------------------------------------------
# Instructor — wildcard allowlist (operator-driven, trusted)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed_type", [
    "note", "task", "project", "person", "concept", "principle",
])
def test_instructor_body_insert_at_wildcard_allows_any_non_denied_type(
    allowed_type,
):
    check_scope("instructor", "body_insert_at", record_type=allowed_type)


@pytest.mark.parametrize("allowed_type", [
    "note", "task", "project", "person", "concept", "principle",
])
def test_instructor_body_replace_wildcard_allows_any_non_denied_type(
    allowed_type,
):
    check_scope("instructor", "body_replace", record_type=allowed_type)


def test_instructor_universal_deny_still_overrides_wildcard():
    """Instructor wildcard does NOT bypass the universal-deny set —
    even operator-driven mutations of an atomic learning record
    refuse at the universal gate."""
    with pytest.raises(ScopeError, match="universally denied"):
        check_scope("instructor", "body_insert_at", record_type="synthesis")
    with pytest.raises(ScopeError, match="universally denied"):
        check_scope("instructor", "body_replace", record_type="synthesis")


# ---------------------------------------------------------------------------
# Existing scope rules unchanged — regression guard
# ---------------------------------------------------------------------------


def test_existing_create_rules_unchanged_for_talker():
    """Regression: adding body_insert_at / body_replace gates does
    NOT alter the existing ``create`` permission shape for any scope.
    Pre-existing test ``test_talker_create_allows_whitelisted_type``
    in test_scope.py covers the positive case; this guards the
    rule-shape didn't drift."""
    rules = SCOPE_RULES["talker"]
    assert rules["create"] == "talker_types_only"
    assert rules["edit"] is True
    assert rules["allow_body_writes"] is True
    # New keys present without breaking old ones.
    assert "allow_body_insert_at" in rules
    assert "allow_body_replace" in rules


def test_existing_create_rules_unchanged_for_distiller():
    rules = SCOPE_RULES["distiller"]
    assert rules["create"] == "learn_types_only"
    assert rules["edit"] == "distiller_fields_only"
    assert rules["allow_body_writes"] is True
    # Per-spec: distiller is fully denied for body mutation tools.
    assert rules["allow_body_insert_at"] == {}
    assert rules["allow_body_replace"] == {}


def test_existing_create_rules_unchanged_for_janitor():
    rules = SCOPE_RULES["janitor"]
    assert rules["create"] == "triage_tasks_only"
    assert rules["edit"] == "field_allowlist"
    assert rules["allow_body_writes"] is False
    # Janitor: insert wildcard, replace denied.
    assert rules["allow_body_insert_at"] == {"*": True}
    assert rules["allow_body_replace"] == {}


# ---------------------------------------------------------------------------
# Unknown scope still raises (regression — same path as pre-c1)
# ---------------------------------------------------------------------------


def test_unknown_scope_still_raises():
    with pytest.raises(ScopeError, match="Unknown scope"):
        check_scope("nonexistent_scope", "body_insert_at", record_type="note")

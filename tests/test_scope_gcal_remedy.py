"""The GCal body_replace refusal names a remedy the caller CAN perform (#80).

The old message said "first vault_delete the event, then vault_create the
replacement". Only two scopes permit ``body_replace`` on ``event`` at all —
``talker`` and ``instructor`` — and BOTH carry ``delete: False``. So the
refusal named the one operation its reader was structurally forbidden from
performing, every single time it fired, and the agent-facing SKILLs carried
warn-clauses telling agents to disregard it.

A refusal that names an impossible remedy is worse than one that names none:
it sends a capable agent into a second failure to discover what the first
should have said.
"""

from __future__ import annotations

import pytest

from alfred.vault.scope import SCOPE_RULES, ScopeError, check_scope

SYNCED_EVENT = {"gcal_event_id": "abc123"}


def replace_event(scope: str):
    return check_scope(
        scope, "body_replace", rel_path="event/Standup.md",
        record_type="event", body_write=True, body_op="body_replace",
        existing_frontmatter=SYNCED_EVENT,
    )


# ---------------------------------------------------------------------------
# The premise — asserted, not assumed
# ---------------------------------------------------------------------------


def test_every_scope_that_can_reach_this_rule_is_delete_less():
    """THE PREMISE OF THE BUG, pinned. If a future scope gains both
    event-body_replace AND delete, this test fails and the remedy branch
    below needs re-reading rather than silently going stale."""
    reachable = {
        s for s, r in SCOPE_RULES.items()
        if isinstance(r.get("allow_body_replace"), dict)
        and (r["allow_body_replace"].get("event")
             or r["allow_body_replace"].get("*"))
    }
    assert reachable == {"talker", "instructor"}
    for scope in reachable:
        assert SCOPE_RULES[scope]["delete"] is False, (
            f"{scope} now holds delete — re-read _gcal_replace_remedy"
        )


# ---------------------------------------------------------------------------
# The refusal still refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["talker", "instructor"])
def test_a_synced_event_still_refuses_body_replace(scope: str):
    """The fix is to the WORDING; the gate must not have loosened."""
    with pytest.raises(ScopeError):
        replace_event(scope)


@pytest.mark.parametrize("scope", ["talker", "instructor"])
def test_an_unsynced_event_is_still_allowed(scope: str):
    check_scope(
        scope, "body_replace", rel_path="event/Standup.md",
        record_type="event", body_write=True, body_op="body_replace",
        existing_frontmatter={},
    )


# ---------------------------------------------------------------------------
# The remedy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["talker", "instructor"])
def test_a_delete_less_scope_is_NEVER_told_to_delete(scope: str):
    with pytest.raises(ScopeError) as exc:
        replace_event(scope)
    message = str(exc.value)
    assert "vault_delete" not in message
    assert "cannot delete records" in message


@pytest.mark.parametrize("scope", ["talker", "instructor"])
def test_a_delete_less_scope_is_pointed_at_something_it_CAN_do(scope: str):
    """body_append is universally available where body writes are on, and
    both of these scopes allow body_insert_at on ``event`` — so the message
    names an operation that will actually succeed."""
    with pytest.raises(ScopeError) as exc:
        replace_event(scope)
    message = str(exc.value)
    assert "body_append" in message
    assert "body_insert_at" in message
    assert "ask the operator to cancel it" in message


def test_the_named_alternative_is_genuinely_permitted_for_that_scope():
    """The message must not become its own dead advice. Whatever it names,
    the scope has to actually allow — checked against the gate, not the
    prose."""
    check_scope(
        "talker", "body_insert_at", rel_path="event/Standup.md",
        record_type="event", body_write=True, body_op="body_insert_at",
        existing_frontmatter=SYNCED_EVENT,
    )


def test_insert_at_is_omitted_when_the_scope_lacks_it(monkeypatch):
    """The remedy is COMPUTED from the scope's own allowlist, not a fixed
    sentence — a scope without event-insert_at must not be told to use it."""
    from alfred.vault import scope as scope_mod

    narrowed = dict(SCOPE_RULES["talker"])
    narrowed["allow_body_insert_at"] = {}
    monkeypatch.setitem(scope_mod.SCOPE_RULES, "talker", narrowed)

    with pytest.raises(ScopeError) as exc:
        replace_event("talker")
    message = str(exc.value)
    assert "body_append" in message
    assert "body_insert_at" not in message


def test_a_delete_CAPABLE_scope_still_gets_the_delete_path(monkeypatch):
    """The original advice was not wrong in general — only wrong for every
    caller that could receive it. A scope holding delete keeps it."""
    from alfred.vault import scope as scope_mod

    capable = dict(SCOPE_RULES["talker"])
    capable["delete"] = True
    monkeypatch.setitem(scope_mod.SCOPE_RULES, "talker", capable)

    with pytest.raises(ScopeError) as exc:
        replace_event("talker")
    message = str(exc.value)
    assert "vault_delete" in message
    assert "cannot delete records" not in message


@pytest.mark.parametrize("scope", ["talker", "instructor"])
def test_the_refusal_still_explains_WHY(scope: str):
    """The remedy replaced the next-step sentence, not the diagnosis."""
    with pytest.raises(ScopeError) as exc:
        replace_event(scope)
    message = str(exc.value)
    assert "gcal_event_id" in message
    assert "sync state" in message
    assert scope in message


# ---------------------------------------------------------------------------
# The sibling copies — one wording, four places
# ---------------------------------------------------------------------------


def test_the_talker_tool_description_does_not_advise_deleting():
    """The tool schema is what the model reads BEFORE it acts; leaving the
    dead advice there would keep producing the failed attempt the scope
    refusal then has to correct."""
    from alfred.telegram.conversation import tools_for_set

    tools = tools_for_set("talker")
    edit = next(t for t in tools if t.get("name") == "vault_edit")
    description = edit["input_schema"]["properties"]["body_replace"]["description"]
    assert "vault_delete" not in description
    assert "body_append" in description
    assert "cannot delete records" in description


def test_no_production_string_tells_a_delete_less_caller_to_vault_delete():
    """SWEEP. The same sentence had been copied into four places; a fix to
    one is a fix to none."""
    import inspect
    from pathlib import Path

    from alfred.vault import ops as ops_mod
    from alfred.vault import scope as scope_mod

    scope_src = Path(scope_mod.__file__).read_text(encoding="utf-8")
    # The only surviving 'vault_delete the event' text must be inside the
    # delete-CAPABLE branch of the remedy helper.
    remedy_src = inspect.getsource(scope_mod._gcal_replace_remedy)
    occurrences = scope_src.count("first vault_delete the event")
    assert occurrences == remedy_src.count("first vault_delete the event")
    assert occurrences == 1

    ops_doc = inspect.getdoc(ops_mod.vault_edit) or ""
    assert "operator must vault_delete first" not in ops_doc

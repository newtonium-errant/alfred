"""``check_scope``'s fail-closed tail — the direction an unknown gate fails.

Until 2026-08-20 the permission chain in ``check_scope`` ended with the last
handler's ``return``. An unrecognised permission string therefore fell off the
end and returned ``None`` — which the caller reads as ALLOW. A scope whose
``edit`` named a misspelled gate, or whose handler was deleted in a refactor
while its ``SCOPE_RULES`` entry survived, permitted EVERY field on EVERY type,
silently: no raise, no log, and the scope's own tests kept passing because they
assert the allowed direction.

This file pins both halves:

  * the CENSUS — every permission string declared across ``SCOPE_RULES`` has a
    handler. This is the measurement that made the tail safe to add, and it is
    the pin that keeps it safe: it reds when someone adds a gate name without
    a branch, which is exactly when the tail would start firing in production.
  * the DIRECTION — an unhandled permission string raises rather than admits.

The census is the more valuable of the two. The tail is a backstop; the census
is what stops anyone ever needing it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from alfred.vault.scope import SCOPE_RULES, ScopeError, check_scope

_SCOPE_SRC = pathlib.Path(__file__).resolve().parents[1] / "src/alfred/vault/scope.py"


def _declared_permission_strings() -> set[str]:
    """Every permission string any scope declares, as VALUES in SCOPE_RULES.

    Bound to the structure, not to text shaped like it: this reads the live
    dict rather than grepping the file for quoted snake_case.
    """
    out: set[str] = set()
    for rules in SCOPE_RULES.values():
        for value in rules.values():
            if isinstance(value, str):
                out.add(value)
    return out


def _handled_permission_strings() -> set[str]:
    return set(re.findall(r'permission == "([a-z_]+)"', _SCOPE_SRC.read_text()))


def test_the_census_instrument_is_not_vacuous() -> None:
    """POSITIVE CONTROL — both sides really parsed and really have content.

    Both assertions below hold trivially against two empty sets read from a
    path that silently moved. This is the check that would have caught the
    inert-control failures two agents shipped this week.
    """
    declared = _declared_permission_strings()
    handled = _handled_permission_strings()
    assert len(declared) >= 20, declared
    assert len(handled) >= 20, handled
    # A known-good member on each side, so a filter that silently dropped
    # everything but one row cannot pass.
    assert "field_allowlist" in declared
    assert "field_allowlist" in handled


def test_every_declared_permission_string_has_a_handler() -> None:
    """The census. Reds when a gate name is declared with no branch.

    That is precisely the state in which the fail-closed tail would start
    refusing real writes in production — so this pin, not the tail, is the
    one that keeps the lights on.
    """
    unhandled = _declared_permission_strings() - _handled_permission_strings()
    assert unhandled == set(), (
        f"these permission strings are declared in SCOPE_RULES but have no "
        f"'permission == ...' branch in check_scope, so every edit under them "
        f"now fails closed: {sorted(unhandled)}"
    )


def test_no_handler_is_dead() -> None:
    """The other direction — a branch no scope declares is dead code.

    Not a failure mode on its own, but a handler that lost its last declaring
    scope is a gate nobody is testing against a real caller.
    """
    dead = _handled_permission_strings() - _declared_permission_strings()
    assert dead == set(), f"handlers no scope declares: {sorted(dead)}"


def test_an_unhandled_permission_string_refuses_rather_than_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE DIRECTION PIN.

    Synthesises a scope whose ``edit`` names a gate that does not exist —
    the typo / deleted-handler shape — and asserts it REFUSES. Before the
    tail, this call returned None and the write proceeded unrestricted.

    Uses monkeypatch so the probe scope never outlives the test.
    """
    probe = dict(SCOPE_RULES["moc_apply"])
    probe["edit"] = "gate_that_does_not_exist"
    monkeypatch.setitem(SCOPE_RULES, "zz_fail_closed_probe", probe)

    with pytest.raises(ScopeError) as exc:
        check_scope(
            "zz_fail_closed_probe",
            "edit",
            record_type="note",
            fields=["anything_at_all"],
            body_write=True,
        )
    # Assert WHY it refused. Without this the pin passes against a build that
    # refuses for any unrelated reason (unknown scope, type gate, field gate).
    message = str(exc.value)
    assert "no handler in check_scope" in message
    assert "gate_that_does_not_exist" in message
    assert "failing closed" in message


def test_the_probe_scope_is_admitted_when_its_gate_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSITIVE CONTROL for the direction pin.

    The SAME probe scope with a REAL gate name is accepted, proving the
    refusal above is caused by the missing handler and not by the probe's
    shape, its synthetic name, or the monkeypatch itself.
    """
    probe = dict(SCOPE_RULES["moc_apply"])
    probe["edit"] = "moc_apply_only"
    monkeypatch.setitem(SCOPE_RULES, "zz_fail_closed_probe", probe)

    check_scope(
        "zz_fail_closed_probe",
        "edit",
        record_type="zettel",
        fields=["mocs"],
        body_write=False,
    )

"""The shared vault_edit description's delete claim stays true (#80 follow-up).

#80 put a FLAT CAPABILITY CLAIM — "You cannot delete records" — into the
``body_replace`` tool description. That description is SHARED: every tool
set built from ``VAULT_TOOLS_BY_SET`` serves the same ``vault_edit`` schema
object, and the model reads it before acting.

A flat claim on a shared surface is only as true as its most-privileged
reader. Today every set is delete-less and the sentence is honest; the day
someone adds a delete tool to ANY set, it silently becomes a lie told to
every other set as well — and a lie in a tool description is worse than one
in a docstring, because the model acts on it.

So the claim and the capability table are pinned TOGETHER. Adding a delete
tool anywhere reddens this file with an instruction to either drop the tool
or reword the description.

ASSERTED ON THE ASSEMBLED SCHEMA, never on source text. The sentence is
split across two adjacent string literals in the source (``"...You cannot
delete "`` / ``"records. ..."``), so a grep for the phrase finds nothing —
which is exactly how a source-level pin would pass while the shipped
description said something else.
"""

from __future__ import annotations

import pytest

from alfred.telegram.conversation import VAULT_TOOLS_BY_SET, tools_for_set

#: The claim under test, as the model receives it.
DELETE_CLAIM = "You cannot delete records."

#: Substrings that mark a tool as delete-capable. Deliberately broader than
#: the single name ``vault_delete``: a future ``vault_remove`` /
#: ``record_delete`` / ``purge_record`` would falsify the claim just as
#: thoroughly, and a pin that only knew one spelling would miss it.
_DELETE_MARKERS = ("delete", "remove", "purge", "destroy")

#: Every surface that serves the shared description: the registered sets
#: plus the fallback an unknown set name resolves to, so an instance with a
#: typo'd ``tool_set`` still reads this text.
#:
#: NAMING TRAP, worth stating because it misleads on sight: the fallback is
#: ``TALKER_VAULT_TOOLS``, which is the 4-tool BASE vault set — NOT what the
#: ``"talker"`` registry key serves. That key maps to ``SALEM_VAULT_TOOLS``
#: (10 tools, base + routine/tier). So "the talker set" and
#: ``TALKER_VAULT_TOOLS`` are different objects, and the fallback is a real
#: fourth surface rather than an alias of an existing row.
ALL_SURFACES = sorted(VAULT_TOOLS_BY_SET) + ["(unregistered-fallback)"]


def _vault_edit_for(surface: str) -> dict:
    name = "" if surface == "(unregistered-fallback)" else surface
    tools = tools_for_set(name)
    edit = next((t for t in tools if t.get("name") == "vault_edit"), None)
    assert edit is not None, f"{surface} serves no vault_edit tool"
    return edit


def _body_replace_description(surface: str) -> str:
    return _vault_edit_for(surface)["input_schema"]["properties"][
        "body_replace"
    ]["description"]


def _delete_capable_tools(surface: str) -> list[str]:
    name = "" if surface == "(unregistered-fallback)" else surface
    return sorted(
        t["name"] for t in tools_for_set(name)
        if any(marker in t["name"].lower() for marker in _DELETE_MARKERS)
    )


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ALL_SURFACES)
def test_every_surface_serves_the_shared_description(surface: str):
    """One description object, many readers — that is the whole premise."""
    assert DELETE_CLAIM in _body_replace_description(surface)


def test_all_surfaces_serve_the_IDENTICAL_text():
    """If the sets ever diverge, the capability table below has to be
    per-surface rather than global — this is the tripwire for that."""
    texts = {_body_replace_description(s) for s in ALL_SURFACES}
    assert len(texts) == 1, (
        "the body_replace description is no longer shared across tool sets; "
        "the delete claim must now be verified per-surface"
    )


# ---------------------------------------------------------------------------
# The capability table that makes the claim true
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ALL_SURFACES)
def test_no_surface_holds_a_delete_capable_tool(surface: str):
    """THE GUARD. The claim is a promise about EVERY reader's toolset."""
    offenders = _delete_capable_tools(surface)
    assert offenders == [], (
        f"tool set {surface!r} now serves {offenders}, which falsifies the "
        f"flat claim {DELETE_CLAIM!r} in the SHARED vault_edit body_replace "
        f"description — a claim every other set reads too. Either drop the "
        f"tool from this set, or reword the description to be scope-aware "
        f"the way vault.scope._gcal_replace_remedy already is."
    )


def test_the_capability_table_is_pinned_whole():
    """The table as measured, so a set gaining ANY tool is visible here
    rather than only when it happens to be delete-shaped. Cheap to update
    deliberately; impossible to change by accident."""
    measured = {s: len(tools_for_set("" if s == "(unregistered-fallback)" else s))
                for s in ALL_SURFACES}
    assert measured == {
        "hypatia": 12,
        "kalle": 13,
        "talker": 10,
        "(unregistered-fallback)": 4,
    }, (
        f"tool-set sizes changed: {measured}. Confirm no new tool is "
        f"delete-capable, then update this pin in the same commit."
    )


def test_the_unregistered_fallback_is_the_BASE_set_not_the_talker_set():
    """Pins the naming trap documented above, because getting it backwards
    is what makes the fallback row look redundant and invites deleting it.

    ``tools_for_set`` falls back to ``TALKER_VAULT_TOOLS`` — the 4-tool base
    — while the ``"talker"`` KEY serves ``SALEM_VAULT_TOOLS``. They are
    different surfaces and both must stay delete-less.
    """
    from alfred.telegram.conversation import (
        SALEM_VAULT_TOOLS,
        TALKER_VAULT_TOOLS,
    )

    assert tools_for_set("no-such-set") == TALKER_VAULT_TOOLS
    assert VAULT_TOOLS_BY_SET["talker"] is SALEM_VAULT_TOOLS
    assert tools_for_set("no-such-set") != tools_for_set("talker")


# ---------------------------------------------------------------------------
# The claim's companion — the alternatives it offers must be real
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ALL_SURFACES)
def test_the_description_offers_operations_the_surface_actually_has(surface: str):
    """#80's own lesson, applied to its own fix: the replacement advice must
    not become dead advice. Both named alternatives are kwargs of the SAME
    vault_edit tool, so every surface serving the text can perform them."""
    description = _body_replace_description(surface)
    assert "body_append" in description
    assert "body_insert_at" in description

    properties = _vault_edit_for(surface)["input_schema"]["properties"]
    assert "body_append" in properties
    assert "body_insert_at" in properties


@pytest.mark.parametrize("surface", ALL_SURFACES)
def test_no_surface_is_told_to_vault_delete(surface: str):
    """The regression #80 closed: the description must never name the one
    operation none of these sets can perform."""
    assert "vault_delete" not in _body_replace_description(surface)

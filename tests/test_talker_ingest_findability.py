"""Web-ingested records must be FINDABLE from EVERY instance's chat surface.

Operator-hit on Salem, then ruled platform-wide ("Make sure Vera — and in turn
everyone else — can do this as well"). A `document` written by the web ingest
could be READ by path and matched by grep and glob, but `vault list document`
died on gate 1 with "Unknown type: 'document' under scope '<scope>'". The
record existed and could be opened; it just could not be ENUMERATED.

All four instances run a web_ingest peer, so all four can hold one. The chat
scope per instance is config-driven (``instance.tool_set``): Salem -> talker,
KAL-LE -> kalle, Hypatia -> hypatia, VERA -> vera.

THE FAMILY IS PARAMETRIZED, BUT MEMBERSHIP IS NOT FITNESS. The read pins run
over all four scopes. The write pins do NOT: `hypatia` legitimately CREATES
documents and sources (it is the Zettelkasten instance — authorship is its
job, and `HYPATIA_CREATE_TYPES` has said so since long before this lane). So
the authorship-closed pins cover the three scopes where closure is the claim,
and `hypatia`'s open write surface gets its own explicit pin recording that it
is intended and untouched. Parametrizing a refusal over `hypatia` would have
asserted something false about it.

The fixtures deliberately use a DIFFERENT vault root and record name per
scope — VERA reads Dame-Bluebird, not Salem's vault — so what the pins
demonstrate is the mechanism, not one instance's paths.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from alfred.vault import ops as vault_ops
from alfred.vault import schema
from alfred.vault.ops import VaultError
from alfred.vault.scope import HYPATIA_CREATE_TYPES, ScopeError, check_scope

#: Every scope an instance's chat surface runs under.
CHAT_SCOPES = ("talker", "kalle", "hypatia", "vera")

#: The subset whose gate-2 create allowlist excludes document/source, i.e.
#: where "findable but not authorable" is the claim being made.
AUTHORSHIP_CLOSED_SCOPES = ("talker", "kalle", "vera")

#: Per-scope realistic shapes: distinct vault roots and record names on
#: purpose. ``vera``'s row is the real record that landed on the box.
SCOPE_SHAPES: dict[str, tuple[str, str]] = {
    "talker": ("Salem", "RBC Statement"),
    "kalle": ("aftermath-lab", "Runbook Export"),
    "hypatia": ("Hypatia", "Paper Draft"),
    "vera": ("Dame-Bluebird", "3335556 Bank Records"),
}


def _ingested_vault(tmp_path: Path, scope: str) -> tuple[Path, str]:
    """Build that scope's own vault shape. Returns (vault_path, record_name)."""
    root_name, record_name = SCOPE_SHAPES[scope]
    vault = tmp_path / root_name
    (vault / "document").mkdir(parents=True)
    (vault / "source").mkdir(parents=True)
    (vault / "document" / f"{record_name}.md").write_text(
        f"---\ntype: document\nname: {record_name}\nstatus: active\n"
        f"ingested_from: web\n---\n\n# {record_name}\n",
        encoding="utf-8",
    )
    (vault / "source" / f"{record_name} Portal.md").write_text(
        f"---\ntype: source\nname: {record_name} Portal\nstatus: active\n"
        f"---\n\n# {record_name} Portal\n",
        encoding="utf-8",
    )
    return vault, record_name


# --- the read side: what every instance was denied ---------------------------


@pytest.mark.parametrize("scope", CHAT_SCOPES)
def test_chat_scope_lists_its_ingested_document(tmp_path: Path, scope: str) -> None:
    """THE BUG, per instance. Pre-fix this raised VaultError at gate 1 for
    talker / kalle / vera (hypatia was already tagged)."""
    vault, record_name = _ingested_vault(tmp_path, scope)

    records = vault_ops.vault_list(vault, "document", scope=scope)

    assert [r["path"] for r in records] == [f"document/{record_name}.md"]
    assert records[0]["name"] == record_name


@pytest.mark.parametrize("scope", CHAT_SCOPES)
def test_chat_scope_lists_its_ingested_source(tmp_path: Path, scope: str) -> None:
    vault, record_name = _ingested_vault(tmp_path, scope)

    records = vault_ops.vault_list(vault, "source", scope=scope)

    assert [r["name"] for r in records] == [f"{record_name} Portal"]


# --- the write side: the paired control that makes the read pins non-vacuous -


@pytest.mark.parametrize("scope", AUTHORSHIP_CLOSED_SCOPES)
@pytest.mark.parametrize("record_type", ("document", "source"))
def test_authorship_closed_scope_still_cannot_create(
    tmp_path: Path, scope: str, record_type: str
) -> None:
    """Gate 2's per-scope create allowlist remains the authorship ceiling.

    This is the half that separates "findability opened" from "the type became
    canonical for everyone". Without it the read pins would be green against a
    far broader change than the one intended.

    It asserts WHICH GATE refuses, not merely that something did — which is
    why it REDS (correctly) if a scope's tag is removed. Untagged, the create
    is still refused, but by gate 1 with `VaultError: Unknown type` rather
    than by gate 2's policy. Both refuse; only one means "authorship is
    bounded by policy". A pin accepting any refusal would report a healthy
    ceiling on a build whose ceiling was really the registry not knowing the
    word.
    """
    vault, _ = _ingested_vault(tmp_path, scope)

    with pytest.raises(ScopeError) as excinfo:
        vault_ops.vault_create(
            vault, record_type, "Should Not Exist",
            set_fields={"name": "Should Not Exist"}, scope=scope,
        )

    assert "can only create" in str(excinfo.value)
    assert not (vault / record_type / "Should Not Exist.md").exists()


@pytest.mark.parametrize("scope", CHAT_SCOPES)
def test_no_chat_scope_can_delete_a_document(tmp_path: Path, scope: str) -> None:
    """Delete is refused under EVERY chat scope, hypatia included — the one
    write verb where the whole family really does agree."""
    with pytest.raises(ScopeError):
        check_scope(scope, "delete", rel_path="document/X.md", record_type="document")


@pytest.mark.parametrize("scope", AUTHORSHIP_CLOSED_SCOPES)
@pytest.mark.parametrize("body_op", ("insert_at", "replace"))
def test_authorship_closed_scope_cannot_body_mutate_a_document(
    scope: str, body_op: str
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        check_scope(
            scope, f"body_{body_op}", rel_path="document/X.md",
            record_type="document", body_write=True, body_op=body_op,
        )

    assert "document" in str(excinfo.value)


def test_hypatia_authorship_stays_open_and_this_lane_did_not_change_it(
    tmp_path: Path,
) -> None:
    """The declared exception, pinned AT the assertion rather than left as a
    gap in the parametrization.

    Hypatia is the Zettelkasten instance: writing documents and sources IS its
    job, and `HYPATIA_CREATE_TYPES` has included both since long before this
    lane. It already carried the scope tag, so nothing here altered its
    surface. This pin exists so that the narrower parametrization above reads
    as a decision rather than an oversight — and so a future change that
    quietly closes Hypatia's authorship has to come here and say so.
    """
    vault, _ = _ingested_vault(tmp_path, "hypatia")

    result = vault_ops.vault_create(
        vault, "document", "Hypatia May Author This",
        set_fields={"name": "Hypatia May Author This"}, scope="hypatia",
    )

    assert result
    assert (vault / "document" / "Hypatia May Author This.md").exists()
    assert {"document", "source"} <= HYPATIA_CREATE_TYPES


# --- scoping: the fix is per-scope, not a global unlock ---------------------


@pytest.mark.parametrize("scope", ("vera_ops", "curator"))
def test_an_untagged_scope_still_cannot_list_documents(
    tmp_path: Path, scope: str
) -> None:
    """The nearest INADMISSIBLE neighbours, one of them instance-adjacent.

    `vera_ops` is a real VERA scope that was deliberately NOT tagged, and
    `curator` never was. Both must still be refused at gate 1 — which is what
    proves the tag is per-scope rather than having made the type universally
    valid. If this ever goes green, the change was wider than intended.
    """
    vault, _ = _ingested_vault(tmp_path, "vera")

    with pytest.raises(VaultError) as excinfo:
        vault_ops.vault_list(vault, "document", scope=scope)

    assert "Unknown type" in str(excinfo.value)


# --- the derived view -------------------------------------------------------


@pytest.mark.parametrize("scope", CHAT_SCOPES)
def test_chat_scope_union_auto_derives_and_only_widens(scope: str) -> None:
    """Each scope's union is a DERIVED view, and the tag widens it without
    dropping anything.

    The superset assertion is load-bearing rather than decorative: before this
    change talker/kalle/vera were not keys at all and took `_validate_type`'s
    `.get(scope, KNOWN_TYPES)` fallback to the canonical set. Now that each has
    a key of its own the fallback no longer applies, so a union that failed to
    contain canonical would break every previously-valid type at once.
    """
    union = schema.KNOWN_TYPES_BY_SCOPE[scope]

    assert {"document", "source"} <= union
    assert schema.KNOWN_TYPES <= union
    assert union == set(schema.TYPE_REGISTRY.known_types(scope))


def test_note_needed_no_tag_because_it_is_canonical() -> None:
    """Documents the decision not to touch `note`: it rides SCOPE_CANONICAL,
    so gate 1 already admitted it under every scope and this lane added
    nothing to it.

    Note carries pre-existing ``vera`` / ``vera_batch`` / ``vera_ops`` tags
    that predate this lane and have nothing to do with findability — so the
    assertion here is specifically that the two scopes THIS lane introduces
    are absent, not that the type is tag-free.
    """
    definition = next(d for d in schema.TYPE_REGISTRY if d.name == "note")

    assert schema.SCOPE_CANONICAL in definition.available_in_scopes
    assert "note" in schema.KNOWN_TYPES
    assert not ({"talker", "kalle"} & definition.available_in_scopes)


def test_jeeves_was_deliberately_left_out_of_the_widening() -> None:
    """Jeeves is the garage capture appliance: it runs no web ingest, and its
    {note, source} set is curated. It keeps its pre-existing `source` tag and
    gains no `document` — a decision, recorded so its absence is legible."""
    document = next(d for d in schema.TYPE_REGISTRY if d.name == "document")
    source = next(d for d in schema.TYPE_REGISTRY if d.name == "source")

    assert "jeeves" not in document.available_in_scopes
    assert "jeeves" in source.available_in_scopes  # pre-existing, untouched

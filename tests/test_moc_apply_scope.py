"""Pins for the MOC-reader's WRITE half — the ``moc_apply`` scope and the
membership writer (2026-08-20, operator ruling R3).

The old apply path had ZERO production callers and was deleted in T5, so
none of this is a restoration: every pin below drives a path that did not
exist at ``afe7d2f8``.

Two pin families here earn their keep specifically:

  * **Provenance-lock composition, BOTH directions.** ``mocs`` is not one
    of the five locked provenance fields, so a MOC membership must APPLY to
    a web-ingested record while its BODY stays refused. This is a live
    composition rather than a theoretical one: ``source`` is both a MOC
    trigger type and a type the web ingest stamps. The body-refusal
    direction is driven on a BODY-CAPABLE scope on purpose — under
    ``moc_apply`` a body write refuses for the scope's own reason, which
    would pass identically against a build with no provenance lock at all.

  * **Refusal pins assert WHY.** Every refusal below asserts the message
    that names the rule that fired. A type-gate refusal, a field-gate
    refusal and a provenance refusal are indistinguishable by outcome —
    same exception class, same untouched record — so the reason string is
    the only thing that tells a working gate from an unrelated denial.

Every refusal family carries its nearest admissible neighbour accepted, so
these pins fail against a build that refuses everything exactly as they
fail against one that refuses nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.surveyor.moc_apply import MOC_APPLY_SCOPE, apply_membership
from alfred.vault.ops import vault_create, vault_edit, vault_read
from alfred.vault.scope import (
    MOC_APPLY_FIELDS,
    MOC_APPLY_TYPES,
    MOC_DIRECTORY,
    MOC_MIRROR_TYPE,
    ScopeError,
)

TAMPER = "TAMPERED-BY-AGENT"


def _mint_member(
    tmp_vault: Path,
    title: str,
    record_type: str = "zettel",
    *,
    ingested: bool = False,
    mocs: list[str] | None = None,
) -> str:
    """Mint a candidate member record. ``ingested=True`` reproduces the
    production web-ingest shape (the provenance marker plus its four
    siblings), which is what makes the composition pins live."""
    fields: dict = {}
    if mocs is not None:
        fields["mocs"] = mocs
    if ingested:
        fields.update(
            {
                "ingested_via": "web",
                "source": "https://example.test/artifact",
                "ingested_by": "andrew",
                "ingested_at": "2026-08-19T00:00:00+00:00",
                "ingest_correlation_id": "corr-123",
            },
        )
        result = vault_create(
            tmp_vault, record_type, title,
            set_fields=fields, body="Verbatim body.\n", scope="web_ingest",
        )
        return result["path"]
    result = vault_create(
        tmp_vault, record_type, title,
        set_fields=fields or None, body="Authored body.\n", scope="hypatia",
    )
    return result["path"]


def _mint_moc(tmp_vault: Path, stem: str) -> str:
    """Mint a topic MOC with the ``# Contents`` section the Sub-arc A hook
    appends into."""
    moc_dir = tmp_vault / "MOC"
    moc_dir.mkdir(exist_ok=True)
    path = moc_dir / f"{stem}.md"
    # ``MOC``, NOT ``moc`` — the canonical registry name, and the ONLY
    # non-lowercase canonical type (schema.py preserves the operator's
    # ``Practical Stoicism MOC.md`` convention). This fixture minted
    # ``moc`` until the gate caught it: a record shape ``vault_create``
    # would REJECT, invisible behind 34 green pins because the fixture and
    # the assertion agreed with each other and with nothing else.
    path.write_text(
        "---\ntype: "
        + MOC_MIRROR_TYPE
        + "\nname: "
        + stem
        + "\ncreated: 2026-08-20\ntags: []\nrelated: []\n---\n\n# Contents\n\n",
        encoding="utf-8",
    )
    return f"MOC/{stem}.md"


def _mocs_of(tmp_vault: Path, rel: str) -> list[str]:
    fm = vault_read(tmp_vault, rel).get("frontmatter") or {}
    raw = fm.get("mocs")
    if raw is None:
        return []
    return [str(e) for e in raw] if isinstance(raw, list) else [str(raw)]


# ---------------------------------------------------------------------------
# The premise the whole type gate rests on
# ---------------------------------------------------------------------------


def test_premise_moc_apply_types_equals_the_hook_trigger_set() -> None:
    """PREMISE PIN. The scope's type gate is only correct because it is the
    Sub-arc A hook's trigger set — a membership on any other type would be
    one the ``# Contents`` mirror never performs. The two are declared in
    different modules, so drift between them is silent and this asserts it
    cannot happen unnoticed."""
    from alfred.vault.zettel_hooks import _MOC_TRIGGER_TYPES

    assert MOC_APPLY_TYPES == set(_MOC_TRIGGER_TYPES)


def test_premise_moc_mirror_type_is_a_CANONICAL_type() -> None:
    """PREMISE PIN, and the one this lane most needed.

    ``MOC_MIRROR_TYPE`` shipped as the literal ``"moc"`` — which is not a
    canonical type at all: ``schema.py`` defines ``TypeDefinition(name="MOC")``,
    the only non-lowercase canonical type, preserved for the operator's
    ``Practical Stoicism MOC.md`` filename convention. Every fixture minted
    that non-existent shape by hand, so 34 pins and two mutation batteries
    agreed with each other and with nothing else.

    Deriving the constant from the registry fixed the value but NOT the
    blind spot: the fixtures now derive from the same constant, so forcing
    it back to ``"moc"`` moves fixtures and assertions together and every
    pin stays green (measured — that mutation scored RED 0 until this test
    existed). Only a check against an INDEPENDENT source can see it, which
    is what makes this a premise pin rather than another self-consistent
    assertion."""
    from alfred.vault.schema import KNOWN_TYPES_BY_SCOPE, TYPE_DIRECTORY

    # It is a real type Hypatia's scope can address...
    assert MOC_MIRROR_TYPE in KNOWN_TYPES_BY_SCOPE["hypatia"]
    # ...and it is the type that OWNS the MOC directory, which is the
    # relationship the mirror arm actually depends on.
    assert TYPE_DIRECTORY[MOC_MIRROR_TYPE] == MOC_DIRECTORY
    # The lowercase spelling that shipped is NOT canonical — pinned
    # explicitly so the regression cannot return as "well, both work".
    assert "moc" not in KNOWN_TYPES_BY_SCOPE["hypatia"]


def test_premise_mocs_is_not_a_locked_provenance_field() -> None:
    """PREMISE PIN for the composition family below: the whole reason a MOC
    apply is permitted on an ingested record is that ``mocs`` is not one of
    the five locked provenance fields. If that set ever gains ``mocs``, the
    composition pins should fail HERE with the reason, not downstream with a
    confusing refusal."""
    from alfred.vault.scope import INGEST_PROVENANCE_LOCKED_FIELDS

    assert MOC_APPLY_FIELDS.isdisjoint(INGEST_PROVENANCE_LOCKED_FIELDS)


# ---------------------------------------------------------------------------
# The scope gate — type, field, fail-closed
# ---------------------------------------------------------------------------


class TestScopeGate:
    def test_mocs_edit_accepted_on_a_trigger_type(self, tmp_vault: Path) -> None:
        """The positive control the refusals below are measured against."""
        rel = _mint_member(tmp_vault, "Accepted Member")
        vault_edit(
            tmp_vault, rel,
            set_fields={"mocs": ["[[MOC/Stoicism MOC]]"]},
            scope=MOC_APPLY_SCOPE,
        )
        assert _mocs_of(tmp_vault, rel) == ["[[MOC/Stoicism MOC]]"]

    def test_non_trigger_type_refused_with_the_type_reason(
        self, tmp_vault: Path,
    ) -> None:
        """A ``note`` is not a MOC trigger type. Asserting the REASON: an
        unknown-record or field refusal would satisfy a bare `raises`."""
        rel = _mint_member(tmp_vault, "Note Member", record_type="note")
        with pytest.raises(ScopeError) as exc:
            vault_edit(
                tmp_vault, rel,
                set_fields={"mocs": ["[[MOC/Stoicism MOC]]"]},
                scope=MOC_APPLY_SCOPE,
            )
        msg = str(exc.value)
        assert "may only edit record types" in msg
        assert "'note'" in msg
        assert "'# Contents' mirror never performs" in msg
        assert _mocs_of(tmp_vault, rel) == []

    def test_other_field_refused_with_the_field_reason(
        self, tmp_vault: Path,
    ) -> None:
        rel = _mint_member(tmp_vault, "Field Member")
        with pytest.raises(ScopeError) as exc:
            vault_edit(
                tmp_vault, rel, set_fields={"tags": ["smuggled"]},
                scope=MOC_APPLY_SCOPE,
            )
        msg = str(exc.value)
        assert "may only edit fields in the allowlist" in msg
        assert "Rejected: tags" in msg

    def test_mixed_edit_refused_whole(self, tmp_vault: Path) -> None:
        """``mocs`` alongside a non-allowlisted field refuses the WHOLE edit
        — the allowed half must not land."""
        rel = _mint_member(tmp_vault, "Mixed Member")
        with pytest.raises(ScopeError):
            vault_edit(
                tmp_vault, rel,
                set_fields={"mocs": ["[[MOC/X]]"], "tags": ["smuggled"]},
                scope=MOC_APPLY_SCOPE,
            )
        assert _mocs_of(tmp_vault, rel) == []
        fm = vault_read(tmp_vault, rel).get("frontmatter") or {}
        assert "smuggled" not in (fm.get("tags") or [])

    def test_create_denied_by_the_SCOPE_gate(self, tmp_vault: Path) -> None:
        """v1 is add-to-EXISTING; ``propose_new`` (MOC creation) is a
        separate lane, and the scope is what makes that boundary real rather
        than a convention the caller is trusted to keep.

        Driven with a CANONICAL type on purpose. Vault writes cross two
        gates in order (``_validate_type`` then ``check_scope``), and a
        Hypatia-only type like ``zettel`` is refused at gate 1 for being
        unknown to this scope — which would pass this pin against a build
        whose ``moc_apply`` had ``create: True``. ``note`` clears gate 1, so
        only the scope's own create-denial can produce this refusal."""
        with pytest.raises(ScopeError) as exc:
            vault_create(
                tmp_vault, "note", "Should Not Exist", scope=MOC_APPLY_SCOPE,
            )
        assert "create" in str(exc.value).lower()

    def test_body_surface_denied_under_this_scope(self, tmp_vault: Path) -> None:
        """Defence in depth, and pinned for its OWN reason: on an unmarked
        record no provenance lock exists, so this refusal can only be the
        scope's."""
        rel = _mint_member(tmp_vault, "Body Member")
        with pytest.raises(ScopeError):
            vault_edit(
                tmp_vault, rel, body_append=TAMPER, scope=MOC_APPLY_SCOPE,
            )
        assert TAMPER not in (tmp_vault / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Provenance-lock composition — BOTH directions
# ---------------------------------------------------------------------------


class TestProvenanceComposition:
    @pytest.mark.parametrize("record_type", ["source"])
    def test_moc_apply_permitted_on_a_web_ingested_record(
        self, tmp_vault: Path, record_type: str,
    ) -> None:
        """DIRECTION 1 — "frontmatter open". The membership lands on a
        record carrying ``ingested_via``.

        ``source`` is the ONLY member of this parametrization, and that is a
        measured fact rather than a thin test: it is the sole type that is
        both a MOC trigger type and a type ``web_ingest`` can create. A
        ``zettel`` parametrization was written here first and REMOVED after
        it failed at gate 1 — ``web_ingest`` cannot create zettels, so an
        ingested zettel is not a record that exists to protect. The
        composition is pinned exactly where it is real."""
        rel = _mint_member(
            tmp_vault, f"Ingested {record_type}",
            record_type=record_type, ingested=True,
        )
        target = _mint_moc(tmp_vault, "Roman Philosophy MOC")
        result = apply_membership(
            tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
        )
        assert result.ok
        assert result.applied == [rel]
        # Read the record OFF DISK — the operator's question is whether the
        # field carries the MOC, not whether a helper returned success.
        assert _mocs_of(tmp_vault, rel) == ["[[MOC/Roman Philosophy MOC]]"]
        # The marker is untouched by the apply.
        fm = vault_read(tmp_vault, rel).get("frontmatter") or {}
        assert fm.get("ingested_via") == "web"

    def test_body_still_refused_on_the_same_marked_record(
        self, tmp_vault: Path,
    ) -> None:
        """DIRECTION 2 — "body locked". Driven under ``hypatia``, a
        BODY-CAPABLE scope, so the provenance lock is the only thing that
        can refuse. Under ``moc_apply`` this pin would pass against a build
        with the provenance lock deleted entirely."""
        rel = _mint_member(
            tmp_vault, "Ingested Body Locked",
            record_type="source", ingested=True,
        )
        before = (tmp_vault / rel).read_text(encoding="utf-8")
        with pytest.raises(ScopeError) as exc:
            vault_edit(tmp_vault, rel, body_append=TAMPER, scope="hypatia")
        msg = str(exc.value)
        assert "may not alter the body" in msg
        assert "ingested_via: web" in msg
        assert (tmp_vault / rel).read_text(encoding="utf-8") == before

        # NEAREST ADMISSIBLE NEIGHBOUR: same scope, same body edit, on the
        # marker-ABSENT twin — accepted. Without this the pin above passes
        # against a build that refuses every body write.
        authored = _mint_member(
            tmp_vault, "Authored Body Open", record_type="source",
        )
        vault_edit(tmp_vault, authored, body_append=TAMPER, scope="hypatia")
        assert TAMPER in (tmp_vault / authored).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The writer — fan-out, idempotence, partial isolation
# ---------------------------------------------------------------------------


class TestApplyMembership:
    def test_applies_every_member_of_the_row(self, tmp_vault: Path) -> None:
        """ONE affirm applies the WHOLE row (2026-08-20 ruling)."""
        members = [
            _mint_member(tmp_vault, f"Fan Member {i}") for i in range(3)
        ]
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        result = apply_membership(
            tmp_vault, member_rel_paths=members, target_moc_rel_path=target,
        )
        assert result.ok
        assert sorted(result.applied) == sorted(members)
        for rel in members:
            assert _mocs_of(tmp_vault, rel) == ["[[MOC/Stoicism MOC]]"]

    def test_reapply_is_idempotent_and_writes_nothing(
        self, tmp_vault: Path,
    ) -> None:
        rel = _mint_member(tmp_vault, "Idempotent Member")
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        apply_membership(
            tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
        )
        first = (tmp_vault / rel).read_text(encoding="utf-8")

        second = apply_membership(
            tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
        )
        assert second.ok
        assert second.already == [rel]
        assert second.applied == []
        assert _mocs_of(tmp_vault, rel) == ["[[MOC/Stoicism MOC]]"]
        # Byte-identical: idempotence means no write, not a rewrite that
        # happens to produce the same field.
        assert (tmp_vault / rel).read_text(encoding="utf-8") == first

    @pytest.mark.parametrize(
        "existing",
        [
            "[[MOC/Stoicism MOC]]",
            "MOC/Stoicism MOC.md",
            "MOC/Stoicism MOC",
            "[[MOC/Stoicism MOC|The Stoics]]",
        ],
    )
    def test_idempotent_across_operator_typo_shapes(
        self, tmp_vault: Path, existing: str,
    ) -> None:
        """The vault carries these shapes for real. Each must be recognised
        as "already cites" — otherwise a second apply appends a duplicate in
        a different spelling."""
        rel = _mint_member(tmp_vault, f"Shape {abs(hash(existing))%9999}",
                           mocs=[existing])
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        result = apply_membership(
            tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
        )
        assert result.already == [rel]
        assert _mocs_of(tmp_vault, rel) == [existing]

    def test_existing_memberships_preserved_verbatim(
        self, tmp_vault: Path,
    ) -> None:
        """Adding a membership must not rewrite the operator's own spelling
        of the memberships already there."""
        rel = _mint_member(
            tmp_vault, "Multi Member", mocs=["MOC/Other MOC.md"],
        )
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        apply_membership(
            tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
        )
        assert _mocs_of(tmp_vault, rel) == [
            "MOC/Other MOC.md", "[[MOC/Stoicism MOC]]",
        ]

    def test_ineligible_member_skipped_not_failed(
        self, tmp_vault: Path,
    ) -> None:
        """The SKILL documents a live backlog row whose members are
        ``session/`` records. An ineligible member is not a failure — it was
        never applicable — and the eligible members in the same row still
        land."""
        good = _mint_member(tmp_vault, "Eligible One")
        bad = _mint_member(tmp_vault, "Ineligible One", record_type="note")
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        result = apply_membership(
            tmp_vault, member_rel_paths=[good, bad],
            target_moc_rel_path=target,
        )
        assert result.ok  # ineligible does NOT fail the apply
        assert result.applied == [good]
        assert [o.rel_path for o in result.ineligible] == [bad]
        assert "not a MOC trigger type" in result.ineligible[0].error
        assert _mocs_of(tmp_vault, bad) == []

    def test_one_failure_does_not_abandon_the_rest(
        self, tmp_vault: Path,
    ) -> None:
        """Per-member failure isolation, and the partial state the queue
        must survive: a missing record fails alone."""
        good_a = _mint_member(tmp_vault, "Isolated A")
        good_b = _mint_member(tmp_vault, "Isolated B")
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        result = apply_membership(
            tmp_vault,
            member_rel_paths=[good_a, "zettel/Does Not Exist.md", good_b],
            target_moc_rel_path=target,
        )
        assert not result.ok
        assert result.partial
        assert sorted(result.applied) == sorted([good_a, good_b])
        assert len(result.failed) == 1
        assert result.first_error
        assert sorted(result.touched) == sorted([good_a, good_b])

    def test_inventory_moc_target_refused_wholesale(
        self, tmp_vault: Path,
    ) -> None:
        """Inventory MOCs are predicate-driven; a membership written into
        one gets regenerated away. Refused for the whole call, and NOTHING
        is written."""
        rel = _mint_member(tmp_vault, "Inventory Victim")
        result = apply_membership(
            tmp_vault, member_rel_paths=[rel],
            target_moc_rel_path="MOC/_Open Questions.md",
        )
        assert not result.ok
        assert result.applied == []
        assert _mocs_of(tmp_vault, rel) == []

        # POSITIVE CONTROL: the same member, a NON-inventory target, lands.
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        ok_result = apply_membership(
            tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
        )
        assert ok_result.ok
        assert _mocs_of(tmp_vault, rel) == ["[[MOC/Stoicism MOC]]"]

    def test_summary_log_always_emitted_even_when_nothing_changed(
        self, tmp_vault: Path,
    ) -> None:
        """ILB: an apply that touched nothing must be distinguishable from
        one that never ran, and every count is present at zero."""
        rel = _mint_member(tmp_vault, "Log Member")
        target = _mint_moc(tmp_vault, "Stoicism MOC")
        apply_membership(
            tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
        )
        with structlog.testing.capture_logs() as captured:
            apply_membership(
                tmp_vault, member_rel_paths=[rel], target_moc_rel_path=target,
            )
        matches = [
            c for c in captured
            if c.get("event") == "surveyor.moc_apply.summary"
        ]
        assert len(matches) == 1
        row = matches[0]
        assert row["applied"] == 0
        assert row["already"] == 1
        assert row["ineligible"] == 0
        assert row["failed"] == 0
        assert row["ok"] is True

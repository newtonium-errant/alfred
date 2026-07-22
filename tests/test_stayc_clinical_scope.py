"""Tests for the ``stayc_clinical`` scope + the ``clinical_note`` type (scribe P1-b).

The sovereign ambient-scribe scope-first matrix. ``clinical_note`` holds PHI;
it is the record the whole no-egress boundary (scribe P1-a) exists to protect,
so it is scoped, gated, and denied-from-egress at every layer.

Coverage (contract pins, mirroring test_vera_scope / test_rrts_intake_scope):
    * Matrix pins — STAYC_CLINICAL_CREATE_TYPES / ATTEST_TYPES / ATTEST_FIELDS,
      _NEVER_PUSH_TYPES. Widening any is a deliberate matrix change (update
      the pin in the same commit, pre-commit checklist #6).
    * Gate-1 AUTO-DERIVE — clinical_note in known_types("stayc_clinical") +
      KNOWN_TYPES_BY_SCOPE["stayc_clinical"], NOT in canonical / other scopes.
    * create gate — clinical_note only; non-clinical + empty type fail-closed.
    * attest edit gate — status/attested_by/attested_at allowed; other fields,
      missing fields, wrong/empty type denied; body writes REFUSED (content
      frozen — the security-critical pin).
    * ANTI-SPOLIATION — delete/move denied; body_insert_at/body_replace denied;
      the three denysets + their PRECEDENCE over a delete:True / "*" scope.
    * _NEVER_PUSH guard — is_never_push + the peer-propose refusal.
    * End-to-end vault_create draft + attest flip + frozen-body/anti-delete.
"""

from __future__ import annotations

import asyncio

import frontmatter
import pytest

from alfred.vault import schema
from alfred.vault.ops import VaultError, vault_create, vault_delete, vault_edit
from alfred.vault.scope import (
    CANONICAL_RECORD_TYPES,
    STAYC_CLINICAL_ATTEST_FIELDS,
    STAYC_CLINICAL_ATTEST_TYPES,
    STAYC_CLINICAL_CREATE_TYPES,
    STAYC_CLINICAL_DRAFT_EDIT_FIELDS,
    ScopeError,
    _BODY_MUTATE_DENIED_TYPES,
    _DELETE_DENIED_TYPES,
    check_scope,
)


_SCOPE = "stayc_clinical"                 # the pipeline/agent scope
_ATTEST_SCOPE = "stayc_clinical_attest"   # the privileged orchestrator scope (P2-a)


# ---------------------------------------------------------------------------
# Matrix pins (the principal artifact)
# ---------------------------------------------------------------------------

def test_create_types_matrix_pin():
    assert STAYC_CLINICAL_CREATE_TYPES == {"clinical_note"}


def test_attest_types_and_fields_matrix_pin():
    assert STAYC_CLINICAL_ATTEST_TYPES == {"clinical_note"}
    assert STAYC_CLINICAL_ATTEST_FIELDS == {"attested_by", "attested_at", "status"}


def test_never_push_types_pin():
    assert schema._NEVER_PUSH_TYPES == frozenset({"clinical_note"})


def test_clinical_note_not_canonical():
    # NOT a canonical type => the create gate skips the propose-hint guard
    # (same check the coordinator did for `task`).
    assert "clinical_note" not in CANONICAL_RECORD_TYPES


# ---------------------------------------------------------------------------
# Gate 1 — AUTO-DERIVE (no KNOWN_TYPES_BY_SCOPE literal edit)
# ---------------------------------------------------------------------------

def test_gate1_autoderives_for_stayc_clinical():
    # known_types(scope) unions canonical with the scope's tagged types.
    assert "clinical_note" in schema.TYPE_REGISTRY.known_types("stayc_clinical")
    # The derived-view dict auto-populates from the registry comprehension.
    assert "clinical_note" in schema.KNOWN_TYPES_BY_SCOPE["stayc_clinical"]


def test_gate1_isolation_clinical_note_not_canonical_or_other_scope():
    # Per-instance isolation: NOT in the canonical set, NOT visible to any
    # other scope (Salem/VERA-ops/KAL-LE/Hypatia cannot even validate it).
    assert "clinical_note" not in schema.KNOWN_TYPES
    assert "clinical_note" not in schema.TYPE_REGISTRY.known_types()  # canonical
    for other in ("vera", "vera_ops", "talker", "kalle", "hypatia"):
        assert "clinical_note" not in schema.TYPE_REGISTRY.known_types(other)


def test_clinical_note_typedefinition_shape():
    d = schema.TYPE_REGISTRY.get("clinical_note")
    assert d is not None
    assert d.directory == "clinical_note"
    assert d.name_field == "title"
    assert d.required_fields == ("title",)
    assert d.statuses == frozenset({"ai_draft", "attested", "amended"})
    # P2-a (#41) tagged the privileged attest scope; 13d-3 tagged the privileged
    # stayc_clinical_destroy scope — both for gate-1 list-safety.
    assert d.available_in_scopes == frozenset(
        {"stayc_clinical", "stayc_clinical_attest", "stayc_clinical_destroy"})
    assert d.is_leaf is True
    assert d.is_learn_type is False


# ---------------------------------------------------------------------------
# create gate — stayc_clinical_types_only
# ---------------------------------------------------------------------------

def test_create_clinical_note_allowed():
    check_scope(_SCOPE, "create", record_type="clinical_note", body_write=True)


@pytest.mark.parametrize("rtype", ["note", "ticket", "task", "person", "session"])
def test_create_non_clinical_denied(rtype):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type=rtype)


def test_create_empty_type_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type="")


# ---------------------------------------------------------------------------
# read/search/list/context = True
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["read", "search", "list", "context"])
def test_read_family_allowed(op):
    check_scope(_SCOPE, op, record_type="clinical_note")  # no raise


# ---------------------------------------------------------------------------
# #41 STRUCTURAL ATTESTATION (scribe P2-a) — the agent scope DENIES the triad;
# the privileged stayc_clinical_attest scope allows exactly the triad.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fields",
    [["status"], ["attested_by"], ["attested_at"], ["title"], []],
)
def test_agent_scope_denies_clinical_note_edits_without_live_draft_status(fields):
    # THE #41 raw-flip pin (mutation: allow raw triad edit => fails). With NO
    # existing_frontmatter the gate cannot see a live ``ai_draft`` status, so it
    # is FAIL-CLOSED: the attest triad is orchestrator-only in ANY status, and a
    # non-triad / empty edit on a note of unknown status is SEALED-denied. The
    # P3-a mutable-while-ai_draft ALLOW path requires existing_frontmatter with
    # status==ai_draft (pinned separately below).
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "edit", record_type="clinical_note", fields=fields)


# The privileged attest scope (reachable ONLY via scribe.attest) allows the
# triad — the P1-b field-allowlist logic now lives here.

@pytest.mark.parametrize(
    "fields",
    [["status"], ["attested_by"], ["attested_at"],
     ["status", "attested_by", "attested_at"]],
)
def test_attest_scope_allowed_triad(fields):
    check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note", fields=fields)


@pytest.mark.parametrize("field", ["title", "assessment", "ai_draft", "synthetic", "draft_original", "body"])
def test_attest_scope_non_triad_field_denied(field):
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note", fields=[field])


def test_attest_scope_missing_fields_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note", fields=None)


def test_attest_scope_empty_type_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="", fields=["status"])


def test_attest_scope_wrong_type_denied():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="note", fields=["status"])


def test_attest_scope_refuses_body_write_SECURITY_PIN():
    # The body-freeze pin, now on the privileged attest scope. A pure
    # body_append arrives with fields=[] (EMPTY, not None) + body_write=True;
    # the explicit body_write refusal is what freezes clinical content.
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note",
                    fields=[], body_write=True)


def test_attest_scope_status_only_no_body_passes():
    check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note",
                fields=["status", "attested_by", "attested_at"], body_write=False)


def test_attest_scope_cannot_create_or_delete():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "create", record_type="clinical_note")
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "delete", record_type="clinical_note")
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "move", record_type="clinical_note")


def test_gate1_admits_clinical_note_under_attest_scope():
    # The attest scope has list:True; gate 1 (_validate_type) must admit
    # clinical_note under it (VERA-P1 lesson). Auto-derived, no literal edit.
    assert "clinical_note" in schema.TYPE_REGISTRY.known_types("stayc_clinical_attest")


# ---------------------------------------------------------------------------
# #13 13d-3 (OQ1=A) — the privileged stayc_clinical_destroy scope
# ---------------------------------------------------------------------------

_DESTROY_SCOPE = "stayc_clinical_destroy"


def test_destroy_scope_deletes_clinical_note():
    # THE capability: the s.49 destruction scope may delete a clinical_note.
    check_scope(_DESTROY_SCOPE, "delete", rel_path="clinical_note/x.md", record_type="clinical_note")


@pytest.mark.parametrize("rtype", ["person", "project", "task", "preference", "event", "session"])
def test_destroy_scope_deletes_ONLY_clinical_note(rtype):
    # The clinical_note_destroy_only gate refuses every OTHER type — the scope's sole capability is
    # destroying a clinical_note (no other type, so no blast radius beyond the one record class).
    with pytest.raises(ScopeError):
        check_scope(_DESTROY_SCOPE, "delete", rel_path=f"{rtype}/x.md", record_type=rtype)


@pytest.mark.parametrize("op", ["create", "edit", "move"])
def test_destroy_scope_denies_all_ops_but_delete(op):
    # no other op — create / edit / move are all denied on clinical_note (and everything else).
    with pytest.raises(ScopeError):
        check_scope(_DESTROY_SCOPE, op, rel_path="clinical_note/x.md", record_type="clinical_note")


@pytest.mark.parametrize("scope", ["janitor", "curator", "distiller", "stayc_clinical",
                                   "stayc_clinical_attest", "talker"])
def test_destroy_scope_is_the_sole_clinical_note_deleter(scope):
    # The universal clinical_note delete-deny stays intact for EVERY other scope — only
    # stayc_clinical_destroy is carved out. This is the anti-spoliation belt (a future scope mistake
    # can't accidentally delete a clinical record).
    with pytest.raises(ScopeError):
        check_scope(scope, "delete", rel_path="clinical_note/x.md", record_type="clinical_note")


@pytest.mark.parametrize("scope", ["janitor", "curator", "distiller", "instructor",
                                   "stayc_clinical", "stayc_clinical_attest", "talker"])
def test_clinical_note_delete_denied_with_EMPTY_record_type_BLOCK1(scope):
    """BLOCK-1 probe: vault_delete scope-checks with record_type="" (parses the type after), so the
    deny MUST be PATH-keyed. The reviewer probed janitor (delete:True) deleting a clinical_note/ path
    with record_type="" → previously ALLOWED. Now DENIED for EVERY non-destroy scope, keyed on the
    clinical_note/ path alone. This makes the allow/deny pair symmetric — the enforcement is REAL."""
    with pytest.raises(ScopeError):
        check_scope(scope, "delete", rel_path="clinical_note/note-x.md", record_type="")


def test_clinical_note_delete_denied_via_real_vault_delete_BLOCK1(tmp_path):
    """BLOCK-1 end-to-end: a real vault_delete (which passes record_type="") of a clinical_note under
    the janitor scope is REFUSED — the exact escalation path the reviewer probed, now closed."""
    from alfred.vault.ops import vault_delete
    vault = tmp_path / "vault"
    (vault / "clinical_note").mkdir(parents=True)
    note = vault / "clinical_note" / "note-x.md"
    note.write_text("---\ntitle: N\ntype: clinical_note\nsource_id: enc-x\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ScopeError):
        vault_delete(vault, "clinical_note/note-x.md", scope="janitor")
    assert note.exists()                                       # NOT deleted — the belt held
    # The privileged destroy scope CAN (the sole authorised path).
    vault_delete(vault, "clinical_note/note-x.md", scope=_DESTROY_SCOPE)
    assert not note.exists()


def test_destroy_scope_gate1_admits_clinical_note():
    # gate 1 (_validate_type) auto-derives clinical_note under the destroy scope's list:True — do NOT
    # edit a KNOWN_TYPES_BY_SCOPE literal (the auto-population must stay live, per CLAUDE.md).
    assert "clinical_note" in schema.TYPE_REGISTRY.known_types(_DESTROY_SCOPE)


def test_destroy_scope_is_agent_unreachable_ESCALATION_PIN():
    """ESCALATION-SURFACE GUARD (13d-3): the privileged delete scope must be reachable ONLY from the
    destroy CLI, NEVER any agent / backend / transport / talker / curator / janitor / distiller route.
    Scope is selected via ALFRED_VAULT_SCOPE (set per-tool by the orchestrator) or passed in-process;
    the destroy CLI passes it directly to vault_delete. Pin: the scope string appears in NO
    agent-reachable module — a future wiring that injected it into an agent path would flip this RED."""
    import pathlib
    src = pathlib.Path(schema.__file__).resolve().parents[1]   # src/alfred
    # WARN-2: temporal/activities._build_env injects profile.scope→ALFRED_VAULT_SCOPE→spawn_agent, and
    # exec --scope + orchestrator spawn are agent-reachable scope-injection routes — include them.
    agent_reachable = [
        "backends", "curator", "janitor", "distiller", "talker.py", "talker",
        "transport", "web", "surveyor", "mail", "brief", "temporal", "orchestrator.py",
    ]
    offenders = []
    for path in src.rglob("*.py"):
        rel = path.relative_to(src).as_posix()
        if not any(rel == m or rel.startswith(m + "/") or rel == m for m in agent_reachable):
            continue
        try:
            if _DESTROY_SCOPE in path.read_text(encoding="utf-8"):
                offenders.append(rel)
        except OSError:
            continue
    assert offenders == [], (
        f"{_DESTROY_SCOPE} is referenced by agent-reachable module(s) {offenders} — the privileged "
        f"s.49 delete scope must be CLI-only. If an agent path can select it, it can delete clinical "
        f"records (the exact escalation this pin guards)."
    )


# ---------------------------------------------------------------------------
# #41 CREATE-BYPASS GUARD — a clinical_note is born ai_draft only.
# ---------------------------------------------------------------------------

def test_create_bypass_born_attested_refused():
    # THE born-attested pin (mutation: allow status=attested at create => fails).
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type="clinical_note",
                    frontmatter={"status": "attested"})


@pytest.mark.parametrize("bad", [{"attested_by": "np_jamie"}, {"attested_at": "2026-07-09T00:00:00Z"}])
def test_create_bypass_attest_fields_at_create_refused(bad):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type="clinical_note", frontmatter=bad)


@pytest.mark.parametrize("fm", [{}, {"status": "ai_draft"}, {"status": "ai_draft", "synthetic": True}])
def test_create_ai_draft_or_absent_status_allowed(fm):
    # status absent (schema default ai_draft) or explicitly ai_draft => OK.
    check_scope(_SCOPE, "create", record_type="clinical_note", frontmatter=fm)


# ---------------------------------------------------------------------------
# ANTI-SPOLIATION — move/delete/body-mutation denied + denyset membership
# ---------------------------------------------------------------------------

def test_clinical_note_in_denysets():
    assert "clinical_note" in _DELETE_DENIED_TYPES
    assert "clinical_note" in _BODY_MUTATE_DENIED_TYPES


@pytest.mark.parametrize("op", ["move", "delete"])
def test_move_delete_denied(op):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, op, record_type="clinical_note")


@pytest.mark.parametrize("op", ["body_insert_at", "body_replace"])
def test_body_mutation_denied_without_live_draft_status(op):
    # Fail-closed: with NO existing_frontmatter the gate cannot see a live
    # ``ai_draft`` status, so BOTH body ops are denied. body_insert_at stays
    # denied even WITH ai_draft (only body_replace is opened — pinned below).
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, op, record_type="clinical_note")


def test_delete_deny_precedence_over_delete_true_scope():
    # janitor carries delete:True, but the universal _DELETE_DENIED_TYPES
    # takes precedence — a clinical note can never be agent-deleted.
    with pytest.raises(ScopeError):
        check_scope("janitor", "delete", record_type="clinical_note")


def test_body_mutate_deny_precedence_over_wildcard_scope():
    # instructor carries allow_body_replace={"*": True}, but the universal
    # _BODY_MUTATE_DENIED_TYPES takes precedence.
    with pytest.raises(ScopeError):
        check_scope("instructor", "body_replace", record_type="clinical_note")


# ---------------------------------------------------------------------------
# P3-a — FROZEN-ON-ATTEST. The clinical_note body is MUTABLE while status==
# ai_draft (the checkpoint co-pilot refreshes it) and SEALED once attested/
# amended. The anti-spoliation invariant moves from create (P2) to attest (P3).
# The gate reads the note's LIVE frontmatter status; fail-closed on unknown.
# ---------------------------------------------------------------------------

def test_draft_edit_fields_matrix_pin():
    # The narrow frontmatter-refresh allowlist for a LIVE draft. Widening is a
    # deliberate matrix change (pre-commit checklist #6). ``draft_original``
    # (P3-b3) is the retain-the-diff AI-body snapshot the pipeline refreshes each
    # checkpoint; ``encounter_completeness`` (#58) is the note-completeness marker
    # the daemon stamps at READY / clears on regen / self-heals — both are
    # DRAFT_EDIT_FIELDS (writable while ai_draft, SEALED at attest), NOT
    # ATTEST_FIELDS (attest writes ONLY the triad, so the marker is frozen at attest).
    assert STAYC_CLINICAL_DRAFT_EDIT_FIELDS == frozenset(
        {"grounding_flags", "draft_original", "encounter_completeness"}
    )
    # #58 — encounter_completeness is a DRAFT_EDIT field, NEVER an ATTEST field.
    assert "encounter_completeness" in STAYC_CLINICAL_DRAFT_EDIT_FIELDS
    assert "encounter_completeness" not in STAYC_CLINICAL_ATTEST_FIELDS


# --- body_replace: the co-pilot's in-place update mechanism ----------------

def test_body_replace_on_ai_draft_ALLOWED():
    # THE mutable-while-ai_draft pin (mutation: break the ``status=='ai_draft'
    # → return`` branch → the universal deny fires → RED). A live draft's body
    # is rewritable by the pipeline scope.
    check_scope(
        _SCOPE, "body_replace", record_type="clinical_note",
        existing_frontmatter={"status": "ai_draft"},
    )  # no raise


@pytest.mark.parametrize("sealed", ["attested", "amended"])
def test_body_replace_on_sealed_note_DENIED_anti_spoliation(sealed):
    # THE load-bearing anti-spoliation pin. The DISTINCT "anti-spoliation" /
    # "SEALED" wording is asserted so removing the status check flips the fired
    # gate to the generic universal-deny message → RED (mutation-bind), even
    # though the defense-in-depth backstop still denies the write.
    with pytest.raises(ScopeError) as ei:
        check_scope(
            _SCOPE, "body_replace", record_type="clinical_note",
            existing_frontmatter={"status": sealed},
        )
    msg = str(ei.value)
    assert "SEALED" in msg and "anti-spoliation" in msg


@pytest.mark.parametrize("fm", [None, {}, {"status": ""}, {"status": "bogus"}])
def test_body_replace_missing_or_unknown_status_fail_closed(fm):
    with pytest.raises(ScopeError):
        check_scope(
            _SCOPE, "body_replace", record_type="clinical_note",
            existing_frontmatter=fm,
        )


@pytest.mark.parametrize("other", ["janitor", "curator", "instructor", "talker"])
def test_body_replace_on_ai_draft_denied_for_non_stayc_scope(other):
    # Even a LIVE ai_draft is body-mutable ONLY by stayc_clinical; every other
    # scope hits the universal deny (defense-in-depth backstop).
    with pytest.raises(ScopeError):
        check_scope(
            other, "body_replace", record_type="clinical_note",
            existing_frontmatter={"status": "ai_draft"},
        )


def test_body_insert_at_on_ai_draft_still_DENIED():
    # Only body_replace is opened for the co-pilot; mid-document body_insert_at
    # stays universally denied even on a live draft (higher corruption risk).
    with pytest.raises(ScopeError):
        check_scope(
            _SCOPE, "body_insert_at", record_type="clinical_note",
            existing_frontmatter={"status": "ai_draft"},
        )


# --- edit gate: narrow frontmatter refresh while ai_draft ------------------

def test_edit_grounding_flags_on_ai_draft_ALLOWED():
    check_scope(
        _SCOPE, "edit", record_type="clinical_note",
        fields=["grounding_flags"],
        existing_frontmatter={"status": "ai_draft"},
    )  # no raise


def test_edit_body_replace_plus_grounding_flags_on_ai_draft_ALLOWED():
    # The pipeline's combined update call: body_write=True + body_op=body_replace
    # + grounding_flags refresh, on a live draft → the edit gate passes (the
    # body_replace gate does the second status check separately). vault_edit
    # derives ``body_op`` from the ``body_replace=`` kwarg the pipeline sends.
    check_scope(
        _SCOPE, "edit", record_type="clinical_note",
        fields=["grounding_flags"], body_write=True, body_op="body_replace",
        existing_frontmatter={"status": "ai_draft"},
    )  # no raise


# --- P3-a WARN tightening: body surface is body_replace-ONLY on a live draft ---
# The pipeline only ever body_replaces. body_append / body_rewriter mutate the
# body WITHOUT refreshing grounding_flags (grounding-integrity), and mid-doc
# body_insert_at is never part of the pipeline — all three are REFUSED at the
# edit gate even while ai_draft. Least-privilege hardening (the anti-spoliation
# freeze-on-attest is intact regardless).

@pytest.mark.parametrize("op", ["body_append", "body_rewriter", "body_insert_at"])
def test_edit_non_replace_body_op_on_ai_draft_DENIED(op):
    # THE tightening pin (mutation-bind: delete the ``body_op != 'body_replace'``
    # check in stayc_clinical_no_attest → body_append / body_rewriter become
    # allowed on a live draft again → RED). body_append / body_rewriter were
    # previously allowed (they ride the generic body_write bool and skip the
    # second gate); body_insert_at was always denied (second-gate universal
    # deny) — this now denies all three at the edit gate.
    with pytest.raises(ScopeError) as ei:
        check_scope(
            _SCOPE, "edit", record_type="clinical_note",
            fields=["grounding_flags"], body_write=True, body_op=op,
            existing_frontmatter={"status": "ai_draft"},
        )
    msg = str(ei.value)
    # The DISTINCT least-privilege / grounding wording is asserted so the deny
    # is pinned to the tightening gate, not an incidental refusal elsewhere.
    assert "body_replace" in msg and "grounding_flags" in msg


def test_edit_body_op_fail_closed_on_unknown_op_on_ai_draft():
    # Fail-CLOSED: a body write with an unrecognised / absent op is refused on a
    # live draft (only the exact "body_replace" surface is permitted).
    with pytest.raises(ScopeError):
        check_scope(
            _SCOPE, "edit", record_type="clinical_note",
            fields=["grounding_flags"], body_write=True, body_op="body_bogus",
            existing_frontmatter={"status": "ai_draft"},
        )
    with pytest.raises(ScopeError):
        # body_write asserted but no op plumbed → fail-closed (caller bug).
        check_scope(
            _SCOPE, "edit", record_type="clinical_note",
            fields=["grounding_flags"], body_write=True, body_op=None,
            existing_frontmatter={"status": "ai_draft"},
        )


def test_edit_pure_frontmatter_refresh_no_body_still_allowed():
    # A narrow frontmatter-only refresh (no body write) is unaffected by the
    # body-op tightening: body_write=False → the body_op gate is inert.
    check_scope(
        _SCOPE, "edit", record_type="clinical_note",
        fields=["grounding_flags"], body_write=False, body_op=None,
        existing_frontmatter={"status": "ai_draft"},
    )  # no raise


@pytest.mark.parametrize("field", ["status", "attested_by", "attested_at"])
def test_edit_triad_on_ai_draft_STILL_DENIED(field):
    # The attest triad is orchestrator-only in EVERY status — a live draft may
    # not self-attest via a raw frontmatter edit.
    with pytest.raises(ScopeError):
        check_scope(
            _SCOPE, "edit", record_type="clinical_note", fields=[field],
            existing_frontmatter={"status": "ai_draft"},
        )


@pytest.mark.parametrize("field", ["title", "assessment", "ai_draft", "synthetic"])
def test_edit_non_allowlist_field_on_ai_draft_DENIED(field):
    # Only STAYC_CLINICAL_DRAFT_EDIT_FIELDS may be refreshed on a live draft;
    # every other clinical field stays locked even while drafting.
    with pytest.raises(ScopeError):
        check_scope(
            _SCOPE, "edit", record_type="clinical_note", fields=[field],
            existing_frontmatter={"status": "ai_draft"},
        )


@pytest.mark.parametrize("sealed", ["attested", "amended"])
def test_edit_grounding_flags_on_sealed_note_DENIED(sealed):
    # Once sealed, even the draft-refresh allowlist is frozen.
    with pytest.raises(ScopeError):
        check_scope(
            _SCOPE, "edit", record_type="clinical_note",
            fields=["grounding_flags"],
            existing_frontmatter={"status": sealed},
        )


# --- end-to-end: draft update while ai_draft, then sealed on attest --------

def test_e2e_body_replace_updates_draft_then_frozen_on_attest(tmp_path):
    from alfred.scribe.attest import attest as scribe_attest

    # 1. Create the AI draft (born ai_draft).
    result = vault_create(
        tmp_path, "clinical_note", "Encounter 2026-07-10 chest pain",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "grounding_flags": []},
        body="## Subjective\nInitial checkpoint.\n",
        scope=_SCOPE,
    )
    rel_path = result["path"]

    # 2. Co-pilot checkpoint: body_replace + grounding_flags refresh is ALLOWED
    # while ai_draft — the note updates in place (no supersede, no duplicate).
    vault_edit(
        tmp_path, rel_path,
        body_replace="## Subjective\nRevised checkpoint with more detail.\n",
        set_fields={"grounding_flags": [{"reason": "number_mismatch"}]},
        scope=_SCOPE,
    )
    post = frontmatter.load(str(tmp_path / rel_path))
    assert "Revised checkpoint" in post.content
    assert post["status"] == "ai_draft"           # still a draft
    assert len(post["grounding_flags"]) == 1      # refreshed

    # 3. Attest (distinct human clinician, via the orchestrator ONLY).
    scribe_attest(
        tmp_path, rel_path, new_status="attested", attester="np_jamie",
        clinician_ids={"np_jamie"},
        audit_path=tmp_path / "clinical_attest_audit.jsonl",
        # #58 — this scope-freeze e2e attests a markerless draft; audited override
        # (the test targets the attest scope gate, not the completeness precondition).
        allow_incomplete=True, override_reason="test — scope e2e",
    )
    assert frontmatter.load(str(tmp_path / rel_path))["status"] == "attested"

    # 4. Now SEALED — a body_replace via the pipeline scope is REFUSED
    # (anti-spoliation moved from create to attest).
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            body_replace="## Subjective\nSNEAKY post-attest rewrite.\n",
            scope=_SCOPE,
        )
    # The attested body is intact — the sneaky rewrite did not land.
    assert "SNEAKY" not in frontmatter.load(str(tmp_path / rel_path)).content


def test_e2e_body_append_and_rewriter_on_live_draft_REFUSED(tmp_path):
    # P3-a WARN tightening, through the REAL ops.py plumbing (vault_edit derives
    # body_op from the kwarg and passes it to the edit gate). On a LIVE ai_draft,
    # body_append and body_rewriter are REFUSED — only body_replace refreshes the
    # draft. This exercises the ops-layer plumbing (a unit check_scope test alone
    # would not catch a missing body_op pass-through in vault_edit).
    result = vault_create(
        tmp_path, "clinical_note", "Encounter 2026-07-11 dyspnea",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "grounding_flags": []},
        body="## Subjective\nInitial checkpoint.\n",
        scope=_SCOPE,
    )
    rel_path = result["path"]

    # body_append on a live draft → refused (would skip grounding_flags refresh).
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            body_append="\nSNEAKY appended line\n",
            scope=_SCOPE,
        )
    # body_rewriter on a live draft → refused.
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            body_rewriter=lambda b: b + "\nSNEAKY rewritten line\n",
            scope=_SCOPE,
        )
    # Neither sneaky mutation landed; the draft body is unchanged.
    body_now = frontmatter.load(str(tmp_path / rel_path)).content
    assert "SNEAKY" not in body_now
    assert "Initial checkpoint." in body_now

    # body_replace on the SAME live draft still works (the pipeline's path).
    vault_edit(
        tmp_path, rel_path,
        body_replace="## Subjective\nRevised via body_replace.\n",
        set_fields={"grounding_flags": [{"reason": "number_mismatch"}]},
        scope=_SCOPE,
    )
    post = frontmatter.load(str(tmp_path / rel_path))
    assert "Revised via body_replace." in post.content
    assert post["status"] == "ai_draft"


# ---------------------------------------------------------------------------
# _NEVER_PUSH guard
# ---------------------------------------------------------------------------

def test_is_never_push():
    assert schema.is_never_push("clinical_note") is True
    assert schema.is_never_push("ticket") is False
    assert schema.is_never_push("note") is False


def test_peer_propose_refuses_never_push_type():
    from alfred.transport.client import peer_propose_canonical_record
    with pytest.raises(ValueError):
        asyncio.run(peer_propose_canonical_record(
            "salem", "clinical_note", "Encounter note",
            self_name="vera-clinical",
        ))


# ---------------------------------------------------------------------------
# End-to-end — draft, attest flip, frozen body, anti-delete
# ---------------------------------------------------------------------------

def test_e2e_draft_then_structural_attest_then_frozen(tmp_path):
    # 1. Create the AI draft (body IS the payload; born ai_draft).
    result = vault_create(
        tmp_path, "clinical_note", "Encounter 2026-07-09 chest pain",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft"},
        body="## Subjective\nSynthetic patient reports chest pain.\n",
        scope=_SCOPE,
    )
    rel_path = result["path"]
    assert (tmp_path / rel_path).exists()

    # 2. RAW attest flip via the agent scope is STRUCTURALLY REFUSED (#41).
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            set_fields={"status": "attested", "attested_by": "stayc_scribe"},
            scope=_SCOPE,
        )
    # Still ai_draft — the raw edit did not land.
    assert frontmatter.load(str(tmp_path / rel_path))["status"] == "ai_draft"

    # 3. Attestation goes ONLY through the scribe.attest orchestrator, by a
    # distinct human clinician.
    from alfred.scribe.attest import attest as scribe_attest
    audit_path = tmp_path / "clinical_attest_audit.jsonl"
    scribe_attest(
        tmp_path, rel_path,
        new_status="attested",
        attester="np_jamie",
        clinician_ids={"np_jamie"},
        audit_path=audit_path,
        # #58 — markerless draft; audited override (targets the attest scope gate).
        allow_incomplete=True, override_reason="test — scope e2e",
    )
    post = frontmatter.load(str(tmp_path / rel_path))
    assert post["status"] == "attested"
    assert post["attested_by"] == "np_jamie"
    assert audit_path.exists()

    # 4. Body is FROZEN — a body_append via the agent scope is refused.
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            body_append="\nSNEAKY addendum\n",
            scope=_SCOPE,
        )

    # 5. Anti-spoliation — delete is refused.
    with pytest.raises((ScopeError, VaultError)):
        vault_delete(tmp_path, rel_path, scope=_SCOPE)


def test_e2e_non_clinical_create_denied(tmp_path):
    with pytest.raises((ScopeError, VaultError)):
        vault_create(
            tmp_path, "note", "A note", set_fields={}, scope=_SCOPE,
        )
